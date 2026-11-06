# Adding a New Broker Integration

This guide walks through integrating a new broker (e.g., Zerodha, Angel One) into the backtesting system.

## Architecture Overview

The broker system is **broker-agnostic** — session management, API routing, and execution are already designed to handle multiple brokers simultaneously. You only need to implement the broker-specific authentication and data-fetching logic.

## Step-by-Step Integration

### 1. Create Broker Module

Create `src/backtest/brokers/<broker_name>.py` (e.g., `zerodha.py`).

**Required base class**: Inherit from `BrokerAuthBase` (from `backtest.brokers.base`).

**If the broker supports live trading**, also inherit from `BrokerOrderBase` and implement order lifecycle methods.

**Minimal implementation skeleton**:

```python
"""Zerodha broker integration."""
from __future__ import annotations

from typing import Any
from backtest.brokers.base import BrokerAuthBase


class ZerodhaBroker(BrokerAuthBase):
    """Zerodha Kite Connect integration."""

    broker_name = "zerodha"

    def __init__(self) -> None:
        super().__init__()
        # Initialize any broker-specific state here
        self._session_token: str | None = None
        self._api_key: str | None = None

    def login(self, username: str, password: str) -> tuple[bool, str]:
        """Step 1: Authenticate with credentials.

        For OAuth-based brokers, this typically generates an auth URL
        and stores temporary context for the callback.
        """
        # Implementation specific to Zerodha
        ...

    def verify_totp(self, totp_code: str) -> tuple[bool, str]:
        """Step 2: Verify TOTP or exchange authorization code.

        For OAuth brokers, this exchanges the parked auth code for tokens.
        """
        # Implementation specific to Zerodha
        ...

    def get_session_status(self) -> dict[str, Any]:
        """Return current session status for UI polling."""
        return {
            "broker": self.broker_name,
            "status": "authenticated" if self._session_token else "unauthenticated",
            "expires_at": None,  # Or actual expiry if known
        }

    def logout(self) -> None:
        """Clear all session state."""
        self._session_token = None
        self._temp_auth_context = {}
```

**Key patterns to follow**:

- **OAuth brokers** (like Upstox): Use `_temp_auth_context` dict to store CSRF state and redirect URL during login. Implement `park_auth_code(code, state)` method for the callback route.
- **Direct-auth brokers** (like mStock): Store credentials temporarily in memory, clear after successful auth.
- **Never log or persist** raw credentials, tokens, or auth codes.
- **Fail-closed**: Reject invalid states explicitly, don't silently ignore errors.

### 2. Register in Session Manager

Edit `src/backtest/brokers/session_manager.py`:

**Add to registry** (around line 72-76):

```python
_BROKER_REGISTRY: dict[str, Callable[[], BrokerAuthBase]] = {
    "mstock": lambda: _lazy("mstock"),
    "dhan": lambda: _lazy("dhan"),
    "upstox": lambda: _lazy("upstox"),
    "zerodha": lambda: _lazy("zerodha"),  # ADD THIS
}
```

**Add lazy loader case** (in `_lazy()` function around lines 86-100):

```python
if module == "zerodha":
    from backtest.brokers.zerodha import ZerodhaBroker
    return ZerodhaBroker()
```

### 3. Add API Routes (OAuth Brokers Only)

If your broker uses OAuth (redirect-based auth), add a callback route in `src/backtest/api/broker_auth.py`:

```python
@broker_auth_bp.get("/api/broker/zerodha/callback")
def zerodha_oauth_callback() -> Response:
    """Zerodha redirect target — park the auth code for the pending login."""
    error = (request.args.get("error") or "").strip()
    if error:
        logger.info("zerodha oauth callback: provider error=%s", error[:80])
        return _oauth_page(
            "Zerodha sign-in not completed",
            "Zerodha returned an error instead of an authorization code.",
        )
    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    try:
        from backtest.brokers.session_manager import get_session_manager

        broker = get_session_manager().get_broker("zerodha")
        ok, message = broker.park_auth_code(code, state)
    except Exception:
        logger.exception("zerodha oauth callback failed")
        ok, message = False, "Internal error while saving the code"
    if ok:
        logger.info("zerodha oauth callback: authorization code received")
        return _oauth_page(
            "✅ Authorization received",
            "Return to the app and press <strong>Continue</strong>.",
        )
    logger.info("zerodha oauth callback rejected: %s", message)
    return _oauth_page(
        "Code not accepted",
        f"{message}. Paste the <code>code</code> value from this page's address.",
    )
```

### 4. Implement Data Fetching (Optional)

If the broker provides historical market data, implement a fetcher following the pattern in `mstock.py`. The fetcher should:

- Accept instrument symbols and date ranges
- Return OHLCV bars in a standard format (list of dicts with keys: `t`/`time`, `o`/`open`, `h`/`high`, `l`/`low`, `c`/`close`, `v`/`volume`)
- Handle rate limits and retries gracefully
- Never fabricate data — use broker APIs only

**Integration point**: Add your broker's fetch function to `src/backtest/api/data_manager.py`:

```python
def _fetch_<broker>_bars_chunked(
    instrument_token: str,
    from_date: str,
    to_date: str,
    exchange: str,
    timeframe: str,
    chunk_days: int,
    should_cancel=None,
    on_progress=None,
    windows: list | None = None,
    breaker: "_CircuitBreaker | None" = None,
) -> tuple[list[dict], int]:
    """Fetch OHLCV bars from <Broker> API."""
    # Implementation here
    return bars, chunk_errors
```

Then add routing in `_run_fetch_job_inner()` (around line 975):

```python
if broker == "<broker_name>":
    bars, chunk_errors = _fetch_<broker>_bars_chunked(...)
```

**Current status**:
- **mStock**: Fully implemented with chunked fetching, retry logic, and circuit breaker
- **Upstox**: Stub added (returns empty), needs implementation
- **Dhan**: Static/no auth mode, no live API configured

### 5. Update Frontend (Optional)

Add broker button/logo in UI templates if you want it visible in the broker selection modal. This is purely cosmetic — the backend works without it.

## Testing Checklist

Before merging:

- [ ] Login flow completes successfully (credentials or OAuth)
- [ ] Session status reports correctly via `/api/broker/<name>/status`
- [ ] Logout clears all session state
- [ ] Auth codes/tokens never appear in logs
- [ ] Concurrent sessions with other brokers remain unaffected
- [ ] Error cases fail gracefully with user-friendly messages

## Common Pitfalls

- **Bytecode caching**: After editing broker files, kill ALL Python processes and restart with `-B` flag to avoid stale imports.
- **Logger filtering**: If debug output doesn't appear, raise the app's log level — never `print()` a broker response to stderr. Token-exchange bodies carry the live access token and land in `server_*.err.log`.
- **Session manager singleton**: The manager is process-wide. Changes require a full restart, not just a reload.
- **OAuth state validation**: Always validate CSRF state in callbacks using `secrets.compare_digest()` to prevent timing attacks.

## Example: Upstox Integration

See `upstox.py` for a complete OAuth-based broker implementation with:
- Two-step login (generate URL → park code → exchange for token)
- CSRF state validation
- Session expiry tracking
- Market data fetching via REST API

Estimated integration time: **2-4 hours** for basic auth, **1-2 days** for full trading + data support.
