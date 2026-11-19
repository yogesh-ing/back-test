"""Legacy synthetic-chain wiring, kept out of the driver's import graph.

Plan V3 rule 1: the driver imports only the pricing-feed interface. The
pre-feed default path (generator + BS quote provider) still exists for the
4,849-test suite, but its concrete imports live here, behind one factory.
"""

from __future__ import annotations

from typing import Any

from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)

__all__ = ["build_synthetic_quotes"]


def build_synthetic_quotes(
    generator: Any | None = None,
) -> tuple[SyntheticChainGenerator, SyntheticQuoteProvider]:
    gen = generator or SyntheticChainGenerator()
    return gen, SyntheticQuoteProvider(gen)