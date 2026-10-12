"""Pine Script v5 parser — converts Pine code to AST using regex.

Simplified regex-based parser (no Lark dependency) for the subset of Pine
Script v5 needed for strategy conversion. More robust and easier to debug
than a full grammar-based parser.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List


class PineScriptParser:
    """Parse Pine Script v5 to AST using regex patterns.

    Supports:
    - Variable declarations
    - Technical indicators (ta.ema, ta.rsi, ta.sma, ta.macd, etc.)
    - Conditional logic (if/else)
    - Crossover/crossunder detection
    - Strategy entry/exit

    Does NOT support (silently ignored):
    - plot(), plotshape(), label.new(), line.new()
    - input() → converted to class __init__ params
    - request.security() → multi-timeframe (future)
    - alertcondition()
    """

    #: Pine ta.* calls that must survive as conditions even though they are
    #: not plain indicator declarations (handled by the codegen).
    _CONDITION_FUNCS = ("crossover", "crossunder", "change")

    def parse(self, pine_code: str) -> Dict[str, Any]:
        """Parse Pine Script to AST.

        Args:
            pine_code: Raw Pine Script v5 source code

        Returns:
            Dictionary representing the AST with keys:
            - type: "script"
            - statements: list of statement dicts
        """
        statements = []

        # Keep original lines with their indentation for proper parsing.
        # Pine expressions may span several lines, so logical statements are
        # rebuilt before the line-by-line walk below.
        original_lines = self._join_continuations(pine_code.split('\n'))

        i = 0
        while i < len(original_lines):
            line = original_lines[i]
            stripped = line.split('//')[0].strip()  # Remove comments

            # Skip empty lines and version declaration
            if not stripped or stripped.startswith('//@version'):
                i += 1
                continue

            # Skip user-defined function blocks (`dirmov(len) =>` + its
            # indented body). Their internals are per-call bookkeeping; the
            # codegen inlines known families (ADX/DM) instead of transpiling
            # arbitrary Pine functions.
            func_def = re.match(r'([A-Za-z_]\w*)\(([^)]*)\)\s*=>\s*$', stripped)
            if func_def:
                i += 1
                while i < len(original_lines):
                    next_line = original_lines[i]
                    if next_line and (next_line[0] == ' ' or next_line[0] == '\t'):
                        i += 1
                    else:
                        break
                continue

            # Skip declaration/plot lines: `strategy("...")`, `indicator(...)`,
            # `plot(...)`, `bgcolor(...)` — none of them compute a signal
            # variable, and the bare-assignment regex would otherwise swallow
            # them as broken `name = "..."` statements.
            if re.match(r'(strategy|indicator|library|plot|bgcolor|barcolor|fill)\s*\(', stripped):
                i += 1
                continue

            # Parse input(): atrPeriod = input(10, "ATR Length")
            #                factor = input.float(3.0, "Factor", step = 0.01)
            input_match = re.match(
                r"(\w+)\s*=\s*input(?:\.(\w+))?\(([^)]*)\)", stripped
            )
            if input_match:
                var_name = input_match.group(1)
                kind = input_match.group(2)
                args_str = input_match.group(3)
                first_arg = args_str.split(",")[0].strip()
                default: Any = first_arg
                try:
                    if "." in first_arg:
                        default = float(first_arg)
                    else:
                        default = int(first_arg)
                except ValueError:
                    default = first_arg.strip("\"'")
                # input.float/input.int pin the type; a quoted default means str.
                if kind == "float":
                    default = float(default)
                elif kind == "int":
                    default = int(float(default))
                elif kind == "bool":
                    default = str(first_arg).lower() in ("true", "1")
                elif kind == "string":
                    default = str(default)
                # Label: first quoted string in the args
                label_match = re.search(r'"([^"]+)"', args_str)
                statements.append({
                    "type": "input",
                    "name": var_name,
                    "default": default,
                    "label": label_match.group(1) if label_match else var_name,
                })

                i += 1
                continue

            # Parse tuple destructuring: [_, direction] = ta.supertrend(f, p)
            tuple_match = re.match(r"\[([^\]]+)\]\s*=\s*(.+)", stripped)
            if tuple_match:
                names = [n.strip() for n in tuple_match.group(1).split(",")]
                expr = tuple_match.group(2).strip()
                func_match = re.fullmatch(r"(ta|math)\.(\w+)\((.*)\)", expr)
                if func_match:
                    namespace = func_match.group(1)
                    func_name = func_match.group(2)
                    args = self._parse_args(func_match.group(3))
                    # Assign each name; conditions use the LAST name (Pine
                    # convention: the direction/signal comes second).
                    for idx, name in enumerate(names):
                        statements.append({
                            "type": "indicator_call",
                            "var_name": name,
                            "namespace": namespace,
                            "function": func_name,
                            "args": args,
                            "tuple_index": idx,
                            "tuple_len": len(names),
                        })
                else:
                    for name in names:
                        statements.append({"type": "assignment", "name": name, "value": expr})

                i += 1
                continue

            # Parse declaration: fast = ta.ema(close, 12)
            decl_match = re.match(r'(\w+)\s*=\s*(.+)', stripped)
            if decl_match:
                var_name = decl_match.group(1)
                expr = decl_match.group(2).strip()

                # Bare function-call condition value (e.g. long = ta.crossover(a, b))
                bare_cond = re.match(r'ta\.(crossover|crossunder)\((.*)\)', expr)
                if bare_cond:
                    statements.append({
                        "type": "assignment",
                        "name": var_name,
                        "value": expr,
                    })

                    i += 1
                    continue

                # Check if the whole expression is an indicator call. It must
                # match in full: `math.abs(close - st) / close * 100` is an
                # expression *containing* a call, and a prefix match would
                # silently drop the trailing arithmetic (that 25% range gate
                # compared raw points, not percent).
                func_match = re.fullmatch(r'(ta|math)\.(\w+)\((.*)\)', expr)
                if func_match:
                    namespace = func_match.group(1)
                    func_name = func_match.group(2)
                    args_str = func_match.group(3)
                    args = self._parse_args(args_str)

                    statements.append({
                        "type": "indicator_call",
                        "var_name": var_name,
                        "namespace": namespace,
                        "function": func_name,
                        "args": args,
                    })
                else:
                    # Simple assignment
                    statements.append({
                        "type": "assignment",
                        "name": var_name,
                        "value": expr,
                    })

                i += 1
                continue

            # Parse if statement — Pine uses indentation, not a trailing
            # colon, so the ':' is optional here (Python-style Pine also works).
            if_match = re.match(r'if\s+(.+?)(?::\s*)?$', stripped)
            if if_match:
                condition_str = if_match.group(1).strip()
                condition = self._parse_condition(condition_str)
                # Keep the source text: a condition spanning `and`/`or` terms
                # cannot be read from the single-operator regex below, and the
                # codegen translates the whole expression itself.
                condition["raw"] = condition_str

                # Parse body (indented lines)
                body = []
                i += 1
                while i < len(original_lines):
                    next_line = original_lines[i]
                    # Check if line is indented (part of if body)
                    if next_line and (next_line[0] == ' ' or next_line[0] == '\t'):
                        body_stripped = next_line.split('//')[0].strip()
                        if body_stripped:  # Skip empty/commented lines in body
                            body_stmt = self._parse_statement(body_stripped)
                            if body_stmt:
                                body.append(body_stmt)
                        i += 1
                    else:
                        break

                # Check for else
                else_body = []
                if i < len(original_lines):
                    else_stripped = original_lines[i].split('//')[0].strip()
                    if re.match(r'else\s*:?\s*$', else_stripped):
                        i += 1
                        while i < len(original_lines):
                            next_line = original_lines[i]
                            if next_line and (next_line[0] == ' ' or next_line[0] == '\t'):
                                else_stripped = next_line.split('//')[0].strip()
                                if else_stripped:
                                    else_stmt = self._parse_statement(else_stripped)
                                    if else_stmt:
                                        else_body.append(else_stmt)
                                i += 1
                            else:
                                break

                statements.append({
                    "type": "if_statement",
                    "condition": condition,
                    "body": body,
                    "else_body": else_body,
                })
                continue

            # Parse strategy call (standalone, not in if body)
            strategy_match = re.match(r'strategy\.(entry|close|exit|order)\((.*)\)', stripped)
            if strategy_match:
                stmt = self._parse_strategy_call(
                    strategy_match.group(1), strategy_match.group(2)
                )
                if stmt:
                    statements.append(stmt)

                i += 1
                continue

            i += 1

        return {"type": "script", "statements": statements}

    # ------------------------------------------------------------------
    # Logical-line rebuild: Pine spreads one expression over several lines
    # ------------------------------------------------------------------

    @staticmethod
    def _continues(stripped: str) -> bool:
        """True when a line is not a complete Pine expression yet.

        Covers both spread styles seen in real scripts: an assignment whose
        right-hand side starts on the next line (``ceSetup =``), and a term
        ending on a boolean/arith/comparison operator or an open bracket.
        """
        if stripped.endswith("="):
            return True
        if re.search(r"\b(?:and|or|not|xor|else)\s*$", stripped):
            return True
        if re.search(r"[+\-*/<>,=&|]\s*$", stripped):
            return True
        return (
            stripped.count("(") != stripped.count(")")
            or stripped.count("[") != stripped.count("]")
        )

    @staticmethod
    def _is_function_header(stripped: str) -> bool:
        """True for a user-defined function header like ``dirmov(len) =>``."""
        return bool(re.search(r"=>\s*$", stripped))

    def _join_continuations(self, lines: List[str]) -> List[str]:
        """Merge continuation lines into single logical statements.

        Indentation of the statement's first line is preserved so the if-body
        walk below still recognises block contents.
        """
        merged: List[str] = []
        buf: str | None = None
        indent = ""
        for raw in lines:
            content = raw.split("//")[0].strip()
            if not content:
                if buf is None:
                    merged.append(raw)  # blank / comment line between statements
                continue  # never terminates an open expression
            if buf is None:
                indent = raw[: len(raw) - len(raw.lstrip())]
                buf = content
            else:
                buf = f"{buf} {content}"
            # A function header (`dirmov(len) =>`) closes the previous
            # statement and stands alone — its body lines follow indented.
            if buf is not None and PineScriptParser._is_function_header(buf):
                merged.append(indent + buf)
                buf = None
                indent = ""
                continue
            if self._continues(buf):
                continue
            merged.append(indent + buf)
            buf = None
        if buf is not None:
            merged.append(indent + buf)
        return merged

    def _parse_strategy_call(self, func: str, args_str: str) -> Dict[str, Any] | None:
        """Parse one strategy.<entry|close|exit>(...) call into a statement.

        ``strategy.exit`` carries the take-profit / stop-loss criteria
        (``limit=``/``profit=`` and ``stop=``/``loss=`` named args), which the
        converter surfaces in the readable summary — without it a script's
        risk exits are invisible and the builder wrongly reports them missing.
        """
        if func == 'entry':
            # strategy.entry("buy", strategy.long)
            parts = [p.strip() for p in args_str.split(',')]
            name = parts[0].strip('"\'')
            direction = parts[1] if len(parts) > 1 else "strategy.long"
            return {
                "type": "strategy_call",
                "function": "strategy.entry",
                "name": name,
                "direction": direction,
            }
        if func == 'close':
            # strategy.close("buy")
            return {
                "type": "strategy_call",
                "function": "strategy.close",
                "name": args_str.strip('"\''),
            }
        if func == 'exit':
            # strategy.exit("x", "buy", stop=sl_price, limit=tp_price)
            positional: List[str] = []
            named: Dict[str, str] = {}
            for part in args_str.split(','):
                part = part.strip()
                if not part:
                    continue
                kv = re.match(r'(\w+)\s*=\s*(.+)$', part)
                if kv:
                    named[kv.group(1).lower()] = kv.group(2).strip()
                else:
                    positional.append(part.strip('"\''))
            return {
                "type": "strategy_call",
                "function": "strategy.exit",
                "name": positional[0] if positional else "",
                "from_order": positional[1] if len(positional) > 1 else "",
                "exit_params": named,
            }
        if func == 'order':
            # strategy.order("open_long", true, when=entry_long) — the entry
            # form real Pine v5 scripts use (Rob Booker ADX breakout etc.),
            # with a when= clause instead of an `if` block.
            positional: List[str] = []
            named: Dict[str, str] = {}
            for part in args_str.split(','):
                part = part.strip()
                if not part:
                    continue
                kv = re.match(r'(\w+)\s*=\s*(.+)$', part)
                if kv:
                    named[kv.group(1).lower()] = kv.group(2).strip()
                else:
                    positional.append(part.strip('"\''))
            side = positional[1].strip() if len(positional) > 1 else "true"
            when = named.get("when", "")
            return {
                "type": "strategy_call",
                "function": "strategy.order",
                "name": positional[0] if positional else "",
                "is_long": side.lower() == "true" or side.strip() == "1",
                "when": when,
            }
        return None

    def _parse_args(self, args_str: str) -> List[Any]:
        """Parse function arguments."""
        args = []
        for arg in args_str.split(','):
            arg = arg.strip()
            if not arg:
                continue

            # Try to parse as number
            try:
                if '.' in arg:
                    args.append(float(arg))
                else:
                    args.append(int(arg))
                continue
            except ValueError:
                pass

            # String literal
            if (arg.startswith('"') and arg.endswith('"')) or \
               (arg.startswith("'") and arg.endswith("'")):
                args.append(arg)
                continue

            # Variable name or keyword
            args.append(arg)

        return args

    def _parse_condition(self, condition_str: str) -> Dict[str, Any]:
        """Parse a condition expression."""
        # Check for ta.crossover(a, b)
        crossover_match = re.match(r'ta\.crossover\(([^,]+),\s*([^)]+)\)', condition_str)
        if crossover_match:
            return {
                "type": "function_call",
                "namespace": "ta",
                "function": "crossover",
                "args": [crossover_match.group(1).strip(), crossover_match.group(2).strip()],
            }

        # Check for ta.crossunder(a, b)
        crossunder_match = re.match(r'ta\.crossunder\(([^,]+),\s*([^)]+)\)', condition_str)
        if crossunder_match:
            return {
                "type": "function_call",
                "namespace": "ta",
                "function": "crossunder",
                "args": [crossunder_match.group(1).strip(), crossunder_match.group(2).strip()],
            }

        # Check for ta.change(direction) < 0 / > 0 (Supertrend flip pattern)
        change_match = re.match(
            r'ta\.change\((\w+)\)\s*(<|>)\s*(-?\d+(?:\.\d+)?)', condition_str
        )
        if change_match:
            return {
                "type": "change_flip",
                "variable": change_match.group(1),
                "operator": change_match.group(2),
                "value": change_match.group(3),
            }

        # Check for comparison: a > b
        comp_match = re.match(r'(.+)\s*(>|<|>=|<=|==|!=)\s*(.+)', condition_str)
        if comp_match:
            return {
                "type": "comparison",
                "left": comp_match.group(1).strip(),
                "operator": comp_match.group(2),
                "right": comp_match.group(3).strip(),
            }

        # Default: treat as boolean variable
        return {
            "type": "identifier",
            "name": condition_str,
        }

    def _parse_statement(self, line: str) -> Dict[str, Any] | None:
        """Parse a single statement line."""
        if not line:
            return None

        # Strategy call
        strategy_match = re.match(r'strategy\.(entry|close|exit|order)\((.*)\)', line)
        if strategy_match:
            return self._parse_strategy_call(
                strategy_match.group(1), strategy_match.group(2)
            )

        # Assignment with function call
        decl_match = re.match(r'(\w+)\s*=\s*(ta|math)\.(\w+)\((.*)\)', line)
        if decl_match:
            var_name = decl_match.group(1)
            namespace = decl_match.group(2)
            func_name = decl_match.group(3)
            args_str = decl_match.group(4)
            args = self._parse_args(args_str)

            return {
                "type": "indicator_call",
                "var_name": var_name,
                "namespace": namespace,
                "function": func_name,
                "args": args,
            }

        return None


# Keep Transformer for API compatibility (not used in regex parser)
class PineToASTTransformer:
    """Placeholder transformer for API compatibility."""
    pass
