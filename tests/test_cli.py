from __future__ import annotations

from unittest.mock import patch

import pytest

from uploader import cli as cli_mod


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAINPYPE_HOME", str(tmp_path))
    yield


def test_cli_list_servers(capsys):
    rc = cli_mod.main(["--list-servers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pixeldrain" in out
    assert "fuckingfast" in out
    assert "google_drive" in out


def test_cli_auth_invokes_authorize_oauth(capsys):
    with patch("uploader.cli.authorize_oauth", return_value="tok") as mock_auth:
        rc = cli_mod.main(["-S", "google_drive", "--auth"])
    assert rc == 0
    mock_auth.assert_called_once()
    assert "Authorized" in capsys.readouterr().out


def test_cli_uploads_file_with_max_uploads(tmp_path, capsys):
    sample = tmp_path / "archive.zip"
    sample.write_bytes(b"zip-bytes")

    from uploader.servers import ParsedResponse
    from uploader.uploader import Job, Uploader

    def fake_transfer(self, _curl_bin, job: Job, _server, _request):
        job.sent = job.size
        return ParsedResponse(f"https://pixeldrain.com/u/{job.name}", job.name, {})

    with (
        patch("uploader.uploader.find_curl", return_value="/usr/bin/curl"),
        patch.object(Uploader, "_transfer", fake_transfer),
    ):
        rc = cli_mod.main(["-S", "pixeldrain", "-K", "test-key", "-m", "2", "-F", str(sample)])

    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "https://pixeldrain.com/u/archive.zip"