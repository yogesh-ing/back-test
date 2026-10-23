"""Wrapper around mStock MTicker for real-time market depth."""
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class MStockWebSocketWrapper:
    """Wraps MTicker to provide synchronous quote access with auto-reconnect.
    
    Manages WebSocket connection lifecycle and caches ticks for synchronous
    access via get_quote(). Handles the asynchronous nature of MTicker.
    """
    
    def __init__(self, mticker_instance: Any) -> None:
        self._ticker = mticker_instance
        self._quotes: Dict[str, dict] = {}
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._last_mode_logged: Dict[str, str] = {}
        
        # Override MTicker callbacks to capture ticks
        original_on_ticks = getattr(mticker_instance, 'on_ticks', None)
        
        def on_ticks(ws, ticks):
            """Capture ticks into our cache before calling original callback."""
            logger.debug("Received %d ticks from MTicker", len(ticks) if ticks else 0)
            with self._lock:
                for tick in (ticks or []):
                    token = str(tick.get('instrument_token', ''))
                    if token:
                        normalized = self._normalize_tick(tick)
                        self._quotes[token] = normalized
                        # Log mode/depth transitions per token (2026-10-23:
                        # app stayed quote-mode for option tokens while a raw
                        # harness got full depth on the same server).
                        mode = str(normalized.get('mode'))
                        has_depth = bool(normalized.get('depth'))
                        sig = f"{mode}/{'depth' if has_depth else 'flat'}"
                        if self._last_mode_logged.get(token) != sig:
                            self._last_mode_logged[token] = sig
                            logger.info(
                                "tick profile token=%s mode=%s depth=%s ltp=%s",
                                token, mode, has_depth, normalized.get('ltp'),
                            )
            
            if original_on_ticks:
                original_on_ticks(ws, ticks)
        
        mticker_instance.on_ticks = on_ticks
        
        # Override on_connect to signal connection ready
        original_on_connect = getattr(mticker_instance, 'on_connect', None)
        
        def on_connect(ws, response):
            self._connected.set()
            logger.info("WebSocket connection established")
            if original_on_connect:
                original_on_connect(ws, response)
        
        mticker_instance.on_connect = on_connect
        
        # Override on_close
        original_on_close = getattr(mticker_instance, 'on_close', None)
        
        def on_close(ws, code, reason):
            self._connected.clear()
            logger.info("WebSocket closed: %s", reason)
            if original_on_close:
                original_on_close(ws, code, reason)
        
        mticker_instance.on_close = on_close
    
    def wait_for_connection(self, timeout: float = 5.0) -> bool:
        """Wait for WebSocket to connect. Returns True if connected."""
        return self._connected.wait(timeout=timeout)
    
    def get_quote(self, instrument_token: str) -> Optional[dict]:
        """Get latest cached quote for an instrument."""
        with self._lock:
            return self._quotes.get(str(instrument_token))
    
    @staticmethod
    def _normalize_tick(tick: dict) -> dict:
        """Normalize MTicker tick to standard quote format."""
        last_price = float(tick.get('last_price', 0))
        
        bid = 0.0
        ask = 0.0
        depth = tick.get('depth')
        
        if depth and isinstance(depth, dict):
            bids = depth.get('bid', [])
            asks = depth.get('ask', [])
            
            if bids and len(bids) > 0:
                bid = float(bids[0].get('price', 0))
            if asks and len(asks) > 0:
                ask = float(asks[0].get('price', 0))
        
        return {
            'ltp': last_price,
            'bid': bid,
            'ask': ask,
            'volume': int(tick.get('volume_traded', 0)),
            'oi': float(tick.get('open_interest', 0)),
            'ohlc': tick.get('ohlc', {}),
            'depth': depth,
            'timestamp': tick.get('exchange_timestamp'),
            'mode': tick.get('mode', 'unknown'),
        }
