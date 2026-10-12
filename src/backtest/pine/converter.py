"""Pine Script converter — orchestrates parsing, codegen, and validation.

Pure code transpilation (NO LLM). Converts Pine Script v5 to Python Strategy
plugins with metadata extraction and validation.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Tuple
from uuid import uuid4

from .codegen import PineCodeGenerator
from .parser import PineScriptParser


class PineScriptConverter:
    """Convert Pine Script v5 to Platform Strategy.

    NO LLM. Pure code transpilation.
    """

    def __init__(
        self,
        parser: PineScriptParser | None = None,
        codegen: PineCodeGenerator | None = None,
    ):
        self.parser = parser or PineScriptParser()
        self.codegen = codegen or PineCodeGenerator()

    def convert(
        self,
        pine_code: str,
        strategy_name: str | None = None,
    ) -> Tuple[str, dict]:
        """Convert Pine Script to Python Strategy.

        Args:
            pine_code: Raw Pine Script v5 source code
            strategy_name: Optional override for the generated class name

        Returns:
            Tuple of (python_code, metadata)

        Raises:
            PineConversionError: If parsing or generation fails
        """
        self._audit_source(pine_code)

        # 1. Extract strategy name from Pine if not provided. User-provided
        # names are sanitized too — they become a Python class name, so
        # "Outside Bar Strategy" must become "OutsideBarStrategy".
        if not strategy_name:
            match = re.search(r'strategy\(["\']([^"\']+)', pine_code)
            strategy_name = match.group(1) if match else "ImportedStrategy"
        strategy_name = self._sanitize_class_name(strategy_name) or "ImportedStrategy"

        # 2. Parse Pine Script to AST
        try:
            pine_ast = self.parser.parse(pine_code)
        except Exception as e:
            raise PineConversionError(f"Failed to parse Pine Script: {e}") from e

        # 3. Generate Python code
        try:
            python_code = self.codegen.generate(pine_ast, strategy_name)
        except Exception as e:
            raise PineConversionError(f"Failed to generate Python code: {e}") from e

        # 4. Validate generated code
        is_valid, error = self._validate_code(python_code)
        if not is_valid:
            raise PineConversionError(f"Generated code validation failed: {error}")

        # 5. An `indicator()` script plots but never trades — with no
        # `strategy.entry()` block nothing is wired into entries(), and the
        # saved plugin would silently stay flat on every bar. strategy.order
        # scripts take the stateful generate_signals() path instead.
        if not re.search(
            r"^\s+(?:long|short)_entries \|=|^\s+def generate_signals\(",
            python_code,
            re.MULTILINE,
        ):
            raise PineConversionError(
                "No `strategy.entry()` call was found, so nothing can be "
                "converted into entries. This looks like an `indicator()` "
                "script: it plots signals but never trades. Declare "
                "`strategy(...)` and wrap the buy/sell conditions in "
                "`if <condition>:` blocks calling `strategy.entry(...)`."
            )

        # 6. Preflight: import it and run the plugin conformance battery, so a
        # script that would only die at load dies here, with the reason.
        self._preflight_generated(python_code, strategy_name)

        # 7. Extract metadata
        metadata = {
            "original_name": strategy_name,
            "pine_version": int(re.search(r"(?m)^\s*//\s*@version\s*=\s*(\d+)", pine_code).group(1)) if re.search(r"(?m)^\s*//\s*@version\s*=\s*(\d+)", pine_code) else None,
            "indicators_used": self._extract_indicators_list(pine_ast),
            "has_long": self._has_long_trades(pine_ast),
            "has_short": self._has_short_trades(pine_ast),
            "complexity": self._estimate_complexity(pine_ast),
            "readable": self._extract_readable_summary(pine_ast, pine_code),
            "warnings": self._collect_warnings(python_code),
        }
        return python_code, metadata

    @staticmethod
    def _audit_source(source: str) -> None:
        """Fail closed on constructs the line-oriented parser cannot preserve.

        This is a compatibility gate, not a full Pine grammar. Add support only
        alongside semantic fixtures demonstrating equivalent positions.
        """
        version = re.search(r"(?m)^\s*//\s*@version\s*=\s*(\d+)", source)
        if version and version.group(1) not in ("5", "6"):
            raise PineConversionError(
                f"Pine v{version.group(1)} is not supported by this converter "
                "(supported versions: v5 and v6)."
            )
        if re.search(r"(?m)^\s*indicator\s*\(", source):
            raise PineConversionError(
                "This is an indicator, not an executable strategy. Its CE/PE "
                "plot/alert signals and displayed stop/targets are not orders. "
                "Define explicit strategy.entry() calls and exits before importing."
            )
        checks = (
            (r"\brequest\.security\s*\(", "multi-timeframe request.security"),
            (r"(?m)^\s*var(?:ip)?\s+", "persistent var state"),
            (r":=", "stateful := reassignment"),
            (r"\bstrategy\.exit\s*\([^\n]*\bqty_percent\s*=", "partial exits"),
            (r"\bstrategy\.position_size\s*\[", "position history"),
        )
        for pattern, feature in checks:
            if re.search(pattern, source):
                raise PineConversionError(
                    f"Unsupported Pine construct: {feature}. Conversion stopped "
                    "rather than silently changing trading behavior."
                )

    def save_as_plugin(
        self,
        python_code: str,
        strategy_name: str,
        metadata: dict,
        segment: str | None = None,
        criteria: dict | None = None,
    ) -> Path:
        """Save generated strategy as plugin.

        Args:
            python_code: Generated Python strategy code
            strategy_name: Name for the strategy
            metadata: Conversion metadata dict
            segment: Optional capital-partition name (config/segments.yaml).
                issues.txt S2: every saved strategy is linked to a segment so
                backtest → paper → live all spawn into the same partition;
                the class-level ``default_segment`` is what the spawn form
                preselects.
            criteria: Optional final readable criteria (issues.txt follow-up:
                every required criterion is either detected in the Pine
                script or typed by the user before Save is allowed). Stored
                as a ``readable_criteria`` class attribute so the strategy's
                plain-language entry/strike/TP/SL definition travels with the
                plugin into backtest, paper and live.

        Returns:
            Path to the saved plugin file
        """
        plugin_dir = Path("plugins/strategies")
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # The plugin loader derives a module name from the filename, so it
        # must be a valid Python identifier: no spaces or special characters.
        safe = re.sub(r"[^a-z0-9_]", "_", strategy_name.lower())
        safe = re.sub(r"_+", "_", safe).strip("_") or "imported_strategy"
        filename = f"{safe}_imported.py"
        filepath = plugin_dir / filename

        # Add header comment
        header = f'''"""
