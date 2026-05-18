"""Unit tests for app.security.ws_token (Phase 10)."""

from __future__ import annotations

import time

import pytest
from jose import jwt

from app.security.ws_token import (
    WS_TOKEN_PURPOSE,
    WS_TOKEN_TTL_SECONDS,
    issue_ws_token,
    verify_ws_token,
)
from app.settings import get_settings


def test_round_trip_returns_subject():
    user_id = "11111111-2222-3333-4444-555555555555"
    token = issue_ws_token(user_id)
    assert verify_ws_token(token) == user_id


def test_verify_empty_string_returns_none():
    assert verify_ws_token("") is None


def test_verify_garbage_returns_none():
    assert verify_ws_token("not-a-jwt") is None


def test_token_carries_purpose_claim():
    """access_token reuse is blocked by the purpose claim mismatch."""
    settings = get_settings()
    token = issue_ws_token("some-user")
    payload = jwt.decode(
        token,
        settings.secret_key.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["purpose"] == WS_TOKEN_PURPOSE


def test_wrong_purpose_rejected():
    """A token signed with our secret but a different purpose is rejected."""
    settings = get_settings()
    now = int(time.time())
    fake = jwt.encode(
        {
            "sub": "some-user",
            "purpose": "wrong",
            "iat": now,
            "exp": now + 30,
        },
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    assert verify_ws_token(fake) is None


def test_missing_sub_rejected():
    settings = get_settings()
    now = int(time.time())
    no_sub = jwt.encode(
        {"purpose": WS_TOKEN_PURPOSE, "iat": now, "exp": now + 30},
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    assert verify_ws_token(no_sub) is None


def test_expired_token_rejected():
    """The 30 s TTL means an old token is invalid."""
    settings = get_settings()
    now = int(time.time())
    expired = jwt.encode(
        {
            "sub": "u",
            "purpose": WS_TOKEN_PURPOSE,
            "iat": now - 60,
            "exp": now - 1,
        },
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    assert verify_ws_token(expired) is None


def test_ttl_constant_is_short():
    """Pin the TTL — a long-lived token defeats the point of the design."""
    assert 10 <= WS_TOKEN_TTL_SECONDS <= 120
