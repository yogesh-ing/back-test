"""Expiry handling — detection, auto-square-off, cash settlement.

Handles option positions as they approach and reach expiry:

- **Detection**: scan open positions for expiring/near-expiry contracts.
- **Auto-square-off**: close positions N minutes before expiry (default 30)
  so paper/live positions never ride into settlement unintentionally.
- **Cash settlement**: index options are cash-settled at expiry — the
  settlement value is the intrinsic value against the spot settlement price.
- **Notifications**: every action emits an alert record for the UI/logs.

Usage::

    from backtest.options.expiry import ExpiryManager, SettlementPriceProvider

    manager = ExpiryManager(broker=option_paper_broker)
    report = manager.process_expiries(
        as_of=datetime(2026, 9, 24, 14, 30),
        settlement_provider=provider,
    )
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from backtest.options.paper_trading import (
    OptionPaperBroker,
    OptionPosition,
    PositionStatus,
)

logger = logging.getLogger("backtest.options.expiry")

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Alert types
# ---------------------------------------------------------------------------

class ExpiryAlertType(Enum):
    UPCOMING_EXPIRY = "upcoming_expiry"
    AUTO_SQUARED_OFF = "auto_squared_off"
    EXPIRED_ITM = "expired_itm"
    EXPIRED_OTM = "expired_otm"
    SETTLED = "settled"
    ERROR = "error"


@dataclass
class ExpiryAlert:
    """A record of an expiry-related event, for UI display and logs."""

    alert_type: ExpiryAlertType
    message: str
    position_ids: list[str] = field(default_factory=list)
    structure_ids: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "type": self.alert_type.value,
            "message": self.message,
            "position_ids": self.position_ids,
            "structure_ids": self.structure_ids,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# Settlement price provider
# ---------------------------------------------------------------------------

class SettlementPriceProvider(Protocol):
    """Provides the spot settlement price for an underlying at expiry."""

    def get_settlement_price(self, underlying: str) -> float:
        """Return the closing/settlement spot price for the underlying."""
        ...


class StaticSettlementProvider:
    """Returns fixed settlement prices (for testing / synthetic data).

    Parameters
    ----------
    prices:
        Mapping of underlying → settlement price.
    default:
        Fallback price for unknown underlyings.
    """

    def __init__(self, prices: dict[str, float] | None = None, default: float = 0.0) -> None:
        self.prices = prices or {}
        self.default = default

    def get_settlement_price(self, underlying: str) -> float:
        return self.prices.get(underlying, self.default)


class LtpFallbackSettlementProvider:
    """Settlement price from a quote provider's LTP (NSE API or broker LTP).

    Uses the underlying's last traded price as the settlement price.
    This is the documented fallback when the official settlement price
    is not available.
    """

    def __init__(self, quote_provider: Any) -> None:
        self._quote_provider = quote_provider

    def get_settlement_price(self, underlying: str) -> float:
        quote = self._quote_provider.get_quote(underlying)
        return float(quote.get("ltp", 0.0))


# ---------------------------------------------------------------------------
# Settlement result
# ---------------------------------------------------------------------------

@dataclass
class SettlementResult:
    """Outcome of settling one position at expiry."""

    position_id: str
    trading_symbol: str
    side: str
    strike: Decimal
    option_type: str
    expiry: date | None
    quantity: int
    settlement_price: float  # spot at expiry
    intrinsic_value: float  # per unit
    realized_pnl: Decimal
    was_itm: bool
    squared_off: bool  # True if closed by auto-square-off before expiry


# ---------------------------------------------------------------------------
# Expiry manager
# ---------------------------------------------------------------------------

class ExpiryManager:
    """Detects and settles expiring option positions.

    Parameters
    ----------
    broker:
        The :class:`OptionPaperBroker` holding the positions.
    squareoff_minutes_before:
        Minutes before expiry at which auto-square-off kicks in.
        Positions found within this window are closed at market (LTP).
        Set to 0 to disable auto-square-off and ride into settlement.
    """

    def __init__(
        self,
        broker: OptionPaperBroker,
        squareoff_minutes_before: int = 30,
    ) -> None:
        self.broker = broker
        self.squareoff_minutes_before = squareoff_minutes_before
        self.alerts: list[ExpiryAlert] = []
        self.settlements: list[SettlementResult] = []

    # ------------------------------------------------------------------
    # T6.1 — Detection
    # ------------------------------------------------------------------

    def detect_expiring(
        self,
        as_of: datetime | None = None,
        minutes_threshold: int | None = None,
        include_stale: bool = False,
    ) -> list[OptionPosition]:
        """Return open positions whose expiry is within the threshold window.

        A position is "expiring" when its expiry date is today AND the
        current time is within ``threshold`` minutes of 15:30 (market close).

        Stale positions (expiry already passed) are excluded by default —
        they belong to :meth:`settle_expired` (cash settlement), not
        market square-off.  Pass ``include_stale=True`` to also list them.

        Parameters
        ----------
        as_of:
            Current timestamp (defaults to now).
        minutes_threshold:
            Override the manager's square-off window for this scan.
        include_stale:
            Also return positions whose expiry date has passed.
        """
        now = as_of or datetime.utcnow()
        threshold = (
            minutes_threshold
            if minutes_threshold is not None
            else self.squareoff_minutes_before
        )

        expiring: list[OptionPosition] = []
        for pos in self.broker.get_open_positions():
            if pos.expiry is None:
                continue
            if self._is_expiring(pos.expiry, now, threshold, include_stale):
                expiring.append(pos)
        return expiring

    @staticmethod
    def _is_expiring(
        expiry: date,
        now: datetime,
        threshold_minutes: int,
        include_stale: bool = False,
    ) -> bool:
        """True when the position's expiry is today within the square-off window."""
        today = now.date()
        if expiry < today:
            return include_stale  # stale — cash settlement territory
        if expiry == today:
            # Market closes at 15:30 IST; square off N minutes before
            market_close = datetime.combine(expiry, time(15, 30))
            cutoff = market_close - _timedelta_minutes(threshold_minutes)
            return now >= cutoff
        return False

    # ------------------------------------------------------------------
    # T6.2 — Auto square off
    # ------------------------------------------------------------------

    def auto_square_off(
        self,
        quote_provider: Any,
        as_of: datetime | None = None,
    ) -> list[SettlementResult]:
        """Close all positions inside the square-off window at market.

        Uses the quote provider's LTP as the exit price (market order
        simulation).  Emits ``AUTO_SQUARED_OFF`` alerts.
        """
        now = as_of or datetime.utcnow()
        results: list[SettlementResult] = []

        expiring = self.detect_expiring(as_of=now)
        if not expiring:
            return results

        # Group by structure so multi-leg structures close atomically
        by_structure: dict[str, list[OptionPosition]] = {}
        for pos in expiring:
            by_structure.setdefault(pos.structure_id, []).append(pos)

        for structure_id, positions in by_structure.items():
            try:
                before_pnl = sum(
                    (p.realized_pnl for p in positions), ZERO
                )
                self.broker.close_structure(structure_id, quote_provider, now)
                after_pnl = sum((p.realized_pnl for p in positions), ZERO)
                structure_pnl = after_pnl - before_pnl

                for pos in positions:
                    result = SettlementResult(
                        position_id=pos.position_id,
                        trading_symbol=pos.trading_symbol,
                        side=pos.side,
                        strike=pos.strike,
                        option_type=pos.option_type,
                        expiry=pos.expiry,
                        quantity=pos.quantity,
                        settlement_price=float(pos.current_price),
                        intrinsic_value=0.0,
                        realized_pnl=pos.realized_pnl,
                        was_itm=False,
                        squared_off=True,
                    )
                    results.append(result)
                    self.settlements.append(result)

                self.alerts.append(ExpiryAlert(
                    alert_type=ExpiryAlertType.AUTO_SQUARED_OFF,
                    message=(
                        f"Auto-squared off {len(positions)} leg(s) of structure "
                        f"{structure_id[:8]} — P&L ₹{structure_pnl:,.2f}"
                    ),
                    position_ids=[p.position_id for p in positions],
                    structure_ids=[structure_id],
                    details={"pnl": str(structure_pnl)},
                    timestamp=now,
                ))
                logger.info(
                    "[expiry] Auto-squared off structure=%s legs=%d pnl=₹%.2f",
                    structure_id[:8], len(positions), float(structure_pnl),
                )

            except Exception as e:
                self.alerts.append(ExpiryAlert(
                    alert_type=ExpiryAlertType.ERROR,
                    message=f"Auto-square-off failed for {structure_id[:8]}: {e}",
                    structure_ids=[structure_id],
                    timestamp=now,
                ))
                logger.error("[expiry] Square-off failed: %s", e)

        return results

    # ------------------------------------------------------------------
    # T6.3 + T6.4 — Cash settlement
    # ------------------------------------------------------------------

    def settle_expired(
        self,
        settlement_provider: SettlementPriceProvider,
        as_of: datetime | None = None,
    ) -> list[SettlementResult]:
        """Cash-settle all positions whose expiry date has passed.

        For each expired position:

        - intrinsic value = |spot - strike| if ITM else 0
        - long ITM position: receives intrinsic × units
        - short ITM position: pays intrinsic × units
        - OTM positions: expire worthless (P&L = premium already paid/received)

        Emits ``EXPIRED_ITM`` / ``EXPIRED_OTM`` / ``SETTLED`` alerts.
        """
        now = as_of or datetime.utcnow()
        results: list[SettlementResult] = []
        settled_structure_ids: set[str] = set()

        for pos in list(self.broker._positions.values()):
            if pos.status != PositionStatus.OPEN:
                continue
            if pos.expiry is None or pos.expiry >= now.date():
                continue  # not expired yet

            spot = settlement_provider.get_settlement_price(pos.underlying)
            intrinsic = self._intrinsic_value(
                spot, float(pos.strike), pos.option_type
            )
            units = pos.total_quantity
            was_itm = intrinsic > 0

            if was_itm:
                if pos.is_long:
                    cash = Decimal(str(intrinsic)) * Decimal(str(units))
                    self.broker.available_cash += cash
                    pnl = cash  # long ITM receives intrinsic (premium was already debited)
                else:
                    cash = Decimal(str(intrinsic)) * Decimal(str(units))
                    self.broker.available_cash -= cash
                    pnl = -cash  # short ITM pays intrinsic
            else:
                pnl = ZERO  # OTM — expires worthless

            pos.realized_pnl = pnl
            pos.status = PositionStatus.EXPIRED
            pos.closed_at = now
            pos.current_price = Decimal(str(intrinsic))
            settled_structure_ids.add(pos.structure_id)

            result = SettlementResult(
                position_id=pos.position_id,
                trading_symbol=pos.trading_symbol,
                side=pos.side,
                strike=pos.strike,
                option_type=pos.option_type,
                expiry=pos.expiry,
                quantity=pos.quantity,
                settlement_price=spot,
                intrinsic_value=intrinsic,
                realized_pnl=pnl,
                was_itm=was_itm,
                squared_off=False,
            )
            results.append(result)
            self.settlements.append(result)

            self.alerts.append(ExpiryAlert(
                alert_type=(
                    ExpiryAlertType.EXPIRED_ITM if was_itm
                    else ExpiryAlertType.EXPIRED_OTM
                ),
                message=(
                    f"{pos.trading_symbol} expired "
                    f"{'ITM' if was_itm else 'OTM'} — "
                    f"spot {spot:,.2f} vs strike {pos.strike} — "
                    f"P&L ₹{pnl:,.2f}"
                ),
                position_ids=[pos.position_id],
                details={
                    "settlement_price": spot,
                    "intrinsic_value": intrinsic,
                    "pnl": str(pnl),
                },
                timestamp=now,
            ))
            logger.info(
                "[expiry] Settled %s: spot=%.2f intrinsic=%.2f pnl=₹%.2f",
                pos.trading_symbol, spot, intrinsic, float(pnl),
            )

        if results:
            self.alerts.append(ExpiryAlert(
                alert_type=ExpiryAlertType.SETTLED,
                message=f"Settled {len(results)} expired position(s)",
                position_ids=[r.position_id for r in results],
                timestamp=now,
            ))

        # Gap G4.2: let the broker fire its closed observers (persistence
        # stamps these rows 'expired') once a structure has no open legs.
        for structure_id in settled_structure_ids:
            self.broker._notify_settlement(structure_id)

        return results

    @staticmethod
    def _intrinsic_value(spot: float, strike: float, option_type: str) -> float:
        """Intrinsic value at expiry: |spot - strike| if ITM, else 0."""
        if option_type == "CE":
            return max(0.0, spot - strike)
        elif option_type == "PE":
            return max(0.0, strike - spot)
        return 0.0

    # ------------------------------------------------------------------
    # Main processing entry point
    # ------------------------------------------------------------------

    def process_expiries(
        self,
        quote_provider: Any,
        settlement_provider: SettlementPriceProvider,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Run the full expiry pipeline: square-off first, then settle.

        Parameters
        ----------
        quote_provider:
            For auto-square-off fill prices (LTP).
        settlement_provider:
            For cash settlement spot prices.
        as_of:
            Current timestamp (defaults to now).

        Returns
        -------
        Summary dict with counts and P&L.
        """
        squared_off = self.auto_square_off(quote_provider, as_of)
        settled = self.settle_expired(settlement_provider, as_of)

        total_pnl = sum(
            (r.realized_pnl for r in squared_off + settled), ZERO
        )
        return {
            "squared_off_count": len(squared_off),
            "settled_count": len(settled),
            "total_pnl": total_pnl,
            "alerts": [a.to_dict() for a in self.alerts],
        }

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def get_alerts(self, alert_type: ExpiryAlertType | None = None) -> list[ExpiryAlert]:
        """Return alerts, optionally filtered by type."""
        if alert_type is None:
            return list(self.alerts)
        return [a for a in self.alerts if a.alert_type == alert_type]

    def clear_alerts(self) -> None:
        self.alerts.clear()


def _timedelta_minutes(minutes: int) -> Any:
    """Small helper to avoid importing timedelta at module top twice."""
    from datetime import timedelta
    return timedelta(minutes=minutes)
