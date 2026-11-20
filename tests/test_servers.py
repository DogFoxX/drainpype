from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from uploader.servers import (
    DEFAULT_SERVERS_DIR,
    ResponseError,
    ServerDefError,
    apply_captures,
    build_request,
    fetch_choices,
    load_servers,
    parse_response,
    parse_server,
    prepare_upload,
)


def _minimal_server_dict(**overrides) -> dict:
    base = {
        "schema": 1,
        "id": "test_srv",
        "name": "Test Server",
        "base_url": "https://example.test",
        "fields": [
            {"key": "api_key", "caption": "API Key", "type": "secret", "mandatory": True},
        ],
        "request": {
            "verb": "PUT",
            "url": [{"text": "https://example.test/upload/{filename|url}"}],
            "headers": [{"header": "Authorization", "text": "Bearer {api_key|trim}"}],
        },
        "response": {
            "results": [{"at": "url", "url": "{value}"}],
        },
    }
    base.update(overrides)
    return base


def test_default_servers_load_cleanly():
    result = load_servers(include_urls=False)
    assert not result.errors
    assert "pixeldrain" in result.servers
    assert "fuckingfast" in result.servers
    assert "google_drive" in result.servers
    assert DEFAULT_SERVERS_DIR.is_dir()


def test_parse_universal_oauth_auth_spec():
    data = _minimal_server_dict(
        auth={
            "type": "oauth2",
            "provider": "dropbox",
            "scopes": ["files.content.write"],
            "auth_uri": "https://www.dropbox.com/oauth2/authorize",
            "token_uri": "https://api.dropboxapi.com/oauth2/token",
            "revoke_uri": "https://api.dropboxapi.com/2/auth/token/revoke",
            "client_id": "db-client-123",
            "client_secret": "db-secret-456",
            "auth_params": {"token_access_type": "offline"},
            "redirect_port": 8765,
            "redirect_path": "/cb",
            "pkce": True,
            "token_key": "oauth_token",
            "credentials_key": "db_creds",
        }
    )
    srv = parse_server(data)
    assert srv.auth is not None
    assert srv.auth.provider == "dropbox"
    assert srv.auth.scopes == ("files.content.write",)
    assert srv.auth.redirect_port == 8765
    assert srv.auth.redirect_path == "/cb"
    assert srv.auth.credentials_key == "db_creds"


def test_parse_rejects_empty_oauth_provider():
    data = _minimal_server_dict(auth={"type": "oauth2", "provider": "   "})
    with pytest.raises(ServerDefError, match="provider"):
        parse_server(data)


def test_build_request_renders_filters_and_multipart():
    data = _minimal_server_dict(
        fields=[
            {"key": "note", "caption": "Note", "type": "text"},
        ],
        request={
            "verb": "POST",
            "body": "multipart",
            "file_field": "upload_file",
            "url": [{"text": "https://example.test/api/upload"}],
            "form": [
                {"field": "note", "text": "{note|trim}", "only_if": ["note"]},
            ],
        },
    )
    srv = parse_server(data)
    req = build_request(srv, "my report.pdf", {"note": "  hello  "}, mime="application/pdf", size=1024)
    assert req.verb == "POST"
    assert req.body == "multipart"
    assert req.file_field == "upload_file"
    assert req.file_mime == "application/pdf"
    assert req.form_fields == [("note", "hello")]


def test_prepare_upload_extracts_and_respects_abort():
    data = _minimal_server_dict(
        fields=[{"key": "folder_name", "caption": "Folder", "type": "text"}],
        prepare=[
            {
                "verb": "POST",
                "url": [{"text": "https://example.test/folders"}],
                "json_body": '{"name":"{folder_name}"}',
                "only_if": ["folder_name"],
                "extract": {"_folder_id": "data.id"},
            }
        ],
    )
    srv = parse_server(data)
    values = {"folder_name": "Releases"}

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"data": {"id": "fld_42"}}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        prepare_upload(srv, values)
        assert mock_open.call_count == 1
        assert values["_folder_id"] == "fld_42"

    # When abort_check returns True, prepare_upload exits early without issuing requests.
    values_aborted = {"folder_name": "Releases"}
    with patch("urllib.request.urlopen") as mock_open_aborted:
        prepare_upload(srv, values_aborted, abort_check=lambda: True)
        assert mock_open_aborted.call_count == 0
        assert "_folder_id" not in values_aborted


def test_parse_response_and_apply_captures():
    data = _minimal_server_dict(
        capture=[{"from": "session_token", "to": "_guest_token", "when_empty": ["_guest_token"]}],
        response={
            "results": [
                {
                    "at": "link",
                    "url": "{value}",
                    "capture": {"session_token": "meta.token"},
                }
            ]
        },
    )
    srv = parse_server(data)
    parsed = parse_response(srv, json.dumps({"link": "https://example.test/f/1", "meta": {"token": "tok_9"}}), 200)
    assert parsed.url == "https://example.test/f/1"
    assert parsed.captured == {"session_token": "tok_9"}

    session_vals: dict[str, str] = {}
    applied = apply_captures(srv, session_vals, parsed)
    assert applied == {"_guest_token": "tok_9"}
    assert session_vals["_guest_token"] == "tok_9"


def test_parse_response_raises_response_error_on_4xx():
    srv = parse_server(_minimal_server_dict())
    with pytest.raises(ResponseError) as exc_info:
        parse_response(srv, json.dumps({"message": "Bad token"}), 401)
    assert exc_info.value.kind == "upload"
    assert "Bad token" in str(exc_info.value)


def test_fetch_choices_returns_static_and_error_on_failure():
    srv = parse_server(
        _minimal_server_dict(
            fields=[
                {
                    "key": "region",
                    "caption": "Region",
                    "type": "select",
                    "choices": [{"value": "", "caption": "Auto"}],
                    "choices_from": {"url": "https://example.test/regions"},
                }
            ]
        )
    )
    field = srv.field("region")
    assert field is not None
    errors: list[str] = []
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        choices = fetch_choices(field, {}, errors.append, base_url=srv.base_url)
    assert choices == [("", "Auto")]
    assert len(errors) == 1