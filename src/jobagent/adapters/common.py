"""Helpers shared by adapters."""

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from selectolax.lexbor import LexborHTMLParser

# Block-level boundaries become line breaks; inline tags (<b>, <a>, <span>) stay within a line.
_BLOCK_BREAK = re.compile(
    r"<br\s*/?>|</(?:p|div|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote)\s*>",
    re.IGNORECASE,
)


def schema_hash(item: Mapping[str, Any]) -> str:
    """Short hash of the set of top-level keys: changes when a source changes its response shape."""
    keys = ",".join(sorted(item))
    return hashlib.sha256(keys.encode()).hexdigest()[:16]


def html_to_text(html: str) -> str:
    """Visible text of an HTML fragment, one block per line, no tags, scripts or styles."""
    tree = LexborHTMLParser(_BLOCK_BREAK.sub(lambda m: m.group(0) + "\n", html))
    for node in tree.css("script, style"):
        node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    lines = (line.strip() for line in root.text(separator="").splitlines())
    return "\n".join(line for line in lines if line)
