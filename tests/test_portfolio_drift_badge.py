"""A strategy-drift notice must not be painted as a risk halt (2026-11-05).

``portfolio.js`` rendered every ``r.error`` in red under the words "⚠ risk halt".
PR #55's ``27f0071`` put a missing space back between concatenated description
literals in 30 strategy files — byte-for-byte semantically identical prose — and
because the spawn-stamped bundle hashes those files, all seven live books woke up
claiming a risk halt while the daily fuse read ``NORMAL``. The same strings were
also interpolated into ``title=`` attributes unescaped.

Static scan of the real file, same shape as ``test_web_escaping.py``:
line-independent and one-way, so governing another site can never break it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_JS = ROOT / "src" / "backtest" / "web" / "static" / "js" / "portfolio.js"
TEXT = PORTFOLIO_JS.read_text(encoding="utf-8")

#: The two codes the bundle drift check raises — neither is a risk event.
DRIFT_CODES = ("STRATEGY_CODE_CHANGED", "CODE_UPDATED_WHILE_IN_TRADE")

#: Wire text fields that land in an attribute; each must be escaped at the sink.
GOVERNED_FIELDS = ("r.error", "r.strategy_name", "row.strategy_name", "r.strategy")

# ``const esc = (v) => String(v ?? "")…`` — the house escaper, copied verbatim
# into every file that builds markup out of server strings.
ESC_DEFINITION = re.compile(r"\b(?:const|function)\s+esc\s*[=(]")


def test_portfolio_js_carries_the_house_escaper() -> None:
    assert ESC_DEFINITION.search(TEXT), "portfolio.js must own an esc() like risk_page.js"


def test_risk_halt_badge_is_guarded_against_strategy_drift() -> None:
    """The red badge may not fire for the codes the amber pill already tells."""
    lines = TEXT.splitlines()
    sites = [
        i
        for i, line in enumerate(lines)
        if "risk halt" in line and "<div" in line
    ]
    assert len(sites) == 1, f"expected one risk-halt render site, got {len(sites)}"
    # The condition and the markup are separate lines of one ternary, so the
    # guard is read from the statement around the site, not the site alone.
    statement = "\n".join(lines[max(0, sites[0] - 2) : sites[0] + 2])
    assert "isCodeDriftError" in statement, statement
    assert "esc(" in lines[sites[0]], "the halt reason must reach the DOM escaped"
    for code in DRIFT_CODES:
        assert code in TEXT, f"isCodeDriftError() must name {code}"


def test_strategy_drift_is_not_reported_as_a_risk_state_anywhere() -> None:
    """No copy may call these codes a halt, fuse or breaker."""
    for line in TEXT.splitlines():
        if any(code in line for code in DRIFT_CODES) and "<" in line:
            lowered = line.lower()
            assert "risk halt" not in lowered, line
            assert not re.search(r"\b(fuse|breaker|halted)\b", lowered), line


def test_governed_wire_fields_reach_the_dom_only_through_esc() -> None:
    """A ``title="`` sink reading a governed field must interpolate it via esc()."""
    violations: list[str] = []
    for number, line in enumerate(TEXT.splitlines(), start=1):
        if 'title="' not in line:
            continue
        for field in GOVERNED_FIELDS:
            # esc(r.error) / esc(row.strategy_name || "") — the field still
            # appears on the line, so anchor on the concatenation that opens it.
            raw = re.search(r'\+\s*' + re.escape(field) + r"\s*(\|\||\+|'|\"|<)", line)
            if raw and "esc(" not in line:
                violations.append(f"{number}: {line.strip()}")
    assert not violations, "unescaped wire text in a title attribute:\n" + "\n".join(violations)


def test_removal_copy_does_not_promise_a_flatten() -> None:
    """``remove_runner`` calls ``stop()``, which leaves positions untouched.

    Both the button tooltip and the confirm dialog used to say the open book
    gets flattened. The claim is the dangerous part: an operator who believes it
    deletes a book whose entry trade then has no matching exit, and whose option
    history (state file only) disappears with it.
    """
    forbidden = ("flattens its book", "positions are flattened", "removal flattens")
    for phrase in forbidden:
        assert phrase not in TEXT, f"portfolio.js still claims {phrase!r}"
    assert "does NOT flatten" in TEXT, "the remove affordance must say what it does not do"
