"""House money formatting for server-rendered pages (finding F9).

Templates used to write ``{{ currency_symbol }}{{ "{:,.0f}".format(x) }}``. The
``str.format`` grouping is locale-less, so those rows grouped western-style
(1,234,567) while the JavaScript on the same page grouped en-IN (12,34,567) —
one screen, two systems for the same number.

``Money`` in ``static/js/components/currency.js`` is the client half of this
rule; both read the same ``<body data-currency-*>`` values, so a deployment
configured for another currency or locale changes both sides at once.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: Locales whose integer part groups as 3 then pairs (lakh / crore).
_LAKH_LOCALES = ("en-IN", "en_IN", "mr-IN", "hi-IN")


def _group_integer(digits: str, locale: str) -> str:
    """Insert thousands separators into a run of integer digits."""
    if not digits:
        return ""
    if str(locale).replace("-", "_") in tuple(x.replace("-", "_") for x in _LAKH_LOCALES):
        if len(digits) <= 3:
            return digits
        head, tail = digits[:-3], digits[-3:]
        pairs = (len(head) % 2)
        chunks = [head[:pairs]] if pairs else []
        chunks += [head[i:i + 2] for i in range(pairs, len(head), 2)]
        return ",".join(chunks) + f",{tail}"
    chunks = []
    while len(digits) > 3:
        chunks.insert(0, digits[-3:])
        digits = digits[:-3]
    chunks.insert(0, digits)
    return ",".join(chunks)


def _decimal(value: object, dp: int) -> tuple[str, str, str]:
    """Return (sign, integer digits, fraction) for ``value`` at ``dp`` places."""
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        number = Decimal(0)
    sign = "-" if number < 0 else ""
    quantum = Decimal(1).scaleb(-dp) if dp > 0 else Decimal(1)
    try:
        rounded = number.copy_abs().quantize(quantum)
    except InvalidOperation:
        rounded = number.copy_abs()
    text = f"{rounded:.{dp}f}" if dp else f"{int(rounded):d}"
    whole, _, frac = text.partition(".")
    return sign, whole.lstrip("0") or "0", frac


def format_money(
    value: object,
    *,
    symbol: str = "₹",
    locale: str = "en-IN",
    dp: int = 0,
) -> str:
    """`-₹12,34,567` — sign, symbol, lakh-grouped magnitude."""
    sign, whole, frac = _decimal(value, dp)
    grouped = _group_integer(whole, locale)
    return f"{sign}{symbol}{grouped}" + (f".{frac}" if frac else "")


def format_count(value: object, *, locale: str = "en-IN") -> str:
    """A whole number with the house grouping and no currency symbol."""
    _, whole, _ = _decimal(value, 0)
    return _group_integer(whole, locale)


# ------------------------------------------------------------- labels (F11)

#: Indicator initials stay capitals; a name stays a name. These two collections
#: are the same ones ``static/js/components/labels.js`` uses — a template and
#: the script on the same page must not disagree about what ``sma_crossover``
#: is called, which is the label-shaped version of the F9 defect.
_ACRONYMS = frozenset({
    "sma", "ema", "rsi", "macd", "roc", "vwap", "atr", "oi", "hf", "ce", "pe", "pob",
})
_PROPER = {"donchian": "Donchian", "bollinger": "Bollinger"}


def format_label(value: object) -> str:
    """``swing_rsi2_meanrev`` → ``Swing RSI2 meanrev``: one word per underscore.

    A part the operator already capitalised (``CE_instant_buy``) is a name, not
    a word to re-case, so it survives untouched.
    """
    words = [w for w in re.split(r"[_\s]+", str(value or "")) if w]
    out = []
    for index, word in enumerate(words):
        alpha = word.rstrip("0123456789")
        digits = word[len(alpha):]
        low = alpha.lower()
        if low in _ACRONYMS:
            out.append(alpha.upper() + digits)
        elif low in _PROPER:
            out.append(_PROPER[low] + digits)
        elif alpha == alpha.upper():
            out.append(word)
        elif index == 0:
            out.append(alpha[:1].upper() + alpha[1:].lower() + digits)
        else:
            out.append(low + digits)
    return " ".join(out)
