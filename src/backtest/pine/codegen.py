"""Code generator — converts Pine AST to Python Strategy code.

Emits the platform-correct plugin pattern: a vectorized ``entries()``
(pd.Series of booleans aligned to the candles) which the Strategy base
turns into ``generate_signals()`` via ``_signals_from_entries_exits``.
All Pine ``ta.*`` functions used are emitted as self-contained numpy
helpers, so the generated file has no dependency on indicator methods
that ``Strategy`` does not actually provide.

Known limitations (documented in generated header):
- The platform equity model is long-only; Pine short entries are OR-ed
  into the entries series and flagged in a comment.
- ``plot()``/``label()``/``alertcondition()`` are ignored (no visualization).
- ``request.security()`` (multi-timeframe) is not supported.
"""

from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional


class PineCodeGenerator:
    """Generate Python Strategy code from Pine AST."""

    #: Pine ta.* function → (python helper name, arg order transform)
    SUPPORTED_FUNCS = {
        "ema": "_ema",
        "sma": "_sma",
        "rsi": "_rsi",
        "atr": None,  # special: _atr(high, low, close, period)
        "supertrend": None,  # special: _supertrend(high, low, close, period, factor)
        "change": "_changed",
        "crossover": "_cross_above",
        "crossunder": "_cross_below",
        "highest": "_highest",
        "lowest": "_lowest",
    }

    # Standard OHLC series names usable as function arguments.
    SERIES_NAMES = {"open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"}

    def generate(self, pine_ast: Dict, strategy_name: str) -> str:
        """Generate a complete, loadable plugin module for ``strategy_name``."""
        env = self._collect_env(pine_ast)
        inputs = [s for s in pine_ast.get("statements", []) if s.get("type") == "input"]
        logic_lines = self._generate_logic(pine_ast, env)
        calc_lines = self._generate_calculations(pine_ast, env)
        helpers = self._required_helpers(pine_ast, env)
        params_block = self._generate_params(inputs)

        class_code = f'''class {strategy_name}(Strategy):
    """
    Auto-generated from Pine Script v5 by the Strategy Builder.

    Entries are OR-ed across all Pine entry conditions; the platform
    equity model is long-only, so short entries are folded into the
    same entries series (flagged below).
    """

    name = "{strategy_name.lower()}"
    description = "Imported from Pine Script v5 via Strategy Builder"
    version = "1.0"
    author = "Pine Converter"

{params_block}

    def entries(self, df: pd.DataFrame) -> pd.Series:
        open = df["open"].values
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        volume = df["volume"].values
        hl2 = (high + low) / 2.0
        hlc3 = (high + low + close) / 3.0
        ohlc4 = (open + high + low + close) / 4.0
        n = len(close)

        # Pine inputs → instance params (bound by Strategy.__init__)
{self._generate_param_bindings(inputs)}
        # Calculations, in Pine source order (indicators then signal variables)
{calc_lines}
        # Entry conditions (long-only platform: shorts folded in)
        long_entries = np.zeros(n, dtype=bool)
        short_entries = np.zeros(n, dtype=bool)
{logic_lines}
        # The base class turns entries/exits into generate_signals().
        return pd.Series(long_entries | short_entries, index=df.index)
'''

        return (
            '"""\n'
            "Auto-generated from Pine Script v5 by the Strategy Builder.\n"
            "\n"
            "Long-only platform: Pine short entries are OR-ed into entries.\n"
            "Plot/draw statements ignored (no visualization).\n"
            '"""\n\n'
            "from backtest.strategy.base import Strategy\n\n"
            "import numpy as np\n"
            "import pandas as pd\n\n\n"
            + helpers
            + "\n\n"
            + class_code
        )

    # ------------------------------------------------------------------
    # Environment: which Pine names become computed variables in entries()
    # ------------------------------------------------------------------

    def _collect_env(self, ast_: Dict) -> Dict[str, str]:
        """Pine name → its source expression (used for helper detection)."""
        env: Dict[str, str] = {}
        for st in ast_.get("statements", []):
            if st.get("type") == "assignment":
                env[st["name"]] = st["value"]
            elif st.get("type") == "indicator_call":
                env[st["var_name"]] = (
                    f"{st.get('namespace', 'ta')}.{st['function']}("
                    + ", ".join(str(a) for a in st.get("args", []))
                    + ")"
                )
        return env

    def _known_names(self, ast_: Dict) -> set:
        """Every Pine name the generated entries() defines or binds."""
        names = set(self.SERIES_NAMES) | {"n", "np", "pd"}
        for st in ast_.get("statements", []):
            stype = st.get("type")
            if stype in ("assignment", "input"):
                names.add(st["name"])
            elif stype == "indicator_call":
                names.add(st["var_name"])
        return names

    # ------------------------------------------------------------------
    # Expression translation
    # ------------------------------------------------------------------

    #: Pine boolean/comparison operators → numpy elementwise equivalents.
    _BINOPS = {
        ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
        ast.Mod: "%", ast.Pow: "**",
    }
    _CMPOPS = {
        ast.Gt: ">", ast.Lt: "<", ast.GtE: ">=", ast.LtE: "<=",
        ast.Eq: "==", ast.NotEq: "!=",
    }

    def _translate_expr(self, text: str, known: set, depth: int = 0) -> Optional[str]:
        """Translate one Pine expression into numpy-compatible Python.

        Pine's ``and``/``or``/``not`` are Python keywords, so a spread
        condition like ``close > ema5 and bullST and stWithin25`` would
        otherwise emit `and` on boolean **arrays** — a runtime
        "truth value is ambiguous". Parsing the expression (Pine's
        arithmetic/boolean subset is valid Python syntax) and rebuilding it
        with ``&``/``|``/``~`` plus ``_hist()`` history offsets is what makes
        multi-term conditions convert at all.

        Returns None when the expression uses syntax this converter cannot
        express, so callers can say so instead of emitting dead code.
        """
        src = str(text).strip()
        if not src:
            return None
        try:
            tree = ast.parse(src, mode="eval")
        except SyntaxError:
            return None
        return self._translate_node(tree.body, known, depth)

    def _translate_node(self, node, known: set, depth: int) -> Optional[str]:
        if depth > 12:  # runaway / self-referencing guard
            return None

        if isinstance(node, ast.BoolOp):
            parts = []
            for value in node.values:
                part = self._translate_node(value, known, depth + 1)
                if part is None:
                    return None
                parts.append(f"({part})")
            joiner = " & " if isinstance(node.op, ast.And) else " | "
            return joiner.join(parts)

        if isinstance(node, ast.UnaryOp):
            inner = self._translate_node(node.operand, known, depth + 1)
            if inner is None:
                return None
            if isinstance(node.op, ast.Not):
                return f"(~({inner}))"
            if isinstance(node.op, ast.USub):
                return f"(-({inner}))"
            return None

        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                return None  # chained comparisons: Pine has no such form
            left = self._translate_node(node.left, known, depth + 1)
            right = self._translate_node(node.comparators[0], known, depth + 1)
            op = self._CMPOPS.get(type(node.ops[0]))
            if left is None or right is None or op is None:
                return None
            return f"({left} {op} {right})"

        if isinstance(node, ast.BinOp):
            left = self._translate_node(node.left, known, depth + 1)
            right = self._translate_node(node.right, known, depth + 1)
            op = self._BINOPS.get(type(node.op))
            if left is None or right is None or op is None:
                return None
            return f"({left} {op} {right})"

        if isinstance(node, ast.Subscript):
            # Pine's x[1] is "value one bar back", not an index.
            if isinstance(node.slice, ast.Constant):
                offset = node.slice.value
            else:  # Python < 3.9 wraps it in ast.Index
                inner = getattr(node.slice, "value", None)
                if not isinstance(inner, ast.Constant):
                    return None
                offset = inner.value
            if not isinstance(offset, int) or offset < 0:
                return None
            src = self._translate_node(node.value, known, depth + 1)
            if src is None:
                return None
            if offset == 0:
                return src
            return f"_hist({src}, {offset})"

        if isinstance(node, ast.Call):
            return self._translate_call(node, known, depth)

        if isinstance(node, ast.Name):
            return self._translate_name(node.id, known)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return "True" if node.value else "False"
            if isinstance(node.value, (int, float)):
                return repr(node.value)
            return None

        return None

    def _translate_name(self, name: str, known: set) -> str:
        if name == "na":
            return "np.nan"
        if name in ("true", "false"):
            return name.capitalize()
        if name in known:
            return name
        # Unknown identifier: emit it as-is so the preflight reports the dead
        # name instead of the converter quietly substituting `False`.
        return name

    def _translate_call(self, node: ast.Call, known: set, depth: int) -> Optional[str]:
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return None
        ns = func.value.id
        if ns not in ("ta", "math"):
            return None
        if node.keywords:
            return None  # named indicator args (e.g. ta.stdev(ddof=1))
        args: List[str] = []
        for arg in node.args:
            text = self._translate_node(arg, known, depth + 1)
            if text is None:
                return None
            args.append(text)
        return self._map_call(ns, func.attr, args)

    def _map_call(self, ns: str, func: str, args: List[str]) -> Optional[str]:
        """Map one ta./math. call to its Python helper, or None if unsupported."""
        if ns == "math":
            if func == "max":
                return f"np.maximum({', '.join(args)})" if len(args) == 2 else None
            if func == "min":
                return f"np.minimum({', '.join(args)})" if len(args) == 2 else None
            if func == "abs":
                return f"np.abs({args[0]})" if args else None
            return None

        if func == "atr" and len(args) >= 1:
            return f"_atr(high, low, close, int({args[-1]}))"
        if func == "supertrend" and len(args) >= 2:
            # Pine returns [supertrend, direction]; we need direction.
            return f"_supertrend(high, low, close, int({args[1]}), float({args[0]}))"
        if func == "crossover" and len(args) == 2:
            return f"_cross_above({args[0]}, {args[1]})"
        if func == "crossunder" and len(args) == 2:
            return f"_cross_below({args[0]}, {args[1]})"
        if func == "change" and len(args) == 1:
            return f"_changed({args[0]})"
        if func == "highest" and len(args) == 2:
            return f"_highest({args[0]}, int({args[1]}))"
        if func == "lowest" and len(args) == 2:
            return f"_lowest({args[0]}, int({args[1]}))"
        helper = self.SUPPORTED_FUNCS.get(func)
        if helper and len(args) >= 2:
            src = self._series_arg(args[0])
            return f"{helper}({src}, int({args[1]}))"
        return None

    def _series_arg(self, arg: str) -> str:
        """Map a Pine series identifier to the local series variable."""
        a = arg.strip()
        if a == "hl2":
            return "(high + low) / 2.0"
        if a == "hlc3":
            return "(high + low + close) / 3.0"
        if a == "ohlc4":
            return "(open + high + low + close) / 4.0"
        if a in self.SERIES_NAMES:
            return a
        return a  # a previously computed variable

    # ------------------------------------------------------------------
    # Logic generation
    # ------------------------------------------------------------------

    def _generate_logic(self, pine_ast: Dict, env: Dict[str, str]) -> str:
        known = self._known_names(pine_ast)
        lines: List[str] = []
        for st in pine_ast.get("statements", []):
            if st.get("type") != "if_statement":
                continue
            cond = self._translate_condition(st["condition"], known)
            for inner in st.get("body", []):
                if inner.get("type") == "strategy_call" and inner["function"] == "strategy.entry":
                    if "long" in inner.get("direction", ""):
                        lines.append(f"        long_entries |= {cond}")
                    else:
                        lines.append(
                            f"        short_entries |= {cond}"
                            "  # short folded in (long-only platform)"
                        )
                elif inner.get("type") == "strategy_call" and inner["function"] == "strategy.close":
                    lines.append(f"        # strategy.close under: {cond} (handled by exits model)")
            for inner in st.get("else_body", []):
                if inner.get("type") == "strategy_call" and inner["function"] == "strategy.entry":
                    if "long" in inner.get("direction", ""):
                        lines.append(f"        # else-branch entry skipped: {cond}")
        if not lines:
            lines.append(
                "        # No entry conditions recognised — strategy stays flat."
            )
            lines.append(
                "        # Re-check the Pine script: only `if <condition>:` +"
                " strategy.entry(...) blocks convert."
            )
        return "\n".join(lines)

    def _translate_condition(self, condition: Dict, known: set) -> str:
        """Translate an if-condition into a numpy boolean expression.

        The parser's structured form only understands a single comparison, so
        the raw source text is translated first and the structured form is the
        fallback for conditions the expression translator rejects.
        """
        if not isinstance(condition, dict):
            return "False"

        raw = str(condition.get("raw") or "").strip()
        if raw:
            translated = self._translate_expr(raw, known)
            if translated is not None:
                return translated

        ctype = condition.get("type")

        if ctype == "function_call":
            ns = condition.get("namespace", "ta")
            func = condition.get("function", "")
            args = [str(a).strip() for a in condition.get("args", [])]
            repl = self._map_call(ns, func, args)
            return repl if repl is not None else "False"

        if ctype == "comparison":
            left = self._fallback_expr(str(condition["left"]), known)
            right = self._fallback_expr(str(condition["right"]), known)
            return f"{left} {condition['operator']} {right}"

        if ctype == "change_flip":
            var = str(condition.get("variable", ""))
            op = condition.get("operator", "<")
            # Pine: ta.change(direction) < 0 → flipped to bull → long;
            #       ta.change(direction) > 0 → flipped to bear → short.
            src = var if var in known else var
            if op == "<":
                return f"_changed({src}) < 0"
            return f"_changed({src}) > 0"

        if ctype == "identifier":
            return self._fallback_expr(str(condition.get("name", "")), known)

        return "False"

    def _fallback_expr(self, text: str, known: set) -> str:
        """Translate a condition fragment, keeping the text if unsupported."""
        translated = self._translate_expr(text, known)
        return translated if translated is not None else text.strip()

    # ------------------------------------------------------------------
    # Calculations + inputs → params
    # ------------------------------------------------------------------

    def _generate_calculations(self, pine_ast: Dict, env: Dict[str, str]) -> str:
        """Emit every Pine calculation as its own variable, in source order.

        Pine assignments used to be inlined into the entry conditions, which
        left a spread condition (`ceSetup = close > ema5 and bullST and
        stWithin25`) as a bare identifier in the generated file — it imported,
        then died with `name 'ceSetup' is not defined`. Materialising them keeps
        dependencies ordered the way the script wrote them.
        """
        lines: List[str] = []
        known = self._known_names(pine_ast)
        emitted: set = set()
        for st in pine_ast.get("statements", []):
            stype = st.get("type")
            if stype == "indicator_call":
                if st["var_name"] in emitted:
                    continue
                lines.extend(self._indicator_lines(pine_ast, st, emitted))
            elif stype == "assignment":
                name = str(st.get("name", ""))
                if not name or name in emitted:
                    continue
                emitted.add(name)
                expr = self._translate_expr(st.get("value", ""), known)
                if expr is None:
                    lines.append(
                        f"        # {name}: Pine expression not convertible — skipped"
                    )
                    continue
                lines.append(f"        {name} = {expr}")
        if not lines:
            lines.append("        # No indicators to precompute")
        return "\n".join(lines)

    def _indicator_lines(self, pine_ast: Dict, st: Dict, emitted: set) -> List[str]:
        var = st["var_name"]
        func = st.get("function", "")
        args = [str(a) for a in st.get("args", [])]
        expr = self._map_call(st.get("namespace", "ta"), func, args)
        if expr is None:
            emitted.add(var)
            return [f"        # {var}: ta.{func} not supported — skipped"]
        if st.get("tuple_len") == 2 and st.get("tuple_index") == 0:
            # [line, direction] = ta.supertrend(...): the price line needs the
            # direction array, which Pine's destructuring assigns separately.
            out: List[str] = []
            partner = self._tuple_partner(pine_ast, st)
            if partner and partner not in emitted:
                emitted.add(partner)
                out.append(f"        {partner} = {expr}")
            emitted.add(var)
            out.append(
                f"        {var} = _supertrend_line(high, low, close, "
                f"int({args[1] if len(args) > 1 else 10}), "
                f"float({args[0] if args else 3.0}), {partner or 'None'})"
            )
            return out
        emitted.add(var)
        return [f"        {var} = {expr}"]

    def _tuple_partner(self, pine_ast: Dict, st: Dict) -> Optional[str]:
        """For a destructured tuple element, find the sibling variable name."""
        idx = st.get("tuple_index")
        if idx is None:
            return None
        for other in pine_ast.get("statements", []):
            if (
                other.get("type") == "indicator_call"
                and other.get("tuple_index") == (1 if idx == 0 else 0)
                and other.get("tuple_len") == st.get("tuple_len")
                and other.get("args") == st.get("args")
            ):
                return other["var_name"]
        return None

    def _generate_params(self, inputs: List[Dict]) -> str:
        if not inputs:
            return "    params: dict = {}  # Pine script declared no input() parameters"
        lines = ["    params = {"]
        for p in inputs:
            default = p.get("default", 0)
            # NB: statement dicts carry their own "type" key ("input"), so the
            # param type is inferred from the default value instead.
            if isinstance(default, float):
                ptype = "float"
            elif isinstance(default, int):
                ptype = "int"
            else:
                ptype = "str"
            lines.append(f'        "{p["name"]}": {{')
            lines.append(f'            "default": {default!r},')
            lines.append(f'            "type": "{ptype}",')
            label = p.get("label", p["name"]).replace("_", " ").title()
            lines.append(f'            "label": "{label}",')
            lines.append('            "tooltip": "Pine Script input()",')
            lines.append("        },")
        lines.append("    }")
        return "\n".join(lines)

    def _generate_param_bindings(self, inputs: List[Dict]) -> str:
        if not inputs:
            return ""
        lines = [
            f"        {p['name']} = self.{p['name']}"  # Pine input → param
            for p in inputs
        ]
        return "\n".join(lines) + "\n" if lines else ""

    # ------------------------------------------------------------------
    # Helpers: only emit what the translated code actually references
    # ------------------------------------------------------------------

    def _required_helpers(self, pine_ast: Dict, env: Dict[str, str]) -> str:
        all_exprs = list(env.values())
        for st in pine_ast.get("statements", []):
            if st.get("type") == "if_statement":
                all_exprs.append(str(st["condition"]))
        blob = " ".join(all_exprs)

        needed = set()
        for func in ("ema", "sma", "rsi", "highest", "lowest"):
            if re.search(rf"ta\.{func}\(", blob):
                needed.add(self.SUPPORTED_FUNCS[func])
        if re.search(r"ta\.atr\(", blob):
            needed.add("_atr")
        if re.search(r"ta\.supertrend\(", blob):
            needed.add("_supertrend")
        if re.search(r"ta\.change\(", blob) or "'change_flip'" in blob:
            needed.add("_changed")
        if re.search(r"ta\.crossover\(", blob):
            needed.add("_cross_above")
        if re.search(r"ta\.crossunder\(", blob):
            needed.add("_cross_below")
        if re.search(r"ta\.atr\(", blob) or re.search(r"ta\.supertrend\(", blob):
            needed.add("_tr_atr")
        # Pine history reference: buySignal[1], close[2], ...
        if re.search(r"[A-Za-z_]\w*\s*\[\s*\d+\s*\]", blob):
            needed.add("_hist")

        helpers = []
        if "_ema" in needed:
            helpers.append(
                "def _ema(src, period):\n"
                "    src = np.asarray(src, dtype=float)\n"
                "    out = np.empty_like(src)\n"
                "    alpha = 2.0 / (period + 1.0)\n"
                "    out[0] = src[0]\n"
                "    for i in range(1, len(src)):\n"
                "        out[i] = alpha * src[i] + (1.0 - alpha) * out[i - 1]\n"
                "    return out"
            )
        if "_sma" in needed:
            helpers.append(
                "def _sma(src, period):\n"
                "    src = np.asarray(src, dtype=float)\n"
                "    out = np.full(len(src), np.nan)\n"
                "    if len(src) >= period:\n"
                "        out[period - 1:] = np.convolve(\n"
                "            src, np.ones(period) / period, mode=\"valid\")\n"
                "    return out"
            )
        if "_rsi" in needed:
            helpers.append(
                "def _rsi(src, period):\n"
                "    src = np.asarray(src, dtype=float)\n"
                "    delta = np.diff(src, prepend=src[0])\n"
                "    gain = np.where(delta > 0, delta, 0.0)\n"
                "    loss = np.where(delta < 0, -delta, 0.0)\n"
                "    avg_gain = _ema(gain, period)\n"
                "    avg_loss = _ema(loss, period)\n"
                "    rs = np.divide(avg_gain, avg_loss, out=np.full(len(src), 100.0),\n"
                "                  where=avg_loss != 0)\n"
                "    return 100.0 - 100.0 / (1.0 + rs)"
            )
        if "_tr_atr" in needed:
            helpers.append(
                "def _tr_atr(high, low, close, period):\n"
                "    n = len(close)\n"
                "    tr = np.empty(n)\n"
                "    tr[0] = high[0] - low[0]\n"
                "    for i in range(1, n):\n"
                "        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]),\n"
                "                 abs(low[i] - close[i - 1]))\n"
                "    atr = np.empty(n)\n"
                "    atr[0] = tr[0]\n"
                "    for i in range(1, n):\n"
                "        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period\n"
                "    return atr"
            )
        if "_atr" in needed:
            helpers.append(
                "def _atr(high, low, close, period):\n"
                "    return _tr_atr(high, low, close, period)"
            )
        if "_supertrend" in needed:
            needed.add("_supertrend_line")
            helpers.append(
                "def _supertrend(high, low, close, period, factor):\n"
                "    \"\"\"Pine ta.supertrend direction: -1 up, +1 down.\"\"\"\n"
                "    n = len(close)\n"
                "    atr = _tr_atr(high, low, close, period)\n"
                "    hl2 = (high + low) / 2.0\n"
                "    upper = hl2 + factor * atr\n"
                "    lower = hl2 - factor * atr\n"
                "    direction = -np.ones(n, dtype=int)\n"
                "    for i in range(1, n):\n"
                "        if not (upper[i] < upper[i - 1] or close[i - 1] > upper[i - 1]):\n"
                "            upper[i] = upper[i - 1]\n"
                "        if not (lower[i] > lower[i - 1] or close[i - 1] < lower[i - 1]):\n"
                "            lower[i] = lower[i - 1]\n"
                "        if direction[i - 1] == -1:\n"
                "            direction[i] = 1 if close[i] < lower[i] else -1\n"
                "        else:\n"
                "            direction[i] = -1 if close[i] > upper[i] else 1\n"
                "    return direction"
            )
            helpers.append(
                "def _supertrend_line(high, low, close, period, factor, direction):\n"
                "    \"\"\"The supertrend price line, given a direction array.\"\"\"\n"
                "    atr = _tr_atr(high, low, close, period)\n"
                "    hl2 = (high + low) / 2.0\n"
                "    return np.where(direction == -1, hl2 - factor * atr, hl2 + factor * atr)"
            )
        if "_changed" in needed:
            helpers.append(
                "def _changed(arr):\n"
                "    arr = np.asarray(arr)\n"
                "    out = np.zeros(len(arr), dtype=arr.dtype)\n"
                "    out[1:] = arr[1:] - arr[:-1]\n"
                "    return out"
            )
        if "_cross_above" in needed:
            helpers.append(
                "def _cross_above(a, b):\n"
                "    a = np.asarray(a, dtype=float)\n"
                "    b = np.asarray(b, dtype=float)\n"
                "    out = np.zeros(len(a), dtype=bool)\n"
                "    out[1:] = (a[:-1] <= b[:-1]) & (a[1:] > b[1:])\n"
                "    return out"
            )
        if "_cross_below" in needed:
            helpers.append(
                "def _cross_below(a, b):\n"
                "    a = np.asarray(a, dtype=float)\n"
                "    b = np.asarray(b, dtype=float)\n"
                "    out = np.zeros(len(a), dtype=bool)\n"
                "    out[1:] = (a[:-1] >= b[:-1]) & (a[1:] < b[1:])\n"
                "    return out"
            )
        if "_highest" in needed:
            helpers.append(
                "def _highest(src, period):\n"
                "    return pd.Series(src).rolling(period).max().values"
            )
        if "_lowest" in needed:
            helpers.append(
                "def _lowest(src, period):\n"
                "    return pd.Series(src).rolling(period).min().values"
            )
        if "_hist" in needed:
            helpers.append(
                "def _hist(src, offset):\n"
                '    """Pine `src[offset]`: the value `offset` bars earlier.\n'
                "\n"
                "    The warm-up bars repeat the first value rather than `na`, so\n"
                '    a "new condition" test (`x and not x[1]`) cannot fire on bar 0.\n'
                '    """\n'
                "    src = np.asarray(src)\n"
                "    out = np.empty(len(src), dtype=src.dtype)\n"
                "    out[:] = src[0] if len(src) else 0\n"
                "    if offset < len(src):\n"
                "        out[offset:] = src[: len(src) - offset]\n"
                "    return out"
            )

        return "\n\n\n".join(helpers)
