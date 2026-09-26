from __future__ import annotations

from abc import ABC
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from .intent import MarketView

# ---------------------------------------------------------------------------
# Contract error
# ---------------------------------------------------------------------------


class StrategyContractError(Exception):
    """Raised when a strategy fails to meet the BaseStrategy contract.

    Used by :meth:`Strategy.validate` (Task 1.1) and swallowed-with-warning by
    the auto-discovery registry (Task 1.2) so a single bad file never crashes
    the app.
    """


# ---------------------------------------------------------------------------
# Param schema helpers
# ---------------------------------------------------------------------------

_VALID_PARAM_TYPES = {"int", "float", "bool", "str"}


def _is_schema(spec: Any) -> bool:
    """True when a ``params`` entry is a full PRD schema dict (vs. a bare default)."""
    return isinstance(spec, dict)


def _infer_type(value: Any) -> str:
    # bool must be checked before int (bool is a subclass of int)
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _type_matches(ptype: str, value: Any) -> bool:
    if ptype == "bool":
        return isinstance(value, bool)
    if ptype == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if ptype == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "str":
        return isinstance(value, str)
    return False


def _coerce_value(ptype: str | None, value: Any) -> Any:
    if ptype is None or value is None:
        return value
    try:
        if ptype == "int":
            return int(value)
        if ptype == "float":
            return float(value)
        if ptype == "bool":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if ptype == "str":
            return str(value)
    except (TypeError, ValueError):
        return value
    return value


def _humanize(key: str) -> str:
    return key.replace("_", " ").title()


def _infer_schema(value: Any) -> dict[str, Any]:
    """Build a full param schema from a bare scalar default (flat form).

    Lets legacy strategies (``params = {"period": 14}``) be described by the
    same schema API as PRD-style strategies without changing a line.
    """
    return {
        "default": value,
        "type": _infer_type(value),
        "min": None,
        "max": None,
        "label": _humanize(str(value)) if False else None,
        "tooltip": "",
    }


# ---------------------------------------------------------------------------
# Strategy base
# ---------------------------------------------------------------------------


