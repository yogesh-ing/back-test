"""Human context for every alert type — the alert-detail modal's copy.

Educational, not prescriptive (PRD UX principles): "what this means" and
"typical responses" describe the situation and the options traders commonly
weigh. The platform never recommends closing a specific position, and there
are no action buttons behind this text.

``section`` is the Risk Board anchor the "View details" deep link scrolls to.
"""

from __future__ import annotations

from typing import Any, Dict

from backtest.alerts.types import AlertType

CATALOG: Dict[str, Dict[str, Any]] = {
    AlertType.PORTFOLIO_GAMMA_CRITICAL.value: {
        "title": "Portfolio gamma critical",
        "section": "pi-greeks",
        "what_it_means": [
            "The book is net short gamma: every move in the underlying pushes "
            "delta against you, so losses accelerate the further the market travels.",
            "Short-gamma P&L is not linear — a 2% move costs roughly four times "
            "what a 1% move does on the convexity term alone.",
            "Short gamma usually comes paired with positive theta: the book is "
            "being paid time decay to carry this risk.",
        ],
        "typical_responses": [
            "Reduce the largest short-gamma structure (see contributors).",
            "Buy protective options (wings) to add positive gamma.",
            "Size new short-premium entries smaller until gamma is back inside the limit.",
            "Monitor closely without acting — e.g. when expiry decay is the intended trade.",
        ],
    },
    AlertType.PORTFOLIO_DELTA_WARNING.value: {
        "title": "Portfolio delta warning",
        "section": "pi-greeks",
        "what_it_means": [
            "The book has a large directional bias: it behaves like a sizeable "
            "long (positive delta) or short (negative delta) position in the market.",
            "Several strategies may be leaning the same way without any single one "
            "looking large on its own.",
        ],
        "typical_responses": [
            "Check whether the bias is intended (a directional view) or accidental.",
            "Offset with an opposite-delta position or reduce the biggest contributor.",
            "Monitor — delta drifts with the market, so the breach may fade on its own.",
        ],
    },
    AlertType.CONCENTRATION_HIGH.value: {
        "title": "Concentration high",
        "section": "pi-concentration",
        "what_it_means": [
            "Most of the book's gross exposure sits in one underlying — "
            "\"different\" strategies are really one bet.",
            "A single adverse move or event in that underlying hits every "
            "strategy that trades it at the same time.",
        ],
        "typical_responses": [
            "Deploy new capital into other underlyings rather than this one.",
            "Reduce the largest position in the concentrated underlying.",
            "Accept it knowingly (e.g. index-only book) and watch the other limits.",
        ],
    },
    AlertType.STRIKE_CLUSTERING.value: {
        "title": "Strike clustering",
        "section": "pi-concentration",
        "what_it_means": [
            "Several positions share the same strike. Exiting them together "
            "competes for the same liquidity, which widens fills in a fast market.",
            "Pin risk near expiry concentrates at that one price level.",
        ],
        "typical_responses": [
            "Stagger strikes on new entries.",
            "Stagger exits instead of closing everything at once.",
            "No action if the positions are small relative to the strike's volume.",
        ],
    },
    AlertType.VIX_REGIME_CHANGE.value: {
        "title": "Volatility regime change",
        "section": "pi-regime",
        "what_it_means": [
            "Market volatility has moved into a different band. Strategies tuned "
            "for one regime can behave very differently in another.",
            "Rising volatility hurts short-premium books (short vega) and helps "
            "long-option books; falling volatility does the reverse.",
        ],
        "typical_responses": [
            "Review strategies flagged unfavourable for the new regime.",
            "Subscribed strategies decide for themselves (pause, resize or ignore).",
            "Wait for confirmation — the detector uses hysteresis, but regimes can revert.",
        ],
    },
    AlertType.OI_ANOMALY.value: {
        "title": "Open-interest anomaly",
        "section": "pi-regime",
        "what_it_means": [
            "Open interest at one strike changed several times faster than its "
            "recent average — large participants may be building or unwinding.",
            "Heavy OI build-up can act as a magnet or a wall for price near expiry.",
        ],
        "typical_responses": [
            "Check whether any open position sits at or near that strike.",
            "Treat as context, not a signal — OI does not say who is long or short.",
        ],
    },
    AlertType.CORRELATION_SPIKE.value: {
        "title": "Strategy correlation spike",
        "section": "pi-correlation",
        "what_it_means": [
            "Two strategies' P&L are moving almost in lock-step — the "
            "diversification the book appears to have is not there right now.",
            "Correlations tend to rise exactly in stressed markets, when they hurt most.",
        ],
        "typical_responses": [
            "Treat the pair as one risk unit when sizing.",
            "Pause or shrink one of the pair if both carry the same exposure.",
            "Monitor — short samples produce noisy correlations.",
        ],
    },
    AlertType.LIQUIDITY_DRY_UP.value: {
        "title": "Liquidity dry-up",
        "section": "pi-regime",
        "what_it_means": [
            "The bid-ask spread on a contract is far wider than its recent "
            "average; market orders there will fill noticeably worse.",
        ],
        "typical_responses": [
            "Prefer limit orders on that contract for now.",
            "Delay non-urgent entries/exits until spreads normalise.",
        ],
    },
    AlertType.DATA_FEED_STALE.value: {
        "title": "Data feed stale",
        "section": "pi-greeks",
        "what_it_means": [
            "Running strategies have not received a new bar recently. Marks, "
            "Greeks and stops are working off stale prices.",
            "This is a system issue, not a market signal.",
        ],
        "typical_responses": [
            "Check the broker connection / session status (top-right).",
            "Check the feed-quality monitor (/api/broker/feed-quality).",
            "Consider pausing runners until the feed recovers.",
        ],
    },
}


def context_for(alert_type: str) -> Dict[str, Any]:
    """Catalog entry for one type (a generic stub for unknown types)."""
    return CATALOG.get(
        str(alert_type),
        {
            "title": str(alert_type).replace("_", " ").title(),
            "section": "pi-greeks",
            "what_it_means": [],
            "typical_responses": [],
        },
    )
