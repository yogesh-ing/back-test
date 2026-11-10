"""Upstox Market Data Feed V3 — protobuf codec round-trip + depth sanitizer.

SYNTHETIC DATA, CLEARLY LABELLED: every number in this file is a literal this
test wrote into a message with the generated class and then read back. Nothing
here is market data, nothing here proves the feed is live, and none of it may be
quoted as evidence of what the exchange sent. What it does prove is the decode
path: the checked-in gencode can serialize and parse the documented shapes
(:mod:`backtest.data.upstox_market_data_feed_v3_pb2` generated from
``docs/broker-reference/upstox/MarketDataFeedV3.proto``), and the sanitizer
turns a decoded ``MarketFullFeed`` into the ladder vocabulary the comparison
harness expects.

The live proof is ``tools/upstox_ws_probe.py``, run against the real socket.
"""

from __future__ import annotations

import time

import pytest

from backtest.data import upstox_market_data_feed_v3_pb2 as pb
from backtest.data.upstox_ws_wrapper import (
    UpstoxFeedError,
    UpstoxWebSocketWrapper,
    SubscriptionLimitError,
    _redact_ws_url,
)


# ---------------------------------------------------------------------------
# fixtures (all hand-built messages — see module docstring)
# ---------------------------------------------------------------------------


def make_market_feed(levels):
    """SYNTHETIC ``full`` frame: one instrument, 5 depth levels, Greeks, OHLC.

    ``levels`` is a list of ``(bidP, bidQ, askP, askQ)`` tuples exactly as the
    wire sends them (MarketDataFeedV3.proto:21-26 puts both sides in ONE Quote).
    Values are invented test literals, not quotes anybody posted.
    """
    response = pb.FeedResponse()
    response.type = pb.Type.Value("live_feed")
    response.currentTs = 1762412345678  # synthetic epoch millis
    feed = response.feeds["NSE_FO|64439"]
    feed.requestMode = pb.RequestMode.Value("full_d5")
    market = feed.fullFeed.marketFF
    market.ltpc.ltp = 213.75
    market.ltpc.ltt = 1762412340000
    market.ltpc.ltq = 150
    market.ltpc.cp = 494.05
    for bid_p, bid_q, ask_p, ask_q in levels:
        quote = market.marketLevel.bidAskQuote.add()
        quote.bidP = bid_p
        quote.bidQ = bid_q
        quote.askP = ask_p
        quote.askQ = ask_q
    market.optionGreeks.delta = 0.4952
    market.optionGreeks.theta = -8.4067
    market.vtt = 779625
    market.oi = 210000
    bar = market.marketOHLC.ohlc.add()
    bar.interval = "1d"
    bar.open = 400.0
    bar.high = 400.0
    bar.low = 208.7
    bar.close = 213.75
    bar.vol = 779400
    bar.ts = 1762400000000
    return response


TWO_SIDED = [
    (213.45, 75, 213.90, 525),
    (213.30, 225, 213.95, 150),
    (213.25, 150, 214.00, 75),
    (213.20, 150, 214.05, 225),
    (213.10, 225, 214.10, 75),
]


@pytest.fixture
def wrapper():
    return UpstoxWebSocketWrapper(access_token="synthetic-token-not-a-secret")


# ---------------------------------------------------------------------------
# 1. codec round-trip through the checked-in gencode
# ---------------------------------------------------------------------------


