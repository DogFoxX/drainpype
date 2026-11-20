r"""Saved API keys: one extensionless JSON file per server in ``<config>/keys``.

``%APPDATA%\drainpype\keys\ffast`` (Windows) / ``~/.config/drainpype/keys/ffast`` (Linux)::

    {"account": "<obscured value>"}

Values are *obscured* (XOR-scrambled with a salt derived from the server id and field
key, reversed, then base64-encoded) so they are not readable at a glance. This is NOT
encryption: anyone with the file and this code can recover the key.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .servers import config_dir


@dataclass(frozen=True)
class SavedKey:
    server_id: str
    field_key: str
    value: str


def keys_dir() -> Path:
    return config_dir() / "keys"


def key_file(server_id: str) -> Path:
    return keys_dir() / server_id


def _mask(salt: str, size: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"drainpype:{salt}:{counter}".encode()).digest()
        counter += 1
    return out[:size]


def _xor(data: bytes, salt: str) -> bytes:
    return bytes(a ^ b for a, b in zip(data, _mask(salt, len(data))))


def obscure(value: str, salt: str) -> str:
    return base64.b64encode(_xor(value.encode("utf-8"), salt)[::-1]).decode("ascii")


def reveal(token: str, salt: str) -> str:
    """Inverse of :func:`obscure`; raises ``ValueError`` on a damaged token."""
    scrambled = base64.b64decode(token.encode("ascii"), validate=True)[::-1]
    return _xor(scrambled, salt).decode("utf-8")


def _read_raw(server_id: str) -> dict[str, str]:
    try:
        data = json.loads(key_file(server_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(data, dict) else {}


def _write_raw(server_id: str, data: dict[str, str]) -> None:
    target = key_file(server_id)
    if not data:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return
    directory = keys_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")  # server ids contain no dots
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(temp, 0o600)
    os.replace(temp, target)


def load_keys(server_id: str) -> dict[str, str]:
    """Saved keys for one server as ``{field key: plain value}``; bad entries are skipped."""
    keys = {}
    for field_key, token in _read_raw(server_id).items():
        try:
            keys[field_key] = reveal(token, f"{server_id}:{field_key}")
        except (ValueError, UnicodeError):
            continue
    return keys


def save_key(server_id: str, field_key: str, value: str) -> None:
    """Write (or overwrite) one key, leaving the server's other keys untouched."""
    data = _read_raw(server_id)
    data[field_key] = obscure(value, f"{server_id}:{field_key}")
    _write_raw(server_id, data)


def delete_key(server_id: str, field_key: str) -> None:
    """Remove one saved key (and delete the server key file if none remain)."""
    data = _read_raw(server_id)
    if field_key in data:
        del data[field_key]
        _write_raw(server_id, data)


def saved_keys() -> list[SavedKey]:
    """Every saved key for every server, for the API Keys window."""
    entries = []
    directory = keys_dir()
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_file() and "." not in path.name:
                for field_key, value in load_keys(path.name).items():
                    if field_key == "oauth_credentials" or (path.name == "googledrive" and field_key == "api_key"):
                        continue
                    entries.append(SavedKey(path.name, field_key, value))
    return entries