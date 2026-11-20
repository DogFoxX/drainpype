"""Universal OAuth 2.0 support for desktop server definitions."""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .keys import delete_key, load_keys, save_key

if TYPE_CHECKING:
    from .servers import Server

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.file"
CREDENTIALS_KEY = "oauth_credentials"

_lock = threading.RLock()


class OAuthError(RuntimeError):
    pass


def _assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


def _provider_label(server: "Server") -> str:
    if not server.auth or not server.auth.provider:
        return "OAuth"
    provider = server.auth.provider.strip()
    return "Google" if provider.lower() == "google" else provider.capitalize()


def _resolve_client_file(server: "Server", client_file: str) -> Path:
    path = Path(client_file).expanduser()
    if path.is_absolute():
        return path

    if server.path is not None:
        candidate = (server.path.parent / path).resolve()
        if candidate.is_file():
            return candidate

    return (_assets_dir() / path).resolve()


def _client_config(server: "Server") -> dict[str, Any]:
    auth = server.auth
    if auth is None:
        raise OAuthError("This server is not configured for OAuth 2.0.")

    label = _provider_label(server)
    is_google = auth.provider.strip().lower() == "google"
    file_config: dict[str, Any] = {}

    if auth.client_file:
        path = _resolve_client_file(server, auth.client_file)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise OAuthError(
                f"{label} OAuth client configuration was not found at {path}."
            ) from None
        except (OSError, ValueError) as exc:
            raise OAuthError(f"Could not read {label} OAuth client configuration: {exc}") from None

        if isinstance(data, dict):
            nested = data.get("installed") or data.get("web")
            file_config = nested if isinstance(nested, dict) else data

    client_id = auth.client_id or str(file_config.get("client_id") or "")
    client_secret = auth.client_secret or str(file_config.get("client_secret") or "")
    auth_uri = auth.auth_uri or str(file_config.get("auth_uri") or (GOOGLE_AUTH_URI if is_google else ""))
    token_uri = auth.token_uri or str(file_config.get("token_uri") or (GOOGLE_TOKEN_URI if is_google else ""))
    revoke_uri = auth.revoke_uri or str(file_config.get("revoke_uri") or (GOOGLE_REVOKE_URI if is_google else ""))

    if not client_id:
        raise OAuthError(f"{label} OAuth configuration is missing 'client_id'.")
    if not auth_uri:
        raise OAuthError(f"{label} OAuth configuration is missing 'auth_uri'.")
    if not token_uri:
        raise OAuthError(f"{label} OAuth configuration is missing 'token_uri'.")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": auth_uri,
        "token_uri": token_uri,
        "revoke_uri": revoke_uri,
    }


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "DrainPypeOAuth/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        expected_path = getattr(self.server, "expected_path", "")
        valid_paths = {"/", "/oauth2callback", "/callback"}
        if expected_path:
            valid_paths.add(expected_path)
        if parsed.path not in valid_paths:
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        self.server.result = {k: v[0] for k, v in params.items()}  # type: ignore[attr-defined]
        label = getattr(self.server, "provider_label", "Account")
        body = (
            "<html><head>"
            "<title>DrainPype</title>"
            "<style>"
            "body {font-family:system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,Oxygen,Ubuntu,Cantarell,\"Open Sans\",\"Helvetica Neue\",sans-serif;padding:5rem 0;text-align:center;background-color:#20242c;}"
            "h1 {margin:auto;color:#dba26a;}"
            "p {color:white;}"
            "</style>"
            "</head><body>"
            "<h1>Authorization Success! 🥳</h1>"
            f"<p>{label} authorization complete. You can close this browser window and return to DrainPype.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        threading.Thread(target=self.server.shutdown, daemon=True).start()  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _token_request(
    token_uri: str,
    form: dict[str, str],
    *,
    label: str = "OAuth",
    expect_refresh: bool = False,
) -> dict[str, Any]:
    req = urllib.request.Request(
        token_uri,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(detail)
            detail = payload.get("error_description") or payload.get("error") or detail
        except ValueError:
            pass
        raise OAuthError(f"{label} OAuth token request failed: {detail}") from None
    except (OSError, ValueError) as exc:
        raise OAuthError(f"{label} OAuth token request failed: {exc}") from None
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(f"{label} OAuth response did not contain an access token.")
    if expect_refresh:
        data["authorization_code"] = True
    return data


def _credential_from_saved(server: "Server") -> dict[str, Any] | None:
    keys = load_keys(server.id)
    raw = keys.get(server.auth.credentials_key if server.auth else CREDENTIALS_KEY, "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _save_credentials(server: "Server", credentials: dict[str, Any]) -> None:
    key = server.auth.credentials_key if server.auth else CREDENTIALS_KEY
    save_key(server.id, key, json.dumps(credentials, separators=(",", ":")))


def authorize_oauth(server: "Server") -> str:
    """Run the desktop OAuth 2.0 flow for any configured provider and save credentials."""
    if not server.auth or not server.auth.provider:
        raise OAuthError("This server is not configured for OAuth 2.0.")
    label = _provider_label(server)
    is_google = server.auth.provider.strip().lower() == "google"
    with _lock:
        config = _client_config(server)
        scopes = tuple(server.auth.scopes) or ((GOOGLE_SCOPE,) if is_google else ())
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(32)
        bind_port = server.auth.redirect_port if server.auth.redirect_port > 0 else 0
        callback = http.server.ThreadingHTTPServer(("127.0.0.1", bind_port), _CallbackHandler)
        port = callback.server_address[1]
        raw_path = server.auth.redirect_path.strip()
        if raw_path and not raw_path.startswith("/"):
            raw_path = "/" + raw_path
        redirect_uri = f"http://127.0.0.1:{port}{raw_path}"
        callback.result = None  # type: ignore[attr-defined]
        callback.expected_path = raw_path  # type: ignore[attr-defined]
        callback.provider_label = label  # type: ignore[attr-defined]

        params: dict[str, str] = {
            "client_id": config["client_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        if is_google:
            params.setdefault("access_type", "offline")
            params.setdefault("prompt", "consent")
        if server.auth.pkce:
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
        for k, v in server.auth.auth_params:
            params[k] = v

        sep = "&" if "?" in config["auth_uri"] else "?"
        url = config["auth_uri"] + sep + urllib.parse.urlencode(params)
        if not webbrowser.open(url):
            callback.server_close()
            raise OAuthError(f"Could not open the {label} authorization page in your browser.")
        callback.timeout = 300
        try:
            callback.handle_request()
        finally:
            callback.server_close()
        result = getattr(callback, "result", None)
        if not isinstance(result, dict):
            raise OAuthError(f"{label} authorization timed out or did not return a response.")
        if result.get("state") != state:
            raise OAuthError(f"{label} authorization failed: invalid OAuth state.")
        if result.get("error"):
            raise OAuthError(result.get("error_description") or result["error"])
        code = result.get("code")
        if not code:
            raise OAuthError(f"{label} authorization did not return an authorization code.")
        form: dict[str, str] = {
            "code": code,
            "client_id": config["client_id"],
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if server.auth.pkce:
            form["code_verifier"] = verifier
        if config.get("client_secret"):
            form["client_secret"] = config["client_secret"]
        token = _token_request(config["token_uri"], form, label=label)
        token["created_at"] = int(time.time())
        _save_credentials(server, token)
        return str(token["access_token"])


def authorize_google(server: "Server") -> str:
    """Backwards-compatible alias for :func:`authorize_oauth`."""
    return authorize_oauth(server)


def access_token(server: "Server") -> str:
    """Return a usable OAuth 2.0 access token, refreshing it when necessary."""
    if not server.auth or not server.auth.provider:
        return ""
    label = _provider_label(server)
    with _lock:
        creds = _credential_from_saved(server)
        if not creds:
            return ""
        access = str(creds.get("access_token", ""))
        expires_in = int(creds.get("expires_in", 3600) or 3600)
        created = int(creds.get("created_at", 0) or 0)
        if access and created and time.time() < created + max(60, expires_in - 60):
            return access
        refresh = str(creds.get("refresh_token", ""))
        if not refresh:
            return access
        config = _client_config(server)
        form = {
            "client_id": config["client_id"],
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        if config.get("client_secret"):
            form["client_secret"] = config["client_secret"]
        refreshed = _token_request(config["token_uri"], form, label=label)
        refreshed["created_at"] = int(time.time())
        refreshed.setdefault("refresh_token", refresh)
        _save_credentials(server, refreshed)
        return str(refreshed["access_token"])


def disconnect_oauth(server: "Server") -> None:
    """Revoke the stored OAuth grant where supported, then remove local credentials."""
    if not server.auth or not server.auth.provider:
        return
    creds = _credential_from_saved(server) or {}
    token = str(creds.get("refresh_token") or creds.get("access_token") or "")
    revoke_uri = ""
    try:
        revoke_uri = _client_config(server).get("revoke_uri", "")
    except OAuthError:
        if server.auth.provider.strip().lower() == "google":
            revoke_uri = GOOGLE_REVOKE_URI
    if token and revoke_uri:
        request = urllib.request.Request(
            revoke_uri,
            data=urllib.parse.urlencode({"token": token}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15):
                pass
        except (OSError, urllib.error.HTTPError):
            pass
    delete_key(server.id, server.auth.credentials_key)
    delete_key(server.id, "api_key")


def disconnect_google(server: "Server") -> None:
    """Backwards-compatible alias for :func:`disconnect_oauth`."""
    disconnect_oauth(server)