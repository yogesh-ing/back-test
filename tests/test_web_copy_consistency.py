"""F12 — the small consistency items, written as rules instead of notes.

Five of the six findings in the 2026-11-03 visual pass' batch shared one
property: none of them looked *broken*, they just said something in a way the
rest of the app does not — a count that disagrees with its noun, an emoji where
every other page uses the ``icon()`` SVG macro, a limit that reads as a blank
box, a cost ratio past 100%, a badge that claims more than its source can
answer for.

Same shape as ``test_web_escaping.py``: scan the real source, one-way, so
improving one site can never redden another. Each rule states the files it
governs; the rest of the app's copy is untouched and unclaimed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "backtest" / "web"
JS = WEB / "static" / "js"
TEMPLATES = WEB / "templates"

# A ``${count} trades`` interpolation or a ``+ ' trades'`` concatenation: the
# noun is hard-coded plural next to a number that is not always plural.
RENDERED_TRADES = re.compile(r"(?:\}\s*(?:closed\s+)?trades\b|\+\s*'\s*(?:closed\s+)?trades\b)")

# Pictographs and dingbats — the ranges the emoji-as-icon habit lives in. The
# typographic marks the app keeps (· — ← ▾ ≠) sit outside them on purpose.
PICTOGRAPH = re.compile(r"[\U0001F000-\U0001FAFF☀-➿\uFE0F]")

ICON_PATH_KEY = re.compile(r"^\s*'([a-z-]+)':", re.MULTILINE)
ICON_CALL = re.compile(r"\{\{\s*icon\(\s*'([a-z-]+)'")

NUMBER_INPUT = re.compile(r"<input\b[^>]*type=\"number\"[^>]*>")
BLANKABLE_VALUE = re.compile(r"value=\"\$\{[^}]*\|\|\s*''")

# The three files this slice touched. Count strings elsewhere in the app are
# unchanged and deliberately not claimed by this rule.
COUNT_FILES = [JS / "backtest.js", JS / "optimize_run.js", JS / "diary.js"]
ICON_PAGES = [TEMPLATES / "risk.html", TEMPLATES / "analytics.html"]


def _lines(text: str) -> list[str]:
    return text.splitlines()


def test_counted_nouns_speak_their_number():
    """``1 trades`` is the finding; the plural must be decided, not assumed."""
    offenders = []
    for path in COUNT_FILES:
        for n, line in enumerate(_lines(path.read_text(encoding="utf-8")), 1):
            if RENDERED_TRADES.search(line) and "=== 1 ?" not in line:
                offenders.append(f"{path.name}:{n}: {line.strip()[:90]}")
    assert not offenders, "hard-plural count strings:\n" + "\n".join(offenders)


def test_the_scan_catches_the_shape_it_is_meant_to():
    """Self-check: the rule is not vacuous — the pre-fix line must trip it."""
    bad = "                    <span>${r.total_trades ?? 0} trades</span>"
    good = "                    <span>${nTrades} trade${nTrades === 1 ? \"\" : \"s\"}</span>"
    concat = "        '(' + (data.trade_count || 0) + ' closed trades across ' +"
    assert RENDERED_TRADES.search(bad) and "=== 1 ?" not in bad
    assert RENDERED_TRADES.search(concat) and "=== 1 ?" not in concat
    assert not RENDERED_TRADES.search(good), "a pluralised count must not be flagged"


def test_no_emoji_stands_where_the_icon_macro_belongs():
    """/risk and /analytics are SVG-icon pages; a pictograph is a regression."""
    for path in ICON_PAGES:
        text = path.read_text(encoding="utf-8")
        hits = [f"{path.name}:{n}" for n, line in enumerate(_lines(text), 1)
                if PICTOGRAPH.search(line)]
        assert not hits, f"emoji used as icons in {path.name}: {hits}"
        assert "{{ icon(" in text, f"{path.name} must render its icons through the macro"


def test_every_icon_name_used_resolves_to_a_path():
    """``icon()`` falls back to 'activity' for a name it does not know, so a
    typo silently draws the wrong glyph — the vocabulary has to be checked."""
    macros = (TEMPLATES / "_macros.html").read_text(encoding="utf-8")
    start = macros.index("icon_paths = {")
    block = macros[start:macros.index("} %}", start)]
    known = set(ICON_PATH_KEY.findall(block))
    assert known, "icon_paths must parse"
    used = {}
    for template in sorted(TEMPLATES.rglob("*.html")):
        for name in ICON_CALL.findall(template.read_text(encoding="utf-8")):
            used.setdefault(name, template.name)
    unknown = {n: f for n, f in used.items() if n not in known}
    assert not unknown, f"icon() names with no path: {unknown}"


def test_an_unset_limit_says_so_instead_of_rendering_a_blank_box():
    """A number input that can hold no value must carry ``placeholder``."""
    src = (JS / "components" / "risk_page.js").read_text(encoding="utf-8")
    blank = [tag for tag in NUMBER_INPUT.findall(src)
             if BLANKABLE_VALUE.search(tag) and "placeholder=" not in tag]
    assert not blank, f"blank-capable inputs with no fallback text: {blank}"


def test_a_cost_ratio_past_gross_stops_being_a_percentage():
    """1494% of a tiny gross is not information; past 100% the wording changes."""
    src = (JS / "diary.js").read_text(encoding="utf-8")
    assert "'Drag: ' + Number(data.commission_drag_pct)" not in src, "the uncapped KPI sub is back"
    assert "exceeds gross" in src and "(cost &gt; gross)" in src, "the cap wording is gone"
    assert src.count("> 100") >= 2, "both the KPI sub and the table cell must cap the ratio"


def test_the_certification_badge_names_what_it_certifies():
    """The badge answers for the source class, never for the symbols loaded."""
    src = (JS / "components" / "data_source_gate.js").read_text(encoding="utf-8")
    assert "certification-grade source" in src, "the badge must say it is about the source"
    assert "does not certify which symbols or dates" in src, "the title must bound the claim"
