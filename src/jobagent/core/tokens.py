"""Token counting and truncation (tiktoken, o200k_base: the GPT-5 family's encoding).

tiktoken downloads its token table on first use, outside our HTTP rules. So we fetch it ourselves
through PoliteClient, check it against a hard-coded sha256, and put it where tiktoken's cache
looks (TIKTOKEN_CACHE_DIR / sha1(url)). `load_encoding()` refuses to run without that file, so
tiktoken never goes to the network on its own.
"""

import hashlib
import os
from pathlib import Path
from typing import Protocol

import tiktoken

from jobagent.core.http import PoliteClient

ENCODING = "o200k_base"
ENCODING_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
ENCODING_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
CACHE_DIR = Path(".cache/tiktoken")  # gitignored


class Tokenizer(Protocol):
    def encode(self, text: str) -> list[int]: ...
    def decode(self, tokens: list[int]) -> str: ...


class TokenizerError(Exception):
    """The token table is missing or corrupt. Message is safe to log."""


def cache_path(cache_dir: Path = CACHE_DIR) -> Path:
    """Where tiktoken looks for the table: its cache key is sha1 of the URL."""
    return cache_dir / hashlib.sha1(ENCODING_URL.encode()).hexdigest()


async def ensure_encoding_file(http: PoliteClient, cache_dir: Path = CACHE_DIR) -> Path:
    """Download the token table once (through PoliteClient) and verify it. Returns its path."""
    path = cache_path(cache_dir)
    if _verified(path):
        return path
    data = await http.get_bytes(ENCODING_URL)
    if _sha256(data) != ENCODING_SHA256:
        raise TokenizerError(f"{ENCODING} token table failed its sha256 check")
    _write(path, data)
    return path


class TiktokenTokenizer:
    """tiktoken without special tokens: "<|endoftext|>" in a posting is plain text, never an error."""

    def __init__(self, encoding: tiktoken.Encoding) -> None:
        self._encoding = encoding

    def encode(self, text: str) -> list[int]:
        return self._encoding.encode_ordinary(text)

    def decode(self, tokens: list[int]) -> str:
        return self._encoding.decode(tokens)


def load_tokenizer(cache_dir: Path = CACHE_DIR) -> TiktokenTokenizer:
    return TiktokenTokenizer(load_encoding(cache_dir))


def load_encoding(cache_dir: Path = CACHE_DIR) -> tiktoken.Encoding:
    """The o200k_base encoding from the verified local file; never downloads."""
    path = cache_path(cache_dir)
    if not path.exists():
        raise TokenizerError(f"{ENCODING} token table missing: call ensure_encoding_file() first")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir.resolve())
    return tiktoken.get_encoding(ENCODING)  # tiktoken checks the same sha256 on load


def truncate(tokenizer: Tokenizer, text: str, max_tokens: int) -> tuple[str, bool]:
    """`text` cut to at most `max_tokens` tokens, and whether it was cut."""
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text, False
    return tokenizer.decode(tokens[:max_tokens]), True


def count(tokenizer: Tokenizer, text: str) -> int:
    return len(tokenizer.encode(text))


def _verified(path: Path) -> bool:
    return path.exists() and _sha256(path.read_bytes()) == ENCODING_SHA256


def _write(path: Path, data: bytes) -> None:
    """Atomic: a crash mid-write never leaves a half file where tiktoken looks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_bytes(data)
    partial.replace(path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
