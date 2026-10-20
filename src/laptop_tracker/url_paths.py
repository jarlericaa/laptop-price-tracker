"""URL path comparison without changing what a path identifies (2026-10-19, independent review).

Two spellings of one product path are equal only under RFC 3986 normalization:

- the hex digits of a percent-escape are compared in upper case (``%e2%84%a2`` = ``%E2%84%A2``);
- an escaped unreserved ASCII character is its literal form (``%41`` = ``A``, ``%2D`` = ``-``);
- a literal non-ASCII character is its UTF-8 escape (``™`` = ``%E2%84%A2``).

Nothing else changes: an escaped reserved character stays escaped (``a%2Fb`` is not ``a/b``),
``%25`` (an encoded percent) is never decoded twice, ordinary path letters keep their case, and
the query (variant IDs) and fragment are not part of the comparison here.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
# Characters left as written when encoding literal non-ASCII text: every ASCII character that
# may appear in a path, and "%" (existing escapes are handled separately).
_SAFE = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) not in '"<>\\^`{|}')


def _escape(match: re.Match[str]) -> str:
    character = chr(int(match[1], 16))
    return character if character in _UNRESERVED else f"%{match[1].upper()}"


def normalized_path(url: str) -> str:
    """The URL's path in RFC 3986 normal form, without a trailing slash."""
    path = urlsplit(url.strip()).path
    path = quote(path, safe=_SAFE)  # literal non-ASCII or space -> UTF-8 escapes
    return _ESCAPE.sub(_escape, path).rstrip("/")
