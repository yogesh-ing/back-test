/**
 * Timeframe vocabulary — the UI's one authority (PRD backTest-enhance §1.4).
 *
 *   Timeframes.toCanonical("1D")        -> "1day"
 *   Timeframes.periodsPerYear("1min")   -> 94500
 *   Timeframes.applyTo(select, ["1day"])
 *
 * The server owns the canonical names (`backtest.data.base.CANONICAL_TIMEFRAMES`)
 * and the coverage endpoint reports which of them a symbol really has. This
 * module is the matching UI-side table: label, trading minutes per NSE day,
 * and the periods-per-year the engine needs to annualise Sharpe/CAGR honestly.
 *
 * Why periods-per-year lives here and not in a comment: a 1-minute backtest
 * scored with 252 periods/year reports a Sharpe that is silently wrong by a
 * factor of ~15. It is the single most consequential number attached to a
 * timeframe, so it is a value, not a convention.
 */
(function (global) {
    "use strict";

    /** NSE equity trading minutes in a session (09:15–15:30). */
    const MINUTES_PER_DAY = 375;
    const TRADING_DAYS_PER_YEAR = 252;

    /**
     * Finest first, matching the server's canonical order.
     * `perDay` = bars per trading day at this granularity.
     */
    const TIMEFRAMES = [
        { id: "1min", label: "1m", perDay: MINUTES_PER_DAY },
        { id: "5min", label: "5m", perDay: 75 },
        { id: "10min", label: "10m", perDay: 60 },
        { id: "15min", label: "15m", perDay: 25 },
        { id: "30min", label: "30m", perDay: 12 },
        { id: "1hour", label: "1H", perDay: 6 },
        { id: "4hour", label: "4H", perDay: 2 },
        { id: "1day", label: "1D", perDay: 1 },
        { id: "1week", label: "1W", perDay: 1 / 5 },
    ];

    const BY_ID = TIMEFRAMES.reduce((acc, tf) => Object.assign(acc, { [tf.id]: tf }), {});

    /**
     * Accepts canonical ("1day") and UI ("1D", "1d") and broker ("day",
     * "60min") spellings. Returns null for anything unrecognised so callers
     * can decide to warn rather than silently substituting daily bars.
     */
    function toCanonical(value) {
        const raw = String(value === null || value === undefined ? "" : value).trim().toLowerCase();
        if (!raw) return null;
        if (BY_ID[raw]) return raw;
        const aliases = {
            "1m": "1min", "m1": "1min", "1minute": "1min", "min1": "1min",
            "5m": "5min", "m5": "5min", "5minute": "5min",
            "15m": "15min", "m15": "15min", "15minute": "15min",
            "30min": "30min", "1h": "1hour", "h1": "1hour", "60min": "1hour", "hour": "1hour",
            "h4": "4hour", "4h": "4hour", "240min": "4hour",
            "d": "1day", "1d": "1day", "day": "1day", "daily": "1day", "1daily": "1day",
            "w": "1week", "1w": "1week", "week": "1week", "weekly": "1week",
        };
        return aliases[raw] || null;
    }

    function labelFor(value) {
        const id = toCanonical(value);
        if (!id) return String(value || "");
        return BY_ID[id].label;
    }

    /**
     * Periods in a year at this granularity — the annualisation factor.
     * Weekly uses 52 weeks, not 252/5, so a weekly Sharpe is not scaled by a
     * 5-day assumption.
     */
    function periodsPerYear(value) {
        const id = toCanonical(value);
        if (!id) return TRADING_DAYS_PER_YEAR;
        if (id === "1week") return 52;
        return Math.round(TRADING_DAYS_PER_YEAR * BY_ID[id].perDay);
    }

    /** Every canonical id, finest first. */
    function all() {
        return TIMEFRAMES.map((tf) => tf.id);
    }

    /**
     * Replace a <select>'s options with ONLY the timeframes that are really
     * stored, keeping the current selection when it survives. Passing nothing
     * (or an empty list) leaves the full list — an unknown coverage is not
     * evidence that nothing exists.
     */
    function applyTo(select, available) {
        if (!select) return [];
        const wanted = (available && available.length)
            ? TIMEFRAMES.filter((tf) => available.indexOf(tf.id) !== -1)
            : TIMEFRAMES.slice();
        if (!wanted.length) return [];   // never empty the control
        const previous = select.value;
        select.innerHTML = wanted
            .map((tf) => `<option value="${tf.id}">${tf.label}</option>`)
            .join("");
        const keep = wanted.find((tf) => tf.id === toCanonical(previous));
        select.value = keep ? keep.id : wanted[wanted.length - 1].id;
        return wanted.map((tf) => tf.id);
    }

    const Timeframes = {
        MINUTES_PER_DAY, TRADING_DAYS_PER_YEAR, TIMEFRAMES,
        toCanonical, labelFor, periodsPerYear, all, applyTo,
    };

    global.Timeframes = Timeframes;
    if (typeof module !== "undefined" && module.exports) module.exports = Timeframes;
}(typeof window !== "undefined" ? window : globalThis));