class Strategy(ABC):
    """Base class all strategies inherit from.

    A strategy declares ``name`` (required) plus optional ``description``,
    ``version`` and ``author`` metadata. Parameters are declared on ``params``
    in one of two equivalent forms:

    * **Flat** (legacy, still supported)::

          params = {"period": 14, "lower": 30}

    * **Schema** (PRD — enables dynamic UI forms)::

          params = {
              "period": {
                  "default": 14, "min": 5, "max": 50, "type": "int",
                  "label": "RSI Period", "tooltip": "Lookback window",
              },
          }

    Either way the resolved default is bound to an instance attribute
    (``self.period``) and overridable via the constructor. Call
    :meth:`validate` to check the contract and :meth:`param_schema` /
    :meth:`default_params` to inspect parameters.
    """

    name: str = ""
    description: str = ""
    version: str = ""
    author: str = ""
    params: dict[str, Any] = {}
    stop_loss: float | None = None
    take_profit: float | None = None
    # Instrument eligibility (2026-09-22): ``None`` = trades any symbol the
    # user picks (equity defaults). A list restricts spawn to exactly these
    # underlyings — the spawn form renders a dropdown instead of free text
    # and the runner/create API enforces the same set (defense in depth).
    # Option strategies declare the indexes their chain source supports.
    eligible_instruments: list[str] | None = None
    # Portfolio Intelligence (2026-09-26). Alerts are *information*: the
    # platform never acts on them. A strategy opts in by listing alert types
    # here (or calling :meth:`subscribe_to_alerts`) and decides in
    # :meth:`on_alert` — pause entries, request an exit, or ignore.
    subscribed_alerts: tuple[str, ...] = ()
    # ``(low, high)`` India VIX range the strategy is designed for; drives the
    # Risk Board "strategy regime fit" column. ``None`` = any regime.
    regime_vix_range: tuple[float, float] | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "name", ""):
            from .registry import register

            register(cls)

    def __init__(self, **overrides: Any) -> None:
        known = set(self.params)
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"unknown strategy params: {sorted(unknown)}")

        for key, default in self.default_params().items():
            setattr(self, key, default)
        for key, value in overrides.items():
            if key in self.params:
                setattr(self, key, self._coerce_override(key, value))

    # -- portfolio alerts (information layer) ------------------------------

    def subscribe_to_alerts(self, alert_types: Any) -> list[str]:
        """Subscribe this strategy to portfolio alert types.

        ``alert_types`` is an iterable of :class:`~backtest.alerts.AlertType`
        members or their string values. Unknown types raise ``ValueError``.
        Alerts arrive via :meth:`on_alert` once the runner is registered with
        the portfolio manager (subscribing later, at runtime, re-registers).
        """
        from backtest.alerts.types import AlertType

        if isinstance(alert_types, (str, AlertType)):
            alert_types = [alert_types]
        known = {t.value for t in AlertType}
        wanted: list[str] = []
        for t in alert_types:
            value = t.value if isinstance(t, AlertType) else str(t)
            if value not in known:
                raise ValueError(
                    f"unknown alert type {value!r}; expected one of {sorted(known)}"
                )
            wanted.append(value)
        current = list(self.__dict__.get("_alert_subscriptions", []))
        for value in wanted:
            if value not in current:
                current.append(value)
        self._alert_subscriptions = current
        registrar = self.__dict__.get("_alert_registrar")
        if callable(registrar):
            registrar()
        return list(current)

    def unsubscribe_from_alerts(self, alert_types: Any = None) -> list[str]:
        """Drop some (or, with ``None``, all instance-level) subscriptions."""
        from backtest.alerts.types import AlertType

        current = list(self.__dict__.get("_alert_subscriptions", []))
        if alert_types is None:
            current = []
        else:
            if isinstance(alert_types, (str, AlertType)):
                alert_types = [alert_types]
            drop = {t.value if isinstance(t, AlertType) else str(t) for t in alert_types}
            current = [t for t in current if t not in drop]
        self._alert_subscriptions = current
        registrar = self.__dict__.get("_alert_registrar")
        if callable(registrar):
            registrar()
        return list(current)

    def alert_subscriptions(self) -> list[str]:
        """Class-level ``subscribed_alerts`` ∪ runtime subscriptions."""
        out: list[str] = []
        for t in list(self.subscribed_alerts or ()) + list(
            self.__dict__.get("_alert_subscriptions", [])
        ):
            value = getattr(t, "value", None) or str(t)
            if value not in out:
                out.append(value)
        return out

    def on_alert(self, alert_type: str, alert_data: dict[str, Any]) -> None:
        """Called when a subscribed portfolio alert fires. Default: ignore.

        ``alert_data`` carries the alert's metrics plus ``self_contribution``
        (this runner's share, or ``None``) and ``is_contributor``. Runs on the
        evaluator thread under this runner's lock — keep it fast and do not
        block. To act, set :attr:`pause_new_entries` or call
        :meth:`request_exit`; the runner executes requests through the normal
        engine path on its next bar. The platform itself never closes
        anything in response to an alert.
        """
        return None

    def on_alert_resolved(self, alert_type: str, alert_data: dict[str, Any]) -> None:
        """Called when an alert this strategy was notified about resolves
        (the condition cleared, or an event alert expired). Default: ignore.
        Typical use: lift a pause set in :meth:`on_alert`.
        """
        return None

    @property
    def pause_new_entries(self) -> bool:
        """When ``True`` the runner skips new entries (exits still run)."""
        return bool(self.__dict__.get("_pause_new_entries", False))

    @pause_new_entries.setter
    def pause_new_entries(self, value: bool) -> None:
        self._pause_new_entries = bool(value)

    def request_exit(
        self,
        fraction: float = 1.0,
        position_key: str | None = None,
        reason: str = "alert",
    ) -> None:
        """Ask the runner to close ``fraction`` of one (or every) position.

        Queued, not executed: the runner drains the queue on its next bar and
        closes through the engine (same audit trail as a stop-loss). Option
        structures close atomically — a partial fraction is refused and
        logged rather than breaking a spread's legs apart.
        """
        fraction = float(fraction)
        if not 0.0 < fraction <= 1.0:
            raise ValueError("fraction must be in (0, 1]")
        queue = self.__dict__.setdefault("_exit_requests", [])
        queue.append({"fraction": fraction, "position_key": position_key, "reason": str(reason)})

    def drain_exit_requests(self) -> list[dict[str, Any]]:
        queue = self.__dict__.get("_exit_requests") or []
        self._exit_requests = []
        return list(queue)

    # -- param access ----------------------------------------------------

    def _coerce_override(self, key: str, value: Any) -> Any:
        """Coerce a constructor override to the declared type.

        Only applied to schema-form params; flat-form params keep the legacy
        set-raw-value behaviour so existing call sites are untouched.
        """
        raw = self.params.get(key)
        if _is_schema(raw):
            return _coerce_value(raw.get("type"), value)
        return value

    def p(self, key: str) -> Any:
        if key not in self.params:
            raise KeyError(key)
        return getattr(self, key)

    # -- param schema ----------------------------------------------------

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        """Resolved ``{param: default_value}`` for this strategy."""
        defaults: dict[str, Any] = {}
        for key, spec in cls.params.items():
            defaults[key] = spec.get("default") if _is_schema(spec) else spec
        return defaults

    @classmethod
    def param_schema(cls) -> dict[str, dict[str, Any]]:
        """Normalised full param schema (flat form is auto-expanded)."""
        schema: dict[str, dict[str, Any]] = {}
        for key, spec in cls.params.items():
            if _is_schema(spec):
                entry = {
                    "default": spec.get("default"),
                    "type": spec.get("type", "str"),
                    "min": spec.get("min"),
                    "max": spec.get("max"),
                    "label": spec.get("label", _humanize(key)),
                    "tooltip": spec.get("tooltip", ""),
                }
                schema[key] = entry
            else:
                entry = _infer_schema(spec)
                entry["label"] = _humanize(key)
                schema[key] = entry
        return schema

    # -- contract validation (Task 1.1) ---------------------------------

    @classmethod
    def validate(cls) -> None:
        """Raise :class:`StrategyContractError` if the contract is not met."""
        if not isinstance(getattr(cls, "name", ""), str) or not cls.name.strip():
            raise StrategyContractError(
                f"{cls.__name__}: strategy must define a non-empty string 'name'"
            )
        if not isinstance(cls.params, dict):
            raise StrategyContractError(f"{cls.name}: 'params' must be a dict")

        for pname, spec in cls.params.items():
            cls._validate_param(cls.name, pname, spec)

        implements_signals = cls.generate_signals is not Strategy.generate_signals
        implements_entries = cls.entries is not Strategy.entries
        if not (implements_signals or implements_entries):
            raise StrategyContractError(
                f"{cls.name}: strategy must implement generate_signals(candles) "
                "or entries(candles)"
            )

    @staticmethod
    def _validate_param(strategy_name: str, pname: str, spec: Any) -> None:
        if not _is_schema(spec):
            return  # bare scalar default — always acceptable

        if "default" not in spec:
            raise StrategyContractError(
                f"{strategy_name}: param '{pname}' schema missing required key 'default'"
            )
        ptype = spec.get("type", "str")
        if ptype not in _VALID_PARAM_TYPES:
            raise StrategyContractError(
                f"{strategy_name}: param '{pname}' has invalid type '{ptype}' "
                f"(allowed: {sorted(_VALID_PARAM_TYPES)})"
            )
        default = spec.get("default")
        if default is not None and not _type_matches(ptype, default):
            raise StrategyContractError(
                f"{strategy_name}: param '{pname}' default {default!r} does not "
                f"match declared type '{ptype}'"
            )
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and hi is not None and lo > hi:
            raise StrategyContractError(f"{strategy_name}: param '{pname}' min ({lo}) > max ({hi})")
        if lo is not None and default is not None and default < lo:
            raise StrategyContractError(
                f"{strategy_name}: param '{pname}' default ({default}) < min ({lo})"
            )
        if hi is not None and default is not None and default > hi:
            raise StrategyContractError(
                f"{strategy_name}: param '{pname}' default ({default}) > max ({hi})"
            )

    # -- signal generation ----------------------------------------------

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def exits(self, candles: pd.DataFrame) -> pd.Series | None:
        return None

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        if self._uses_entries_model():
            return self._signals_from_entries_exits(candles)
        raise NotImplementedError

    def _uses_entries_model(self) -> bool:
        return type(self).entries is not Strategy.entries

    def _signals_from_entries_exits(self, candles: pd.DataFrame) -> pd.Series:
        entries = self.entries(candles)
        exits = self.exits(candles)

        signals = pd.Series(0, index=candles.index, dtype=int)
        held = False
        for idx in candles.index:
            if entries.get(idx, False):
                held = True
                signals.loc[idx] = 1
            elif exits is not None and exits.get(idx, False):
                held = False
                signals.loc[idx] = 0
            else:
                signals.loc[idx] = 1 if held else 0
        return signals

    # -- market view for options (Phase 2 expression layer) ----------

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        """Emit a directional view for options expression.

        Override this to enable options trading for the strategy.  The default
        implementation derives a view from ``generate_signals()``: a ``1`` signal
        maps to ``BULLISH``, ``0`` maps to ``NEUTRAL`` (no trade).

        Returns ``None`` when the strategy has no directional conviction
        (e.g. flat / neutral), which the expression layer interprets as
        "no option trade this bar".

        Parameters
        ----------
        candles:
            The same OHLCV DataFrame passed to ``generate_signals``.
        """
        from backtest.strategy.intent import Direction, MarketView

        try:
            signals = self.generate_signals(candles)
        except NotImplementedError:
            return None

        if signals is None or signals.empty:
            return None

        last_signal = signals.iloc[-1]
        last_close = candles["close"].iloc[-1] if "close" in candles.columns else 0
        ts = candles.index[-1] if len(candles.index) > 0 else None

        # Determine underlying from metadata or default to NIFTY
        underlying = self.params.get("underlying", {}).get("default", "NIFTY")
        if isinstance(underlying, dict):
            underlying = "NIFTY"

        if last_signal == 1:
            direction = Direction.BULLISH
        else:
            direction = Direction.NEUTRAL

        return MarketView(
            direction=direction,
            confidence=abs(float(last_signal)),
            underlying=underlying,
            spot_price=Decimal(str(last_close)),
            bar_timestamp=ts,
        )


#: PRD name for the strategy base class.
BaseStrategy = Strategy
