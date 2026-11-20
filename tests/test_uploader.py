from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from uploader.servers import parse_server
from uploader.uploader import (
    MAX_MAX_UPLOADS,
    MIN_MAX_UPLOADS,
    Job,
    State,
    Uploader,
    human_eta,
)


def _dummy_server():
    return parse_server(
        {
            "schema": 1,
            "id": "dummy",
            "name": "Dummy",
            "request": {
                "verb": "PUT",
                "url": [{"text": "https://example.test/upload/{filename}"}],
            },
            "response": {"results": [{"at": "url", "url": "{value}"}]},
        }
    )


def test_max_uploads_clamping():
    up = Uploader()
    up.set_max_uploads(0)
    assert up.max_uploads == MIN_MAX_UPLOADS
    up.set_max_uploads(999)
    assert up.max_uploads == MAX_MAX_UPLOADS
    up.set_max_uploads(5)
    assert up.max_uploads == 5


def test_job_eta_counts_down_from_eta_updated():
    job = Job(id=1, path=Path("a.bin"), name="a.bin", size=1000, state=State.UPLOADING)
    now = time.monotonic()
    job.eta_seconds = 10.0
    job.eta_updated = now - 2.5
    eta = job.eta
    assert eta is not None
    assert 7.0 <= eta <= 7.8


def test_human_eta_formats_units():
    assert human_eta(None) == ""
    assert human_eta(0) == "0s"
    assert human_eta(45) == "45s"
    assert human_eta(61) == "1min 1s"
    assert human_eta(3660) == "1h 1min"
    assert human_eta(86400) == "1d"
    assert human_eta(31 * 86400) == "1mo 1d"
    assert human_eta(396 * 86400) == "1y 1mo"


def test_uploader_lifecycle_with_mocked_transfer(tmp_path):
    f1 = tmp_path / "one.txt"
    f2 = tmp_path / "two.txt"
    f1.write_bytes(b"hello")
    f2.write_bytes(b"world")

    seen_states: list[tuple[int, State]] = []
    lock = threading.Lock()
    done = threading.Event()

    def listener(job: Job) -> None:
        with lock:
            seen_states.append((job.id, job.state))
            if len(up.jobs) == 2 and all(j.state is State.DONE for j in up.jobs):
                done.set()

    up = Uploader(listener=listener)
    up.set_max_uploads(2)
    up.add_files([f1, f2])

    from uploader.servers import ParsedResponse

    def fake_transfer(_curl_bin, job: Job, _server, _request):
        job.sent = job.size
        return ParsedResponse(f"https://example.test/u/{job.name}", job.name, {})

    with (
        patch("uploader.uploader.find_curl", return_value="/usr/bin/curl"),
        patch.object(Uploader, "_transfer", side_effect=fake_transfer),
    ):
        assert up.start(_dummy_server(), {})
        assert done.wait(timeout=5.0)

    total, completed, failed = up.counts()
    assert (total, completed, failed) == (2, 2, 0)
    assert up.urls() == ["https://example.test/u/one.txt", "https://example.test/u/two.txt"]
    assert any(st is State.PROCESSING for _, st in seen_states)