r"""Upload queue and per-file state machine backed by ``curl``.

Each :class:`Job` moves through::

    READY -> WAITING -> PROCESSING -> UPLOADING -> DONE
                                           \-> FAILED

``stop()`` sends unfinished jobs back to READY and terminates any active ``curl``
subprocesses. ``restart()`` sends finished jobs (DONE or FAILED) back through the same
cycle from scratch. Progress updates are emitted every 100 ms (``EMIT_INTERVAL``).
"""
from __future__ import annotations

import itertools
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .mime import DEFAULT_MIME, guess_mime
from .oauth import OAuthError, access_token
from .servers import (
    ResponseError,
    Server,
    apply_captures,
    build_request,
    parse_response,
    prepare_upload,
)

CHUNK_SIZE = 64 * 1024
DEFAULT_MAX_UPLOADS = 1
MIN_MAX_UPLOADS = 1
MAX_MAX_UPLOADS = 100
EMIT_INTERVAL = 0.1  # 100 ms progress updates per job
SPEED_WINDOW = 5.0  # seconds averaged for the speed readout
SPEED_SMOOTHING = 0.25
_CURL_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$", re.IGNORECASE)


def _curl_size_bytes(value: str) -> float | None:
    """Convert a curl progress-meter size (for example ``12.4M``) to bytes."""
    match = _CURL_SIZE_RE.match(value.strip())
    if not match:
        return None
    amount = float(match.group(1))
    suffix = match.group(2).upper()
    power = "KMGTP".find(suffix) + 1 if suffix else 0
    return amount * (1024 ** power)


def _parse_curl_progress_line(line: str) -> float | None:
    """Return curl's cumulative uploaded byte count from a progress-meter row."""
    parts = line.strip().split()
    # curl data row: % Total Received Xferd Dload Upload Total Spent Left Speed
    if len(parts) < 10 or not parts[0].rstrip("%").replace(".", "", 1).isdigit():
        return None
    # The Upload column is the sixth token; Xferd is the combined total.
    return _curl_size_bytes(parts[5])


CONNECT_TIMEOUT = 60
# curl aborts when the connection moves less than 1 byte/s for this long. That covers a
# dead link *and* a server that never answers after the last byte, without capping the
# total time a legitimately large upload may take.
STALL_TIMEOUT = 300


class State(str, Enum):
    READY = "ready"
    WAITING = "waiting"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"


RUNNING_STATES = (State.WAITING, State.PROCESSING, State.UPLOADING)


class CurlNotFoundError(RuntimeError):
    """Raised when the ``curl`` executable cannot be found on the system."""


class UploadAborted(Exception):
    """Raised inside a worker when the user pressed Stop."""


class ServerBusyError(RuntimeError):
    """Uploads for a different server are still running; one server per run."""


