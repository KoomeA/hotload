"""Server-side admin commands.  python -m hotload.admin grant you@example.com | revoke you@example.com

Admin is a database flag changed only here, never derived from an email address: registration does not verify
email yet, so "admin by email" would let anyone sign up as the admin first.
"""

import argparse
import asyncio
import sys

from .config import get_settings
from .db import Database


async def run(action: str, email: str) -> int:
    db = Database(get_settings())
    await db.migrate()
    try:
        ok = await db.set_admin(email, action == "grant")
    finally:
        await db.dispose()
    print(f"{'granted' if action == 'grant' else 'revoked'} admin for {email}" if ok else f"no account for {email}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hotload admin commands")
    ap.add_argument("action", choices=["grant", "revoke", "cookie-key"])
    ap.add_argument("email", nargs="?")
    args = ap.parse_args(argv)
    if args.action == "cookie-key":  # prints a fresh key for HOTLOAD_COOKIE_KEY; store it in your secret manager
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0
    if not args.email:
        ap.error("an email is required for grant/revoke")
    return asyncio.run(run(args.action, args.email))


if __name__ == "__main__":
    sys.exit(main())
