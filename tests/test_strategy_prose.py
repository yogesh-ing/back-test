"""Strategy prose must not fuse words at a line join (finding F10).

Every strategy module writes its description as several implicit-concatenation
literals, one per source line. 188 of those lines forgot the trailing space, so
the About panel read "…below the lower Bollinger band on theassumption the
stretch snaps back…". Python folds adjacent literals at compile time, so the
fused word is invisible in the source and only ever shows up on screen.

The guard below tokenizes the strategy modules — the built-in ones and the
plugin pack, both of which register prose the About panel prints — and fails on
any pair of adjacent string literals whose join fuses a lowercase word into the
next one: the exact defect, at the exact place it is born. The runtime half then
checks the registered descriptions for the artifacts a hand sweep could leave
behind (double spaces, a space before punctuation, padded ends).
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIRS = (
    ROOT / "src" / "backtest" / "strategies",
    ROOT / "plugins" / "strategies",
)

SKIP_TOKENS = {tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}
FUSED_END = re.compile(r"[a-z]\Z")
FUSED_START = re.compile(r"\A[a-z]")


def _split_literal(text: str) -> tuple[bool, str]:
    i = 0
    while i < len(text) and text[i] in "rRbBfFuU":
        i += 1
    triple = text[i:i + 3] in ('"""', "'''")
    q = 3 if triple else 1
    return triple, text[i + q:len(text) - q]


def fused_joins(path: Path) -> list[tuple[int, str, str]]:
    """(line, tail, head) for every adjacent-literal pair that fuses two words."""
    tokens = list(tokenize.generate_tokens(io.open(path, encoding="utf-8").readline))
    significant = [t for t in tokens if t.type not in SKIP_TOKENS]
    out = []
    for first, second in zip(significant, significant[1:]):
        if first.type != tokenize.STRING or second.type != tokenize.STRING:
            continue
        _, first_inner = _split_literal(first.string)
        _, second_inner = _split_literal(second.string)
        if first_inner and second_inner and FUSED_END.search(first_inner) and FUSED_START.match(second_inner):
            out.append((second.start[0], first_inner[-20:], second_inner[:20]))
    return out


def _strategy_modules() -> list[Path]:
    modules = [path for d in STRATEGY_DIRS for path in sorted(d.glob("*.py"))]
    assert len(STRATEGY_DIRS) == len({p.parent for p in modules}), (
        "a strategy directory moved or is empty — the ratchet below would "
        "silently stop covering it"
    )
    return modules


def test_no_strategy_literal_fuses_a_word_into_the_next():
    offenders = {
        f"{path.relative_to(ROOT)}:{line}": (tail, head)
        for path in _strategy_modules()
        for line, tail, head in fused_joins(path)
    }
    assert not offenders, (
        "a description line is missing its trailing space — the About panel "
        f"prints the fused word: {offenders}"
    )


def _registered_prose() -> list[tuple[str, str]]:
    from backtest.plugins import discover_plugins
    from backtest.strategy.registry import get_all

    discover_plugins()  # the About panel prints plugin prose too, not just built-ins
    prose = []
    for strategy in get_all():
        name = strategy.get("name", "?")
        description = strategy.get("description") or ""
        if description:
            prose.append((f"{name}.description", description))
        for key, value in (strategy.get("description_sections") or {}).items():
            if value:
                prose.append((f"{name}.{key}", value))
    return prose


def test_every_registered_strategy_carries_prose():
    prose = _registered_prose()
    assert len(prose) >= 12, f"expected one description per strategy module, got {len(prose)}"
    assert any(where.startswith("supertrend_equity.") for where, _ in prose), (
        "the plugin pack did not register — the About panel would be the only "
        "thing left covering its prose"
    )


def test_the_sweep_left_no_spacing_artifacts():
    for where, text in _registered_prose():
        assert not re.search(r"[a-z]  [a-z]", text), f"{where} gained a double space"
        assert not re.search(r" [.,;:!?]", text), f"{where} gained a space before punctuation"
        assert text == text.strip(), f"{where} is padded with whitespace"
