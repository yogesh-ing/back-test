"""One resolver for the identifiers that have a human name (finding F11).

``bollinger_reversion`` on a Recent Runs tile, ``equity_intraday`` in a badge and
``bear_put_spread`` in the risk rulebook all forced the reader to parse an
identifier to find out what they were looking at. The keys stay snake_case —
the runner, ``config/brokers.yaml`` and the ledger need them that way — but
every surface that *reads* one now prints a label and keeps the raw key in a
``title``.

This is the F9 shape again: two halves of one rule. ``Labels`` in
``static/js/components/labels.js`` resolves for the browser, ``format_label`` in
``web/formatting.py`` (registered as the ``label`` Jinja filter) resolves for the
two server-rendered portfolio tables. They share one vocabulary, and the parity
tests below are what stops them drifting.

The last two tests are static ratchets, and they earn their place: the wire
probe (``pw_f11.js`` in the QA scratch dir) can only see what the QA data
happens to render. It reported zero defects while the alert widget, the risk
board's closed-trades table and the Orders segment sub-line were all still
printing raw keys — three surfaces no fixture reached.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "backtest" / "web"
SCRIPTS = WEB / "static" / "js"
TEMPLATES = WEB / "templates"
LABELS_JS = SCRIPTS / "components" / "labels.js"
NODE = shutil.which("node")


# ---------------------------------------------------------------------------
# The server half
# ---------------------------------------------------------------------------

def test_the_server_resolver_reads_one_word_per_underscore():
    from backtest.web.formatting import format_label

    assert format_label("bollinger_reversion") == "Bollinger reversion"
    assert format_label("sma_crossover") == "SMA crossover"
    assert format_label("swing_rsi2_meanrev") == "Swing RSI2 meanrev"
    assert format_label("hf_equity_scalp") == "HF equity scalp"
    assert format_label("banknifty_ema_momentum") == "Banknifty EMA momentum"
    assert format_label("swing_donchian_breakout") == "Swing Donchian breakout"


def test_a_part_the_operator_capitalised_survives():
    from backtest.web.formatting import format_label

    # "CE_instant_buy" and "NIFTY50" are names, not words to re-case.
    assert format_label("CE_instant_buy") == "CE instant buy"
    assert format_label("PE_instant_buy") == "PE instant buy"
    assert format_label("NIFTY_50") == "NIFTY 50"


def test_nothing_to_resolve_is_not_an_error():
    from backtest.web.formatting import format_label

    assert format_label("") == ""
    assert format_label(None) == ""
    assert format_label("donchian") == "Donchian"


def test_the_filter_is_registered_under_the_name_the_templates_use():
    from backtest.brokers.session_manager import reset_default_manager
    from backtest.web.app import create_app

    reset_default_manager()
    app = create_app(source="synthetic", replay_speed=0)
    assert "label" in app.jinja_env.filters
    assert app.jinja_env.filters["label"]("sma_crossover") == "SMA crossover"
    reset_default_manager()


# ---------------------------------------------------------------------------
# The two halves are one rule
# ---------------------------------------------------------------------------

def _js_vocabulary() -> tuple[set[str], dict[str, str]]:
    """Read ACRONYMS and PROPER back out of labels.js."""
    text = LABELS_JS.read_text(encoding="utf-8")
    acronyms = re.search(r"const ACRONYMS = new Set\(\[(.*?)\]\)", text, re.S)
    proper = re.search(r"const PROPER = \{(.*?)\};", text, re.S)
    assert acronyms and proper, (
        "labels.js no longer declares ACRONYMS/PROPER in the shape this test "
        "reads — fix the test's regex alongside the component, not instead of it")
    return (
        set(re.findall(r'"([^"]+)"', acronyms.group(1))),
        dict(re.findall(r'(\w+):\s*"([^"]+)"', proper.group(1))),
    )


def test_the_two_halves_share_one_vocabulary():
    from backtest.web import formatting

    js_acronyms, js_proper = _js_vocabulary()
    assert js_acronyms == set(formatting._ACRONYMS), (
        "labels.js and formatting.py disagree on which parts are indicator "
        "initials, so a template and a page script would print the same "
        "strategy two ways — the exact defect the shared resolver exists to end")
    assert js_proper == dict(formatting._PROPER)


HOUSE_KEYS = [
    "bollinger_reversion", "sma_crossover", "swing_rsi2_meanrev", "hf_equity_scalp",
    "banknifty_ema_momentum", "swing_donchian_breakout", "CE_instant_buy",
    "PE_instant_buy", "ema_reversion_pob", "volume_oi_surge", "vwap_ema_rsi",
    "supertrend_equity", "one_min_scalp", "immediate_strangle", "NIFTY_50",
    "equity_intraday", "options_index", "bear_put_spread", "direction_aware",
]


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_the_browser_and_the_template_resolve_the_same_key_the_same_way():
    from backtest.web.formatting import format_label

    script = (
        "const L=require(process.argv[1]);"
        "const keys=JSON.parse(process.argv[2]);"
        "process.stdout.write(JSON.stringify(keys.map((k)=>L.humanise(k))));"
    )
    result = subprocess.run(
        [NODE, "-e", script, str(LABELS_JS), json.dumps(HOUSE_KEYS)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    for key, js_label in zip(HOUSE_KEYS, json.loads(result.stdout)):
        assert format_label(key) == js_label, (
            f"{key}: the Jinja `label` filter says {format_label(key)!r}, "
            f"labels.js says {js_label!r}")


# ---------------------------------------------------------------------------
# Static ratchets — they cover the surfaces no fixture renders
# ---------------------------------------------------------------------------

#: Attribute names that hold a governed key rather than free text.
_GOVERNED = re.compile(
    r"strategy_name|strategy_id|structure_type|\.strategy\b|\.segment\b")
#: Marks the line as putting that value on screen rather than sending it back.
_RENDERED = re.compile(r"\$\{|innerHTML|textContent|esc\(|escAttr\(")


def test_no_script_renders_a_governed_key_without_the_resolver():
    """A render site that bypasses Labels is the defect, in any component.

    Deliberately line-level: ``tune_this.js`` reads ``cfg.strategy`` into a
    prefill payload and must stay free to do so. Only a value that reaches the
    screen is governed.
    """
    offenders = []
    for path in sorted(SCRIPTS.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        hits = [
            (n, line.strip()) for n, line in enumerate(text.splitlines(), 1)
            if _GOVERNED.search(line) and _RENDERED.search(line)
        ]
        if hits and "Labels" not in text:
            offenders += [f"{path.relative_to(ROOT)}:{n}: {line[:90]}" for n, line in hits]
    assert not offenders, (
        "these lines print a strategy/segment/structure key straight to the "
        "screen. Route them through components/labels.js (Labels.strategy / "
        ".segment / .structure) and keep the raw key in a title:\n"
        + "\n".join(offenders))


def test_no_template_prints_a_governed_key_without_the_label_filter():
    """Content-position ``{{ … }}`` must carry ``| label``; attributes must not.

    ``<td title="{{ r.strategy_name }}">{{ r.strategy_name | label }}</td>`` is
    the intended pair: the hover keeps the identifier an operator matches
    against a config file or a DB row, the cell carries the name.
    """
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\{\{(.*?)\}\}", text, re.S):
            expr = m.group(1)
            if not re.search(r"strategy_name|strategy_id|structure_type|\.strategy\b|\.segment\b", expr):
                continue
            # outside a tag when the nearest ">" is closer than the nearest "<"
            in_content = text.rfind(">", 0, m.start()) > text.rfind("<", 0, m.start())
            if in_content and "| label" not in expr:
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {{{{{expr.strip()[:70]}}}}}")
    assert not offenders, (
        "these template expressions print a governed key as page text without "
        "the `label` filter (web/formatting.py:format_label):\n"
        + "\n".join(offenders))
