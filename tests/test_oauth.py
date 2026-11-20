from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from uploader import keys as keys_mod
from uploader import oauth as oauth_mod
from uploader.oauth import OAuthError, access_token, authorize_oauth, disconnect_oauth
from uploader.servers import AuthSpec, parse_server


@pytest.fixture(autouse=True)
def _isolate_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAINPYPE_HOME", str(tmp_path))
    yield


def _oauth_server(**auth_overrides) -> object:
    auth_cfg = {
        "type": "oauth2",
        "provider": "custom_cloud",
        "scopes": ["read", "write"],
        "auth_uri": "https://auth.example.test/authorize",
        "token_uri": "https://auth.example.test/token",
        "revoke_uri": "https://auth.example.test/revoke",
        "client_id": "cid-1",
        "client_secret": "csec-1",
        "token_key": "oauth_token",
        "credentials_key": "oauth_credentials",
    }
    auth_cfg.update(auth_overrides)
    return parse_server(
        {
            "schema": 1,
            "id": "custom_cloud",
            "name": "Custom Cloud",
            "auth": auth_cfg,
            "request": {"verb": "POST", "url": [{"text": "https://auth.example.test/upload"}]},
        }
    )


def test_access_token_returns_valid_cached_token():
    srv = _oauth_server()
    keys_mod.save_key(
        srv.id,
        "oauth_credentials",
        json.dumps({"access_token": "cached-tok", "expires_in": 3600, "created_at": int(time.time())}),
    )
    assert access_token(srv) == "cached-tok"


def test_access_token_refreshes_expired_token():
    srv = _oauth_server()
    keys_mod.save_key(
        srv.id,
        "oauth_credentials",
        json.dumps({
            "access_token": "old-tok",
            "refresh_token": "ref-tok",
            "expires_in": 10,
            "created_at": int(time.time()) - 1000,
        }),
    )

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"access_token": "fresh-tok", "expires_in": 3600}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        tok = access_token(srv)
        assert tok == "fresh-tok"
        assert mock_open.call_count == 1

    saved = json.loads(keys_mod.load_keys(srv.id)["oauth_credentials"])
    assert saved["access_token"] == "fresh-tok"
    assert saved["refresh_token"] == "ref-tok"


def test_authorize_oauth_exchanges_code_and_persists_credentials():
    srv = _oauth_server()
    captured_state: dict[str, str] = {}

    def fake_open(url: str) -> bool:
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        captured_state["state"] = qs["state"][0]
        return True

    class DummyServer:
        server_address = ("127.0.0.1", 54321)
        timeout = 300
        result = None
        expected_path = ""
        provider_label = "Custom_cloud"

        def handle_request(self) -> None:
            self.result = {"code": "auth-code-123", "state": captured_state["state"]}

        def server_close(self) -> None:
            pass

    mock_token_resp = MagicMock()
    mock_token_resp.read.return_value = json.dumps({
        "access_token": "new-access-tok",
        "refresh_token": "new-refresh-tok",
        "expires_in": 3600,
    }).encode("utf-8")
    mock_token_resp.__enter__.return_value = mock_token_resp

    with (
        patch("http.server.ThreadingHTTPServer", return_value=DummyServer()),
        patch("webbrowser.open", side_effect=fake_open),
        patch("urllib.request.urlopen", return_value=mock_token_resp),
    ):
        token = authorize_oauth(srv)

    assert token == "new-access-tok"
    assert access_token(srv) == "new-access-tok"


def test_disconnect_oauth_revokes_and_clears_keys():
    srv = _oauth_server()
    keys_mod.save_key(srv.id, "oauth_credentials", json.dumps({"access_token": "tok-1"}))
    keys_mod.save_key(srv.id, "api_key", "legacy-key")

    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        disconnect_oauth(srv)
        assert mock_open.call_count == 1

    saved = keys_mod.load_keys(srv.id)
    assert "oauth_credentials" not in saved
    assert "api_key" not in saved


def test_authorize_oauth_missing_client_id_raises():
    srv = _oauth_server(client_id="", client_file="")
    with pytest.raises(OAuthError, match="client_id"):
        authorize_oauth(srv)