from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet

from app.settings import get_settings

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")

_fernet: Optional[Fernet] = None


def _pad_b64(data: str) -> str:
    """Pad a base64 string to a multiple of 4."""
    return data + "=" * (-len(data) % 4)


def _decode_part(part_b64: str) -> bytes:
    """Decode a 32-byte url-safe base64 Fernet key part."""
    raw = base64.urlsafe_b64decode(_pad_b64(part_b64).encode("ascii"))
    if len(raw) != 32:
        raise ValueError(
            f"Fernet key part must decode to 32 bytes, got {len(raw)}"
        )
    return raw


def _load_full_key() -> bytes:
    """Combine part1 (settings) and part2 (file) into a single Fernet key.

    Both parts must each be a valid Fernet key (32 random bytes, url-safe
    base64-encoded). Their raw bytes are XOR'd, then re-base64-encoded.

    Dev fallback: if the part2 file does not exist, emits a WARNING and
    uses part1 alone. NEVER do this in production — it weakens the
    split-knowledge guarantee.
    """
    settings = get_settings()
    part1_b64 = settings.fernet_key_part1.get_secret_value().strip()
    part1_raw = _decode_part(part1_b64)

    part2_path = settings.fernet_key_part2_path
    if not os.path.exists(part2_path):
        logger.warning(
            "Fernet key part2 not found at %s — using part1 alone (DEV MODE ONLY). "
            "This is INSECURE for production.",
            part2_path,
        )
        return base64.urlsafe_b64encode(part1_raw)

    with open(part2_path, "r", encoding="utf-8") as fh:
        part2_b64 = fh.read().strip()
    part2_raw = _decode_part(part2_b64)

    combined = bytes(a ^ b for a, b in zip(part1_raw, part2_raw))
    return base64.urlsafe_b64encode(combined)


def get_fernet() -> Fernet:
    """Return the lazily-initialized Fernet cipher."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_full_key())
    return _fernet


def encrypt(plaintext: str) -> bytes:
    """Encrypt plaintext into bytes suitable for a LargeBinary column."""
    return get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Decrypt ciphertext bytes back to a string.

    Emits a structured audit log entry on every call so an audit_log
    table writer can subscribe via a logging handler.
    """
    plaintext = get_fernet().decrypt(ciphertext).decode("utf-8")
    audit_logger.info(
        "secret_decrypt",
        extra={
            "event": "secret_decrypt",
            "ciphertext_len": len(ciphertext),
        },
    )
    return plaintext


def rotate_check() -> None:
    """Placeholder for future keyset_version / key-rotation support."""
    return None
