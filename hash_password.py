"""
Generate a salted scrypt hash for the dashboard password.
=============================================================

Run this locally. It prompts for a password (not echoed to the screen, not
passed as a command-line argument, so it never lands in shell history) and
prints a hash string. Paste ONLY that printed hash into:

  - .streamlit/secrets.toml  (local dev)   as: password_hash = "..."
  - Render's Environment tab (deployed)    as: DASHBOARD_PASSWORD_HASH

The plaintext password itself is never written to disk by this script and
never needs to appear in any file that gets committed or pasted anywhere.
Uses hashlib.scrypt (stdlib, no extra dependency) -- a memory-hard KDF
appropriate for password storage, with a random salt per password.

Usage:
    python hash_password.py
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import os

N, R, P, DKLEN = 2 ** 14, 8, 1, 32


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def main():
    pw = getpass.getpass("New dashboard password: ")
    if not pw:
        raise SystemExit("Empty password -- nothing generated.")
    confirm = getpass.getpass("Confirm: ")
    if pw != confirm:
        raise SystemExit("Passwords didn't match -- nothing generated.")

    print()
    print("Paste this line into .streamlit/secrets.toml:")
    print(f'  password_hash = "{hash_password(pw)}"')
    print()
    print("...or, for the deployed site, set an env var named DASHBOARD_PASSWORD_HASH")
    print("to just the value between the quotes above (no `password_hash = ` prefix).")


if __name__ == "__main__":
    main()
