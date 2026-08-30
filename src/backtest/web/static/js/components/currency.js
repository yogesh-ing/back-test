/**
 * One money formatter for the whole app (gap G3).
 *
 * The Backtest/Compare pages printed `$`, the Forward page printed `₹`, and the
 * Dashboard mixed both — the same number looked like two different currencies
 * depending on the tab. The symbol, locale and code now come from the server via
 * the `<body data-currency-*>` attributes set in base.html (configured with
 * `--currency` / `BACKTEST_CURRENCY`, ₹ by default because this app trades NSE).
 *
 * Usage: Money.signed(metrics.total_pnl) → "-₹1,234.50"
 */
const Money = (() => {
    const DEFAULTS = { code: "INR", symbol: "₹", locale: "en-IN" };

    function cfg() {
        const ds = (typeof document !== "undefined" && document.body) ? document.body.dataset : {};
        return {
            code: ds.currencyCode || DEFAULTS.code,
            symbol: ds.currencySymbol != null ? ds.currencySymbol : DEFAULTS.symbol,
            locale: ds.currencyLocale || DEFAULTS.locale,
        };
    }

    function symbol() { return cfg().symbol; }

    /** Unsigned, locale-grouped magnitude. */
    function amount(value, dp = 2) {
        const n = Number(value);
        const safe = Number.isFinite(n) ? Math.abs(n) : 0;
        return safe.toLocaleString(cfg().locale, {
            minimumFractionDigits: dp, maximumFractionDigits: dp,
        });
    }

    /** "₹1,234.50" / "-₹1,234.50" */
    function format(value, dp = 2) {
        const n = Number(value);
        const sign = Number.isFinite(n) && n < 0 ? "-" : "";
        return `${sign}${symbol()}${amount(value, dp)}`;
    }

    /** "+₹1,234.50" — for P&L, where the sign is the point. */
    function signed(value, dp = 2) {
        const n = Number(value);
        const sign = Number.isFinite(n) && n < 0 ? "-" : "+";
        return `${sign}${symbol()}${amount(value, dp)}`;
    }

    return { cfg, symbol, amount, format, signed, DEFAULTS };
})();

if (typeof globalThis !== "undefined") globalThis.Money = Money;
