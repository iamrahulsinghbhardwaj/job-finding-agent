"""The Gmail layer with a fake Google service (no network): MIME, encoding, lookups, errors."""

import base64
import logging
from datetime import UTC, datetime
from email import message_from_bytes, policy
from typing import Any

import pytest

from jobagent.core.settings import Settings
from jobagent.digest.gmail import (
    SCOPES,
    GmailMailer,
    MailError,
    build_message,
    missing_gmail_settings,
    new_message_id,
    subject_token,
)

ADDRESS = "private.person@example.com"


class Request:
    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result, self.error = result, error

    def execute(self, num_retries: int = 0) -> Any:
        if self.error:
            raise self.error
        return self.result


class FakeMessages:
    """Records calls to users().messages().send / list / get."""

    def __init__(self, *, lists: list[Any] | None = None, gets: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lists = lists or [{"messages": [{"id": "m1"}]}]
        self.gets = gets or {}
        self.error: BaseException | None = None

    def send(self, **kwargs: Any) -> Request:
        self.calls.append(("send", kwargs))
        return Request({"id": "sent-1"}, self.error)

    def list(self, **kwargs: Any) -> Request:
        self.calls.append(("list", kwargs))
        return Request(self.lists.pop(0) if self.lists else {}, self.error)

    def get(self, **kwargs: Any) -> Request:
        self.calls.append(("get", kwargs))
        return Request(self.gets.get(kwargs["id"], {}), self.error)


class FakeService:
    def __init__(self, messages: FakeMessages) -> None:
        self._messages = messages

    def users(self) -> "FakeService":
        return self

    def messages(self) -> FakeMessages:
        return self._messages


def mailer(messages: FakeMessages) -> GmailMailer:
    return GmailMailer(FakeService(messages))


def test_scopes_are_send_and_readonly_only() -> None:
    assert SCOPES == (
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    )


def test_message_is_multipart_text_then_html() -> None:
    message = build_message(
        to=ADDRESS, subject="9 new: 6 India, 3 Europe [r57]", html="<p>hi</p>", text="hi", message_id="<a@b>"
    )
    assert message["To"] == ADDRESS
    assert message["Subject"] == "9 new: 6 India, 3 Europe [r57]"
    assert message["Message-ID"] == "<a@b>"
    assert message["From"] is None  # Gmail sets it: the authenticated bot account
    assert message.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in message.iter_parts()] == ["text/plain", "text/html"]


def test_message_ids_are_per_run_and_unique() -> None:
    a, b = new_message_id(57), new_message_id(57)
    assert a.startswith("<jobagent-r57-")
    assert a.endswith("@jobagent.local>")
    assert a != b
    assert subject_token(57) == "[r57]"


def test_send_base64url_encodes_the_raw_message() -> None:
    messages = FakeMessages()
    raw = build_message(to=ADDRESS, subject="s", html="<b>é</b>", text="é", message_id="<x@y>").as_bytes()
    assert mailer(messages).send(raw) == "sent-1"
    [(name, kwargs)] = messages.calls
    assert (name, kwargs["userId"]) == ("send", "me")
    decoded = message_from_bytes(base64.urlsafe_b64decode(kwargs["body"]["raw"]), policy=policy.default)
    assert decoded["Message-ID"] == "<x@y>"


def test_find_by_message_id_searches_rfc822msgid() -> None:
    messages = FakeMessages()
    assert mailer(messages).find_by_message_id("<jobagent-r57-ab@jobagent.local>") == "m1"
    assert messages.calls[0][1]["q"] == "rfc822msgid:jobagent-r57-ab@jobagent.local"


def test_find_by_message_id_none_when_absent() -> None:
    assert mailer(FakeMessages(lists=[{}])).find_by_message_id("<x@y>") is None


def test_subject_lookup_checks_the_exact_token_in_a_window() -> None:
    messages = FakeMessages(
        lists=[{"messages": [{"id": "a"}, {"id": "b"}]}],
        gets={
            "a": {"payload": {"headers": [{"name": "Subject", "value": "12 new: 12 India [r570]"}]}},
            "b": {"payload": {"headers": [{"name": "Subject", "value": "9 new: 6 India, 3 Europe [r57]"}]}},
        },
    )
    start, end = datetime(2026, 11, 27, 8, tzinfo=UTC), datetime(2026, 11, 27, 10, tzinfo=UTC)
    assert mailer(messages).find_by_subject_token("[r57]", start, end) == "b"
    query = messages.calls[0][1]["q"]
    assert query == f'in:sent subject:"[r57]" after:{int(start.timestamp())} before:{int(end.timestamp())}'


class HttpLikeError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"<HttpError {status} details: body mentions {ADDRESS}>")
        self.resp = type("Resp", (), {"status": status})()


class RefreshError(Exception):
    """Same name as google.auth.exceptions.RefreshError."""


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HttpLikeError(403), "Gmail send failed: HTTP 403"),
        (ConnectionError("dns"), "Gmail send failed: ConnectionError"),
        (RefreshError("invalid_grant"), "refresh token was rejected"),
    ],
)
def test_errors_become_mail_errors_without_details(error: BaseException, expected: str) -> None:
    messages = FakeMessages()
    messages.error = error
    with pytest.raises(MailError) as info:
        mailer(messages).send(b"raw")
    assert expected in str(info.value)
    assert ADDRESS not in str(info.value)
    assert info.value.__cause__ is None


def test_missing_settings_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_CLIENT_ID", "id")
    monkeypatch.setenv("DIGEST_TO", ADDRESS)
    assert missing_gmail_settings(Settings()) == ["GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"]


def test_google_client_logs_are_quiet() -> None:
    assert logging.getLogger("googleapiclient").level == logging.WARNING
