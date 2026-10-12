"""Pine Script v5 to Platform Strategy converter.

Pure code transpiler (no LLM in runtime path):
    Pine Script → AST Parser → Semantic Analyzer → Code Generator → Validator → Plugin Strategy

Supports Pine Script v5 subset:
- Variable declarations and assignments
- Technical indicators (ta.ema, ta.rsi, ta.sma, ta.macd, etc.)
- Conditional logic (if/else)
- Crossover/crossunder detection
- Strategy entry/exit

Ignores (silently):
- plot(), plotshape(), label.new(), line.new()
- input() → converted to class __init__ params
- request.security() → multi-timeframe (future)
- alertcondition()
"""

from .parser import PineScriptParser, PineToASTTransformer
from .codegen import PineCodeGenerator
from .converter import PineScriptConverter, PineConversionError
from .validator import ConvertedStrategyValidator

__all__ = [
    "PineScriptParser",
    "PineToASTTransformer",
    "PineCodeGenerator",
    "PineScriptConverter",
    "PineConversionError",
    "ConvertedStrategyValidator",
]
