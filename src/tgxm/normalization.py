"""Loss-minimizing normalization for untrusted Telegram text."""

from __future__ import annotations

import re
import unicodedata


_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2212": "-",  # mathematical minus
    }
)

_CONFIRMED_TYPO_HIT = re.compile(r"\bH(?:I|L)T\b", re.IGNORECASE)


def normalize_text(text: str) -> str:
    """Return stable parser text without changing numeric meaning.

    The original string remains owned by the caller (normally in a
    :class:`~tgxm.models.RawTelegramEvent`).  This function intentionally does
    not translate decimal commas, thousands separators, words, or numeric
    glyphs beyond Unicode NFKC because guessing their meaning could turn an
    ambiguous price into an executable one.
    """

    if not isinstance(text, str):
        raise TypeError("Telegram message text must be a string")

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u2028", "\n").replace("\u2029", "\n")
    normalized = normalized.translate(_DASH_TRANSLATION)

    # Format controls include zero-width spaces/joiners, BOM, word joiner, and
    # bidi controls.  None are valid signal grammar and retaining them can make
    # visually identical text parse differently.
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )

    lines: list[str] = []
    previous_blank = False
    for raw_line in normalized.split("\n"):
        line = " ".join(raw_line.split())
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False

    while lines and not lines[-1]:
        lines.pop()

    normalized = "\n".join(lines)
    normalized = _CONFIRMED_TYPO_HIT.sub("HIT", normalized)
    return normalized