Auto-generated from Pine Script v{metadata.get("pine_version") or "unknown"}
Generated: {datetime.now().isoformat()}

Original Strategy: {metadata['original_name']}
Indicators: {', '.join(metadata['indicators_used'])}
Supports: {'Long' if metadata['has_long'] else ''} {'Short' if metadata['has_short'] else ''}

DO NOT EDIT THIS FILE DIRECTLY.
Re-convert from Pine Script if changes needed.
"""

from backtest.strategy.base import Strategy

import pandas as pd
import numpy as np

'''

        full_code = header + python_code

        injections = []
        if segment:
            seg = re.sub(r"[^a-z0-9_]", "", str(segment).strip().lower())
            if not seg:
                raise PineConversionError(f"invalid segment name: {segment!r}")
            injections.append(f'    default_segment = "{seg}"\n')
        if criteria:
            injections.append(
                "    readable_criteria = "
                + json.dumps(self._clean_criteria(criteria), ensure_ascii=False)
                + "\n"
            )
        if injections:
            for line in injections:
                full_code = self._insert_into_class_body(full_code, line)
            try:
                ast.parse(full_code)
            except SyntaxError as exc:
                raise PineConversionError(
                    f"Failed to link strategy metadata to the generated "
                    f"strategy: {exc}"
                )

        filepath.write_text(full_code, encoding="utf-8")

        return filepath

    #: Field order kept identical to the builder checklist so plugin source,
    #: API payload and UI read the same way.
    CRITERIA_KEYS = (
        "entry",
        "entry_strike",
        "expiry",
        "take_profit",
        "stop_loss",
        "exit",
        "opt_moneyness",
        "opt_type",
        "opt_expiry",
    )

    @classmethod
    def _clean_criteria(cls, criteria: dict) -> dict:
        """Sanitize the criteria dict for embedding as a Python literal.

        Only the known keys survive, values are plain strings truncated to a
        sane length, and ``sources`` is restricted to 'detected'/'user' so a
        crafted payload can never smuggle code into the generated file
        (json.dumps escapes everything else, the allow-list keeps it honest).
        """
        out: dict = {}
        for key in cls.CRITERIA_KEYS:
            value = str(criteria.get(key) or "").strip()
            if value:
                out[key] = value[:400]
        sources = criteria.get("sources") or {}
        clean_sources = {
            k: ("user" if str(sources.get(k)) == "user" else "detected")
            for k in out
            if k != "sources"
        }
        if clean_sources:
            out["sources"] = clean_sources
        return out

    @staticmethod
    def _insert_into_class_body(python_code: str, line: str) -> str:
        """Insert one class-body attribute line after the metadata block.

        Anchored on a class-body line the codegen always emits (version →
        author → name, first match wins); values are pre-sanitized by the
        caller, so the inserted line can never break out of its literal.
        """
        for anchor in ('    version = "', '    author = "', '    name = "'):
            idx = python_code.find(anchor)
            if idx != -1:
                end = python_code.find("\n", idx)
                end = len(python_code) if end == -1 else end + 1
                return python_code[:end] + line + python_code[end:]
        raise PineConversionError(
            "generated strategy is missing its metadata block — cannot link a segment"
        )

    @staticmethod
    def _inject_default_segment(python_code: str, segment: str) -> str:
        """Add ``default_segment`` to the generated class body.

        Kept for callers/tests that only need the segment; same anchor and
        sanitization as :meth:`save_as_plugin`.
        """
        seg = re.sub(r"[^a-z0-9_]", "", str(segment).strip().lower())
        if not seg:
            raise PineConversionError(f"invalid segment name: {segment!r}")
        return PineScriptConverter._insert_into_class_body(
            python_code, f'    default_segment = "{seg}"\n'
        )

    def _validate_code(self, code: str) -> Tuple[bool, str]:
        """Validate generated Python code."""
        # 1. Syntax check
        try:
            ast.parse(code)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        # 2. Required methods — the plugin contract is entries()/generate_signals()
        if "def entries" not in code and "def generate_signals" not in code:
            return False, "Missing entries()/generate_signals() method"

        # 3. Security check
        dangerous = [
            "open(",
            "urllib",
            "requests",
            "subprocess",
            "eval(",
            "exec(",
            "__import__",
            "os.system",
        ]
        for pattern in dangerous:
            if pattern in code:
                return False, f"Dangerous pattern detected: {pattern}"

        return True, ""

    def _collect_warnings(self, python_code: str) -> List[str]:
        """Transpilation limits the operator must know about before saving.

        The entries/exits model needs a vectorisable exit condition. Pine
        stops and targets built on ``var`` state (``trailStop := ...``) are
        per-trade bookkeeping, so they never become an ``exits()`` — and the
        Save checklist still accepts them as typed criteria strings, which
        otherwise leaves the impression the level is enforced in code. The
        stateful path enforces strategy.exit() levels inside the loop, so it
        has nothing to warn about.
        """
        if re.search(r"^\s+def (exits|generate_signals)\(", python_code, re.MULTILINE):
            return []
        return [
            "No exits() was generated: the strategy enters and then holds the "
            "position. Pine stop/target levels built on `var` state "
            "(trailStop := ...) are per-trade bookkeeping and do not "
            "transpile — add exits() by hand if the script relies on them."
        ]

    def _preflight_generated(self, python_code: str, class_name: str) -> None:
        """Import the generated module and vet it like the plugin loader would.

        ``ast.parse`` accepts a strategy whose entry line reads
        ``long_entries |= buyCE`` with no ``buyCE`` anywhere — the Pine
        variable the converter could not translate. That file imports, saves,
        and then gets refused by :func:`backtest.plugins.conformance_errors`
        during discovery, which the UI can only report as "failed to load".
        Running the same battery here moves the failure to Convert, where the
        user can still act on it, and names the dead identifier.
        """
        from backtest.plugins import conformance_errors
        from backtest.strategy.base import Strategy
        from backtest.strategy.registry import _REGISTRY

        # The generated class always sets `name = <ClassName>.lower()`; if that
        # name is already registered (a re-converted plugin), the base class's
        # __init_subclass__ would refuse the preview with a duplicate-name
        # error. Hold the live registration aside and restore it after.
        preview_key = class_name.lower()
        held = _REGISTRY.pop(preview_key, None)
        modname = f"_pine_preview_{uuid4().hex[:12]}"
        module = ModuleType(modname)
        sys.modules[modname] = module
        try:
            try:
                exec(
                    compile(python_code, f"<strategy-builder {class_name}>", "exec"),
                    module.__dict__,
                )
            except Exception as exc:  # noqa: BLE001 — the user's code, any failure
                raise PineConversionError(
                    f"Generated strategy does not import: {exc.__class__.__name__}: {exc}"
                ) from exc

            failures: List[str] = []
            for attr in list(module.__dict__.values()):
                if not (isinstance(attr, type) and issubclass(attr, Strategy)):
                    continue
                if attr.__module__ != modname:
                    continue  # the imported Strategy base itself
                result = conformance_errors(attr)
                if not result.ok:
                    failures.extend(result.errors)
                _REGISTRY.pop(str(getattr(attr, "name", "")) or preview_key, None)
            if failures:
                raise PineConversionError(
                    "Generated strategy would be refused by the plugin loader: "
                    + "; ".join(sorted(set(failures)))
                )
        finally:
            sys.modules.pop(modname, None)
            if held is not None and _REGISTRY.get(preview_key) is None:
                _REGISTRY[preview_key] = held

    def _sanitize_class_name(self, name: str) -> str:
        """Convert 'EMA Cross' → 'EmaCross' and keep 'MyCustomStrategy' as typed.

        ``word.capitalize()`` lower-cases everything after the first letter, so
        a PascalCase name from the Strategy Builder's custom-name field came out
        as "Mycustomstrategy". A word that is already mixed case is a legal
        identifier the user chose — leave it alone (apart from the first letter).
        """
        clean = re.sub(r"[^a-zA-Z0-9\s]", "", name)
        parts = []
        for word in clean.split():
            if word.isupper() or word.islower():
                parts.append(word.capitalize())
            else:
                parts.append(word[0].upper() + word[1:])
        return "".join(parts)

    def _extract_indicators_list(self, ast: Dict) -> list[str]:
        """Extract list of indicator names used."""
        indicators = []
        for statement in ast.get("statements", []):
            # Check both top-level indicator calls and those inside if statements
            if statement.get("type") == "indicator_call":
                namespace = statement.get("namespace", "ta")
                func = statement.get("function", "")
                indicators.append(f"{namespace}.{func}")
            elif statement.get("type") == "if_statement":
                # Check inside if/else bodies
                for body_list in [statement.get("body", []), statement.get("else_body", [])]:
                    for inner in body_list:
                        if inner.get("type") == "indicator_call":
                            namespace = inner.get("namespace", "ta")
                            func = inner.get("function", "")
                            indicators.append(f"{namespace}.{func}")
        return sorted(set(indicators))

    def _has_long_trades(self, ast: Dict) -> bool:
        """Check if strategy has long entry signals."""
        for statement in ast.get("statements", []):
            if statement.get("function") == "strategy.order":
                if statement.get("is_long"):
                    return True
                continue
            if statement.get("type") == "if_statement":
                for inner in statement.get("body", []):
                    if inner.get("type") == "strategy_call":
                        if (
                            inner["function"] == "strategy.entry"
                            and "long" in inner.get("direction", "")
                        ):
                            return True
        return False

    def _has_short_trades(self, ast: Dict) -> bool:
        """Check if strategy has short entry signals."""
        for statement in ast.get("statements", []):
            if statement.get("function") == "strategy.order":
                if not statement.get("is_long"):
                    return True
                continue
            if statement.get("type") == "if_statement":
                for inner in statement.get("body", []):
                    if inner.get("type") == "strategy_call":
                        if (
                            inner["function"] == "strategy.entry"
                            and "short" in inner.get("direction", "")
                        ):
                            return True
        return False

    def _estimate_complexity(self, ast: Dict) -> str:
        """Estimate strategy complexity (simple/medium/complex)."""
        indicators = len(self._extract_indicators_list(ast))
        conditions = sum(
            1
            for s in ast.get("statements", [])
            if s.get("type") == "if_statement"
        )

        score = indicators * 2 + conditions
        if score < 5:
            return "simple"
        elif score < 15:
            return "medium"
        return "complex"

    def _condition_text(self, cond: Dict) -> str:
        """Render a parsed Pine condition as a short readable phrase."""
        if not isinstance(cond, dict):
            return str(cond) if cond else ""
        ctype = cond.get("type", "")
        if ctype == "function_call":
            args = ", ".join(str(a) for a in cond.get("args", []))
            return f"{cond.get('namespace', '')}.{cond.get('function', '')}({args})"
        if ctype == "change_flip":
            return (f"{cond.get('variable', '')} flips "
                    f"{cond.get('operator', '')} {cond.get('value', '')}")
        if ctype == "comparison":
            return (f"{cond.get('left', '')} {cond.get('operator', '')} "
                    f"{cond.get('right', '')}")
        if ctype == "identifier":
            return str(cond.get("name", ""))
        return str(ctype)

    def _extract_readable_summary(self, ast: Dict, pine_code: str) -> dict:
        """Extract a human-readable summary of a strategy from its Pine AST.

        Covers the issues.txt S1 requirements: entry criteria, entry strike
        (options), take profit and stop loss. Any field the user left out is
        flagged in ``missing`` (computed server-side) so the UI and the API
        agree on what is absent. Strike/expiry are only required when the
        script looks option-related — an equity strategy has no strike, and
        requiring one used to block every equity Save.
        """
        entry = exit_ = take_profit = stop_loss = entry_strike = expiry = None

        # Variable → expression text, so `stop=sl_price` can be shown as the
        # actual formula ("close * 0.98") instead of a bare variable name.
        var_text: Dict[str, str] = {}
        for stmt in ast.get("statements", []):
            stype = stmt.get("type")
            if stype == "assignment":
                var_text[stmt.get("name", "")] = str(stmt.get("value", ""))
            elif stype == "indicator_call":
                var_text[stmt.get("var_name", "")] = (
                    f"{stmt.get('namespace', 'ta')}.{stmt.get('function', '')}("
                    + ", ".join(str(a) for a in stmt.get("args", []))
                    + ")"
                )

        def resolve(token: str, depth: int = 0) -> str:
            tok = str(token).strip()
            if depth < 3 and tok in var_text:
                return resolve(var_text[tok], depth + 1)
            return tok

        def all_calls(kind: str):
            """Yield (enclosing if-condition text | None, call statement)."""
            for stmt in ast.get("statements", []):
                if stmt.get("type") == "strategy_call" and stmt.get("function") == kind:
                    yield None, stmt
                elif stmt.get("type") == "if_statement":
                    for inner in list(stmt.get("body", [])) + list(stmt.get("else_body", [])):
                        if inner.get("type") == "strategy_call" and inner.get("function") == kind:
                            yield self._condition_text(stmt.get("condition")), inner

        # 1. Entry criteria: the condition(s) feeding strategy.entry.
        for cond, call in all_calls("strategy.entry"):
            entry = entry or cond or call.get("direction", "")

        # 1b. strategy.order(..., when=...) scripts: the when-condition is the
        #     entry criteria (resolved one level so entry_long reads as its
        #     isBuyValid/opentrades formula, not a bare variable name).
        if entry is None:
            for stmt in ast.get("statements", []):
                if stmt.get("function") == "strategy.order":
                    when = str(stmt.get("when", "")).strip()
                    if when:
                        entry = resolve(when)
                        break  # first order is the primary direction

        # 2. Exit: strategy.close triggers a reversal/exits.
        for cond, call in all_calls("strategy.close"):
            exit_ = exit_ or cond or call.get("name", "")

        # 3. Take profit / stop loss — prefer strategy.exit named args
        #    (limit/profit → TP, stop/loss → SL); fall back to the *_tp /
        #    *_sl variable-name convention for older scripts.
        for _cond, call in all_calls("strategy.exit"):
            params = call.get("exit_params", {}) or {}
            tp = params.get("limit") or params.get("profit")
            sl = params.get("stop") or params.get("loss")
            if tp and take_profit is None:
                take_profit = f"limit = {resolve(tp)}"
            if sl and stop_loss is None:
                stop_loss = f"stop = {resolve(sl)}"
        for stmt in ast.get("statements", []):
            if stmt.get("type") not in ("indicator_call", "assignment"):
                continue
            var = stmt.get("var_name") or stmt.get("name") or ""
            if take_profit is None and ("take_profit" in var or "takeprofit" in var
                                        or var.endswith("tp") or var.endswith("tp_price")):
                take_profit = f"{var} = {resolve(var)}"
            if stop_loss is None and ("stop_loss" in var or "stoploss" in var
                                      or var.endswith("sl") or var.endswith("sl_price")):
                stop_loss = f"{var} = {resolve(var)}"

        # 4. Option strike / expiry detection:
        # Check for relative moneyness (ATM, ATM+1, ATM+2, ATM-1, ITM, OTM),
        # option type (CE, PE, CE+PE), and expiry (current week, next week, current month).
        moneyness_pat = re.search(
            r"(?i)\b(ATM[+-]\d+|ITM[+-]\d+|OTM[+-]\d+)\b", pine_code
        )
        if not moneyness_pat:
            moneyness_pat = re.search(
                r"(?i)\b(ATM|ITM|OTM)\b", pine_code
            )
        opt_type_pat = re.search(
            r"(?i)\b(CE\s*\+\s*PE|CE|PE|Call|Put)\b", pine_code
        )
        expiry_pat = re.search(
            r"(?i)\b(current\s*week(?:\s*dt\.?)?|next\s*week(?:\s*dt\.?)?|"
            r"current\s*month(?:\s*dt\.?)?|next\s*month(?:\s*dt\.?)?|monthly|weekly)\b",
            pine_code,
        )

        opt_moneyness = moneyness_pat.group(1).upper() if moneyness_pat else None
        opt_type = None
        if opt_type_pat:
            t = opt_type_pat.group(1).upper()
            if "CALL" in t:
                opt_type = "CE"
            elif "PUT" in t:
                opt_type = "PE"
            elif "+" in t:
                opt_type = "Both (CE+PE)"
            else:
                opt_type = t

        opt_expiry = None
        if expiry_pat:
            e = expiry_pat.group(1).lower()
            if "next" in e and "month" in e:
                opt_expiry = "next month dt."
            elif "current" in e and "month" in e or "monthly" in e:
                opt_expiry = "current month dt."
            elif "next" in e and "week" in e:
                opt_expiry = "next week dt."
            elif "current" in e and "week" in e or "weekly" in e:
                opt_expiry = "current week dt."

        # If both moneyness and option type are detected:
        if opt_moneyness and opt_type:
            exp_suffix = f" expiry {opt_expiry}" if opt_expiry else ""
            entry_strike = f"{opt_moneyness} {opt_type}{exp_suffix}".strip()
            if opt_expiry:
                expiry = opt_expiry
        else:
            # Fall back to numeric strike literals near words strike/expiry
            for pat in re.finditer(
                r"(?i)(\bstrike\b|expiry)[^;]{0,80}?(\d{1,3}(?:\.\d{1,2})?)", pine_code
            ):
                keyword = pat.group(1).lower()
                if entry_strike is None and "strike" in keyword:
                    entry_strike = pat.group(2)
                if expiry is None and "expiry" in keyword:
                    expiry = pat.group(2)

        # Heuristic option-ness: a script that talks about strikes/CE/PE/options/straddles
        # must state its strike/expiry; equity scripts are not blocked by fields they do not have.
        is_options = bool(
            entry_strike is not None
            or opt_moneyness is not None
            or opt_type is not None
            or opt_expiry is not None
            or re.search(
                r"(?i)\b(strike|expiry|CE|PE|call|put|options?|straddle|strangle)\b",
                pine_code,
            )
        )

        missing: list[str] = []
        if not entry:
            missing.append("entry")
        if not take_profit:
            missing.append("take profit")
        if not stop_loss:
            missing.append("stop loss")
        if is_options:
            if not entry_strike:
                missing.append("entry strike")
            if not expiry:
                missing.append("expiry")

        return {
            "entry": entry,
            "exit": exit_,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "entry_strike": entry_strike,
            "expiry": expiry,
            "opt_moneyness": opt_moneyness,
            "opt_type": opt_type,
            "opt_expiry": opt_expiry,
            "is_options": is_options,
            "missing": missing,
        }


class PineConversionError(Exception):
    """Raised when Pine Script conversion fails."""

    pass