@dataclass
class Job:
    id: int
    path: Path
    name: str
    size: int
    state: State = State.READY
    sent: int = 0
    speed: float = 0.0
    url: str = ""
    error: str = ""
    captured: dict[str, str] = field(default_factory=dict)
    mime: str = DEFAULT_MIME
    server_name: str = ""
    server: Server | None = field(default=None, repr=False)
    server_values: dict[str, Any] = field(default_factory=dict, repr=False)
    processing_progress: float = field(default=0.0, repr=False)
    requeue_after_stop: bool = field(default=False, repr=False)
    emitted: float = field(default=0.0, repr=False)
    eta_seconds: float | None = field(default=None, repr=False)
    eta_updated: float = field(default=0.0, repr=False)
    user_stopped: bool = field(default=False, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    proc: Any = field(default=None, repr=False)

    @property
    def progress(self) -> float:
        if self.state is State.DONE:
            return 1.0
        if self.state is State.PROCESSING:
            return min(1.0, max(0.0, self.processing_progress))
        return min(1.0, self.sent / self.size) if self.size else 0.0

    @property
    def eta(self) -> float | None:
        if self.state is not State.UPLOADING:
            return None
        if self.eta_seconds is not None and self.eta_updated > 0:
            elapsed = max(0.0, time.monotonic() - self.eta_updated)
            return max(0.0, self.eta_seconds - elapsed)
        if self.speed > 0:
            return max(self.size - self.sent, 0) / self.speed
        return None


Listener = Callable[[Job], None]


def find_curl() -> str:
    """Locate the ``curl`` binary or raise :class:`CurlNotFoundError`."""
    candidates = ["curl.exe", "curl"] if sys.platform == "win32" else ["curl"]
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    raise CurlNotFoundError(
        "Could not find 'curl' on PATH. Install curl or add it to your system PATH."
    )


class Uploader:
    """Owns the job list and runs uploads on background threads.

    ``listener`` is called (from worker threads!) whenever a job changes; GUIs must
    hop back to their own thread, e.g. through a Qt signal.
    """

    REMOVABLE = (State.READY, State.DONE, State.FAILED)

    def __init__(self, listener: Listener | None = None):
        self.listener: Listener = listener or (lambda job: None)
        self._max_uploads = DEFAULT_MAX_UPLOADS
        self.jobs: list[Job] = []
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._queue_cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._manager: threading.Thread | None = None
        self._active_queue: list[Job] = []
        self._active_workers = 0
        self._running_jobs: list[Job] = []
        self._active_server: Server | None = None
        self._active_values: dict[str, Any] = {}

    @property
    def max_uploads(self) -> int:
        return self._max_uploads

    def set_max_uploads(self, count: int) -> None:
        clamped = max(MIN_MAX_UPLOADS, min(MAX_MAX_UPLOADS, int(count)))
        with self._queue_cond:
            self._max_uploads = clamped
            active_jobs = [job for job in self._running_jobs if job.state in RUNNING_STATES]
            excess = max(0, len(active_jobs) - clamped)
            for job in reversed(active_jobs):
                if excess <= 0:
                    break
                if job.user_stopped:
                    continue
                job.requeue_after_stop = True
                job.stop_event.set()
                if job.proc is not None:
                    try:
                        job.proc.kill()
                    except OSError:
                        pass
                excess -= 1
            self._queue_cond.notify_all()

    def set_parallel(self, enabled: bool) -> None:
        self.set_max_uploads(3 if enabled else 1)

    def move_job(self, job_id: int, target_index: int) -> bool:
        """Move one row to an absolute queue position and preserve pending FIFO order."""
        with self._queue_cond:
            current = next((i for i, job in enumerate(self.jobs) if job.id == job_id), None)
            if current is None or not self.jobs:
                return False
            target = max(0, min(len(self.jobs) - 1, int(target_index)))
            if current == target:
                return False
            job = self.jobs.pop(current)
            self.jobs.insert(target, job)
            self._sort_pending_queue_locked()
            self._queue_cond.notify_all()
            return True

    def move_jobs(self, job_ids: list[int], direction: int) -> bool:
        """Move selected rows up or down one position, keeping their order intact."""
        if direction not in (-1, 1):
            raise ValueError("direction must be -1 (up) or 1 (down)")
        selected = set(job_ids)
        if not selected:
            return False
        changed = False
        with self._queue_cond:
            if direction < 0:
                positions = range(1, len(self.jobs))
            else:
                positions = range(len(self.jobs) - 2, -1, -1)
            for i in positions:
                neighbor = i + direction
                if self.jobs[i].id in selected and self.jobs[neighbor].id not in selected:
                    self.jobs[i], self.jobs[neighbor] = self.jobs[neighbor], self.jobs[i]
                    changed = True
            if changed:
                self._sort_pending_queue_locked()
                self._queue_cond.notify_all()
        return changed

    def _sort_pending_queue_locked(self) -> None:
        order = {job.id: index for index, job in enumerate(self.jobs)}
        self._active_queue.sort(key=lambda job: order.get(job.id, len(order)))

    # ------------------------------------------------------------------ queue
    def add_files(self, paths) -> list[Job]:
        added: list[Job] = []
        with self._lock:
            busy = {j.path for j in self.jobs if j.state not in (State.DONE, State.FAILED)}
            for raw in paths:
                path = Path(raw)
                if not path.is_file() or path in busy:
                    continue
                job = Job(
                    next(self._ids),
                    path,
                    path.name,
                    path.stat().st_size,
                    mime=guess_mime(path),
                )
                self.jobs.append(job)
                busy.add(path)
                added.append(job)
        for job in added:
            self._set(job)
        return added

    def remove(self, ids) -> list[int]:
        """Remove idle jobs (ready/done/failed); returns the ids actually removed."""
        id_set = set(ids)
        with self._lock:
            gone = [j for j in self.jobs if j.id in id_set and j.state in self.REMOVABLE]
            self.jobs = [j for j in self.jobs if j not in gone]
        return [j.id for j in gone]

    def counts(self) -> tuple[int, int, int]:
        with self._lock:
            done = sum(j.state is State.DONE for j in self.jobs)
            failed = sum(j.state is State.FAILED for j in self.jobs)
            return len(self.jobs), done, failed

    def urls(self) -> list[str]:
        with self._lock:
            return [j.url for j in self.jobs if j.state is State.DONE and j.url]

    # ------------------------------------------------------------------ control
    def start(self, server: Server, values: dict[str, Any], job_ids: list[int] | None = None) -> bool:
        """Start uploads for the selected ``job_ids`` (or all eligible jobs if ``None``)."""
        curl_bin = find_curl()

        with self._queue_cond:
            if self._manager and self._manager.is_alive() and self._stop.is_set():
                self._manager.join(timeout=5)

            selected_set = set(job_ids) if job_ids is not None else None
            targets = [
                j for j in self.jobs
                if selected_set is None or j.id in selected_set
            ]

            for job in targets:
                job.user_stopped = False
                job.stop_event.clear()

            candidates = [
                j for j in targets
                if j.state in (State.READY, State.FAILED)
            ]

            running = (
                self._manager is not None
                and self._manager.is_alive()
                and not self._stop.is_set()
                and self._active_server is not None
            )
            if running:
                for job in candidates:
                    if job not in self._active_queue:
                        job.server = server
                        job.server_name = server.name
                        job.server_values = dict(values)
                        self._set(
                            job,
                            State.WAITING,
                            sent=0,
                            speed=0.0,
                            eta_seconds=None,
                            eta_updated=0.0,
                            error="",
                            url="",
                            captured={},
                        )
                        self._active_queue.append(job)
                self._queue_cond.notify_all()
                return bool(candidates)

            if not candidates:
                return False

            self._stop.clear()
            self._active_server = server
            self._active_values = dict(values)
            self._active_queue = []
            for job in candidates:
                job.server = server
                job.server_name = server.name
                job.server_values = dict(values)
                self._set(
                    job,
                    State.WAITING,
                    sent=0,
                    speed=0.0,
                    eta_seconds=None,
                    eta_updated=0.0,
                    error="",
                    url="",
                    captured={},
                )
                self._active_queue.append(job)

            self._manager = threading.Thread(
                target=self._manage, args=(curl_bin,), daemon=True
            )
            self._manager.start()
            return True

    def restart(self, server: Server, values: dict[str, Any], job_ids: list[int] | None = None) -> bool:
        """Upload finished jobs (DONE or FAILED) again from scratch.

        The file is re-checked on disk first (size and type are refreshed; a file that has
        disappeared turns the job FAILED). The earlier link is dropped, and the new upload
        gets its own link. Returns ``False`` when there was nothing to restart.
        """
        find_curl()
        with self._queue_cond:
            wanted = None if job_ids is None else set(job_ids)
            finished = [
                j for j in self.jobs
                if j.state in (State.DONE, State.FAILED) and (wanted is None or j.id in wanted)
            ]
            ready: list[int] = []
            for job in finished:
                try:
                    size = job.path.stat().st_size
                except OSError:
                    self._set(
                        job,
                        State.FAILED,
                        sent=0,
                        speed=0.0,
                        eta_seconds=None,
                        eta_updated=0.0,
                        url="",
                        captured={},
                        error="File not found",
                    )
                    continue
                self._set(
                    job,
                    State.READY,
                    size=size,
                    mime=guess_mime(job.path),
                    sent=0,
                    speed=0.0,
                    eta_seconds=None,
                    eta_updated=0.0,
                    error="",
                    url="",
                    captured={},
                )
                ready.append(job.id)
        return bool(ready) and self.start(server, values, job_ids=ready)

    def stop(self, job_ids: list[int] | None = None) -> None:
        """Stop all jobs (``job_ids=None``) or specific selected jobs."""
        with self._queue_cond:
            if job_ids is None:
                self._stop.set()
                targets = list(self.jobs)
            else:
                selected_set = set(job_ids)
                targets = [j for j in self.jobs if j.id in selected_set]

            for job in targets:
                if job_ids is not None:
                    job.user_stopped = True
                job.stop_event.set()
                proc = job.proc
                if proc is not None:
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                    except OSError:
                        pass
                    try:
                        proc.kill()
                    except OSError:
                        pass
                if job.state is State.WAITING:
                    job.stop_event.clear()
                    self._set(job, State.READY, sent=0, speed=0.0, eta_seconds=None, eta_updated=0.0)
            self._queue_cond.notify_all()

    # ------------------------------------------------------------------ internals
    def _set(self, job: Job, state: State | None = None, **changes) -> None:
        if state is not None:
            job.state = state
        for key, value in changes.items():
            setattr(job, key, value)
        job.emitted = time.monotonic()
        self.listener(job)

    def _next_queued_job_locked(self) -> Job | None:
        while self._active_queue:
            job = self._active_queue[0]
            if job.state is not State.WAITING or job.user_stopped:
                self._active_queue.pop(0)
                continue
            self._active_queue.pop(0)
            return job
        return None

    def _has_pending_work_locked(self) -> bool:
        return any(
            job.state is State.WAITING and not job.user_stopped
            for job in self._active_queue
        )

    def _cancelled_job(self, job: Job) -> None:
        """Return a stopped worker to READY or requeue it after reducing concurrency."""
        with self._queue_cond:
            should_requeue = (
                job.requeue_after_stop
                and not self._stop.is_set()
                and not job.user_stopped
            )
            job.requeue_after_stop = False
            job.stop_event.clear()
            if should_requeue:
                self._set(
                    job,
                    State.WAITING,
                    sent=0,
                    speed=0.0,
                    eta_seconds=None,
                    eta_updated=0.0,
                )
                if job not in self._active_queue:
                    self._active_queue.append(job)
            else:
                self._set(
                    job,
                    State.READY,
                    sent=0,
                    speed=0.0,
                    eta_seconds=None,
                    eta_updated=0.0,
                )
            self._queue_cond.notify_all()

    def _manage(self, curl_bin: str) -> None:
        if self._active_server is None:
            return
        threads: list[threading.Thread] = []

        while True:
            with self._queue_cond:
                while (
                    not self._stop.is_set()
                    and self._has_pending_work_locked()
                    and self._active_workers >= self._max_uploads
                ):
                    self._queue_cond.wait(timeout=0.15)

                if self._stop.is_set():
                    break

                job = self._next_queued_job_locked()
                if job is None:
                    if self._active_workers == 0:
                        break
                    self._queue_cond.wait(timeout=0.15)
                    continue

                self._active_workers += 1
                self._running_jobs.append(job)

            worker = threading.Thread(
                target=self._worker,
                args=(curl_bin, job, job.server, job.server_values),
                daemon=True,
            )
            worker.start()
            threads.append(worker)

        for worker in threads:
            worker.join()

        with self._queue_cond:
            for job in list(self._active_queue):
                if job.state is State.WAITING:
                    self._set(job, State.READY, sent=0, speed=0.0, eta_seconds=None, eta_updated=0.0)
            self._active_queue.clear()
            self._active_server = None
            self._queue_cond.notify_all()

    def _worker(
        self,
        curl_bin: str,
        job: Job,
        server: Server,
        values: dict[str, Any],
    ) -> None:
        try:
            if self._stop.is_set() or job.stop_event.is_set():
                raise UploadAborted

            try:
                job.size = job.path.stat().st_size  # the file may have changed since it was added
            except OSError:
                raise ResponseError("upload", "File not found") from None

            self._set(job, State.PROCESSING, sent=0, speed=0.0, processing_progress=0.05,
                      eta_seconds=None, eta_updated=0.0)
            with self._lock:
                if self._stop.is_set() or job.stop_event.is_set():
                    raise UploadAborted
                if server.auth and server.auth.provider:
                    token = access_token(server)
                    if not token:
                        provider_label = (
                            "Google"
                            if server.auth.provider.strip().lower() == "google"
                            else server.auth.provider.capitalize()
                        )
                        raise ResponseError(
                            "upload",
                            f"{provider_label} account is not authorized. Click 'Authorize' first.",
                        )
                    values[server.auth.token_key] = token
                self._set(job, processing_progress=0.2)
                prepare_upload(
                    server,
                    values,
                    abort_check=lambda: self._stop.is_set() or job.stop_event.is_set(),
                    progress=lambda fraction: self._set(
                        job, processing_progress=0.2 + 0.65 * fraction
                    ),
                )
                if self._stop.is_set() or job.stop_event.is_set():
                    raise UploadAborted
                request = build_request(server, job.name, values, mime=job.mime, size=job.size)
                self._set(job, processing_progress=0.95)

            self._set(job, State.UPLOADING, sent=0, speed=0.0, eta_seconds=None, eta_updated=0.0)
            parsed = self._transfer(curl_bin, job, server, request)
            with self._lock:
                applied = apply_captures(server, values, parsed)
                captured_snapshot = {
                    k: str(v).strip()
                    for k, v in applied.items()
                    if k.startswith("_") and str(v).strip()
                }
            self._set(
                job,
                State.DONE,
                url=parsed.url,
                sent=job.size,
                speed=0.0,
                eta_seconds=None,
                eta_updated=0.0,
                captured=captured_snapshot,
            )
        except UploadAborted:
            self._cancelled_job(job)
        except ResponseError as exc:
            self._set(job, State.FAILED, error=str(exc), speed=0.0, eta_seconds=None, eta_updated=0.0)
        except OAuthError as exc:
            self._set(job, State.FAILED, error=str(exc), speed=0.0, eta_seconds=None, eta_updated=0.0)
        except OSError as exc:
            if self._stop.is_set() or job.stop_event.is_set():
                self._cancelled_job(job)
            else:
                self._set(
                    job,
                    State.FAILED,
                    error=f"Network error: {exc or type(exc).__name__}",
                    speed=0.0,
                    eta_seconds=None,
                    eta_updated=0.0,
                )
        finally:
            with self._queue_cond:
                self._active_workers = max(0, self._active_workers - 1)
                if job in self._running_jobs:
                    self._running_jobs.remove(job)
                self._queue_cond.notify_all()

    def _build_body_framing(self, job: Job, request) -> tuple[str, bytes, bytes, int]:
        """Return ``(content_type, preamble_bytes, epilogue_bytes, total_content_length)``."""
        if request.body == "multipart":
            boundary = f"----DrainPypeBoundary{uuid.uuid4().hex}"
            chunks: list[bytes] = []
            for field_name, field_val in request.form_fields:
                chunks.append(
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'
                        f"{field_val}\r\n"
                    ).encode("utf-8")
                )
            safe_name = (
                job.name.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", "")
                .replace("\n", "")
            )
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{request.file_field}"; filename="{safe_name}"\r\n'
                    f"Content-Type: {request.file_mime}\r\n\r\n"
                ).encode("utf-8")
            )
            preamble = b"".join(chunks)
            epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
            total_len = len(preamble) + job.size + len(epilogue)
            return f"multipart/form-data; boundary={boundary}", preamble, epilogue, total_len

        if request.body == "multipart_related":
            boundary = f"----DrainPypeRelated{uuid.uuid4().hex}"
            chunks = []
            if request.metadata:
                chunks.append(
                    (
                        f"--{boundary}\r\n"
                        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                        f"{request.metadata}\r\n"
                    ).encode("utf-8")
                )
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    f"Content-Type: {request.file_mime}\r\n\r\n"
                ).encode("utf-8")
            )
            preamble = b"".join(chunks)
            epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
            total_len = len(preamble) + job.size + len(epilogue)
            return f"multipart/related; boundary={boundary}", preamble, epilogue, total_len

        return "", b"", b"", job.size

    def _transfer(self, curl_bin: str, job: Job, server: Server, request):
        parts = urllib.parse.urlsplit(request.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ResponseError("upload", f"Bad upload URL: {request.url}")

        content_type, preamble, epilogue, content_length = self._build_body_framing(job, request)
        direct_file = request.body == "raw" and not preamble and not epilogue
        if not content_type and not any(n.lower() == "content-type" for n, _ in request.headers):
            # Raw body: without this curl would label the file application/x-www-form-urlencoded.
            content_type = request.file_mime

        cmd: list[str] = [
            curl_bin,
            "--show-error",
            "--progress-meter",
            "-X",
            request.verb,
            "--data-binary",
            f"@{job.path}" if direct_file else "@-",
            "--connect-timeout",
            str(CONNECT_TIMEOUT),
            "--speed-limit",
            "1",
            "--speed-time",
            str(STALL_TIMEOUT),
            "-A",
            f"DrainPype/{__version__}",
            "-H",
            f"Content-Length: {content_length}",
            "-w",
            "\n%{http_code}",
        ]
        if request.keepalive > 0:
            cmd += ["--keepalive-time", str(request.keepalive)]
        if content_type:
            cmd += ["-H", f"Content-Type: {content_type}"]
        for name, value in request.headers:
            if value == "":
                cmd += ["-H", f"{name};"]
            else:
                cmd += ["-H", f"{name}: {value}"]
        cmd.append(request.url)

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL if direct_file else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        job.proc = proc

        progress_lock = threading.Lock()
        bytes_sent = 0.0
        done_polling = threading.Event()
        stderr_messages: list[str] = []

        def read_curl_progress() -> None:
            """Read curl's transfer counters; stdin writes only measure pipe buffering."""
            nonlocal bytes_sent
            assert proc.stderr is not None
            pending = ""
            while True:
                data = proc.stderr.read1(4096)
                if not data:
                    break
                pending += data.decode("utf-8", errors="replace")
                lines = re.split(r"[\r\n]", pending)
                pending = lines.pop() if lines else ""
                for line in lines:
                    uploaded = _parse_curl_progress_line(line)
                    if uploaded is not None:
                        # Remove multipart framing so progress is file bytes only.
                        file_bytes = max(0.0, min(float(job.size), uploaded - len(preamble)))
                        with progress_lock:
                            bytes_sent = max(bytes_sent, file_bytes)
                    elif line.strip() and not line.lstrip().startswith(("% Total", "Dload Upload")):
                        stderr_messages.append(line.strip())
            if pending:
                uploaded = _parse_curl_progress_line(pending)
                if uploaded is not None:
                    with progress_lock:
                        bytes_sent = max(bytes_sent, max(0.0, min(float(job.size), uploaded - len(preamble))))
                elif pending.strip():
                    stderr_messages.append(pending.strip())

        progress_reader = threading.Thread(target=read_curl_progress, daemon=True)
        progress_reader.start()

        def poll_progress() -> None:
            samples = deque([(time.monotonic(), 0.0)])
            while not done_polling.wait(EMIT_INTERVAL):
                if self._stop.is_set() or job.stop_event.is_set():
                    break
                with progress_lock:
                    cur = bytes_sent
                now = time.monotonic()
                prev_sent = job.sent
                samples.append((now, cur))
                while len(samples) > 2 and now - samples[0][0] > SPEED_WINDOW:
                    samples.popleft()
                span = now - samples[0][0]
                measured_speed = (cur - samples[0][1]) / span if span > 0 else 0.0
                if measured_speed > 0 or job.speed > 0:
                    job.speed = (
                        SPEED_SMOOTHING * measured_speed + (1 - SPEED_SMOOTHING) * job.speed
                        if job.speed > 0 else measured_speed
                    )
                job.sent = int(cur)
                if job.speed > 0:
                    remaining = max(job.size - job.sent, 0)
                    raw_eta = remaining / job.speed
                    if job.eta_seconds is None or job.sent > prev_sent:
                        job.eta_seconds = raw_eta
                        job.eta_updated = now
                job.emitted = now
                self.listener(job)

        poller = threading.Thread(target=poll_progress, daemon=True)
        poller.start()

        try:
            if self._stop.is_set() or job.stop_event.is_set():
                raise UploadAborted
            if not direct_file:
                assert proc.stdin is not None
                if preamble:
                    proc.stdin.write(preamble)

                with open(job.path, "rb") as handle:
                    while True:
                        if self._stop.is_set() or job.stop_event.is_set():
                            raise UploadAborted
                        chunk = handle.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        proc.stdin.write(chunk)

                if epilogue:
                    proc.stdin.write(epilogue)
                proc.stdin.flush()
                proc.stdin.close()

            stdout_bytes = proc.stdout.read() if proc.stdout else b""
            proc.wait()
            progress_reader.join(timeout=2.0)
            done_polling.set()
            poller.join(timeout=1.0)

            if self._stop.is_set() or job.stop_event.is_set():
                raise UploadAborted

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = "\n".join(stderr_messages)

            body, sep, code_str = stdout_text.rpartition("\n")
            if not sep:
                body = ""
                code_str = stdout_text
            status = int(code_str.strip()) if code_str.strip().isdigit() else 0

            if proc.returncode != 0 and status == 0:
                raise ResponseError("network", stderr_text or f"curl failed (exit {proc.returncode})")

            if 200 <= status < 400:
                job.sent = job.size
            return parse_response(server, body, status)
        except BrokenPipeError:
            done_polling.set()
            poller.join(timeout=1.0)
            if self._stop.is_set() or job.stop_event.is_set():
                raise UploadAborted
            stdout_bytes = proc.stdout.read() if proc.stdout else b""
            proc.wait()
            progress_reader.join(timeout=2.0)
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = "\n".join(stderr_messages)
            body, sep, code_str = stdout_text.rpartition("\n")
            if not sep:
                body = ""
                code_str = stdout_text
            status = int(code_str.strip()) if code_str.strip().isdigit() else 0
            if status > 0:
                return parse_response(server, body, status)
            raise ResponseError("network", stderr_text or "Connection closed early") from None
        finally:
            done_polling.set()
            job.proc = None
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except OSError:
                    pass
            progress_reader.join(timeout=2.0)
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass


# ---------------------------------------------------------------------- formatting
def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def human_speed(bytes_per_second: float) -> str:
    return f"{human_size(bytes_per_second)}/s" if bytes_per_second > 0 else ""


def human_eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = max(0, int(round(seconds)))
    if total == 0:
        return "0s"

    units = (
        ("y", 365 * 86400),
        ("mo", 30 * 86400),
        ("d", 86400),
        ("h", 3600),
        ("min", 60),
        ("s", 1),
    )
    parts: list[str] = []
    rem = total
    for suffix, unit_secs in units:
        if rem >= unit_secs:
            count, rem = divmod(rem, unit_secs)
            parts.append(f"{count}{suffix}")
            if len(parts) == 2:
                break
    return " ".join(parts)
