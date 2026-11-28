"""A fake Mailer (no network): records what it 'sends' and answers the two lookups like Gmail."""

from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage

from jobagent.digest.gmail import MailError


class FakeMailer:
    """
    - fail_sends=n: the first n sends raise MailError (Gmail rejected them; nothing was sent).
    - rewrite_message_id: Gmail "replaced" our Message-ID, so only the subject lookup can find it.
    - search_error: every lookup raises MailError.
    `sent` holds every message Gmail accepted; `outbox` lets a test pretend an earlier process sent one.
    """

    def __init__(
        self, *, fail_sends: int = 0, rewrite_message_id: bool = False, search_error: bool = False
    ) -> None:
        self.fail_sends = fail_sends
        self.rewrite_message_id = rewrite_message_id
        self.search_error = search_error
        self.sent: list[EmailMessage] = []
        self.send_calls = 0
        self.lookups: list[str] = []
        self.on_send: object = None  # optional callable run inside send (to inspect DB state)

    def send(self, raw: bytes) -> str:
        self.send_calls += 1
        if callable(self.on_send):
            self.on_send()
        if self.fail_sends:
            self.fail_sends -= 1
            raise MailError("Gmail send failed: HTTP 500")
        message = message_from_bytes(raw, policy=policy.default)
        assert isinstance(message, EmailMessage)
        self.sent.append(message)
        return f"gmail-{len(self.sent)}"

    def find_by_message_id(self, rfc_message_id: str) -> str | None:
        self.lookups.append("message_id")
        if self.search_error:
            raise MailError("Gmail search failed: HTTP 503")
        if self.rewrite_message_id:
            return None
        for i, message in enumerate(self.sent, 1):
            if message["Message-ID"] == rfc_message_id:
                return f"gmail-{i}"
        return None

    def find_by_subject_token(self, token: str, start: datetime, end: datetime) -> str | None:
        self.lookups.append("subject")
        if self.search_error:
            raise MailError("Gmail search failed: HTTP 503")
        now = datetime.now(UTC)
        for i, message in enumerate(self.sent, 1):
            if token in str(message["Subject"]) and start <= now <= end:
                return f"gmail-{i}"
        return None

    def jobs_emailed(self) -> list[str]:
        """Every Apply link in every accepted email (a job sent twice shows up twice)."""
        links: list[str] = []
        for message in self.sent:
            body = message.get_body(preferencelist=("plain",))
            assert body is not None
            links += [
                line.strip()
                for line in body.get_content().splitlines()
                if line.strip().startswith("https://")
            ]
            links += [
                part.rsplit("| ", 1)[1].strip()
                for part in body.get_content().splitlines()
                if part.startswith("  ") and "| https://" in part
            ]
        return links
