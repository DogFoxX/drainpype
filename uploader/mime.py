"""Resolve a file's MIME type without any third-party dependency.

Order of precedence (first hit wins):

1. A built-in extension table. It is deterministic across Windows / macOS / Linux
   (the stdlib ``mimetypes`` module reads the OS registry or ``/etc/mime.types``, which
   differ per machine and often lack ``.mkv``, ``.7z``, ``.webp`` and friends).
2. The stdlib ``mimetypes`` module, for anything the table does not know.
3. Magic-byte sniffing of the first few KB, for unknown or missing extensions.
4. ``text/plain`` if the head of the file looks like UTF-8 text.
5. ``application/octet-stream``.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

DEFAULT_MIME = "application/octet-stream"
SNIFF_BYTES = 4096

_MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")

# Extension (lower-case, with dot) -> MIME type. Only entries that are commonly missing
# or inconsistent in OS databases need to be here, but common ones are kept for stability.
EXTENSIONS: dict[str, str] = {
    # archives / disk images / packages
    ".zip": "application/zip", ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar", ".tar": "application/x-tar", ".gz": "application/gzip",
    ".tgz": "application/gzip", ".bz2": "application/x-bzip2", ".xz": "application/x-xz",
    ".zst": "application/zstd", ".iso": "application/x-iso9660-image",
    ".img": "application/octet-stream", ".dmg": "application/x-apple-diskimage",
    ".apk": "application/vnd.android.package-archive", ".jar": "application/java-archive",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".msi": "application/x-msi", ".deb": "application/vnd.debian.binary-package",
    ".rpm": "application/x-rpm", ".torrent": "application/x-bittorrent",
    # documents
    ".pdf": "application/pdf", ".epub": "application/epub+zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    # text / data
    ".txt": "text/plain", ".log": "text/plain", ".md": "text/markdown",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".html": "text/html",
    ".htm": "text/html", ".css": "text/css", ".js": "text/javascript",
    ".json": "application/json", ".xml": "application/xml", ".yaml": "application/yaml",
    ".yml": "application/yaml", ".toml": "application/toml", ".sql": "application/sql",
    # images
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jfif": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".heic": "image/heic", ".heif": "image/heif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".svg": "image/svg+xml",
    ".ico": "image/vnd.microsoft.icon", ".psd": "image/vnd.adobe.photoshop",
    # audio
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma", ".mid": "audio/midi", ".midi": "audio/midi",
    # video
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg", ".ts": "video/mp2t", ".3gp": "video/3gpp",
    # fonts
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
}

# (offset, signature, mime). Checked in order against the head of the file.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"%PDF-", "application/pdf"),
    (0, b"PK\x03\x04", "application/zip"),
    (0, b"PK\x05\x06", "application/zip"),
    (0, b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (0, b"Rar!\x1a\x07", "application/vnd.rar"),
    (0, b"\x1f\x8b", "application/gzip"),
    (0, b"BZh", "application/x-bzip2"),
    (0, b"\xfd7zXZ\x00", "application/x-xz"),
    (0, b"\x28\xb5\x2f\xfd", "application/zstd"),
    (0, b"\x1a\x45\xdf\xa3", "video/x-matroska"),
    (0, b"OggS", "audio/ogg"),
    (0, b"fLaC", "audio/flac"),
    (0, b"ID3", "audio/mpeg"),
    (0, b"\x7fELF", "application/x-executable"),
    (0, b"MZ", "application/vnd.microsoft.portable-executable"),
    (0, b"SQLite format 3\x00", "application/vnd.sqlite3"),
    (0, b"wOFF", "font/woff"),
    (0, b"wOF2", "font/woff2"),
    (257, b"ustar", "application/x-tar"),
)


def is_valid_mime(value: str) -> bool:
    """True for a plausible ``type/subtype`` string (no spaces, no parameters)."""
    return bool(_MIME_RE.match(value.strip()))


def sniff(head: bytes) -> str:
    """Guess a type from file contents alone; ``""`` when nothing matches."""
    for offset, signature, mime in _SIGNATURES:
        if head[offset:offset + len(signature)] == signature:
            return mime
    if len(head) >= 12:
        if head[:4] == b"RIFF":
            kind = head[8:12]
            if kind == b"WEBP":
                return "image/webp"
            if kind == b"WAVE":
                return "audio/wav"
            if kind == b"AVI ":
                return "video/x-msvideo"
        if head[4:8] == b"ftyp":  # ISO base media: MP4, M4A, MOV, HEIC, AVIF ...
            brand = head[8:12]
            if brand in (b"heic", b"heix", b"mif1", b"msf1"):
                return "image/heic"
            if brand == b"avif":
                return "image/avif"
            if brand == b"qt  ":
                return "video/quicktime"
            if brand == b"M4A ":
                return "audio/mp4"
            return "video/mp4"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "audio/mpeg"  # bare MP3 frame
    return ""


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut off at the end of the sample is still text.
        if exc.start < len(head) - 3:
            return False
    return True


def guess_mime(path: str | Path, head: bytes | None = None) -> str:
    """Return the best-known MIME type for ``path`` (never raises, never empty).

    ``head`` may carry the first bytes of the file; when omitted and needed they are read
    from disk (unreadable files simply fall back to the default type).
    """
    name = Path(path).name
    ext = Path(name).suffix.lower()
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    guessed, _encoding = mimetypes.guess_type(name, strict=False)
    if guessed and is_valid_mime(guessed):
        return guessed.lower()
    if head is None:
        try:
            with open(path, "rb") as handle:
                head = handle.read(SNIFF_BYTES)
        except OSError:
            return DEFAULT_MIME
    sniffed = sniff(head)
    if sniffed:
        return sniffed
    if _looks_like_text(head):
        return "text/plain"
    return DEFAULT_MIME
