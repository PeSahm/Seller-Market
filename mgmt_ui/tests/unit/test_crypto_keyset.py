"""Unit tests for app.security.crypto with keyset versioning (Phase 10)."""

from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.security import crypto


def _fresh_key() -> str:
    """Generate a Fernet key for tests (44 url-safe base64 chars)."""
    return Fernet.generate_key().decode("ascii")


@pytest.fixture(autouse=True)
def _reset_keyset():
    """Clear the lazy cache between tests so monkeypatched env vars stick."""
    crypto._reset_for_tests()
    yield
    crypto._reset_for_tests()


def test_envelope_round_trip_default_keyset():
    """Encrypt / decrypt round-trip uses the legacy split-key as v=1."""
    plaintext = "hunter2"
    ct = crypto.encrypt(plaintext)
    assert plaintext == crypto.decrypt(ct)


def test_envelope_is_json_with_v_and_ct():
    """The on-disk format is ``{"v": N, "ct": "<token>"}`` UTF-8."""
    ct = crypto.encrypt("anything")
    envelope = json.loads(ct.decode("utf-8"))
    assert envelope.keys() == {"v", "ct"}
    assert envelope["v"] == 1
    assert isinstance(envelope["ct"], str)


def test_explicit_keyset_via_env(monkeypatch):
    """MGMT_FERNET_KEY_VERSIONS overrides the split-key path."""
    k2 = _fresh_key()
    k3 = _fresh_key()
    monkeypatch.setenv(
        "MGMT_FERNET_KEY_VERSIONS", json.dumps({"2": k2, "3": k3})
    )
    monkeypatch.setenv("MGMT_FERNET_CURRENT_VERSION", "3")
    crypto._reset_for_tests()
    ct = crypto.encrypt("secret")
    envelope = json.loads(ct.decode("utf-8"))
    assert envelope["v"] == 3
    assert crypto.decrypt(ct) == "secret"


def test_keyset_decrypts_older_versions(monkeypatch):
    """An envelope encrypted at v=2 still decrypts after v=3 is current."""
    k2 = _fresh_key()
    k3 = _fresh_key()
    monkeypatch.setenv(
        "MGMT_FERNET_KEY_VERSIONS", json.dumps({"2": k2, "3": k3})
    )
    # Start with current=2, encrypt, switch to current=3, decrypt.
    monkeypatch.setenv("MGMT_FERNET_CURRENT_VERSION", "2")
    crypto._reset_for_tests()
    ct_v2 = crypto.encrypt("old-value")
    monkeypatch.setenv("MGMT_FERNET_CURRENT_VERSION", "3")
    crypto._reset_for_tests()
    # The fresh cache still loads both versions, so the v=2 envelope
    # decrypts cleanly.
    assert crypto.decrypt(ct_v2) == "old-value"


def test_decrypt_rejects_unknown_version(monkeypatch):
    """Envelope with a version not in the current keyset raises."""
    k1 = _fresh_key()
    monkeypatch.setenv("MGMT_FERNET_KEY_VERSIONS", json.dumps({"1": k1}))
    crypto._reset_for_tests()
    # Craft an envelope with v=99 — manually, since encrypt won't.
    fake = json.dumps({"v": 99, "ct": "x"}).encode("utf-8")
    with pytest.raises(ValueError, match="keyset_version=99"):
        crypto.decrypt(fake)


def test_legacy_unversioned_ciphertext_still_decrypts(monkeypatch):
    """Pre-Phase-10 ciphertexts are raw Fernet tokens. They must still work."""
    # Build a legacy token using the same split-key path the module uses.
    k = Fernet.generate_key()
    monkeypatch.setenv(
        "MGMT_FERNET_KEY_VERSIONS", json.dumps({"1": k.decode("ascii")})
    )
    crypto._reset_for_tests()
    legacy_token = Fernet(k).encrypt(b"legacy-value")
    assert crypto.decrypt(legacy_token) == "legacy-value"


def test_current_keyset_version_returns_current(monkeypatch):
    k1 = _fresh_key()
    k2 = _fresh_key()
    monkeypatch.setenv(
        "MGMT_FERNET_KEY_VERSIONS", json.dumps({"1": k1, "2": k2})
    )
    monkeypatch.setenv("MGMT_FERNET_CURRENT_VERSION", "2")
    crypto._reset_for_tests()
    assert crypto.current_keyset_version() == 2


def test_invalid_versions_json_raises(monkeypatch):
    monkeypatch.setenv("MGMT_FERNET_KEY_VERSIONS", "not-json")
    crypto._reset_for_tests()
    with pytest.raises(ValueError, match="valid JSON"):
        crypto.encrypt("x")


def test_unknown_current_version_raises(monkeypatch):
    k1 = _fresh_key()
    monkeypatch.setenv("MGMT_FERNET_KEY_VERSIONS", json.dumps({"1": k1}))
    monkeypatch.setenv("MGMT_FERNET_CURRENT_VERSION", "99")
    crypto._reset_for_tests()
    with pytest.raises(ValueError, match="not in keyset"):
        crypto.encrypt("x")


def test_current_version_defaults_to_max(monkeypatch):
    """If MGMT_FERNET_CURRENT_VERSION is unset, max() of the keyset wins."""
    k1 = _fresh_key()
    k5 = _fresh_key()
    monkeypatch.setenv(
        "MGMT_FERNET_KEY_VERSIONS", json.dumps({"1": k1, "5": k5})
    )
    monkeypatch.delenv("MGMT_FERNET_CURRENT_VERSION", raising=False)
    crypto._reset_for_tests()
    assert crypto.current_keyset_version() == 5
