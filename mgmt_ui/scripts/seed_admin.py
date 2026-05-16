"""Seed the first admin user.

Usage:
    python -m scripts.seed_admin <username> <password>

Idempotent: if a user with that username already exists, prints a notice and exits.
Reads DATABASE_URL from environment (load .env first via python-dotenv if running locally).
"""
from __future__ import annotations

import asyncio
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


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m scripts.seed_admin <username> <password>", file=sys.stderr)
        return 2
    asyncio.run(seed(sys.argv[1], sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
