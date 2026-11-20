"""Load and interpret ``.json`` / ``.yaml`` / ``.yml`` server definitions.

A server definition is a JSON document that describes, declaratively, how to talk to
one upload server: which options the user can fill in, optional pre-upload API steps
(such as looking up or creating a target folder), how to build the HTTP request for a
file, and how to read the response. Adding a server needs no Python.

Templates
---------
Text values may contain ``{name}`` or ``{name|filter|filter}`` tokens. ``name`` is a
field ``key``, a captured/prepared session variable (``_guest_token``, ``_folder_id``),
``short_date`` (``YYYY-MM-DD``), ``base_url``, or one of the per-file variables
``filename``, ``stem`` (name without extension), ``ext`` (extension without the dot),
``mime`` (auto-detected MIME type) and ``size`` (bytes); inside ``response.results`` the
variable is ``value``. Filters: ``trim``, ``trim_slash``, ``url`` (percent-encode
everything), ``path`` (percent-encode but keep ``/``), ``b64`` (base64), ``basic_pass``
(base64 ``:<value>``), ``json`` (JSON string-escaped), and ``gquery`` (Google Drive
query literal-escaped). Any entry with ``only_if`` is used only when all listed keys
are non-empty, and skipped if any key in ``unless`` is non-empty.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .mime import DEFAULT_MIME, is_valid_mime

SCHEMA_VERSION = 1
JSON_SUFFIXES = (".json",)
YAML_SUFFIXES = (".yaml", ".yml")
SUFFIXES = JSON_SUFFIXES + YAML_SUFFIXES
APP_DIR_NAME = "drainpype"
WIDGETS = (
    "text",
    "textarea",
    "notes",
    "secret",
    "select",
    "checkbox",
    "capture_table",
    "button",
    "urlButton",
)
BODY_MODES = ("raw", "multipart", "multipart_related")
FILE_KEYS = {"filename", "stem", "ext", "mime", "size"}  # per-file template variables
RESERVED_KEYS = {"value", "short_date", "base_url"} | FILE_KEYS

DEFAULT_SERVERS_DIR = Path(__file__).parent / "assets" / "default_servers"
SAMPLES_DIR = Path(__file__).parent / "samples"


# --------------------------------------------------------------------------- errors
class DrainError(Exception):
    """Base class for DrainPype errors."""


class ServerDefError(DrainError):
    """A server definition file or URL is unreadable or invalid."""


class ResponseError(DrainError):
    """The server answered, but not with a usable result.

    ``kind`` is ``"network"`` (server/transport trouble) or ``"upload"`` (rejected).
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------- paths
def config_dir() -> Path:
    """``%APPDATA%\\drainpype`` on Windows, ``$XDG_CONFIG_HOME/drainpype`` elsewhere."""
    override = os.environ.get("DRAINPYPE_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def custom_servers_dir() -> Path:
    return config_dir() / "servers"


def servers_dir() -> Path:
    return custom_servers_dir()


def _server_urls_file() -> Path:
    return config_dir() / "server_urls.json"


def _load_server_urls() -> list[str]:
    try:
        raw = json.loads(_server_urls_file().read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [u.strip() for u in raw if isinstance(u, str) and u.strip()]
    except (OSError, ValueError):
        pass
    return []


def _save_server_urls(urls: list[str]) -> None:
    path = _server_urls_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    deduped: list[str] = []
    for u in urls:
        u = u.strip()
        if u and u not in deduped:
            deduped.append(u)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(deduped, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _iter_server_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = [
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in SUFFIXES
    ]
    return sorted(files, key=lambda p: p.name.lower())


def ensure_samples(directory: Path | None = None) -> list[Path]:
    """Deprecated compatibility shim; embedded defaults are read in place."""
    return []


# --------------------------------------------------------------------------- model
@dataclass(frozen=True)
class Part:
    """A template plus optional conditions; ``name`` is the query param / header / form name."""

    text: str
    only_if: tuple[str, ...] = ()
    unless: tuple[str, ...] = ()
    name: str = ""


@dataclass(frozen=True)
class ChoiceSource:
    url: str
    headers: tuple[Part, ...]
    timeout: float
    list_at: tuple[str, ...]
    value_keys: tuple[str, ...]
    caption_keys: tuple[str, ...]
    reload_on: tuple[str, ...]


@dataclass(frozen=True)
class CaptureColumn:
    caption: str
    source: str


@dataclass(frozen=True)
class CaptureExportSpec:
    template: str
    default_filename: str = "export.txt"


@dataclass(frozen=True)
class CaptureSaveTokenSpec:
    source: str
    to_key: str
    caption: str = "Save Token"

@dataclass
class AuthConfig:
    auth_type: str = "oauth2"
    provider: str = ""
    scopes: list[str] = field(default_factory=list)
    client_file: str = ""
    pkce: bool = True
    token_key: str = "oauth_token"
    credentials_key: str = "oauth_credentials"
    source_path: Path | None = None


@dataclass(frozen=True)
class AuthSpec:
    provider: str
    scopes: tuple[str, ...] = ()
    auth_uri: str = ""
    token_uri: str = ""
    revoke_uri: str = ""
    client_id: str = ""
    client_secret: str = ""
    client_file: str = ""
    auth_params: tuple[tuple[str, str], ...] = ()
    redirect_port: int = 0
    redirect_path: str = ""
    pkce: bool = True
    token_key: str = "api_key"
    credentials_key: str = "oauth_credentials"


@dataclass(frozen=True)
class ButtonAction:
    action_type: str
    url: str = ""
    text: str = ""
    targets: tuple[str, ...] = ()
    reload_fields: tuple[str, ...] = ()
    verb: str = "POST"
    query: tuple[Part, ...] = ()
    headers: tuple[Part, ...] = ()
    json_body: str = ""
    form: tuple[Part, ...] = ()
    extract: tuple[tuple[str, str], ...] = ()
    message: str = ""
    provider: str = ""
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Field:
    key: str
    caption: str
    widget: str
    notes: tuple[str, ...] = ()
    help: str = ""
    save_label: str = ""
    mandatory: bool = False
    needs: str = ""
    needs_multiple: bool = False
    disabled_when_all: tuple[str, ...] = ()
    checked_when_all: tuple[str, ...] = ()
    column: int = 0
    choices: tuple[tuple[str, str], ...] = ()
    choices_from: ChoiceSource | None = None
    table_columns: tuple[CaptureColumn, ...] = ()
    export: CaptureExportSpec | None = None
    save_token: CaptureSaveTokenSpec | None = None
    url: str = ""
    action: ButtonAction | None = None


@dataclass(frozen=True)
class PrepareStep:
    verb: str
    url: tuple[Part, ...]
    query: tuple[Part, ...] = ()
    headers: tuple[Part, ...] = ()
    json_body: str = ""
    only_if: tuple[str, ...] = ()
    unless: tuple[str, ...] = ()
    when_empty: tuple[str, ...] = ()
    extract: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RequestSpec:
    verb: str
    url: tuple[Part, ...]
    query: tuple[Part, ...]
    headers: tuple[Part, ...]
    keepalive: int
    body: str = "raw"
    file_field: str = "file"
    form: tuple[Part, ...] = ()
    metadata: tuple[Part, ...] = ()
    mime: str = "auto"  # "auto" = detect per file; otherwise a fixed type or a template


@dataclass(frozen=True)
class ResultRule:
    at: str
    url: str
    name_at: str = ""
    starts_with: str = ""
    ok_at: str = ""
    ok_equals: str = ""
    capture: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CaptureRule:
    source: str
    target: str
    only_if: tuple[str, ...] = ()
    when_empty: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResponseSpec:
    network_error_from: int
    upload_error_from: int
    fallback_url_pattern: str
    results: tuple[ResultRule, ...]
    error_at: tuple[str, ...]
    error_map: tuple[tuple[str, str], ...] = ()
    status_errors: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    fields: tuple[Field, ...]
    request: RequestSpec
    response: ResponseSpec
    base_url: str = ""
    prepare: tuple[PrepareStep, ...] = ()
    capture: tuple[CaptureRule, ...] = ()
    path: Path | None = None
    source_url: str = ""
    auth: AuthSpec | None = None

    @property
    def title(self) -> str:
        return self.name

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)

    def is_field_enabled(self, f: Field, values: dict[str, str], multiple_files: bool) -> bool:
        if f.needs and not str(values.get(f.needs, "")).strip():
            return False
        if f.needs_multiple and not multiple_files:
            return False
        if f.disabled_when_all and all(str(values.get(k, "")).strip() for k in f.disabled_when_all):
            return False
        return True

    def is_field_forced_checked(self, f: Field, values: dict[str, str]) -> bool:
        if not f.checked_when_all:
            return False
        return all(str(values.get(k, "")).strip() for k in f.checked_when_all)

    def check_values(self, values: dict[str, str]) -> list[str]:
        """Return human-readable problems with the user's option values."""
        problems = []
        for f in self.fields:
            if f.widget in ("capture_table", "button", "urlButton", "notes"):
                continue
            value = str(values.get(f.key, "")).strip()
            if f.mandatory and not value:
                problems.append(f"{f.caption} is required")
            if value and f.needs and not str(values.get(f.needs, "")).strip():
                needed = self.field(f.needs)
                problems.append(f"{f.caption} requires {needed.caption if needed else f.needs}")
        return problems


@dataclass(frozen=True)
class LoadResult:
    servers: dict[str, Server]
    errors: dict[str, str]


@dataclass(frozen=True)
class ServerEntry:
    name: str
    location: str
    base_url: str = ""
    error: str = ""


@dataclass(frozen=True)
class HttpRequest:
    verb: str
    url: str
    headers: list[tuple[str, str]]
    keepalive: int
    body: str = "raw"
    file_field: str = "file"
    form_fields: list[tuple[str, str]] = field(default_factory=list)
    metadata: str = ""
    file_mime: str = DEFAULT_MIME


@dataclass(frozen=True)
class ParsedResponse:
    url: str
    filename: str
    raw: Any
    captured: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- templates
_FILTERS: dict[str, Callable[[str], str]] = {
    "trim": lambda s: s.strip(),
    "trim_slash": lambda s: s.strip("/"),
    "url": lambda s: urllib.parse.quote(s, safe=""),
    "path": lambda s: urllib.parse.quote(s, safe="/"),
    "b64": lambda s: base64.b64encode(s.encode("utf-8")).decode("ascii"),
    "basic_pass": lambda s: base64.b64encode(f":{s}".encode("utf-8")).decode("ascii"),
    "json": lambda s: json.dumps(s)[1:-1],
    "gquery": lambda s: s.replace("\\", "\\\\").replace("'", "\\'"),
}

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")


def _context(values: dict[str, Any]) -> dict[str, str]:
    ctx: dict[str, str] = {}
    for k, v in values.items():
        if v is None:
            ctx[k] = ""
        elif k == "filename":
            ctx[k] = str(v)
        else:
            ctx[k] = str(v).strip()
    ctx.setdefault("short_date", datetime.date.today().isoformat())
    return ctx


def _active(part: Part, ctx: dict[str, str]) -> bool:
    if any(not ctx.get(k) for k in part.only_if):
        return False
    if any(ctx.get(k) for k in part.unless):
        return False
    return True


def render(template: str, context: dict[str, Any]) -> str:
    """Substitute ``{key}`` and ``{key|filter|...}`` tokens."""

    def repl(match: re.Match[str]) -> str:
        expr = match.group(1)
        pieces = expr.split("|")
        key = pieces[0].strip()
        raw = context.get(key, "")
        val = "" if raw is None else str(raw)
        for flt_name in pieces[1:]:
            fn = _FILTERS.get(flt_name.strip())
            if fn is not None:
                val = fn(val)
        return val

    return _TOKEN_RE.sub(repl, template)


def render_textarea(field: Field, values: dict[str, Any] | None = None, base_url: str = "") -> str:
    """Render an editable textarea's optional template content."""
    if field.widget != "textarea":
        return ""
    ctx = _context(values or {})
    if base_url:
        ctx["base_url"] = base_url
    return render("\n".join(field.notes), ctx) if field.notes else ""


def render_notes(field: Field, values: dict[str, Any] | None = None, base_url: str = "") -> str:
    """Render a read-only notes field as one line per entry."""
    if field.widget != "notes":
        return ""
    ctx = _context(values or {})
    if base_url:
        ctx["base_url"] = base_url
    return "\n".join(render(line, ctx) for line in field.notes)


def resolve_url(base_url: str, target: str) -> str:
    """Resolve a relative path against ``base_url`` unless ``target`` is already absolute."""
    target = target.strip()
    if not target:
        return base_url.strip()
    if target.startswith(("http://", "https://")):
        return target
    base = base_url.strip().rstrip("/")
    if not base:
        return target
    return f"{base}/{target.lstrip('/')}"


def dig(data: Any, dotted: str) -> Any:
    """Walk a dotted path (supporting dict keys and integer list indices) into JSON data."""
    if not dotted:
        return None
    for step in dotted.split("."):
        if isinstance(data, list) and step.isdigit():
            idx = int(step)
            if idx < 0 or idx >= len(data):
                return None
            data = data[idx]
        elif isinstance(data, dict) and step in data:
            data = data[step]
        else:
            return None
    return data


# --------------------------------------------------------------------------- parsing
def _get(mapping: dict, key: str, expected: type, where: str, default: Any = ...):
    if key not in mapping:
        if default is not ...:
            return default
        raise ServerDefError(f"{where}: missing required '{key}'")
    val = mapping[key]
    if expected is bool:
        if not isinstance(val, bool):
            raise ServerDefError(f"{where}.{key} must be a boolean")
        return val
    if expected is int:
        if not isinstance(val, int) or isinstance(val, bool):
            raise ServerDefError(f"{where}.{key} must be an integer")
        return val
    if expected is float:
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ServerDefError(f"{where}.{key} must be a number")
        return float(val)
    if not isinstance(val, expected):
        raise ServerDefError(f"{where}.{key} must be of type {expected.__name__}")
    return val


def _strings(raw: list, where: str, key: str) -> tuple[str, ...]:
    out = []
    for item in raw:
        if not isinstance(item, str):
            raise ServerDefError(f"{where}.{key} entries must be strings")
        out.append(item)
    return tuple(out)


def _is_valid_key(key: str, field_keys: set[str]) -> bool:
    return key in field_keys or key in RESERVED_KEYS or key.startswith("_")


def _validate_template_tokens(text: str, where: str, field_keys: set[str]) -> None:
    for match in _TOKEN_RE.finditer(text):
        pieces = match.group(1).split("|")
        key = pieces[0].strip()
        if not _is_valid_key(key, field_keys):
            raise ServerDefError(f"{where}: unknown template variable '{key}'")
        for flt in pieces[1:]:
            if flt.strip() not in _FILTERS:
                raise ServerDefError(f"{where}: unknown template filter '{flt.strip()}'")


def _parse_parts(
    raw: list,
    where: str,
    field_keys: set[str],
    name_key: str = "",
) -> tuple[Part, ...]:
    parts: list[Part] = []
    for i, item in enumerate(raw):
        here = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise ServerDefError(f"{here} must be an object")
        text = _get(item, "text", str, here)
        _validate_template_tokens(text, here, field_keys)
        name = _get(item, name_key, str, here) if name_key else ""
        only_if = _strings(_get(item, "only_if", list, here, []), here, "only_if")
        unless = _strings(_get(item, "unless", list, here, []), here, "unless")
        for cond_key in (*only_if, *unless):
            if not _is_valid_key(cond_key, field_keys):
                raise ServerDefError(f"{here}: unknown condition key '{cond_key}'")
        parts.append(Part(text=text, only_if=only_if, unless=unless, name=name))
    return tuple(parts)


def _parse_choice_source(raw: dict, where: str, field_keys: set[str]) -> ChoiceSource:
    url = _get(raw, "url", str, where)
    _validate_template_tokens(url, f"{where}.url", field_keys)
    headers = _parse_parts(_get(raw, "headers", list, where, []), f"{where}.headers", field_keys, "header")
    timeout = _get(raw, "timeout", float, where, 15.0)
    list_at = _strings(_get(raw, "list_at", list, where, ["data", "items"]), where, "list_at")
    value_keys = _strings(_get(raw, "value_keys", list, where, ["id", "value"]), where, "value_keys")
    caption_keys = _strings(_get(raw, "caption_keys", list, where, ["name", "label", "title"]), where, "caption_keys")
    reload_on = _strings(_get(raw, "reload_on", list, where, []), where, "reload_on")
    return ChoiceSource(
        url=url,
        headers=headers,
        timeout=timeout,
        list_at=list_at,
        value_keys=value_keys,
        caption_keys=caption_keys,
        reload_on=reload_on,
    )


def _parse_button_action(raw: dict, where: str, field_keys: set[str]) -> ButtonAction:
    atype = _get(raw, "type", str, where)
    if atype not in ("open_url", "copy", "clear", "request", "oauth2"):
        raise ServerDefError(f"{where}: unsupported button action type '{atype}'")
    url = _get(raw, "url", str, where, "")
    if url:
        _validate_template_tokens(url, f"{where}.url", field_keys)
    text = _get(raw, "text", str, where, "")
    if text:
        _validate_template_tokens(text, f"{where}.text", field_keys)
    targets = _strings(_get(raw, "targets", list, where, []), where, "targets")
    reload_fields = _strings(_get(raw, "reload_fields", list, where, []), where, "reload_fields")
    verb = _get(raw, "verb", str, where, "POST").upper()
    query = _parse_parts(_get(raw, "query", list, where, []), f"{where}.query", field_keys, "param")
    headers = _parse_parts(_get(raw, "headers", list, where, []), f"{where}.headers", field_keys, "header")
    json_body = _get(raw, "json_body", str, where, "")
    if json_body:
        _validate_template_tokens(json_body, f"{where}.json_body", field_keys)
    form = _parse_parts(_get(raw, "form", list, where, []), f"{where}.form", field_keys, "field")
    raw_extract = _get(raw, "extract", dict, where, {})
    extract: list[tuple[str, str]] = []
    for k, v in raw_extract.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ServerDefError(f"{where}.extract must map string to string")
        extract.append((k, v))
    message = _get(raw, "message", str, where, "")
    provider = _get(raw, "provider", str, where, "")
    scopes = _strings(_get(raw, "scopes", list, where, []), where, "scopes")
    if message:
        _validate_template_tokens(message, f"{where}.message", field_keys)
    return ButtonAction(
        action_type=atype,
        url=url,
        text=text,
        targets=targets,
        reload_fields=reload_fields,
        verb=verb,
        query=query,
        headers=headers,
        json_body=json_body,
        form=form,
        extract=tuple(extract),
        message=message,
        provider=provider,
        scopes=scopes,
    )


def _parse_field(raw: Any, where: str, field_keys: set[str]) -> Field:
    if not isinstance(raw, dict):
        raise ServerDefError(f"{where} must be an object")
    key = _get(raw, "key", str, where)
    if not re.fullmatch(r"[A-Za-z0-9_]+", key):
        raise ServerDefError(f"{where}: invalid field key '{key}'")
    caption = _get(raw, "caption", str, where, key)
    widget = _get(raw, "type", str, where, "text")
    if widget not in WIDGETS:
        raise ServerDefError(f"{where}: unknown field type '{widget}'")

    if "widget" in raw:
        raise ServerDefError(f"{where}: 'widget' is no longer supported; use 'type' instead")
    if "editable" in raw:
        raise ServerDefError(f"{where}: 'editable' is no longer supported")
    if "text" in raw:
        raise ServerDefError(f"{where}: field 'text' is no longer supported; use 'notes' for read-only text")

    raw_notes = raw.get("notes", [])
    if not isinstance(raw_notes, list) or not all(isinstance(x, str) for x in raw_notes):
        raise ServerDefError(f"{where}.notes must be an array of strings")
    notes = tuple(raw_notes)
    for i, line in enumerate(notes):
        _validate_template_tokens(line, f"{where}.notes[{i}]", field_keys)
    if widget == "notes" and "notes" not in raw:
        raise ServerDefError(f"{where}: notes field requires 'notes'")
    if widget != "notes" and notes:
        raise ServerDefError(f"{where}: 'notes' is only valid for type 'notes'")

    raw_choices = _get(raw, "choices", list, where, [])
    choices: list[tuple[str, str]] = []
    for i, c in enumerate(raw_choices):
        if not isinstance(c, dict):
            raise ServerDefError(f"{where}.choices[{i}] must be an object")
        choices.append((_get(c, "value", str, f"{where}.choices[{i}]"),
                        _get(c, "caption", str, f"{where}.choices[{i}]")))

    choices_from = None
    if "choices_from" in raw and raw["choices_from"] is not None:
        choices_from = _parse_choice_source(
            _get(raw, "choices_from", dict, where), f"{where}.choices_from", field_keys
        )

    table_columns: list[CaptureColumn] = []
    if raw.get("table_columns") is not None:
        for i, col in enumerate(_get(raw, "table_columns", list, where)):
            if not isinstance(col, dict):
                raise ServerDefError(f"{where}.table_columns[{i}] must be an object")
            table_columns.append(
                CaptureColumn(
                    caption=_get(col, "caption", str, f"{where}.table_columns[{i}]"),
                    source=_get(col, "source", str, f"{where}.table_columns[{i}]"),
                )
            )

    export_spec = None
    if "export" in raw and raw["export"] is not None:
        exp_raw = _get(raw, "export", dict, where)
        export_spec = CaptureExportSpec(
            template=_get(exp_raw, "template", str, f"{where}.export"),
            default_filename=_get(exp_raw, "default_filename", str, f"{where}.export", "export.txt"),
        )

    save_token_spec = None
    if "save_token" in raw and raw["save_token"] is not None:
        st_raw = _get(raw, "save_token", dict, where)
        save_token_spec = CaptureSaveTokenSpec(
            source=_get(st_raw, "source", str, f"{where}.save_token"),
            to_key=_get(st_raw, "to_key", str, f"{where}.save_token"),
            caption=_get(st_raw, "caption", str, f"{where}.save_token", "Save Token"),
        )

    url = _get(raw, "url", str, where, "")
    if url:
        _validate_template_tokens(url, f"{where}.url", field_keys)

    action_spec = None
    if "action" in raw and raw["action"] is not None:
        action_spec = _parse_button_action(
            _get(raw, "action", dict, where), f"{where}.action", field_keys
        )

    return Field(
        key=key,
        caption=caption,
        widget=widget,
        notes=notes,
        help=_get(raw, "help", str, where, ""),
        save_label=_get(raw, "save_label", str, where, ""),
        mandatory=_get(raw, "mandatory", bool, where, False),
        needs=_get(raw, "needs", str, where, ""),
        needs_multiple=_get(raw, "needs_multiple", bool, where, False),
        disabled_when_all=_strings(_get(raw, "disabled_when_all", list, where, []), where, "disabled_when_all"),
        checked_when_all=_strings(_get(raw, "checked_when_all", list, where, []), where, "checked_when_all"),
        column=_get(raw, "column", int, where, 0),
        choices=tuple(choices),
        choices_from=choices_from,
        table_columns=tuple(table_columns),
        export=export_spec,
        save_token=save_token_spec,
        url=url,
        action=action_spec,
    )


def _parse_prepare(raw: list, where: str, field_keys: set[str]) -> tuple[PrepareStep, ...]:
    steps: list[PrepareStep] = []
    for i, item in enumerate(raw):
        here = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise ServerDefError(f"{here} must be an object")
        verb = _get(item, "verb", str, here, "GET").upper()
        url = _parse_parts(_get(item, "url", list, here), f"{here}.url", field_keys)
        query = _parse_parts(_get(item, "query", list, here, []), f"{here}.query", field_keys, "param")
        headers = _parse_parts(_get(item, "headers", list, here, []), f"{here}.headers", field_keys, "header")
        json_body = _get(item, "json_body", str, here, "")
        if json_body:
            _validate_template_tokens(json_body, f"{here}.json_body", field_keys)
        only_if = _strings(_get(item, "only_if", list, here, []), here, "only_if")
        unless = _strings(_get(item, "unless", list, here, []), here, "unless")
        when_empty = _strings(_get(item, "when_empty", list, here, []), here, "when_empty")
        for cond_key in (*only_if, *unless, *when_empty):
            if not _is_valid_key(cond_key, field_keys):
                raise ServerDefError(f"{here}: unknown condition key '{cond_key}'")
        raw_extract = _get(item, "extract", dict, here, {})
        extract: list[tuple[str, str]] = []
        for k, v in raw_extract.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ServerDefError(f"{here}.extract must map string to string")
            if not _is_valid_key(k, field_keys):
                raise ServerDefError(f"{here}: extract target '{k}' is invalid")
            extract.append((k, v))
        steps.append(
            PrepareStep(
                verb=verb,
                url=url,
                query=query,
                headers=headers,
                json_body=json_body,
                only_if=only_if,
                unless=unless,
                when_empty=when_empty,
                extract=tuple(extract),
            )
        )
    return tuple(steps)


def _parse_request(raw: dict, where: str, field_keys: set[str]) -> RequestSpec:
    verb = _get(raw, "verb", str, where, "POST").upper()
    body = _get(raw, "body", str, where, "raw")
    if body not in BODY_MODES:
        raise ServerDefError(f"{where}: unsupported body mode '{body}'")
    url = _parse_parts(_get(raw, "url", list, where), f"{where}.url", field_keys)
    query = _parse_parts(_get(raw, "query", list, where, []), f"{where}.query", field_keys, "param")
    headers = _parse_parts(_get(raw, "headers", list, where, []), f"{where}.headers", field_keys, "header")
    form = _parse_parts(_get(raw, "form", list, where, []), f"{where}.form", field_keys, "field")
    metadata = _parse_parts(_get(raw, "metadata", list, where, []), f"{where}.metadata", field_keys)
    mime = _get(raw, "mime", str, where, "auto").strip() or "auto"
    if mime.lower() != "auto":
        _validate_template_tokens(mime, f"{where}.mime", field_keys)
        if "{" not in mime and not is_valid_mime(mime):
            raise ServerDefError(f"{where}.mime: '{mime}' is not a valid MIME type (or use \"auto\")")
    return RequestSpec(
        verb=verb,
        url=url,
        query=query,
        headers=headers,
        keepalive=_get(raw, "keepalive", int, where, 30),
        body=body,
        file_field=_get(raw, "file_field", str, where, "file"),
        form=form,
        metadata=metadata,
        mime=mime,
    )


def _parse_response(raw: dict, where: str) -> ResponseSpec:
    raw_results = _get(raw, "results", list, where, [])
    rules: list[ResultRule] = []
    for i, r in enumerate(raw_results):
        here = f"{where}.results[{i}]"
        if not isinstance(r, dict):
            raise ServerDefError(f"{here} must be an object")
        raw_cap = _get(r, "capture", dict, here, {})
        cap: list[tuple[str, str]] = []
        for k, v in raw_cap.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ServerDefError(f"{here}.capture must map string to string")
            cap.append((k, v))
        rules.append(
            ResultRule(
                at=_get(r, "at", str, here),
                url=_get(r, "url", str, here),
                name_at=_get(r, "name_at", str, here, ""),
                starts_with=_get(r, "starts_with", str, here, ""),
                ok_at=_get(r, "ok_at", str, here, ""),
                ok_equals=_get(r, "ok_equals", str, here, ""),
                capture=tuple(cap),
            )
        )

    pattern = _get(raw, "fallback_url_pattern", str, where, "")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ServerDefError(f"{where}: bad 'fallback_url_pattern': {exc}") from exc

    raw_emap = _get(raw, "error_map", dict, where, {})
    error_map: list[tuple[str, str]] = []
    for k, v in raw_emap.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ServerDefError(f"{where}.error_map must map string to string")
        error_map.append((k, v))

    raw_smap = _get(raw, "status_errors", dict, where, {})
    status_errors: list[tuple[int, str]] = []
    for k, v in raw_smap.items():
        if not isinstance(v, str) or not str(k).isdigit():
            raise ServerDefError(f"{where}.status_errors must map HTTP status code to string")
        status_errors.append((int(k), v))

    return ResponseSpec(
        network_error_from=_get(raw, "network_error_from", int, where, 500),
        upload_error_from=_get(raw, "upload_error_from", int, where, 400),
        fallback_url_pattern=pattern,
        results=tuple(rules),
        error_at=_strings(_get(raw, "error_at", list, where, ["message", "error"]), where, "error_at"),
        error_map=tuple(error_map),
        status_errors=tuple(status_errors),
    )


def _parse_captures(raw: list, where: str, keys: set[str]) -> tuple[CaptureRule, ...]:
    rules: list[CaptureRule] = []
    for i, item in enumerate(raw):
        here = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise ServerDefError(f"{here} must be an object")
        source = _get(item, "from", str, here)
        target = _get(item, "to", str, here)
        if not _is_valid_key(target, keys):
            raise ServerDefError(f"{here}: 'to' names unknown field '{target}'")
        only_if = _strings(_get(item, "only_if", list, here, []), here, "only_if")
        when_empty = _strings(_get(item, "when_empty", list, here, []), here, "when_empty")
        rules.append(CaptureRule(source=source, target=target, only_if=only_if, when_empty=when_empty))
    return tuple(rules)


def _parse_auth(raw_auth: dict, where: str) -> AuthSpec:
    auth_type = _get(raw_auth, "type", str, where)
    if auth_type != "oauth2":
        raise ServerDefError(f"{where}: unsupported type '{auth_type}'")
    provider = _get(raw_auth, "provider", str, where).strip()
    if not provider:
        raise ServerDefError(f"{where}.provider must not be empty")
    scopes = _strings(_get(raw_auth, "scopes", list, where, []), where, "scopes")
    auth_uri = _get(raw_auth, "auth_uri", str, where, "")
    token_uri = _get(raw_auth, "token_uri", str, where, "")
    revoke_uri = _get(raw_auth, "revoke_uri", str, where, "")
    client_id = _get(raw_auth, "client_id", str, where, "")
    client_secret = _get(raw_auth, "client_secret", str, where, "")
    default_client_file = "google_oauth_client.json" if (provider == "google" and not client_id) else ""
    client_file = _get(raw_auth, "client_file", str, where, default_client_file)
    raw_params = _get(raw_auth, "auth_params", dict, where, {})
    auth_params: list[tuple[str, str]] = []
    for k, v in raw_params.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ServerDefError(f"{where}.auth_params must map string to string")
        auth_params.append((k, v))
    redirect_port = _get(raw_auth, "redirect_port", int, where, 0)
    if not (0 <= redirect_port <= 65535):
        raise ServerDefError(f"{where}.redirect_port must be between 0 and 65535")
    redirect_path = _get(raw_auth, "redirect_path", str, where, "")
    pkce = _get(raw_auth, "pkce", bool, where, True)
    token_key = _get(raw_auth, "token_key", str, where, "api_key")
    credentials_key = _get(raw_auth, "credentials_key", str, where, "oauth_credentials")
    return AuthSpec(
        provider=provider,
        scopes=scopes,
        auth_uri=auth_uri,
        token_uri=token_uri,
        revoke_uri=revoke_uri,
        client_id=client_id,
        client_secret=client_secret,
        client_file=client_file,
        auth_params=tuple(auth_params),
        redirect_port=redirect_port,
        redirect_path=redirect_path,
        pkce=pkce,
        token_key=token_key,
        credentials_key=credentials_key,
    )


def parse_server(
    data: Any,
    source: Path | None = None,
    *,
    source_url: str = "",
) -> Server:
    where = source.name if source else (source_url or "server")
    if not isinstance(data, dict):
        raise ServerDefError(f"{where}: top level must be an object")
    if _get(data, "schema", int, where) != SCHEMA_VERSION:
        raise ServerDefError(f"{where}: unsupported schema (expected {SCHEMA_VERSION})")
    server_id = _get(data, "id", str, where)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", server_id):
        raise ServerDefError(f"{where}: 'id' may only contain letters, digits, '-' and '_'")

    name = _get(data, "name", str, where, "") or _get(data, "title", str, where, server_id)
    base_url = _get(data, "base_url", str, where, "")

    raw_fields = _get(data, "fields", list, where, [])
    keys = {f.get("key") for f in raw_fields if isinstance(f, dict) and isinstance(f.get("key"), str)}
    fields = tuple(_parse_field(f, f"{where}: fields[{i}]", keys) for i, f in enumerate(raw_fields))
    if len(fields) != len(keys):
        raise ServerDefError(f"{where}: duplicate field 'key'")
    if keys & RESERVED_KEYS:
        raise ServerDefError(f"{where}: field keys {sorted(RESERVED_KEYS)} are reserved")

    auth: AuthSpec | None = None
    if "auth" in data and data["auth"] is not None:
        auth = _parse_auth(_get(data, "auth", dict, where), f"{where}.auth")
        if auth.token_key:
            keys.add(auth.token_key)

    prepare = _parse_prepare(
        _get(data, "prepare", list, where, []), f"{where}: prepare", keys
    )
    captures = _parse_captures(
        _get(data, "capture", list, where, []), f"{where}: capture", keys
    )

    return Server(
        id=server_id,
        name=name,
        base_url=base_url,
        fields=fields,
        request=_parse_request(_get(data, "request", dict, where), f"{where}: request", keys),
        response=_parse_response(_get(data, "response", dict, where, {}), f"{where}: response"),
        prepare=prepare,
        capture=captures,
        path=source,
        source_url=source_url,
        auth=auth,
    )


def _decode_document(text: str, where: str, suffix: str = "") -> Any:
    """Parse server-definition text. ``.yaml``/``.yml`` need PyYAML; JSON is tried otherwise,
    with a YAML fallback (YAML is a superset of JSON) when the suffix is unknown."""
    suffix = suffix.lower()
    if suffix not in YAML_SUFFIXES:
        try:
            return json.loads(text)
        except ValueError as exc:
            if suffix in JSON_SUFFIXES:
                raise ServerDefError(f"{where}: invalid JSON ({exc})") from exc
            json_error = exc
    try:
        import yaml
    except ImportError:
        if suffix in YAML_SUFFIXES:
            raise ServerDefError(f"{where}: YAML definitions need PyYAML (pip install pyyaml)") from None
        raise ServerDefError(f"{where}: invalid JSON ({json_error})") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ServerDefError(f"{where}: invalid YAML ({exc})") from exc


def load_server(path: Path) -> Server:
    path = Path(path)
    if path.suffix.lower() not in SUFFIXES:
        raise ServerDefError(
            f"{path.name}: unsupported server definition extension "
            f"(expected .json, .yaml, or .yml)"
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        raise ServerDefError(f"{path.name}: {exc}") from exc
    return parse_server(_decode_document(text, path.name, path.suffix), path)


def _fetch_remote_server(url: str, timeout: float = 15.0) -> Server:
    suffix = Path(urllib.parse.urlsplit(url).path).suffix
    if suffix and suffix.lower() not in SUFFIXES:
        raise ServerDefError(
            f"{url}: unsupported server definition extension "
            f"(expected .json, .yaml, or .yml)"
        )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"DrainPype/{__version__}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_text = resp.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        raise ServerDefError(f"{url}: HTTP {exc.code}") from exc
    except OSError as exc:
        raise ServerDefError(f"{url}: {exc}") from exc
    data = _decode_document(raw_text, url, suffix)
    return parse_server(data, source=None, source_url=url)


def add_local_server(source_path: Path) -> Server:
    """Validate a local server definition and copy it into the user's servers directory."""
    src = Path(source_path)
    server = load_server(src)
    directory = custom_servers_dir()
    directory.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower() if src.suffix.lower() in SUFFIXES else ".json"
    target = directory / f"{server.id}{suffix}"
    if src.resolve() != target.resolve():
        shutil.copy2(src, target)
    return load_server(target)


def add_url_server(url: str) -> Server:
    """Fetch and validate a remote server JSON URL and register it in ``server_urls.json``."""
    clean_url = url.strip()
    if not clean_url.startswith(("http://", "https://")):
        raise ServerDefError("Server URL must start with http:// or https://")
    server = _fetch_remote_server(clean_url)
    urls = _load_server_urls()
    if clean_url not in urls:
        urls.append(clean_url)
        _save_server_urls(urls)
    return server


def remove_server(location: str) -> None:
    """Delete a local server definition file or unregister a remote server URL."""
    loc = location.strip()
    if loc.startswith(("http://", "https://")):
        urls = [u for u in _load_server_urls() if u != loc]
        _save_server_urls(urls)
        return
    path = Path(loc)
    if path.is_file():
        path.unlink()


def load_servers(
    directory: Path | str | None = None,
    *,
    include_urls: bool = True,
) -> LoadResult:
    """Load local server definitions (and optionally registered remote URLs)."""
    custom_dir = directory is not None
    target_dir = Path(directory) if custom_dir else custom_servers_dir()
    servers: dict[str, Server] = {}
    errors: dict[str, str] = {}

    if not custom_dir and DEFAULT_SERVERS_DIR.is_dir():
        for path in _iter_server_files(DEFAULT_SERVERS_DIR):
            try:
                server = load_server(path)
            except ServerDefError as exc:
                errors[str(path)] = str(exc)
                continue
            servers[server.id] = server

    for path in _iter_server_files(target_dir):
        try:
            server = load_server(path)
        except ServerDefError as exc:
            errors[str(path)] = str(exc)
            continue
        if server.id in servers:
            # User-installed definitions override embedded defaults.
            servers[server.id] = server
            continue
        servers[server.id] = server

    if include_urls and not custom_dir:
        for url in _load_server_urls():
            try:
                server = _fetch_remote_server(url)
            except ServerDefError as exc:
                errors[url] = str(exc)
                continue
            if server.id in servers:
                errors[url] = f"{url}: duplicate server id '{server.id}'"
                continue
            servers[server.id] = server

    ordered = dict(sorted(servers.items(), key=lambda kv: kv[1].name.lower()))
    return LoadResult(ordered, errors)


def list_server_entries(loaded: LoadResult | None = None) -> list[ServerEntry]:
    """Return display rows (including any broken files/URLs) for the Servers modal."""
    if loaded is None:
        loaded = load_servers(include_urls=False)
    entries: list[ServerEntry] = []
    seen_locations: set[str] = set()

    for server in loaded.servers.values():
        if server.path is not None:
            try:
                is_embedded = server.path.resolve().is_relative_to(DEFAULT_SERVERS_DIR.resolve())
            except AttributeError:
                is_embedded = str(server.path.resolve()).startswith(str(DEFAULT_SERVERS_DIR.resolve()) + os.sep)
            loc = "Embedded" if is_embedded else str(server.path)
        else:
            loc = server.source_url
        seen_locations.add(loc)
        entries.append(
            ServerEntry(
                name=server.name,
                location=loc,
                base_url=server.base_url,
                error="",
            )
        )

    for loc, err in loaded.errors.items():
        if loc in seen_locations:
            continue
        seen_locations.add(loc)
        display_name = Path(loc).stem if not loc.startswith(("http://", "https://")) else loc.rsplit("/", 1)[-1]
        entries.append(
            ServerEntry(
                name=display_name or loc,
                location=loc,
                base_url="",
                error=err,
            )
        )

    return sorted(entries, key=lambda e: e.name.lower())


# --------------------------------------------------------------------------- requests
def prepare_upload(
    server: Server,
    values: dict[str, Any],
    *,
    abort_check: Callable[[], bool] | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Run any declarative pre-upload API steps (such as looking up or creating a folder)."""
    if not server.prepare:
        return
    for index, step in enumerate(server.prepare):
        if abort_check and abort_check():
            return
        if progress:
            progress(index / max(1, len(server.prepare)))
        ctx = _context(values)
        ctx["base_url"] = server.base_url
        if any(not ctx.get(k) for k in step.only_if):
            continue
        if any(ctx.get(k) for k in step.unless):
            continue
        if any(ctx.get(k) for k in step.when_empty):
            continue

        url = "".join(render(p.text, ctx) for p in step.url if _active(p, ctx))
        query = [f"{p.name}={render(p.text, ctx)}" for p in step.query if _active(p, ctx)]
        if query:
            url += ("&" if "?" in url else "?") + "&".join(query)

        headers = {"User-Agent": f"DrainPype/{__version__}"}
        headers.update({p.name: render(p.text, ctx) for p in step.headers if _active(p, ctx)})
        body: bytes | None = None
        if step.json_body:
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
            body = render(step.json_body, ctx).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers, method=step.verb)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            if exc.code >= server.response.network_error_from:
                raise ResponseError("network", "Server Error") from None
            raise ResponseError("upload", _short_reason(server.response, err_body, exc.code)) from None
        except OSError as exc:
            raise ResponseError("network", f"Network Error: {exc}") from None

        try:
            payload = json.loads(raw_text)
        except ValueError:
            raise ResponseError("network", "Invalid server response") from None

        for target_key, json_path in step.extract:
            found = dig(payload, json_path)
            if found is not None and str(found).strip():
                values[target_key] = str(found).strip()
        if progress:
            progress((index + 1) / max(1, len(server.prepare)))


def execute_button_action(
    server: Server, field: Field, values: dict[str, Any]
) -> tuple[dict[str, str], str]:
    """Execute a button field's HTTP ``request`` action and return ``(captured, message)``."""
    action = field.action
    if action is None or action.action_type != "request":
        return {}, ""

    ctx = _context(values)
    ctx["base_url"] = server.base_url
    url = resolve_url(server.base_url, render(action.url, ctx))
    query = [f"{p.name}={render(p.text, ctx)}" for p in action.query if _active(p, ctx)]
    if query:
        url += ("&" if "?" in url else "?") + "&".join(query)

    headers = {"User-Agent": f"DrainPype/{__version__}", "Accept": "application/json"}
    headers.update({p.name: render(p.text, ctx) for p in action.headers if _active(p, ctx)})

    body: bytes | None = None
    if action.json_body:
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
        body = render(action.json_body, ctx).encode("utf-8")
    elif action.form:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        pairs = [(p.name, render(p.text, ctx)) for p in action.form if _active(p, ctx)]
        body = urllib.parse.urlencode(pairs).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=action.verb)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(_short_reason(server.response, err_body, exc.code)) from None
    except OSError as exc:
        raise RuntimeError(f"Network Error: {exc}") from None

    try:
        payload = json.loads(raw_text)
    except ValueError:
        payload = {}

    captured: dict[str, str] = {}
    if isinstance(payload, dict):
        for target_key, json_path in action.extract:
            found = dig(payload, json_path)
            if found is not None and str(found).strip():
                captured[target_key] = str(found).strip()

    msg_ctx = dict(ctx)
    msg_ctx.update(captured)
    message = render(action.message, msg_ctx) if action.message else ""
    return captured, message


def build_request(
    server: Server,
    filename: str,
    values: dict[str, Any],
    mime: str = "",
    size: int = 0,
) -> HttpRequest:
    """Turn the user's option values into the concrete HTTP request for one file.

    ``mime`` is the auto-detected type of the file; the definition's ``request.mime`` may
    override it with a fixed type or a template.
    """
    spec = server.request
    ctx = _context(values)
    ctx["base_url"] = server.base_url
    ctx["filename"] = filename  # never stripped or altered
    name = Path(filename)
    ctx["stem"] = name.stem
    ctx["ext"] = name.suffix.lstrip(".")
    ctx["size"] = str(size)
    file_mime = mime if mime and is_valid_mime(mime) else DEFAULT_MIME
    ctx["mime"] = file_mime
    if spec.mime.lower() != "auto":
        declared = render(spec.mime, ctx).strip().lower()
        if is_valid_mime(declared):
            file_mime = declared
            ctx["mime"] = file_mime

    url = "".join(render(p.text, ctx) for p in spec.url if _active(p, ctx))
    query = [f"{p.name}={render(p.text, ctx)}" for p in spec.query if _active(p, ctx)]
    if query:
        url += ("&" if "?" in url else "?") + "&".join(query)

    headers = [(p.name, render(p.text, ctx)) for p in spec.headers if _active(p, ctx)]
    form_fields = [(p.name, render(p.text, ctx)) for p in spec.form if _active(p, ctx)]
    metadata = next(
        (render(p.text, ctx) for p in spec.metadata if _active(p, ctx)),
        "",
    )
    return HttpRequest(
        spec.verb,
        url,
        headers,
        spec.keepalive,
        body=spec.body,
        file_field=spec.file_field,
        form_fields=form_fields,
        metadata=metadata,
        file_mime=file_mime,
    )


def apply_captures(server: Server, values: dict[str, Any], parsed: ParsedResponse) -> dict[str, str]:
    """Update shared session values from captured response fields; returns applied updates."""
    applied: dict[str, str] = {}
    if not server.capture or not parsed.captured:
        return applied
    for rule in server.capture:
        extracted = str(parsed.captured.get(rule.source, "")).strip()
        if not extracted:
            continue
        if any(not str(values.get(k, "")).strip() for k in rule.only_if):
            continue
        if any(str(values.get(k, "")).strip() for k in rule.when_empty):
            continue
        values[rule.target] = extracted
        applied[rule.target] = extracted
    return applied


def _first_truthy(mapping: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if mapping.get(key):
            return mapping[key]
    return None


def fetch_choices(
    field: Field,
    values: dict[str, Any],
    on_error: Callable[[str], None] | None = None,
    base_url: str = "",
) -> list[tuple[str, str]]:
    """Load a select field's options from its ``choices_from`` endpoint (blocking).

    Always returns the static ``choices`` first. On any failure only those are returned
    and the reason is passed to ``on_error`` (so the GUI can show why a list is empty).
    """
    base = list(field.choices)
    source = field.choices_from
    if source is None:
        return base

    def fail(message: str) -> list[tuple[str, str]]:
        if on_error:
            on_error(message)
        return base

    ctx = _context(values)
    ctx["base_url"] = base_url
    headers = {"User-Agent": f"DrainPype/{__version__}", "Accept": "application/json"}
    headers.update({p.name: render(p.text, ctx) for p in source.headers if _active(p, ctx)})
    target_url = resolve_url(base_url, render(source.url, ctx))
    request = urllib.request.Request(target_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=source.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return fail(f"HTTP {exc.code} from {request.full_url}")
    except OSError as exc:
        return fail(f"Could not reach {request.full_url}: {exc}")
    except ValueError:
        return fail(f"{request.full_url} did not return valid JSON")

    while isinstance(data, dict):
        key = next((k for k in source.list_at if k in data), None)
        if key is None:
            break
        data = data[key]

    items: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            caption = _first_truthy(value, source.caption_keys) if isinstance(value, dict) else value
            items.append((str(key), str(caption or key)))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                items.append((item, item))
            elif isinstance(item, dict):
                value = _first_truthy(item, source.value_keys)
                if value is not None:
                    items.append((str(value), str(_first_truthy(item, source.caption_keys) or value)))
    if not items:
        return fail("The response contained no options")
    return base + items


# --------------------------------------------------------------------------- responses
def _short_reason(spec: ResponseSpec, body: str, status: int) -> str:
    message = ""
    raw_status = ""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw_status = str(payload.get("status") or "")
        for path in spec.error_at:
            found = dig(payload, path)
            if found:
                message = str(found)
                break
    emap = dict(spec.error_map)
    if raw_status and raw_status in emap:
        return emap[raw_status]
    if message and message in emap:
        return emap[message]
    smap = dict(spec.status_errors)
    if status in smap:
        return smap[status]
    if not message:
        message = body[:120].strip()
    return message or (f"HTTP {status}" if status else "Upload failed")


def parse_response(server: Server, body: str, status: int) -> ParsedResponse:
    """Interpret a server reply; raises :class:`ResponseError` on failure."""
    spec = server.response
    text = body.strip()
    if status >= spec.network_error_from:
        raise ResponseError("network", "Server Error")
    if status >= spec.upload_error_from:
        raise ResponseError("upload", _short_reason(spec, text, status))
    if not text:
        raise ResponseError("upload", "Empty server response")
    try:
        payload = json.loads(text)
    except ValueError:
        if spec.fallback_url_pattern:
            match = re.search(spec.fallback_url_pattern, text)
            if match:
                return ParsedResponse(match.group(0), "", {})
        raise ResponseError("network", "Invalid server response") from None
    if isinstance(payload, dict):
        for rule in spec.results:
            if rule.ok_at:
                ok_val = dig(payload, rule.ok_at)
                if str(ok_val or "") != rule.ok_equals:
                    continue
            value = dig(payload, rule.at)
            if not value:
                continue
            if rule.starts_with and not (isinstance(value, str) and value.startswith(rule.starts_with)):
                continue
            name = str(dig(payload, rule.name_at) or "") if rule.name_at else ""
            captured = {}
            for cap_key, cap_path in rule.capture:
                cap_val = dig(payload, cap_path)
                if cap_val is not None and str(cap_val).strip():
                    captured[cap_key] = str(cap_val).strip()
            return ParsedResponse(
                render(rule.url, {"value": str(value), "base_url": server.base_url}),
                name,
                payload,
                captured,
            )
    raise ResponseError("upload", _short_reason(spec, text, status))
