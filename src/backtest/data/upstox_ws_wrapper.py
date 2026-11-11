"""Upstox Market Data Feed V3 — real-time 5-level depth over WebSocket (Protobuf).

Built from the vendor doc, not from live guessing. Every non-obvious choice below
cites the file and line it came from::

    docs/broker-reference/upstox/sections/market-data-feed-v3.md            (V3 feed)
    docs/broker-reference/upstox/sections/market-data-feed-authorize-v3.md  (socket URI)
    docs/broker-reference/upstox/MarketDataFeedV3.proto                     (frame schema)

WHY THIS EXISTS
---------------
Upstox has so far been used only through POLLED REST quotes
(:mod:`backtest.data.upstox_live_feed`), which cannot be compared tick-for-tick
against mStock's WebSocket ladder. This module is the independent second depth
feed: ``full`` mode carries LTPC + 5 levels + option Greeks
(market-data-feed-v3.md:99), so the two brokers' ladders can be graded against
each other during a live session.

WHAT THIS MODULE IS *NOT*
-------------------------
Nothing here is wired into the strategy, risk or runner path. It caches sanitized
depth and serves it to whoever asks. Depth that is absent stays absent: no
level is interpolated, defaulted or carried over silently as if it were fresh.

SHAPE
-----
``get_quote()`` returns the same field names and the same ``ladder_status``
vocabulary as :class:`backtest.data.mstock_ws_wrapper.MStockWebSocketWrapper`
(ltp/bid/ask/volume/oi/ohlc/depth/timestamp/recv_mono/recv_utc/source/mode/
ladder_status/ltp_mono/ladder_mono), so one comparison harness can consume
either feed. Internals differ completely — mStock goes through the Twisted
MTicker; this speaks websocket-client plus protobuf directly.

CLOCK
-----
Follows :mod:`backtest.timekeeping`: arrival instants are aware UTC via
``iso_utc(now_utc())``; the exchange's own ``currentTs`` (epoch millis,
market-data-feed-v3.md:415) enters through ``from_market_clock()``. No
``utcnow()``, no bare ``now()``, no tz stripping.

SECRETS
-------
The authorize response embeds a single-use ``code`` in the wss URI
(market-data-feed-authorize-v3.md:34), i.e. it *is* a credential. It is never
logged, printed or written; only :func:`_redact_ws_url` output can reach a log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import requests
import websocket

from backtest.data import upstox_market_data_feed_v3_pb2 as pb
from backtest.data.feed_depth_recorder import record_depth_packet
from backtest.data.ladder_shape import order_problem
from backtest.timekeeping import from_market_clock, iso_utc, now_utc

logger = logging.getLogger("backtest.data.upstox_ws_wrapper")

__all__ = ["UpstoxWebSocketWrapper", "UpstoxFeedError", "SubscriptionLimitError"]

#: REST call that hands back the one-time ``wss://`` URI
#: (market-data-feed-authorize-v3.md:5 — "API to retrieve the designated socket
#: endpoint URI for Market updates ... serves as an alternative method to
#: acquire the necessary wss:// URL").
#: THE LOCAL DOC DOES NOT PRINT THIS PATH — it documents the response body and
#: the doc route name (``/v3/get-market-data-feed``, ``/get-market-data-feed-authorize-v3``)
#: only, so the path was established by one read-only GET per candidate with a
#: live token (measured 2026-11-06): v3/get-market-data-feed/authorize,
#: v3/market-data-feed/authorize, v3/get-market-data-feed-authorize,
#: v2/get-market-data-feed/authorize and v1/market-data-feed/authorize all
#: returned UDAPI100060 "Resource not Found";
#: ``/v3/feed/market-data-feed/authorize`` returned 200 + the wss URI. It also
#: matches the one authorize URL the doc corpus does print for the sibling
#: feed — ``/v2/feed/portfolio-stream-feed/authorize``
#: (sections/portfolio-stream-feed-authorize.md:24). Overridable for sandboxing.
AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

#: Subscription envelope modes (market-data-feed-v3.md:95-100).
MODE_LTPC = "ltpc"
MODE_OPTION_GREEKS = "option_greeks"
MODE_FULL = "full"
MODE_FULL_D30 = "full_d30"
VALID_MODES = (MODE_LTPC, MODE_OPTION_GREEKS, MODE_FULL, MODE_FULL_D30)

#: Instrument-key ceilings per mode (market-data-feed-v3.md:43-45 and :52).
#: ``full_d30`` exists only under Upstox Plus (:52); 50 is the individual cap.
MODE_KEY_LIMITS = {MODE_LTPC: 5000, MODE_OPTION_GREEKS: 3000, MODE_FULL: 2000,
                   MODE_FULL_D30: 50}

#: ``method`` values for the envelope (market-data-feed-v3.md:87-91).
METHOD_SUB = "sub"
METHOD_CHANGE_MODE = "change_mode"
METHOD_UNSUB = "unsub"

#: ``Accept`` for the authorize call (market-data-feed-authorize-v3.md:17).
ACCEPT_JSON = "application/json"

#: How long to wait for the first frame after (re)connecting before the reader
#: gives up. Nothing in the doc promises an interval, so this is ours.
_DEFAULT_RECONNECT_DELAY_S = 2.0
_DEFAULT_MAX_RECONNECTS = 20


class UpstoxFeedError(RuntimeError):
    """The feed could not be authorised or the socket failed unrecoverably."""


class SubscriptionLimitError(ValueError):
    """More instrument keys requested than the documented mode ceiling."""


def _redact_ws_url(url: str) -> str:
    """Strip the single-use credential from a socket URI before display."""
    return re.sub(r"([?&](?:code|requestId)=)[^&]*", r"\1<redacted>", str(url))


def _utc_iso() -> str:
    """Arrival stamp for a frame: aware UTC, offset stated (timekeeping contract)."""
    return iso_utc(now_utc())


class UpstoxWebSocketWrapper:
    """Latest sanitized V3 book per instrument key, fed by one WebSocket reader.

    Parameters
    ----------
    access_token:
        Upstox access token for the ``Bearer`` header of the authorize call
        (market-data-feed-v3.md:62). Pass ``None`` with ``token_provider`` when
        the token must be re-resolved on every re-authorization.
    token_provider:
        Callable returning the token. Used instead of ``access_token`` when set.
    mode:
        Subscription mode; ``full`` gives LTPC + 5 levels + Greeks (:99).
    authorize_url / reconnect_delay_s / max_reconnects:
        Wire overrides, mostly for tests.
    """

    #: Ladder quality labels — the same vocabulary mStock's wrapper publishes,
    #: so one harness can diff the two brokers.
    LADDER_OK = "ok"
    LADDER_BLANK = "blank"
    LADDER_NONE = "none"
    LADDER_ONE_SIDED = "one_sided"
    LADDER_CROSSED = "crossed"
    #: Levels arrived out of order, so neither touch price is the venue's best.
    LADDER_UNSORTED = "unsorted"

    def __init__(
        self,
        access_token: Optional[str] = None,
        *,
        token_provider: Optional[Callable[[], str]] = None,
        mode: str = MODE_FULL,
        authorize_url: Optional[str] = None,
        reconnect_delay_s: float = _DEFAULT_RECONNECT_DELAY_S,
        max_reconnects: int = _DEFAULT_MAX_RECONNECTS,
        request_timeout_s: float = 15.0,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        if not access_token and token_provider is None:
            raise ValueError("upstox ws needs access_token or token_provider")
        self._token = str(access_token or "")
        self._token_provider = token_provider
        self.mode = mode
        self.authorize_url = str(
            authorize_url or os.getenv("UPSTOX_WS_AUTHORIZE_URL") or AUTHORIZE_URL
        )
        self._reconnect_delay_s = float(reconnect_delay_s)
        self._max_reconnects = int(max_reconnects)
        self._request_timeout_s = float(request_timeout_s)

        self._quotes: Dict[str, dict] = {}
        self._frames: Dict[str, int] = {}
        self._blank_ladder_counts: Dict[str, int] = {}
        self._last_status_logged: Dict[str, str] = {}
        self._raw_types: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._last_any_tick_mono: float = 0.0
        self._keys: List[str] = []
        self._app: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None

        #: Public observability — what the socket actually did.
        self.closes: List[tuple] = []
        self.errors: List[str] = []
        self.authorizations = 0
        self.segment_status: Dict[str, str] = {}
        self.last_ws_url: str = ""
        self.last_error: Optional[str] = None

    # -- credentials ---------------------------------------------------------

    def _credentials(self) -> str:
        """Token for the Bearer header, resolved per authorization attempt."""
        if self._token_provider is not None:
            token = str(self._token_provider() or "")
        else:
            token = self._token
        if not token:
            raise UpstoxFeedError("no Upstox access token — borrow or log in first")
        return token

    # -- lifecycle -----------------------------------------------------------

    def start(self, instrument_keys: List[str], mode: Optional[str] = None) -> None:
        """Authorize, connect and subscribe ``instrument_keys`` in background."""
        keys = [str(k).strip() for k in instrument_keys if str(k).strip()]
        if not keys:
            raise ValueError("no instrument keys to subscribe")
        self.check_limits(keys, mode or self.mode)
        self._keys = keys
        self.mode = mode or self.mode
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._session_loop, name="upstox-ws-feed", daemon=True
        )
        self._thread.start()

    def check_limits(self, keys: List[str], mode: str) -> None:
        """Refuse a subscription the documented feeder limits would drop (:43-52)."""
        limit = MODE_KEY_LIMITS.get(mode)
        if limit is not None and len(keys) > limit:
            raise SubscriptionLimitError(
                f"mode {mode!r} allows {limit} instrument keys "
                f"(market-data-feed-v3.md:43-52); asked for {len(keys)}"
            )

    def wait_for_connection(self, timeout: float = 15.0) -> bool:
        """True once the socket is up. Frames still may take a moment."""
        return self._connected.wait(timeout=timeout)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def last_any_tick_mono(self) -> float:
        """Monotonic arrival of the newest frame across any subscribed key."""
        with self._lock:
            return self._last_any_tick_mono

    def close(self) -> None:
        """Stop the reader thread and close the socket. Idempotent."""
        self._stop.set()
        self._connected.clear()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:  # noqa: BLE001 - shutdown best effort
                logger.debug("upstox ws: close() raised", exc_info=True)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def __enter__(self) -> "UpstoxWebSocketWrapper":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    # -- authorization -------------------------------------------------------

    def authorize(self) -> str:
        """Return the single-use ``wss://`` socket URI for this connection.

        The URI is one-time because its ``code`` query parameter is itself the
        WebSocket authentication token (market-data-feed-authorize-v3.md:34),
        so every (re)connect needs a fresh call. The URI is never logged.
        """
        headers = {
            "Authorization": f"Bearer {self._credentials()}",
            "Accept": ACCEPT_JSON,
        }
        try:
            resp = requests.get(self.authorize_url, headers=headers,
                                timeout=self._request_timeout_s)
        except requests.RequestException as exc:  # network / timeout
            raise UpstoxFeedError(f"authorize request failed: {exc}") from exc
        if resp.status_code != 200:
            raise UpstoxFeedError(
                f"authorize returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json() or {}
        except ValueError as exc:
            raise UpstoxFeedError(f"authorize body is not JSON: {resp.text[:200]}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        # The doc names the field ``authorized_redirect_uri``
        # (market-data-feed-authorize-v3.md:25,34). The live V3 response is
        # labelled with the camelCase alias the same corpus shows for V2
        # (market-data-feed-authorize-discontinued.md:20-21 lists both), so
        # either spelling is accepted rather than guessing one.
        uri = ""
        if isinstance(data, dict):
            uri = str(data.get("authorized_redirect_uri")
                      or data.get("authorizedRedirectUri") or "")
        if not uri.startswith("wss://"):
            raise UpstoxFeedError(
                f"authorize gave no wss URI: {json.dumps(payload)[:200]}"
            )
        self.authorizations += 1
        self.last_ws_url = _redact_ws_url(uri)
        return uri

    # -- session loop --------------------------------------------------------

    def _session_loop(self) -> None:
        """Connect, stream, and re-authorize on every close (URI is single-use)."""
        attempt = 0
        while not self._stop.is_set():
            try:
                uri = self.authorize()
            except UpstoxFeedError as exc:
                self.last_error = str(exc)
                self.errors.append(f"authorize: {exc}")
                logger.error("upstox ws authorize failed: %s", exc)
                attempt += 1
                if attempt > self._max_reconnects:
                    logger.error("upstox ws: giving up after %s attempts", attempt)
                    return
                if self._stop.wait(self._reconnect_delay_s):
                    return
                continue

            app = websocket.WebSocketApp(
                uri,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._app = app
            # No client-side ping_interval: the server sends the heartbeat ping
            # and websocket-client answers it with a pong automatically
            # (market-data-feed-v3.md:708-710, market-data-feed-authorize-v3.md:36;
            # the auto-pong is websocket/_app.py:464 in websocket-client 1.9.0).
            try:
                app.run_forever(skip_utf8_validation=True)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.errors.append(self.last_error)
                logger.warning("upstox ws run_forever raised: %s", self.last_error)
            self._app = None
            self._connected.clear()
            if self._stop.is_set():
                return
            attempt += 1
            if attempt > self._max_reconnects:
                logger.error("upstox ws: reconnect ceiling (%s) reached", self._max_reconnects)
                return
            if self._stop.wait(self._reconnect_delay_s):
                return
            logger.info("upstox ws re-authorizing (attempt %s, socket closed)", attempt)

    # -- websocket callbacks -------------------------------------------------

    def forget_token(self, instrument_key: str) -> bool:
        """Drop every cached trace of a key (unsubscribe / re-login).

        A key evicted from the subscription registry and later re-subscribed
        must not re-serve its pre-eviction row — the cold-start rule that makes
        the broker's serve gate meaningful (mStock's wrapper has the twin;
        ``docs/OPTION-DEPTH-GARBAGE-ROOT-CAUSE-2026-11-03.md``).
        """
        key = str(instrument_key)
        with self._lock:
            existed = self._quotes.pop(key, None) is not None
            self._blank_ladder_counts.pop(key, None)
            self._last_status_logged.pop(key, None)
        return existed

    def _purge_all(self) -> None:
        """Start a cache generation: no row may outlive the socket that wrote it.

        Every (re)connect is a new socket — a row cached before a drop would
        otherwise become servable again the instant the liveness watermark
        moved (``mstock_ws_wrapper._purge_all`` is the twin). Frame counters
        and raw-type telemetry are session-wide and stay.
        """
        with self._lock:
            self._quotes.clear()
            self._blank_ladder_counts.clear()
            self._last_status_logged.clear()
            self._last_any_tick_mono = 0.0

    def _on_open(self, ws: Any) -> None:
        """Subscribe on every (re)connect; the socket starts with no subscriptions.

        The cache generation starts with the socket: rows written by a previous
        connection are purged before any subscription frame goes out.
        """
        self._purge_all()
        self._connected.set()
        self.last_error = None
        logger.info("upstox ws connected via %s", self.last_ws_url)
        try:
            self.send(ws, METHOD_SUB, self._keys, self.mode)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"subscribe: {exc}"
            logger.error("upstox ws subscribe failed: %s", exc)

    def _on_message(self, ws: Any, message: Any) -> None:
        """Decode one binary frame into cached rows. Text frames are not expected."""
        recv_mono = time.monotonic()
        recv_utc = _utc_iso()
        if isinstance(message, str):
            # The doc specifies Protobuf only; a text frame means the server is
            # speaking another dialect, so record it rather than decode it.
            self.errors.append(f"unexpected text frame: {message[:160]}")
            logger.warning("upstox ws: text frame ignored (%s)", message[:120])
            return
        response = pb.FeedResponse()
        try:
            response.ParseFromString(message)
        except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the reader
            self.errors.append(f"protobuf decode: {type(exc).__name__}: {exc}")
            logger.warning("upstox ws: undecodable frame (%s bytes): %s", len(message), exc)
            return
        self._ingest(response, recv_mono=recv_mono, recv_utc=recv_utc)

    def _on_error(self, ws: Any, error: Any) -> None:
        text = f"{type(error).__name__}: {error}"
        self.errors.append(text[:300])
        self.last_error = text[:300]
        logger.warning("upstox ws socket error: %s", text[:200])

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        self._connected.clear()
        self.closes.append((code, reason))
        if not self._stop.is_set():
            logger.info("upstox ws closed code=%s reason=%s — will re-authorize", code, reason)

    # -- wire frames ---------------------------------------------------------

    def build_envelope(self, method: str, keys: List[str], mode: Optional[str] = None) -> bytes:
        """V3 subscription JSON as **binary** bytes.

        ``guid`` is "globally unique identifier for the request" and no format
        is given (market-data-feed-v3.md:80), so a uuid4 prefix is used. The
        payload must go out as a binary frame, not text (:34-36).
        """
        data: Dict[str, Any] = {"instrumentKeys": [str(k) for k in keys]}
        if mode:
            data["mode"] = mode
        return json.dumps(
            {"guid": uuid.uuid4().hex[:20], "method": method, "data": data}
        ).encode("utf-8")

    def send(self, ws: Any, method: str, keys: List[str], mode: Optional[str] = None) -> None:
        """Send one envelope as a binary frame (``send_bytes`` = OPCODE_BINARY)."""
        if method in (METHOD_SUB, METHOD_CHANGE_MODE):
            self.check_limits(keys, mode or self.mode or MODE_FULL)
        ws.send_bytes(self.build_envelope(method, keys, mode))

    def subscribe(self, keys: List[str], mode: Optional[str] = None) -> None:
        """Add keys mid-session (``method: sub``, market-data-feed-v3.md:89)."""
        self._mutate_keys(METHOD_SUB, keys, mode)

    def change_mode(self, keys: List[str], mode: str) -> None:
        """Switch mode for already-subscribed keys without re-subscribing (:90)."""
        self._mutate_keys(METHOD_CHANGE_MODE, keys, mode)

    def unsubscribe(self, keys: List[str]) -> None:
        """Stop updates for keys (:91) and purge their cache."""
        self._mutate_keys(METHOD_UNSUB, keys, None)
        with self._lock:
            for key in keys:
                self._quotes.pop(str(key), None)
                self._frames.pop(str(key), None)
                self._blank_ladder_counts.pop(str(key), None)
                self._last_status_logged.pop(str(key), None)

    def _mutate_keys(self, method: str, keys: List[str], mode: Optional[str]) -> None:
        keys = [str(k).strip() for k in keys if str(k).strip()]
        if not keys:
            raise ValueError("no instrument keys given")
        app = self._app
        if app is None:
            raise UpstoxFeedError("socket is not connected — call start() first")
        with self._lock:
            for key in keys:
                if method != METHOD_SUB and key not in self._keys:
                    raise ValueError(f"{key} is not subscribed")
            if method == METHOD_SUB:
                for key in keys:
                    if key not in self._keys:
                        self._keys.append(key)
            elif method == METHOD_UNSUB:
                self._keys = [k for k in self._keys if k not in keys]
        self.send(app, method, keys, mode)
        logger.info("upstox ws sent %s for %s keys mode=%s", method, len(keys), mode)

    # -- decode + sanitize ---------------------------------------------------

    def _ingest(
        self, response: Any, *, recv_mono: Optional[float] = None, recv_utc: Optional[str] = None
    ) -> Dict[str, dict]:
        """Fold a decoded ``FeedResponse`` into the cache. Returns the new rows.

        ``type`` is one of ``initial_feed`` / ``live_feed`` / ``market_info``
        (MarketDataFeedV3.proto:46-50). ``market_info`` carries segment status
        and no ladders (:112); the first ``live_feed`` is the snapshot and the
        rest are deltas (:104-108), which is why deltas go through
        :meth:`_retain_ladder` instead of overwriting.
        """
        mono = time.monotonic() if recv_mono is None else float(recv_mono)
        utc = _utc_iso() if recv_utc is None else str(recv_utc)
        try:
            type_name = pb.Type.Name(response.type)
        except (ValueError, AttributeError):
            type_name = f"type_{int(response.type)}"
        with self._lock:
            self._raw_types[type_name] = self._raw_types.get(type_name, 0) + 1
        if type_name == "market_info" and response.HasField("marketInfo"):
            self._record_segment_status(response.marketInfo)
            return {}
        rows: Dict[str, dict] = {}
        for key, feed in response.feeds.items():
            row = self._normalize_feed(feed, response.currentTs, key, mono, utc)
            with self._lock:
                self._frames[key] = self._frames.get(key, 0) + 1
                retained = self._retain_ladder(key, row)
                self._quotes[key] = retained
                self._last_any_tick_mono = mono
            rows[key] = retained
            self._log_transition(key, retained)
            record_depth_packet("upstox", retained, token=key)
        return rows

    def _record_segment_status(self, market_info: Any) -> None:
        """Remember the segment map from ``market_info`` (market-data-feed-v3.md:112)."""
        status: Dict[str, str] = {}
        for segment, value in market_info.segmentStatus.items():
            try:
                status[str(segment)] = pb.MarketStatus.Name(value)
            except (ValueError, AttributeError):
                status[str(segment)] = str(int(value))
        with self._lock:
            self.segment_status = status
        logger.info("upstox ws market_info segments: %s", status)

    def _normalize_feed(
        self, feed: Any, current_ts: int, key: str, mono: float, utc: str
    ) -> dict:
        """One ``Feed`` union member → the mStock-compatible row."""
        union = feed.WhichOneof("FeedUnion")
        ltpc: Any = None
        greeks: Optional[dict] = None
        ohlc_by_interval: Dict[str, dict] = {}
        depth_source: Any = None
        volume: Optional[int] = None
        oi: Optional[float] = None
        extras: Dict[str, Any] = {}

        if union == "ltpc":
            ltpc = feed.ltpc
        elif union == "fullFeed":
            inner = feed.fullFeed.WhichOneof("FullFeedUnion")
            if inner == "marketFF":
                ff = feed.fullFeed.marketFF
                ltpc = ff.ltpc
                if ff.HasField("marketLevel"):
                    depth_source = list(ff.marketLevel.bidAskQuote)
                else:
                    depth_source = []
                greeks = _greeks_dict(ff.optionGreeks) if ff.HasField("optionGreeks") else None
                ohlc_by_interval = _ohlc_dict(ff.marketOHLC)
                volume = int(ff.vtt) if ff.vtt else None
                oi = float(ff.oi) if ff.oi else None
                # CAS + balance fields (market-data-feed-v3.md:404-414)
                extras = {
                    "atp": float(ff.atp) if ff.atp else None,
                    "iv": float(ff.iv) if ff.iv else None,
                    "tbq": float(ff.tbq) if ff.tbq else None,
                    "tsq": float(ff.tsq) if ff.tsq else None,
                    "iep": float(ff.iep) if ff.iep else None,
                    "rp": float(ff.rp) if ff.rp else None,
                    "ieq": int(ff.ieq) if ff.ieq else None,
                    "iiq_total": int(ff.iiqTotal) if ff.iiqTotal else None,
                    "iiq_m": int(ff.iiqM) if ff.iiqM else None,
                    "cas_eligible": bool(ff.casEligible),
                }
            elif inner == "indexFF":
                # Indices carry ltpc + OHLC only (MarketDataFeedV3.proto:71-74),
                # so an index has *no ladder by schema*, not a broken one.
                ix = feed.fullFeed.indexFF
                ltpc = ix.ltpc
                ohlc_by_interval = _ohlc_dict(ix.marketOHLC)
                depth_source = None
            else:
                depth_source = None
        elif union == "firstLevelWithGreeks":
            # ``option_greeks`` shape: one depth level + Greeks (:218-241).
            fl = feed.firstLevelWithGreeks
            ltpc = fl.ltpc
            depth_source = [fl.firstDepth] if fl.HasField("firstDepth") else []
            greeks = _greeks_dict(fl.optionGreeks) if fl.HasField("optionGreeks") else None
            volume = int(fl.vtt) if fl.vtt else None
            oi = float(fl.oi) if fl.oi else None
        else:
            depth_source = None

        depth, bid, ask, status = self._sanitize_depth(depth_source)
        # Was a ladder *expected* from this frame? ``IndexFullFeed`` and a bare
        # ``ltpc`` delta have no depth field by schema (proto:71-74, :93-98), so
        # their empty ladder is structural, not the garbage the 2026-11-03
        # incident was about — those frames must not inflate the blank counter.
        depth_expected = depth_source is not None
        day = ohlc_by_interval.get("1d") or {}
        row: dict = {
            "instrument_key": str(key),
            "ltp": float(ltpc.ltp) if ltpc is not None and ltpc.ltp else None,
            "bid": bid,
            "ask": ask,
            "spread": round(ask - bid, 4) if bid and ask else None,
            "volume": int(volume) if volume else None,
            "oi": float(oi) if oi else None,
            "ohlc": {
                "open": day.get("open"), "high": day.get("high"),
                "low": day.get("low"), "close": day.get("close"),
                "volume": day.get("vol"),
            },
            "ohlc_intervals": ohlc_by_interval,
            "depth": depth,
            "depth_levels": len(depth.get("bid", [])) + len(depth.get("ask", [])),
            "greeks": greeks,
            "timestamp": _epoch_iso(current_ts),
            "recv_mono": mono,
            "recv_utc": utc,
            "source": "ws",
            "feed_union": union,
            "mode": _request_mode_name(feed.requestMode),
            "ladder_status": status,
            "depth_expected": depth_expected,
            # LTP keeps its own clock: a delta frame with no ladder still
            # refreshes the last price (mstock_ws_wrapper.py:430 does the same).
            "ltp_mono": mono,
            "last_trade_ts": _epoch_iso(getattr(ltpc, "ltt", 0) if ltpc is not None else 0),
            "close_price": float(ltpc.cp) if ltpc is not None and ltpc.cp else None,
        }
        if status == self.LADDER_OK:
            row["ladder_mono"] = mono
        row.update(extras)
        return row

    def _retain_ladder(self, key: str, incoming: dict) -> dict:
        """Partial V3 delta frames must not erase the last honest ladder.

        The feed streams *updates for the requested keys* rather than a guaranteed
        complete book on every tick (market-data-feed-v3.md:204, :104-108), and
        mStock proved the failure mode: a frame with a fine header and no ladder
        that silently replaces a good one (2026-11-03, ₹71,639 of fake profit).
        So a frame without a usable ladder keeps the previous ladder *and its
        stamp*, takes only the fields this frame genuinely carries, and is
        labelled ``kept:<reason>`` — never presented as a fresh book.
        mStock's wrapper tests the previous row's *status label* instead
        (mstock_ws_wrapper.py:203), which drops the book on the second
        consecutive ladder-less frame — V3 deltas legitimately omit unchanged
        fields, so this version tests the retained ladder itself. Absent levels
        stay absent; the retained book is always labelled ``kept:<reason>``.
        Caller holds no lock; single reader thread.
        """
        status = str(incoming.get("ladder_status") or self.LADDER_NONE)
        if status == self.LADDER_OK:
            return incoming
        # Only a frame that *should* have carried a ladder is a blank one; an
        # index or a bare ltpc delta simply has no ladder in its schema.
        if incoming.get("depth_expected"):
            self._blank_ladder_counts[key] = self._blank_ladder_counts.get(key, 0) + 1
        previous = self._quotes.get(key)
        if previous is None or not self._has_two_sided_book(previous):
            # Nothing honest to retain: serve the frame exactly as it arrived.
            return incoming
        merged = dict(previous)
        for field in (
            "ltp", "volume", "oi", "ohlc", "ohlc_intervals", "greeks", "timestamp",
            "recv_mono", "recv_utc", "mode", "feed_union", "last_trade_ts",
            "close_price", "atp", "iv", "tbq", "tsq", "depth_expected",
        ):
            merged[field] = incoming.get(field)
        merged["ltp_mono"] = incoming.get("recv_mono")
        merged["ladder_status"] = f"kept:{status}"
        return merged

    @staticmethod
    def _has_two_sided_book(row: dict) -> bool:
        """Does this cached row actually hold a priced ladder on both sides?"""
        depth = row.get("depth") or {}
        return bool(depth.get("bid")) and bool(depth.get("ask"))

    @classmethod
    def _sanitize_depth(cls, levels: Any) -> tuple[Dict[str, list], float, float, str]:
        """``Quote`` list → ``(depth, bid, ask, ladder_status)``.

        Upstox packs each level as ONE ``Quote`` holding both sides
        (``bidQ/bidP/askQ/askP``, MarketDataFeedV3.proto:21-26), so the two
        ladders are split out of the same array in the order sent — level *n* of
        ``bid`` and level *n* of ``ask`` came from the same wire element.
        proto3 scalars default to 0, and 0 means *nobody is quoting there*, so a
        level survives only with a positive price and positive quantity
        (same rule as mstock_ws_wrapper.py:364). Absent levels stay absent; they
        are never filled with the previous level or with a tick size.

        Statuses: ``ok`` two-sided, ordered, with bid<ask; ``one_sided`` a real
        venue-side gap; ``crossed``/``unsorted``/``blank``/``none`` → no usable
        side is published. ``unsorted`` is the case the level-index split makes
        possible on either feed: a book whose steps fold back towards the touch
        cannot have a best price at index 0, and mStock published ten such
        packets on 2026-11-09 while Upstox published none. Refusing rather than
        sorting is the same rule that forbids taking ``max``/``min`` — the app
        may not invent a better price than the venue showed.
        """
        if levels is None:
            return {}, 0.0, 0.0, cls.LADDER_NONE
        bids: List[dict] = []
        asks: List[dict] = []
        for level in levels:
            bid_p = float(getattr(level, "bidP", 0.0) or 0.0)
            ask_p = float(getattr(level, "askP", 0.0) or 0.0)
            bid_q = int(getattr(level, "bidQ", 0) or 0)
            ask_q = int(getattr(level, "askQ", 0) or 0)
            if bid_p > 0 and bid_q > 0:
                bids.append({"price": bid_p, "quantity": bid_q})
            if ask_p > 0 and ask_q > 0:
                asks.append({"price": ask_p, "quantity": ask_q})
        cleaned = {"bid": bids, "ask": asks}
        if not bids and not asks:
            return cleaned, 0.0, 0.0, cls.LADDER_BLANK
        if order_problem(bids, asks) is not None:
            return cleaned, 0.0, 0.0, cls.LADDER_UNSORTED
        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 0.0
        if not bids or not asks:
            return cleaned, best_bid, best_ask, cls.LADDER_ONE_SIDED
        if best_bid >= best_ask:
            return cleaned, 0.0, 0.0, cls.LADDER_CROSSED
        return cleaned, best_bid, best_ask, cls.LADDER_OK

    def _log_transition(self, key: str, row: dict) -> None:
        sig = f"{row.get('mode')}/{row.get('ladder_status')}/{row.get('feed_union')}"
        with self._lock:
            previous = self._last_status_logged.get(key)
            if previous == sig:
                return
            self._last_status_logged[key] = sig
            blanks = self._blank_ladder_counts.get(key, 0)
        logger.info(
            "tick profile key=%s mode=%s union=%s ladder=%s ltp=%s bid=%s ask=%s "
            "levels=%s blank_frames=%s",
            key, row.get("mode"), row.get("feed_union"), row.get("ladder_status"),
            row.get("ltp"), row.get("bid"), row.get("ask"), row.get("depth_levels"), blanks,
        )

    # -- read API ------------------------------------------------------------

    def get_quote(self, instrument_key: str) -> Optional[dict]:
        """Latest cached row for ``instrument_key`` (mStock-shaped), or None."""
        with self._lock:
            row = self._quotes.get(str(instrument_key))
            return dict(row) if row is not None else None

    def quotes(self) -> Dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._quotes.items()}

    def get_depth_levels(self, instrument_key: str) -> int:
        """Count of priced levels on both sides for this key (0 until first ladder)."""
        row = self.get_quote(instrument_key)
        return int(row.get("depth_levels", 0)) if row else 0

    def frames_for(self, instrument_key: str) -> int:
        """Frames in which this key actually appeared (per-key, not per-socket)."""
        with self._lock:
            return int(self._frames.get(str(instrument_key), 0))

    def blank_ladder_count(self, instrument_key: str) -> int:
        """Frames kept rather than served: arrived without a usable ladder."""
        with self._lock:
            return int(self._blank_ladder_counts.get(str(instrument_key), 0))

    def frame_type_counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._raw_types)

    def subscribed_keys(self) -> List[str]:
        with self._lock:
            return list(self._keys)

    def stats(self) -> dict:
        """Socket-level counters for the probe / harness header."""
        with self._lock:
            return {
                "connected": self._connected.is_set(),
                "frames_by_type": dict(self._raw_types),
                "keys_with_frames": len(self._frames),
                "keys_subscribed": len(self._keys),
                "authorizations": self.authorizations,
                "closes": len(self.closes),
                "errors": list(self.errors[-5:]),
            }


# ---------------------------------------------------------------------------
# proto helpers (pure)
# ---------------------------------------------------------------------------


def _greeks_dict(greeks: Any) -> dict:
    """``OptionGreeks`` → dict (market-data-feed-v3.md:390-395)."""
    return {
        "delta": float(greeks.delta), "theta": float(greeks.theta),
        "gamma": float(greeks.gamma), "vega": float(greeks.vega),
        "rho": float(greeks.rho),
    }


def _ohlc_dict(market_ohlc: Any) -> Dict[str, dict]:
    """``MarketOHLC`` list keyed by interval string ('1d', 'I1', ...) (:398)."""
    out: Dict[str, dict] = {}
    for bar in getattr(market_ohlc, "ohlc", []) or []:
        out[str(bar.interval)] = {
            "open": float(bar.open), "high": float(bar.high),
            "low": float(bar.low), "close": float(bar.close),
            "vol": int(bar.vol), "ts": _epoch_iso(bar.ts),
        }
    return out


def _request_mode_name(value: Any) -> str:
    """``RequestMode`` enum → name. Note the wire says ``full_d5`` for ``full``
    (MarketDataFeedV3.proto:102-107) — the doc's mode table (:99) and the enum
    disagree on the spelling, not on the content (5 levels)."""
    try:
        return str(pb.RequestMode.Name(int(value)))
    except (ValueError, TypeError):
        return f"mode_{value}"


def _epoch_iso(value: Any) -> Optional[str]:
    """Exchange epoch millis → aware-UTC ISO through the declared door.

    ``currentTs``/``ltt``/``ts`` are epoch milliseconds
    (market-data-feed-v3.md:211, :273), i.e. absolute instants, so
    :func:`backtest.timekeeping.from_market_clock` is the right reader and the
    result is serialized with :func:`iso_utc` — never a naive string.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return iso_utc(from_market_clock(number))
