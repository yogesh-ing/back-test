/**
 * Session State Manager (PRD Task 5.2).
 * Thin, typed wrapper around sessionStorage for cross-page hand-off.
 */
const SessionState = (() => {
    const KEY_COMPARE = "compare_slots";
    const KEY_BACKTEST_PREFILL = "backtest_prefill";
    const KEY_FORWARD_PREFILL = "forward_prefill";
    const MAX_COMPARE_SLOTS = 4;

    function read(key) {
        try { return JSON.parse(sessionStorage.getItem(key) || "null"); }
        catch { return null; }
    }
    function write(key, value) {
        sessionStorage.setItem(key, JSON.stringify(value));
    }
    function clear(key) { sessionStorage.removeItem(key); }

    return {
        keys: { compare: KEY_COMPARE, backtestPrefill: KEY_BACKTEST_PREFILL, forwardPrefill: KEY_FORWARD_PREFILL },
        maxCompareSlots: MAX_COMPARE_SLOTS,
        get, set, clear,
        get compareSlots() { return read(KEY_COMPARE) || []; },
        addCompareSlot(slot) {
            const slots = this.compareSlots;
            if (slots.length >= MAX_COMPARE_SLOTS) return { ok: false, reason: "full" };
            slots.push(slot);
            write(KEY_COMPARE, slots);
            return { ok: true, index: slots.length };
        },
        get backtestPrefill() { return read(KEY_BACKTEST_PREFILL); },
        set backtestPrefill(v) { write(KEY_BACKTEST_PREFILL, v); },
        get forwardPrefill() { return read(KEY_FORWARD_PREFILL); },
        set forwardPrefill(v) { write(KEY_FORWARD_PREFILL, v); },
    };
})();