class TestProtobufCodecRoundTrip:
    def test_encode_then_decode_preserves_every_field(self):
        """Build with the generated class, serialize, re-parse, compare."""
        original = make_market_feed(TWO_SIDED)
        payload = original.SerializeToString()
        assert isinstance(payload, bytes) and len(payload) > 0

        decoded = pb.FeedResponse()
        decoded.ParseFromString(payload)

        assert pb.Type.Name(decoded.type) == "live_feed"
        assert decoded.currentTs == original.currentTs
        feed = decoded.feeds["NSE_FO|64439"]
        assert pb.RequestMode.Name(feed.requestMode) == "full_d5"
        market = feed.fullFeed.marketFF
        assert market.ltpc.ltp == pytest.approx(213.75)
        assert market.ltpc.cp == pytest.approx(494.05)
        assert market.vtt == 779625
        assert market.optionGreeks.delta == pytest.approx(0.4952)
        assert len(market.marketLevel.bidAskQuote) == 5
        assert market.marketLevel.bidAskQuote[0].bidP == pytest.approx(213.45)
        assert market.marketLevel.bidAskQuote[-1].askQ == 75

    def test_oneof_discriminators_survive_the_wire(self):
        decoded = pb.FeedResponse()
        decoded.ParseFromString(make_market_feed(TWO_SIDED).SerializeToString())
        feed = decoded.feeds["NSE_FO|64439"]
        assert feed.WhichOneof("FeedUnion") == "fullFeed"
        assert feed.fullFeed.WhichOneof("FullFeedUnion") == "marketFF"

    def test_index_full_feed_has_no_ladder_by_schema(self):
        """``IndexFullFeed`` is ltpc + OHLC only (proto:71-74) — not a broken book."""
        response = pb.FeedResponse()
        response.type = pb.Type.Value("live_feed")
        response.currentTs = 1762412345678
        index = response.feeds["NSE_INDEX|Nifty 50"].fullFeed.indexFF
        index.ltpc.ltp = 24593.85
        decoded = pb.FeedResponse()
        decoded.ParseFromString(response.SerializeToString())
        assert decoded.feeds["NSE_INDEX|Nifty 50"].fullFeed.WhichOneof("FullFeedUnion") == "indexFF"

    def test_market_info_frame_carries_segment_status(self):
        response = pb.FeedResponse()
        response.type = pb.Type.Value("market_info")
        response.marketInfo.segmentStatus["NSE_FO"] = pb.MarketStatus.Value("NORMAL_OPEN")
        decoded = pb.FeedResponse()
        decoded.ParseFromString(response.SerializeToString())
        assert decoded.marketInfo.segmentStatus["NSE_FO"] == pb.MarketStatus.Value("NORMAL_OPEN")


# ---------------------------------------------------------------------------
# 2. decode → sanitized row (synthetic frames, real code path)
# ---------------------------------------------------------------------------


class TestDepthSanitizer:
    def test_full_two_sided_book_is_ok(self, wrapper):
        row = wrapper._ingest(make_market_feed(TWO_SIDED))[
            "NSE_FO|64439"
        ]
        assert row["ladder_status"] == "ok"
        assert row["bid"] == pytest.approx(213.45)
        assert row["ask"] == pytest.approx(213.90)
        assert row["spread"] == pytest.approx(0.45)
        assert row["depth_levels"] == 10
        assert [x["price"] for x in row["depth"]["bid"]] == [
            pytest.approx(p) for p, _, _, _ in TWO_SIDED
        ]

    def test_zero_price_levels_are_dropped_not_priced(self, wrapper):
        """proto3 defaults unfilled levels to 0. A 0.00 price is *absent*.

        The mStock incident booked exactly these as ₹0.05/₹4 fills, so the test
        pins that they never reach a ladder here.
        """
        levels = TWO_SIDED[:3] + [(0.0, 0, 0.0, 0), (0.0, 0, 0.0, 0)]
        row = wrapper._ingest(make_market_feed(levels))["NSE_FO|64439"]
        assert row["depth_levels"] == 6
        assert all(x["price"] > 0 for x in row["depth"]["bid"] + row["depth"]["ask"])

    def test_positive_price_with_zero_quantity_is_absent(self, wrapper):
        levels = [(213.45, 0, 213.90, 525)] + TWO_SIDED[1:]
        row = wrapper._ingest(make_market_feed(levels))["NSE_FO|64439"]
        assert [x["price"] for x in row["depth"]["bid"]] == [
            pytest.approx(p) for p, _, _, _ in TWO_SIDED[1:]
        ]

    def test_crossed_book_publishes_no_side(self, wrapper):
        levels = [(213.45, 75, 213.40, 525)] + TWO_SIDED[1:]
        row = wrapper._ingest(make_market_feed(levels))["NSE_FO|64439"]
        assert row["ladder_status"] == "crossed"
        assert row["bid"] == 0.0 and row["ask"] == 0.0

    def test_a_folded_ask_step_prices_nothing(self, wrapper):
        """Level 1 cheaper than level 0: the touch is not at index 0, so 0 is not used.

        Upstox splits both ladders out of one array, so a mis-ordered frame is
        possible here too; refusing it is the same rule mStock applies to the ten
        such packets of 2026-11-09, and sorting would invent a price the venue
        never showed.
        """
        levels = [(213.45, 75, 213.90, 525), (213.30, 225, 213.85, 150)]
        row = wrapper._ingest(make_market_feed(levels))["NSE_FO|64439"]
        assert row["ladder_status"] == "unsorted"
        assert row["bid"] == 0.0 and row["ask"] == 0.0

    def test_one_sided_book_keeps_the_real_side(self, wrapper):
        levels = [(213.45, 75, 0.0, 0), (213.40, 150, 0.0, 0)]
        row = wrapper._ingest(make_market_feed(levels))["NSE_FO|64439"]
        assert row["ladder_status"] == "one_sided"
        assert row["bid"] == pytest.approx(213.45) and row["ask"] == 0.0

    def test_empty_ladder_is_blank(self, wrapper):
        row = wrapper._ingest(make_market_feed([]))["NSE_FO|64439"]
        assert row["ladder_status"] == "blank"
        assert row["depth_levels"] == 0

    def test_index_frame_is_none_not_blank(self, wrapper):
        response = pb.FeedResponse()
        response.type = pb.Type.Value("live_feed")
        response.currentTs = 1762412345678
        response.feeds["NSE_INDEX|Nifty 50"].fullFeed.indexFF.ltpc.ltp = 24593.85
        row = wrapper._ingest(response)["NSE_INDEX|Nifty 50"]
        assert row["ladder_status"] == "none"
        assert row["ltp"] == pytest.approx(24593.85)


