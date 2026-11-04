"""F14 — a server string on /risk reaches the DOM only through ``esc()``.

``risk_page.js`` builds its whole screen with ``innerHTML`` template literals.
The markup is the file's own; every *value* interpolated into it comes off the
wire — a halt reason, an audit action, a runner name, a stored limit an
operator typed into a form. Left raw, one of those strings is a live tag.

Same shape as ``test_label_resolver.py``'s render-site rule: a governed
vocabulary, a line-independent scan of the real file, one-way so that escaping
another site can never make this test fail.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RISK_PAGE = ROOT / "src" / "backtest" / "web" / "static" / "js" / "components" / "risk_page.js"

# Fields the /risk payloads carry as text. Numeric-looking ones are in there
# because they are stored, not computed: ``max_open_positions`` arrives from a
# form and is echoed straight into a ``value=`` attribute.
GOVERNED = {
    "action", "broker", "count", "detail", "display_name", "error", "group",
    "halt_mode", "halt_reason", "instance_id", "label", "message", "mode",
    "name", "reason", "runners", "scope", "side", "structs", "sub", "symbol",
    "text", "title", "total", "value",
}

# esc(), or a formatter that already owns the escaping contract.
ESCAPES = re.compile(r"\b(?:esc|fmtMoney|fmtSigned|Labels\.\w+)\s*\(")

# ``X.field === "lit" ? "pos" : "neg"`` — reads a governed field to pick a
# class name. Both outcomes are literals, so nothing from the wire is emitted.
LITERAL_TERNARY = re.compile(r"^[^?]*\?\s*(?:\"[^\"\\]*\"|'[^'\\]*')\s*:\s*(?:\"[^\"\\]*\"|'[^'\\]*')\s*$")

# ``s.mode``, ``cur.halt_reason``, ``meta.label || gid`` — the final segment is
# what the vocabulary is keyed on, so ``b.runner_count`` cannot match ``count``.
PROPERTY = re.compile(r"\.([a-zA-Z_][a-zA-Z0-9_]*)")

INTERP = re.compile(r"\$\{(?:[^{}]|\{[^{}]*\})*\}")

# A tag opener on the same line, before the interpolation. ``risk_page.js``
# also builds plain strings (a concentration warning) that it escapes later, at
# the render boundary — those lines are not render sites.
MARKUP_BEFORE = re.compile(r"<[a-zA-Z/!]")


def _interpolations(text: str) -> list[tuple[int, str]]:
    """Return (line, expression) for every ``${ … }`` that sits in markup."""
    hits = []
    lines = text.splitlines()
    for match in INTERP.finditer(text):
        line_no = text.count("\n", 0, match.start())
        col = match.start() - text.rfind("\n", 0, match.start()) - 1
        if not MARKUP_BEFORE.search(lines[line_no][:col]):
            continue
        hits.append((line_no + 1, match.group(0)[2:-1].strip()))
    return hits


def test_risk_page_ships_an_escaper():
    text = RISK_PAGE.read_text(encoding="utf-8")
    assert re.search(r"const esc = ", text), (
        "risk_page.js lost its escaper — every interpolation of a server "
        "string in this file depends on it (finding F14)")


def test_no_governed_field_reaches_the_screen_unescaped():
    text = RISK_PAGE.read_text(encoding="utf-8")
    offenders = []
    for line, expr in _interpolations(text):
        fields = {m.group(1) for m in PROPERTY.finditer(expr)} & GOVERNED
        if not fields:
            continue
        if ESCAPES.search(expr) or LITERAL_TERNARY.match(expr):
            continue
        offenders.append(f"{RISK_PAGE.name}:{line}: {expr[:80]} ({', '.join(sorted(fields))})")
    assert not offenders, (
        "these interpolations put a stored value into innerHTML as-is; one of "
        "them is a halt reason or a runner name and would render as markup. "
        "Wrap the value in esc() — for a class name, keep the ternary to "
        "literal branches:\n" + "\n".join(offenders))


def test_the_rule_catches_a_raw_field_and_forgives_an_escaped_one():
    """The scan must be able to fail, or it is decoration."""
    raw = 'box.innerHTML = `<strong>${s.display_name}</strong>`;'
    assert _offenders(raw), "a governed field slipped through the scan"
    for guarded in (
        'box.innerHTML = `<strong>${esc(s.display_name)}</strong>`;',
        'box.innerHTML = `<span class="${t.side===\'BUY\'?\'pnl-pos\':\'pnl-neg\'}">x</span>`;',
        'box.innerHTML = `<td>${fmtMoney(s.notional)}</td>`;',
        'box.innerHTML = `<td>${esc(Labels.symbol(sym))}</td>`;',
    ):
        assert not _offenders(guarded), f"false positive: {guarded}"


def _offenders(fragment: str) -> list[str]:
    found = []
    for line, expr in _interpolations(fragment):
        fields = {m.group(1) for m in PROPERTY.finditer(expr)} & GOVERNED
        if fields and not (ESCAPES.search(expr) or LITERAL_TERNARY.match(expr)):
            found.append(expr)
    return found
