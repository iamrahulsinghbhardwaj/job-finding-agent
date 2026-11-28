"""Gmail for the digest: build the email, send it, find it again (HLD §8).

The digest is sent FROM a dedicated bot Gmail account (whose refresh token is GMAIL_REFRESH_TOKEN)
TO DIGEST_TO, your personal address. The bot token has two scopes only: gmail.send (send the
digest) and gmail.readonly (find a digest again after an interrupted run, see digest/send.py).

HTTP here goes through Google's client library, not PoliteClient: PoliteClient is for crawling
public sites; your own accounts' APIs (OpenAI, Gmail, Azure blob) use their SDKs.

Nothing here logs the address, subject or content: errors carry a status code at most.
"""

import base64
import logging
import secrets
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any, Protocol

from jobagent.core.settings import Settings

SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_SETTINGS = ("gmail_client_id", "gmail_client_secret", "gmail_refresh_token", "digest_to")

logging.getLogger("googleapiclient").setLevel(logging.WARNING)  # its INFO lines include request URLs


class MailError(Exception):
    """Sending or searching failed. Message is safe to log (no address, subject or content)."""


class Mailer(Protocol):
    def send(self, raw: bytes) -> str:
        """Send one RFC 822 message; return Gmail's message id."""
        ...

    def find_by_message_id(self, rfc_message_id: str) -> str | None:
        """Gmail id of the sent message with this Message-ID header, if any."""
        ...

    def find_by_subject_token(self, token: str, start: datetime, end: datetime) -> str | None:
        """Gmail id of a message in Sent whose subject contains `token`, sent in [start, end]."""
        ...


def new_message_id(run_id: int) -> str:
    """Our Message-ID for one run's digest: the run id plus a random part (unique across databases)."""
    return f"<jobagent-r{run_id}-{secrets.token_hex(6)}@jobagent.local>"


def subject_token(run_id: int) -> str:
    """Added to the subject, e.g. "[r57]": the fallback lookup if Gmail rewrote our Message-ID."""
    return f"[r{run_id}]"


def build_message(*, to: str, subject: str, html: str, text: str, message_id: str) -> EmailMessage:
    """multipart/alternative: plain text first, HTML second (clients show the last one they support).
    From is left to Gmail: it is the authenticated bot account."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = formatdate(usegmt=True)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


class GmailMailer:
    """Mailer over the Gmail API (users.messages send / list / get) for the authenticated account."""

    def __init__(self, service: Any) -> None:
        self._messages = service.users().messages()

    @classmethod
    def from_settings(cls, settings: Settings) -> "GmailMailer":
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials(
            None,
            refresh_token=settings.require("gmail_refresh_token"),
            token_uri=TOKEN_URI,
            client_id=settings.require("gmail_client_id"),
            client_secret=settings.require("gmail_client_secret"),
            scopes=list(SCOPES),
        )
        # static_discovery: the Gmail API description ships with the library (no extra request)
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False, static_discovery=True)
        return cls(service)

    def send(self, raw: bytes) -> str:
        body = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        result = _execute(self._messages.send(userId="me", body=body), "send")
        return str(result["id"])

    def find_by_message_id(self, rfc_message_id: str) -> str | None:
        query = f"rfc822msgid:{rfc_message_id.strip('<>')}"
        found = _execute(self._messages.list(userId="me", q=query, maxResults=5), "search")
        messages = found.get("messages") or []
        return str(messages[0]["id"]) if messages else None

    def find_by_subject_token(self, token: str, start: datetime, end: datetime) -> str | None:
        query = f'in:sent subject:"{token}" after:{int(start.timestamp())} before:{int(end.timestamp())}'
        found = _execute(self._messages.list(userId="me", q=query, maxResults=20), "search")
        for item in found.get("messages") or []:
            request = self._messages.get(
                userId="me", id=item["id"], format="metadata", metadataHeaders=["Subject"]
            )
            meta = _execute(request, "read")
            headers = meta.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h.get("name", "").lower() == "subject"), "")
            if token in subject:  # Gmail search is word-based: check the exact token
                return str(item["id"])
        return None


def missing_gmail_settings(settings: Settings) -> list[str]:
    return [name.upper() for name in GMAIL_SETTINGS if getattr(settings, name) is None]


def _execute(request: Any, action: str) -> dict[str, Any]:
    try:
        return request.execute(num_retries=2)  # Google's own backoff for 429 / 5xx
    except Exception as exc:  # HttpError, RefreshError (bad or expired token), network errors
        if type(exc).__name__ == "RefreshError":
            raise MailError(
                f"Gmail {action} failed: the refresh token was rejected (expired or revoked); "
                "run scripts/gmail_auth.py again"
            ) from None
        status = getattr(getattr(exc, "resp", None), "status", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        raise MailError(f"Gmail {action} failed: {detail}") from None
