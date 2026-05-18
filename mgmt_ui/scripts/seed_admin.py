"""Seed the first admin user.

Usage:
    # Recommended: read the password from stdin so it never appears in `ps`.
    printf '%s' "$pw" | python -m scripts.seed_admin <username> --password-stdin

    # Or: pass via env var.
    SEED_ADMIN_PASSWORD="$pw" python -m scripts.seed_admin <username>

    # Legacy positional (kept for back-compat; password is visible in `ps`).
    python -m scripts.seed_admin <username> <password>

Idempotent: if a user with that username already exists, prints a notice and exits.
Reads DATABASE_URL from environment (load .env first via python-dotenv if running locally).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Make repo root importable when run as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # type: ignore

load_dotenv(_ROOT / ".env")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.db import AsyncSessionLocal  # noqa: E402
from app.models.users import User  # noqa: E402
from app.security.auth import hash_password  # noqa: E402


async def seed(username: str, password: str) -> None:
    async with AsyncSessionLocal() as session:  # type: AsyncSession
        existing = await session.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none() is not None:
            print(f"[seed_admin] user '{username}' already exists; skipping")
            return

        user = User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
        )
        session.add(user)
        await session.commit()
        print(f"[seed_admin] created admin '{username}'")


def _read_password(args: argparse.Namespace) -> str | None:
    """Resolve the password from the safest available source.

    Priority: --password-stdin > $SEED_ADMIN_PASSWORD > positional <password>.
    The positional form is kept for back-compat but leaks the password into
    the host's process table — callers should prefer one of the other two.
    """
    if args.password_stdin:
        return sys.stdin.read().rstrip("\n")
    env_pw = os.environ.get("SEED_ADMIN_PASSWORD")
    if env_pw:
        return env_pw
    if args.password is not None:
        return args.password
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_admin",
        description="Seed the first admin user.",
    )
    parser.add_argument("username")
    parser.add_argument(
        "password",
        nargs="?",
        help="Plain-text password (legacy; visible in `ps`). Prefer --password-stdin.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin (no trailing newline).",
    )
    args = parser.parse_args()

    password = _read_password(args)
    if not password:
        print(
            "error: no password supplied. Pass --password-stdin, set "
            "SEED_ADMIN_PASSWORD, or provide a positional <password>.",
            file=sys.stderr,
        )
        return 2

    asyncio.run(seed(args.username, password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
