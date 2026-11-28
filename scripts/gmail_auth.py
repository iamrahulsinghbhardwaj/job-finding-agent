"""One-time Gmail authorisation for the BOT account that sends the digest.

    uv run python scripts/gmail_auth.py --write-env   # recommended: token into .env, nothing printed
    uv run python scripts/gmail_auth.py               # prints the token: copy it into .env yourself

Reads GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET from .env (a Google Cloud OAuth client of type
"Desktop app"; see README "Gmail setup"). Opens your browser: SIGN IN AS THE BOT ACCOUNT, not your
personal one. Scopes: gmail.send and gmail.readonly only.

Run it yourself, in your own terminal: the refresh token is a secret.
"""

import argparse
import re
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from jobagent.core.settings import Settings
from jobagent.digest.gmail import SCOPES, TOKEN_URI

ENV_FILE = Path(".env")
KEY = "GMAIL_REFRESH_TOKEN"


def client_config(settings: Settings) -> dict[str, dict[str, object]]:
    return {
        "installed": {
            "client_id": settings.require("gmail_client_id"),
            "client_secret": settings.require("gmail_client_secret"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def set_env_value(text: str, key: str, value: str) -> str:
    """`text` (a .env file) with `key=value` replaced, or appended when the key is absent."""
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text + ("" if not text or text.endswith("\n") else "\n") + line + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Authorise the digest bot's Gmail account (one time).")
    parser.add_argument(
        "--write-env", action="store_true", help=f"write {KEY} into .env instead of printing it"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    missing = [n for n in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET") if getattr(settings, n.lower()) is None]
    if missing:
        print(f"Missing in .env: {', '.join(missing)} (see README 'Gmail setup')", file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_config(client_config(settings), scopes=list(SCOPES))
    print("Opening your browser. Sign in as the BOT account (the digest is sent from it).", file=sys.stderr)
    # offline + consent: Google returns a refresh token every time, even after an earlier grant
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token = credentials.refresh_token
    if not token:
        print(
            "Google returned no refresh token: remove the app's access in the bot account, retry.",
            file=sys.stderr,
        )
        return 1
    granted = set(credentials.scopes or [])
    if granted != set(SCOPES):
        print(f"Unexpected scopes granted: {sorted(granted)}", file=sys.stderr)
        return 1

    if args.write_env:
        current = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        ENV_FILE.write_text(set_env_value(current, KEY, token), encoding="utf-8")
        print(f"{KEY} written to .env (not printed).", file=sys.stderr)
    else:
        print(f"{KEY}={token}")
        print("Copy that line into .env. Do not paste it anywhere else.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
