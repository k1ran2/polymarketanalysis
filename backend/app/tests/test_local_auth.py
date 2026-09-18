"""
Unit tests for app.local_auth -- the shared secret gating /api/trading/*
routes (once they exist) from any other webpage open in the same browser.
See that module's docstring for why CORS alone isn't sufficient.
"""
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from fastapi import HTTPException

from app import local_auth


def test_get_or_create_local_token_persists_across_calls(monkeypatch, tmp_path):
    token_path = tmp_path / ".local_token"
    monkeypatch.setattr(local_auth, "_TOKEN_PATH", token_path)

    first = local_auth.get_or_create_local_token()
    second = local_auth.get_or_create_local_token()
    assert first == second
    assert token_path.exists()


def test_get_or_create_local_token_is_owner_only_permissions(monkeypatch, tmp_path):
    token_path = tmp_path / ".local_token"
    monkeypatch.setattr(local_auth, "_TOKEN_PATH", token_path)

    local_auth.get_or_create_local_token()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_get_or_create_local_token_is_long_and_random(monkeypatch, tmp_path):
    monkeypatch.setattr(local_auth, "_TOKEN_PATH", tmp_path / ".local_token")
    token = local_auth.get_or_create_local_token()
    assert len(token) >= 32

    monkeypatch.setattr(local_auth, "_TOKEN_PATH", tmp_path / "other_token")
    other = local_auth.get_or_create_local_token()
    assert other != token


def test_require_local_token_accepts_the_correct_token(monkeypatch, tmp_path):
    monkeypatch.setattr(local_auth, "_TOKEN_PATH", tmp_path / ".local_token")
    token = local_auth.get_or_create_local_token()
    local_auth.require_local_token(x_local_token=token)  # must not raise


def test_require_local_token_rejects_a_wrong_token(monkeypatch, tmp_path):
    monkeypatch.setattr(local_auth, "_TOKEN_PATH", tmp_path / ".local_token")
    local_auth.get_or_create_local_token()
    with pytest.raises(HTTPException) as exc_info:
        local_auth.require_local_token(x_local_token="wrong-token")
    assert exc_info.value.status_code == 401


def test_require_local_token_rejects_a_missing_token(monkeypatch, tmp_path):
    monkeypatch.setattr(local_auth, "_TOKEN_PATH", tmp_path / ".local_token")
    local_auth.get_or_create_local_token()
    with pytest.raises(HTTPException) as exc_info:
        local_auth.require_local_token(x_local_token=None)
    assert exc_info.value.status_code == 401


def test_require_local_token_never_leaks_the_expected_value_in_the_error(monkeypatch, tmp_path):
    monkeypatch.setattr(local_auth, "_TOKEN_PATH", tmp_path / ".local_token")
    token = local_auth.get_or_create_local_token()
    with pytest.raises(HTTPException) as exc_info:
        local_auth.require_local_token(x_local_token="wrong-token")
    assert token not in str(exc_info.value.detail)
