"""Copy an existing Woopsa login out of Chrome into the uploader's profile.

Why this exists: a fresh OMS login emails an OTP that can only be retrieved by
phoning head office, so logging in again inside Playwright is expensive. If
you're already logged in in Chrome, this lifts that session across instead.

What it does:
  1. Reads Chrome's cookie DB (a copy of it — never the live file).
  2. Decrypts the values with the key from your macOS Keychain.
  3. Keeps ONLY cookies for the OMS host, and writes them as a Playwright
     storage_state file.

Chrome must be QUIT first: it holds an exclusive lock on the database.

Only cookies for OMS_URL's host are read. Nothing is written to Chrome, and
cookies for other sites are never touched or stored.

Usage:
    ./.venv/bin/python import_chrome_session.py
    ./.venv/bin/python oms_uploader.py --dry-run --limit 1 --headed
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .. import config

CHROME_DIR = Path.home() / "Library/Application Support/Google/Chrome"


def _chrome_running() -> bool:
    return subprocess.run(
        ["pgrep", "-x", "Google Chrome"], capture_output=True
    ).returncode == 0


def _decryption_key() -> bytes:
    """Chrome's cookie key, from the login keychain.

    macOS may show a keychain prompt the first time — that's the OS asking your
    permission for this script to read Chrome's key. Allow it once.
    """
    from hashlib import pbkdf2_hmac

    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage",
         "-a", "Chrome"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            "could not read Chrome's key from the Keychain.\n"
            "If a permission dialog appeared, click Allow and re-run."
        )
    password = result.stdout.strip().encode()
    # Chrome on macOS: PBKDF2-SHA1, salt "saltysalt", 1003 iterations, 16 bytes.
    return pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)


def _decrypt(encrypted: bytes, key: bytes) -> str:
    """Decrypt one v10 cookie value (AES-128-CBC, fixed IV of 16 spaces)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not encrypted:
        return ""
    if not encrypted.startswith(b"v10"):
        return encrypted.decode("utf-8", "ignore")  # already plaintext

    payload = encrypted[3:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = decryptor.update(payload) + decryptor.finalize()

    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16:
            plain = plain[:-pad]
    # Newer Chrome prefixes a 32-byte SHA256 of the domain; strip if present.
    text = plain.decode("utf-8", "ignore")
    if len(plain) > 32 and not text.isprintable():
        text = plain[32:].decode("utf-8", "ignore")
    return text


def main() -> None:
    if not config.OMS_URL:
        raise SystemExit("OMS_URL is not set in .env")

    host = urlparse(config.OMS_URL).hostname
    if not host:
        raise SystemExit(f"could not parse a host out of OMS_URL={config.OMS_URL!r}")

    if _chrome_running():
        raise SystemExit(
            "Chrome is running and locks its cookie database.\n"
            "Quit Chrome completely (Cmd-Q, not just closing the window), then re-run."
        )

    cookie_db = CHROME_DIR / "Default/Cookies"
    if not cookie_db.exists():
        raise SystemExit(f"no Chrome cookie database at {cookie_db}")

    key = _decryption_key()

    # Work on a copy: never open Chrome's live database.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(cookie_db, copy)

        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT host_key, name, encrypted_value, path, expires_utc, "
            "       is_secure, is_httponly "
            "FROM cookies WHERE host_key LIKE ?",
            (f"%{host}%",),
        ).fetchall()
        con.close()

    cookies = []
    for host_key, name, enc, path, expires, secure, http_only in rows:
        value = _decrypt(enc, key)
        if not value:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": host_key,
            "path": path or "/",
            # Chrome stores microseconds since 1601; Playwright wants unix secs.
            "expires": (expires / 1_000_000 - 11_644_473_600) if expires else -1,
            "httpOnly": bool(http_only),
            "secure": bool(secure),
            "sameSite": "Lax",
        })

    if not cookies:
        raise SystemExit(
            f"no cookies found for {host!r}.\n"
            "Log in to the OMS in Chrome first, then quit Chrome and re-run."
        )

    config.STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STORAGE_STATE_PATH.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2)
    )

    print(f"imported {len(cookies)} cookie(s) for {host}")
    for c in cookies:
        print(f"  {c['name']}")
    print(f"\nwritten to {config.STORAGE_STATE_PATH}")
    print("\nNext:  ./.venv/bin/python oms_uploader.py --dry-run --limit 1 --headed")


if __name__ == "__main__":
    main()