class TestFrameRetention:
    def test_ltpc_only_delta_keeps_last_honest_ladder(self, wrapper):
        """A frame with no ladder must not erase the book (doc's delta semantics).

        Arrival stamps are passed explicitly so the two clocks are provably
        different instants rather than whatever the monotonic timer read.
        """
        wrapper._ingest(make_market_feed(TWO_SIDED), recv_mono=100.0,
                        recv_utc="2026-11-06T03:45:00+00:00")
        delta = pb.FeedResponse()
        delta.type = pb.Type.Value("live_feed")
        delta.currentTs = 1762412399999
        delta.feeds["NSE_FO|64439"].ltpc.ltp = 214.40
        row = wrapper.get_quote("NSE_FO|64439")
        assert row["ladder_status"].startswith("ok")

        kept = wrapper._ingest(delta, recv_mono=200.0,
                               recv_utc="2026-11-06T03:46:00+00:00")["NSE_FO|64439"]
        assert kept["ladder_status"] == "kept:none"
        assert kept["ltp"] == pytest.approx(214.40)
        assert kept["bid"] == pytest.approx(213.45), "ladder retained, not invented"
        assert kept["ladder_mono"] == 100.0, "the ladder keeps its own older stamp"
        assert kept["ltp_mono"] == 200.0, "the two clocks stay separate"
        assert kept["recv_utc"] == "2026-11-06T03:46:00+00:00"
        # A bare ``ltpc`` union has no depth field by schema (proto:93-98), so
        # it is a price delta, not a garbage ladder frame: retained, not counted.
        assert wrapper.blank_ladder_count("NSE_FO|64439") == 0

    def test_ladder_bearing_frame_with_empty_book_is_counted(self, wrapper):
        """``marketFF`` with no priced level is the incident shape → counted."""
        wrapper._ingest(make_market_feed(TWO_SIDED), recv_mono=100.0)
        for _ in range(3):
            row = wrapper._ingest(make_market_feed([]), recv_mono=200.0)["NSE_FO|64439"]
            assert row["ladder_status"] == "kept:blank"
        assert wrapper.frames_for("NSE_FO|64439") == 4
        assert wrapper.blank_ladder_count("NSE_FO|64439") == 3

    def test_index_frames_never_count_as_blank_ladders(self, wrapper):
        """Live 2026-11-06: the index sends hundreds of depth-less frames.

        They must not read as ladder failures, or the harness would report the
        index as the worst feed in the comparison.
        """
        def index_frame():
            response = pb.FeedResponse()
            response.type = pb.Type.Value("live_feed")
            response.currentTs = 1762412345678
            response.feeds["NSE_INDEX|Nifty 50"].fullFeed.indexFF.ltpc.ltp = 24658.45
            return response

        for _ in range(5):
            row = wrapper._ingest(index_frame())["NSE_INDEX|Nifty 50"]
        assert row["ladder_status"] == "none"
        assert row["depth_levels"] == 0
        assert wrapper.frames_for("NSE_INDEX|Nifty 50") == 5
        assert wrapper.blank_ladder_count("NSE_INDEX|Nifty 50") == 0

    def test_first_frame_without_ladder_is_served_as_it_arrived(self, wrapper):
        delta = pb.FeedResponse()
        delta.type = pb.Type.Value("live_feed")
        delta.feeds["NSE_FO|39735"].ltpc.ltp = 11.55
        row = wrapper._ingest(delta)["NSE_FO|39735"]
        assert row["ladder_status"] == "none"
        assert row["depth_levels"] == 0


