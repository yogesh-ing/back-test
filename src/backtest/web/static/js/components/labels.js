/**
 * One resolver for the identifiers that have a human name (finding F11).
 *
 * Strategy keys, segment codes and option-structure codes are stored as
 * snake_case because that is what the runner, the config file and the ledger
 * need. They are not what an operator reads: "bollinger_reversion" on a Recent
 * Runs tile, "equity_intraday" in a badge and "bear_put_spread" in the risk
 * rulebook all forced the reader to parse an identifier to find out what they
 * were looking at. Every surface now prints the label from here and keeps the
 * raw key in a `title`, so the identifier is still one hover away for anyone
 * who has to match it against a config file or a DB row.
 *
 * Deliberately NOT resolved here, because the identifier *is* the name:
 *   - runner and pool names ("QA13_sma_TCS_15min", "NIFTY_50") — chosen by the
 *     operator when the runner was spawned; rewriting them would make two
 *     different runners read the same;
 *   - broker profile ids on /settings ("td_ameritrade", "india_tiered") — the
 *     key in config/brokers.yaml the operator edits;
 *   - file paths ("config/risk_limits.yaml").
 */
const Labels = (() => {
    "use strict";

    // Indicator initials stay capitals; a name stays a name.
    const ACRONYMS = new Set([
        "sma", "ema", "rsi", "macd", "roc", "vwap", "atr", "oi", "hf", "ce", "pe", "pob",
    ]);
    const PROPER = { donchian: "Donchian", bollinger: "Bollinger" };

    function parts(key) {
        return String(key ?? "").split(/[_\s]+/).filter(Boolean);
    }

    /** "swing_rsi2_meanrev" → "Swing RSI2 meanrev": one word per underscore. */
    function humanise(key) {
        return parts(key).map((part, index) => {
            const alpha = part.replace(/[0-9]+$/, "");
            const digits = part.slice(alpha.length);
            if (ACRONYMS.has(alpha.toLowerCase())) return alpha.toUpperCase() + digits;
            if (PROPER[alpha.toLowerCase()]) return PROPER[alpha.toLowerCase()] + digits;
            // an already-capitalised part ("CE_instant_buy", "NIFTY50") is a name,
            // not a word to re-case
            if (alpha === alpha.toUpperCase()) return part;
            return index === 0
                ? alpha.charAt(0).toUpperCase() + alpha.slice(1).toLowerCase() + digits
                : alpha.toLowerCase() + digits;
        }).join(" ");
    }

    /** Strategy key → the name the About panel and the dropdowns use. */
    function strategy(key) {
        return humanise(key);
    }

    // Segment display names belong to the operator's own configuration, so they
    // are read from /api/segments rather than guessed; humanise() is only the
    // fallback for a page that renders before (or without) that read.
    let segments = null;
    let warming = null;

    function warm() {
        if (segments) return Promise.resolve(segments);
        if (warming) return warming;
        warming = fetch("/api/segments")
            .then((r) => (r.ok ? r.json() : {}))
            .then((body) => {
                segments = {};
                for (const s of (body && body.segments) || []) {
                    if (s && s.name) segments[s.name] = s.display_name || humanise(s.name);
                }
                return segments;
            })
            .catch(() => { segments = {}; return segments; });
        return warming;
    }

    /** Segment code → its configured display name. */
    function segment(code, known) {
        if (known) return known;
        if (segments && segments[code]) return segments[code];
        return humanise(code);
    }

    /** Option structure code → the label the spawn form already uses. */
    function structure(code) {
        const vocab = typeof OptionConfig !== "undefined" ? OptionConfig.STRUCTURES : null;
        if (vocab) {
            const hit = vocab.find((s) => s.id === code);
            if (hit) return hit.label;
        }
        return humanise(code);
    }

    /**
     * The same label without its parenthetical gloss, for compact cells and
     * card sub-lines. "Direction-aware (bull → call spread, bear → put spread)"
     * is right in a picker and unreadable in a table row, so the truncation is
     * a house rule rather than something each view re-implements.
     */
    function structureShort(code) {
        return structure(code).split(" (")[0];
    }

    /**
     * An option book stores its trade symbol as "<UNDERLYING> <structure_type>"
     * (paper_runner.py:1872, options_bridge.py:1285), so the persisted value
     * carries a governed key and every surface that prints it would leak
     * "NIFTY short_straddle". The underlying is an exchange symbol and stays
     * verbatim; only a lowercase snake_case token after it can be a structure.
     */
    function symbol(raw) {
        return String(raw ?? "").split(" ").map((token, index) => (
            index > 0 && /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(token)
                ? structureShort(token)
                : token
        )).join(" ");
    }

    return { humanise, strategy, segment, structure, structureShort, symbol, warm };
})();

// Browser global; the Node harness reads the same file and evaluates it.
if (typeof window !== "undefined") window.Labels = Labels;
if (typeof module !== "undefined" && module.exports) module.exports = Labels;
