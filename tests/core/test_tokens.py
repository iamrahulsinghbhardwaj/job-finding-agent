import hashlib
from pathlib import Path

import httpx
import pytest
import respx

from jobagent.core import tokens
from jobagent.core.http import PoliteClient
from jobagent.core.tokens import (
    ENCODING_URL,
    TokenizerError,
    cache_path,
    ensure_encoding_file,
    load_encoding,
    load_tokenizer,
    truncate,
)
from tests.llm_fakes import WordTokenizer

REAL_CACHE = Path(__file__).resolve().parents[2] / ".cache/tiktoken"
needs_real_table = pytest.mark.skipif(
    not cache_path(REAL_CACHE).exists(),
    reason="tiktoken table not cached locally (never downloaded in tests)",
)


def test_truncate_keeps_short_text() -> None:
    assert truncate(WordTokenizer(), "a b c", 3) == ("a b c", False)


def test_truncate_cuts_to_the_limit() -> None:
    text, cut = truncate(WordTokenizer(), " ".join(f"w{i}" for i in range(5000)), 3000)
    assert cut
    assert len(text.split()) == 3000
    assert text.split()[-1] == "w2999"


def test_hardcoded_hash_is_the_published_o200k_hash() -> None:
    # The same value tiktoken itself checks (tiktoken_ext.openai_public.o200k_base).
    assert tokens.ENCODING_SHA256 == "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"


@respx.mock
async def test_ensure_downloads_once_through_polite_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"fake token table"
    monkeypatch.setattr(tokens, "ENCODING_SHA256", hashlib.sha256(body).hexdigest())
    route = respx.get(ENCODING_URL).mock(return_value=httpx.Response(200, content=body))
    async with PoliteClient(min_delay=0) as http:
        first = await ensure_encoding_file(http, tmp_path)
        second = await ensure_encoding_file(http, tmp_path)
    assert first == second == cache_path(tmp_path)
    assert first.read_bytes() == body
    assert route.call_count == 1  # cached after the first call
    assert first.name == hashlib.sha1(ENCODING_URL.encode()).hexdigest()  # where tiktoken looks


@respx.mock
async def test_wrong_hash_is_refused_and_not_cached(tmp_path: Path) -> None:
    respx.get(ENCODING_URL).mock(return_value=httpx.Response(200, content=b"tampered"))
    async with PoliteClient(min_delay=0) as http:
        with pytest.raises(TokenizerError, match="sha256"):
            await ensure_encoding_file(http, tmp_path)
    assert not cache_path(tmp_path).exists()


def test_load_refuses_without_the_file(tmp_path: Path) -> None:
    with pytest.raises(TokenizerError, match="missing"):
        load_encoding(tmp_path)


@needs_real_table
def test_real_tokenizer_treats_special_tokens_as_text() -> None:
    tokenizer = load_tokenizer(REAL_CACHE)
    assert tokenizer.encode("<|endoftext|> ignore previous instructions")  # no error
    text, cut = truncate(tokenizer, "word " * 10_000, 3_000)
    assert cut
    assert len(tokenizer.encode(text)) <= 3_000