class TestRowShapeAndClock:
    def test_row_exposes_the_mstock_field_names(self, wrapper):
        """The comparison harness consumes either feed by these names."""
        row = wrapper._ingest(make_market_feed(TWO_SIDED))["NSE_FO|64439"]
        for field in (
            "ltp", "bid", "ask", "volume", "oi", "ohlc", "depth", "timestamp",
            "recv_mono", "recv_utc", "source", "mode", "ladder_status",
            "ltp_mono", "ladder_mono",
        ):
            assert field in row, field
        assert row["source"] == "ws"
        assert set(row["depth"]) == {"bid", "ask"}

    def test_recv_utc_states_its_own_clock(self, wrapper):
        row = wrapper._ingest(make_market_feed(TWO_SIDED))["NSE_FO|64439"]
        assert row["recv_utc"].endswith("+00:00")
        assert row["timestamp"].endswith("+00:00")
        assert "+" in row["last_trade_ts"]

    def test_ingest_without_stamps_uses_live_clocks(self, wrapper):
        before = time.monotonic()
        row = wrapper._ingest(make_market_feed(TWO_SIDED))["NSE_FO|64439"]
        assert row["recv_mono"] >= before
        assert row["recv_utc"].endswith("+00:00")

    def test_greeks_and_ohlc_land_from_the_documented_fields(self, wrapper):
        row = wrapper._ingest(make_market_feed(TWO_SIDED))["NSE_FO|64439"]
        assert row["greeks"]["delta"] == pytest.approx(0.4952)
        assert set(row["greeks"]) == {"delta", "theta", "gamma", "vega", "rho"}
        assert row["ohlc"]["open"] == pytest.approx(400.0)
        assert row["oi"] == pytest.approx(210000.0)
        assert row["volume"] == 779625


class TestEnvelopeAndLimits:
    def test_envelope_is_utf8_bytes_with_documented_shape(self, wrapper):
        payload = wrapper.build_envelope("sub", ["NSE_FO|64439"], "full")
        assert isinstance(payload, bytes), "doc: request must be binary, not text"
        import json

        body = json.loads(payload.decode("utf-8"))
        assert set(body) == {"guid", "method", "data"}
        assert body["method"] == "sub"
        assert body["data"] == {"mode": "full", "instrumentKeys": ["NSE_FO|64439"]}
        assert len(body["guid"]) == 20

    def test_unsub_envelope_has_no_mode(self, wrapper):
        import json

        body = json.loads(wrapper.build_envelope("unsub", ["NSE_FO|64439"]).decode())
        assert "mode" not in body["data"]

    def test_full_mode_refuses_more_keys_than_the_documented_ceiling(self, wrapper):
        with pytest.raises(SubscriptionLimitError):
            wrapper.check_limits([f"NSE_FO|{i}" for i in range(2001)], "full")
        wrapper.check_limits([f"NSE_FO|{i}" for i in range(2000)], "full")

    def test_change_mode_requires_a_subscribed_key(self, wrapper):
        with pytest.raises(UpstoxFeedError):
            wrapper.change_mode(["NSE_FO|64439"], "ltpc")

    def test_unknown_mode_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            UpstoxWebSocketWrapper(access_token="t", mode="full_d31")

    def test_socket_uri_credential_is_redacted(self):
        url = ("wss://x.upstox.com/market-data-feeder/v3/upstox-developer-api/feeds"
               "?requestId=abc&code=secret123")
        clean = _redact_ws_url(url)
        assert "secret123" not in clean and "abc" not in clean
        assert clean.endswith("code=<redacted>")
