"""Command-line interface for DrainPype.

When invoked with no arguments (`drainpipe` or `python -m uploader`), launches the GUI.
When invoked with arguments (for instance ``drainpipe -F archive.zip``), uploads the file(s)
using the declarative server definitions.
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import __version__
from .keys import load_keys
from .oauth import OAuthError, authorize_oauth
from .servers import Server, load_servers, prepare_upload
from .uploader import (
    CurlNotFoundError,
    DEFAULT_MAX_UPLOADS,
    MAX_MAX_UPLOADS,
    MIN_MAX_UPLOADS,
    State,
    Uploader,
    human_eta,
    human_size,
    human_speed,
)


def _build_parser(servers: dict[str, Server], default_id: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drainpipe",
        description="Upload files using DrainPype server definitions, or run with no arguments for the GUI.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-S", "--server",
        dest="server",
        default=default_id,
        metavar="SERVER",
        help=f"Target server id (default: {default_id}). Available: {', '.join(servers) or 'none'}.",
    )
    parser.add_argument(
        "-F", "--file",
        dest="files",
        action="append",
        default=[],
        metavar="FILE",
        help="Path to a file to upload (may be repeated).",
    )
    parser.add_argument(
        "-K", "--key",
        dest="key",
        default=None,
        metavar="KEY",
        help="API key / token for the server (overrides saved key).",
    )
    parser.add_argument(
        "-o", "--option",
        dest="options",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Server option value (may be repeated, e.g. -o note='hello' -o folder='Uploads').",
    )
    parser.add_argument(
        "-m", "--max-uploads",
        dest="max_uploads",
        type=int,
        default=DEFAULT_MAX_UPLOADS,
        metavar="N",
        help=f"Maximum concurrent uploads ({MIN_MAX_UPLOADS}-{MAX_MAX_UPLOADS}, default: {DEFAULT_MAX_UPLOADS}).",
    )
    parser.add_argument(
        "--auth",
        dest="auth",
        action="store_true",
        help="Run interactive OAuth 2.0 authorization for the selected server and exit.",
    )
    parser.add_argument(
        "--list-servers",
        dest="list_servers",
        action="store_true",
        help="List available server definitions and exit.",
    )
    return parser


def _resolve_values(server: Server, args: argparse.Namespace) -> dict[str, str]:
    values: dict[str, str] = {}
    saved = load_keys(server.id)
    for f in server.fields:
        if f.widget in ("capture_table", "button", "urlButton", "notes"):
            continue
        if f.widget == "secret" and f.key in saved:
            values[f.key] = saved[f.key]
        elif f.widget == "select" and f.choices:
            values[f.key] = f.choices[0][0]
        else:
            values[f.key] = ""

    if args.key is not None:
        secret_field = next((f for f in server.fields if f.widget == "secret"), None)
        target_key = secret_field.key if secret_field else "api_key"
        values[target_key] = args.key

    for raw in args.options:
        if "=" not in raw:
            raise ValueError(f"Invalid option '{raw}': expected KEY=VALUE")
        k, v = raw.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"Invalid option '{raw}': empty key")
        values[k] = v

    return values


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if not args_list:
        from .gui import run
        return run()

    loaded = load_servers()
    servers = loaded.servers
    default_id = "pixeldrain" if "pixeldrain" in servers else (next(iter(servers), "pixeldrain"))

    parser = _build_parser(servers, default_id)
    args = parser.parse_args(args_list)

    if args.list_servers:
        for sid, srv in servers.items():
            print(f"{sid}\t{srv.name}\t{srv.base_url}")
        for loc, err in loaded.errors.items():
            print(f"[error] {loc}: {err}", file=sys.stderr)
        return 0

    server = servers.get(args.server)
    if server is None:
        print(f"Unknown server '{args.server}'. Available: {', '.join(servers) or 'none'}.", file=sys.stderr)
        return 2

    if args.auth:
        try:
            authorize_oauth(server)
        except OAuthError as exc:
            print(f"Authorization failed: {exc}", file=sys.stderr)
            return 1
        print(f"Authorized {server.name} successfully.")
        return 0

    if not args.files:
        parser.error("the following arguments are required: -F/--file (or omit arguments to open the GUI)")

    try:
        values = _resolve_values(server, args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    problems = server.check_values(values)
    if problems:
        for p in problems:
            print(f"Error: {p}", file=sys.stderr)
        return 2

    if server.prepare:
        try:
            prepare_upload(server, values)
        except Exception as exc:
            print(f"Prepare failed: {exc}", file=sys.stderr)
            return 1

    done_event = threading.Event()
    lock = threading.Lock()
    last_len = 0

    def on_job(job) -> None:
        nonlocal last_len
        with lock:
            if job.state is State.PROCESSING:
                line = f"[{job.name}] Preparing..."
            elif job.state is State.UPLOADING:
                pct = int(job.progress * 100)
                line = (
                    f"[{job.name}] {pct}% "
                    f"({human_size(job.sent)}/{human_size(job.size)}) "
                    f"{human_speed(job.speed)} ETA {human_eta(job.eta)}"
                )
            elif job.state is State.DONE:
                line = f"[{job.name}] Done -> {job.url}"
            elif job.state is State.FAILED:
                line = f"[{job.name}] Failed: {job.error}"
            else:
                line = f"[{job.name}] {job.state.value}"
            pad = " " * max(0, last_len - len(line))
            last_len = len(line)
            end = "\n" if job.state in (State.DONE, State.FAILED) else "\r"
            sys.stderr.write(line + pad + end)
            sys.stderr.flush()
            if job.state in (State.DONE, State.FAILED):
                last_len = 0

            if len(uploader.jobs) == len(added) and all(
                j.state in (State.DONE, State.FAILED) for j in uploader.jobs
            ):
                done_event.set()

    uploader = Uploader(listener=on_job)
    uploader.set_max_uploads(args.max_uploads)

    try:
        added = uploader.add_files(args.files)
    except CurlNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    missing = [f for f in args.files if not Path(f).is_file()]
    for m in missing:
        print(f"File not found: {m}", file=sys.stderr)

    if not added:
        return 1

    try:
        uploader.start(server, values)
    except CurlNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    done_event.wait()

    for url in uploader.urls():
        print(url)

    _, _, failed = uploader.counts()
    return 1 if failed else 0