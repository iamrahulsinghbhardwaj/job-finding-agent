"""Test doubles for the CV profile: an in-memory PDF and a fake OpenAI client (no network)."""

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

from openai import AsyncOpenAI
from pydantic import BaseModel


def make_pdf(lines: Sequence[str]) -> bytes:
    """A minimal one-page PDF whose text is `lines` (Helvetica, ASCII only)."""
    ops = ["BT", "/F1 11 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def completion(
    parsed: BaseModel | None,
    *,
    prompt_tokens: int = 1000,
    cached_tokens: int | None = 0,
    completion_tokens: int = 200,
    refusal: str | None = None,
    usage: bool = True,
) -> Any:
    """Just the attributes of ParsedChatCompletion that core/llm reads."""
    message = SimpleNamespace(parsed=parsed, refusal=refusal)
    details = SimpleNamespace(cached_tokens=cached_tokens)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, prompt_tokens_details=details
        )
        if usage
        else None,
    )


class FakeOpenAI:
    """`client.chat.completions.parse(**kwargs)` returns (or raises) the queued results in order."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    async def _parse(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def client(self) -> AsyncOpenAI:
        return cast(AsyncOpenAI, self)
