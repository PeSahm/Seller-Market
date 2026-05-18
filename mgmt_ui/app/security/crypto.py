"""Fernet encryption with keyset versioning (Phase 10).

Versioned envelope format
=========================

Phase 10 introduces a keyset version so we can rotate Fernet keys
without a stop-the-world re-encrypt of every existing ciphertext.

The on-disk format is a small JSON envelope::

    {"v": 1, "ct": "<urlsafe-b64 Fernet token>"}

stored as UTF-8 bytes (still fits a ``LargeBinary`` column).

``encrypt(plaintext)`` writes envelopes at the **current** version
(``CURRENT_KEYSET_VERSION``). ``decrypt(blob)`` parses the envelope,
looks up the key for ``v``, and decrypts. If the blob is NOT JSON
(legacy unversioned ciphertext from Phases 2-9), we fall back to
the v1 key — backwards-compat so an alembic rotation is not required
to enable this code path. New writes always get the envelope.

Rotation procedure (operator):

1. Generate a fresh 32-byte random url-safe-base64 key.
2. Add it to the keyset as a higher version number (e.g. v=2):
   set env var ``MGMT_FERNET_KEY_VERSIONS='{"1":"<old>","2":"<new>"}'``.
3. Bump ``MGMT_FERNET_CURRENT_VERSION=2``.
4. Restart the mgmt UI.
5. From now on, every NEW write lands as v=2. v=1 reads still work.
6. Lazily over time, every old row will be re-encrypted as part of
   normal edits; or run a one-shot ``re-encrypt-all`` script.
7. Once the audit log shows no more v=1 reads, drop v=1 from the
   keyset.

If ``MGMT_FERNET_KEY_VERSIONS`` is unset, we fall back to the
existing single split-key (``MGMT_FERNET_KEY_PART1`` + part2 file) as
v=1 — fully backwards-compat with Phase 1.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.settings import get_settings

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")

# Lazily-built {version: Fernet} map.
_keyset: Optional[dict[int, Fernet]] = None
_current_version: Optional[int] = None


def _pad_b64(data: str) -> str:
    return data + "=" * (-len(data) % 4)


def _decode_part(part_b64: str) -> bytes:
    raw = base64.urlsafe_b64decode(_pad_b64(part_b64).encode("ascii"))
    if len(raw) != 32:
        raise ValueError(
            f"Fernet key part must decode to 32 bytes, got {len(raw)}"
        )
    return raw


def _load_split_key_v1() -> bytes:
    """The Phase 1 split-key path: part1 from settings + part2 from file.

    Returns the combined url-safe-base64 Fernet key. Used as the v=1
    key when ``MGMT_FERNET_KEY_VERSIONS`` is unset.
    """
    settings = get_settings()
    part1_b64 = settings.fernet_key_part1.get_secret_value().strip()
    part1_raw = _decode_part(part1_b64)

    part2_path = settings.fernet_key_part2_path
    if not os.path.exists(part2_path):
        logger.warning(
            "Fernet key part2 not found at %s — using part1 alone "
            "(DEV MODE ONLY). This is INSECURE for production.",
            part2_path,
        )
        return base64.urlsafe_b64encode(part1_raw)

    with open(part2_path, "r", encoding="utf-8") as fh:
        part2_b64 = fh.read().strip()
    part2_raw = _decode_part(part2_b64)

    combined = bytes(a ^ b for a, b in zip(part1_raw, part2_raw))
    return base64.urlsafe_b64encode(combined)


def _load_keyset() -> tuple[dict[int, Fernet], int]:
    """Build the {version: Fernet} map and pick the current version.

    Two modes:

    * Explicit keyset (Phase 10): ``MGMT_FERNET_KEY_VERSIONS`` env var
      is a JSON map ``{"1": "<key1-b64>", "2": "<key2-b64>"}`` and
      ``MGMT_FERNET_CURRENT_VERSION`` picks one of those versions to
      use for new writes.

    * Legacy single key (Phase 1 - 9): env var is unset; we use
      ``_load_split_key_v1()`` as version 1.

    In either case, the returned dict ALWAYS contains v=1 if the
    legacy split-key is present, so old envelopes still decrypt.
    """
    versions_env = os.environ.get("MGMT_FERNET_KEY_VERSIONS")
    if versions_env:
        try:
            raw_map = json.loads(versions_env)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "MGMT_FERNET_KEY_VERSIONS must be valid JSON like "
                '{"1": "<key>", "2": "<key>"}'
            ) from exc
        if not isinstance(raw_map, dict) or not raw_map:
            raise ValueError(
                "MGMT_FERNET_KEY_VERSIONS must be a non-empty JSON object"
            )
        keyset: dict[int, Fernet] = {}
        for k, v in raw_map.items():
            try:
                version = int(k)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"MGMT_FERNET_KEY_VERSIONS keys must be integers, got {k!r}"
                ) from exc
            if version < 1:
                raise ValueError(f"keyset version must be >= 1, got {version}")
            if not isinstance(v, str):
                raise ValueError(
                    f"MGMT_FERNET_KEY_VERSIONS value for {version} must be a string"
                )
            # Each value is a full Fernet key (url-safe-base64 of 32 raw bytes).
            keyset[version] = Fernet(v.encode("ascii"))
        cur_env = os.environ.get("MGMT_FERNET_CURRENT_VERSION")
        if cur_env:
            try:
                current = int(cur_env)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"MGMT_FERNET_CURRENT_VERSION must be int, got {cur_env!r}"
                ) from exc
        else:
            current = max(keyset.keys())
        if current not in keyset:
            raise ValueError(
                f"MGMT_FERNET_CURRENT_VERSION={current} not in keyset "
                f"{sorted(keyset.keys())}"
            )
        return keyset, current

    # Legacy path: derive v=1 from the split key.
    v1_key = _load_split_key_v1()
    return {1: Fernet(v1_key)}, 1


def _get_keyset() -> tuple[dict[int, Fernet], int]:
    global _keyset, _current_version
    if _keyset is None or _current_version is None:
        _keyset, _current_version = _load_keyset()
    return _keyset, _current_version


def encrypt(plaintext: str) -> bytes:
    """Encrypt plaintext, wrapping in the versioned envelope.

    Output bytes are JSON ``{"v": N, "ct": "<token>"}`` UTF-8 encoded.
    Still safe to store in a ``LargeBinary`` column.
    """
    keyset, current = _get_keyset()
    f = keyset[current]
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    envelope = json.dumps({"v": current, "ct": token}, separators=(",", ":"))
    return envelope.encode("utf-8")


def decrypt(ciphertext: bytes) -> str:
    """Decrypt a versioned envelope OR a legacy unversioned Fernet token.

    Versioned: parse JSON, look up key by ``v``, decrypt ``ct``.
    Legacy: fall back to the v=1 key.

    Emits a structured audit log entry on every call.
    """
    keyset, _ = _get_keyset()

    # Try envelope first.
    try:
        envelope = json.loads(ciphertext.decode("utf-8"))
        if isinstance(envelope, dict) and "v" in envelope and "ct" in envelope:
            version = int(envelope["v"])
            token = envelope["ct"].encode("ascii")
            if version not in keyset:
                raise ValueError(
                    f"ciphertext keyset_version={version} not in current keyset "
                    f"{sorted(keyset.keys())}"
                )
            plaintext = keyset[version].decrypt(token).decode("utf-8")
            audit_logger.info(
                "secret_decrypt",
                extra={
                    "event": "secret_decrypt",
                    "ciphertext_len": len(ciphertext),
                    "keyset_version": version,
                },
            )
            return plaintext
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Not an envelope — fall through to legacy path.
        pass

    # Legacy path: ciphertext is a bare Fernet token from before
    # versioning landed. Use v=1.
    if 1 not in keyset:
        raise InvalidToken(
            "legacy unversioned ciphertext requires v=1 in keyset"
        )
    plaintext = keyset[1].decrypt(ciphertext).decode("utf-8")
    audit_logger.info(
        "secret_decrypt",
        extra={
            "event": "secret_decrypt",
            "ciphertext_len": len(ciphertext),
            "keyset_version": 1,
            "legacy_format": True,
        },
    )
    return plaintext


def current_keyset_version() -> int:
    """Return the version newly-encrypted ciphertexts will carry."""
    _, current = _get_keyset()
    return current


def _reset_for_tests() -> None:
    """Test helper. Clears the lazy cache so env-var changes take effect."""
    global _keyset, _current_version
    _keyset = None
    _current_version = None


# Backwards-compat shims for any caller using the Phase 1-9 API.
def get_fernet() -> Fernet:
    """Return the Fernet for the current keyset version.

    Deprecated — new code should call :func:`encrypt` / :func:`decrypt`
    directly so the envelope is preserved.
    """
    keyset, current = _get_keyset()
    return keyset[current]


def rotate_check() -> None:
    """No-op — kept for backwards-compat with the Phase 1 placeholder."""
    return None
