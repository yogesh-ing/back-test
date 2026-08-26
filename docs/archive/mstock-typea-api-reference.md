# mStock TypeA API Reference

Source: `reference-code/mstock-pytradingapi-typeA-OG/tradingapi_a/mconnect.py` and its `examples/api_connect_test.py`.

Base URL: `https://api.mstock.trade`

All requests use the SDK header:

```text
X-Mirae-Version: 1
```

Authenticated requests additionally use:

```text
Authorization: token <api_key>:<access_token>
```

The SDK sends form-encoded bodies for ordinary POST/PUT/DELETE calls. The order-margin endpoint sends JSON.

## Authentication and Session

| SDK call | HTTP | Route | Parameters | Purpose |
|---|---|---|---|---|
| `login(username, password)` | POST | `/openapi/typea/connect/login` | Form: `Username`, `Password` | Login with broker username and password. This precedes TOTP verification. |
| `verify_totp(api_key, totp)` | POST | `/openapi/typea/session/verifytotp` | Form: `api_key`, `totp` | Verify the current authenticator-app TOTP and obtain an access token. |
| `generate_session(api_key, request_token, checksum)` | POST | `/openapi/typea/session/token` | Form: `api_key`, `request_token`, `checksum` | Generate an access token using SMS OTP when TOTP is not enabled. |
| `logout()` | GET | `/openapi/typea/logout` | None | End the authenticated session. |

Typical TOTP sequence:

```text
login(username, password)
ask user for current TOTP
verify_totp(api_key, totp)
use mconnect_obj.access_token for later calls
```

## Market Data

| SDK call | HTTP | Route | Parameters | Purpose |
|---|---|---|---|---|
| `get_instruments()` | GET | `/openapi/typea/instruments/scriptmaster` | None | Download the instrument master. Use this to map exchange, symbol, and security token before historical or quote requests. |
| `get_historical_chart(segment, security_token, interval, from_date, to_date)` | GET | `/openapi/typea/instruments/historical/{segment}/{security_token}/{interval}` | Query: `from`, `to` | Fetch historical OHLC data for an instrument token. Example: `NSE`, a security token, `60minute`, dates. |
| `get_ohlc(items)` | GET | `/openapi/typea/instruments/quote/ohlc` | Repeated query parameter: `i=EXCHANGE:SYMBOL` | Fetch OHLC quote data for multiple symbols. |
| `get_ltp(items)` | GET | `/openapi/typea/instruments/quote/ltp` | Repeated query parameter: `i=EXCHANGE:SYMBOL` | Fetch last traded price for multiple symbols. |
| `get_intraday_chart(segment_id, symbol, interval)` | GET | `/openapi/typea/instruments/intraday/{segment_id}/{symbol}/{interval}` | Path parameters | Fetch intraday chart data using the provider's segment ID and symbol format. |

### Historical data workflow

1. Call `get_instruments()` and save or parse the returned CSV.
2. Filter the instrument master by the desired exchange and instrument name.
3. Read the matching security token from the instrument master.
4. Call `get_historical_chart("NSE", security_token, "day", "2024-01-01", "2024-06-30")`.
5. Inspect the response before converting it to the project's canonical OHLCV format.

Do not guess an index token. NIFTY index, NIFTY ETF, and NIFTY derivatives are different instruments. The instrument master is the source of truth.

## Orders

These calls can place, change, or cancel live orders. They must not be run from the example unchanged.

| SDK call | HTTP | Route | Parameters | Purpose |
|---|---|---|---|---|
| `place_order(variety, tradingsymbol, exchange, transaction_type, order_type, quantity, product, validity, price, trigger_price, disclosed_quantity, tag)` | POST | `/openapi/typea/orders/regular` | Form order packet | Place a regular order when `variety="regular"`. |
| `place_order(..., variety="amo", ...)` | POST | `/openapi/typea/orders/amo` | Same order packet | Place an after-market order. |
| `place_order(..., variety="co", ...)` | POST | `/openapi/typea/orders/co` | Same order packet | Place a cover order. |
| `modify_order(order_id, order_type, quantity, price, validity, trigger_price, disclosed_quantity)` | PUT | `/openapi/typea/orders/regular/{order_id}` | Path `order_id`; form order fields | Modify an existing regular order. |
| `cancel_order(order_id)` | DELETE | `/openapi/typea/orders/regular/{order_id}` | Path `order_id` | Cancel one order. |
| `cancel_all()` | POST | `/openapi/typea/orders/cancelall` | None | Cancel all open orders. |
| `get_order_book()` | GET | `/openapi/typea/orders` | None | Fetch the order book for the authenticated user. |
| `get_order_details(order_id, segment="E")` | POST | `/openapi/typea/order/details` | Form: `order_no`, `segment` | Fetch the status/details of one order. |
| `calculate_order_margin(exchange, tradingsymbol, transaction_type, variety, product, order_type, quantity, price, trigger_price)` | POST | `/openapi/typea/margins/orders` | JSON order packet | Calculate required margin for a prospective order without placing it. |

Common order fields:

```text
tradingsymbol, exchange, transaction_type, order_type, quantity,
product, validity, price, trigger_price, disclosed_quantity, tag
```

## Portfolio and Account

| SDK call | HTTP | Route | Parameters | Purpose |
|---|---|---|---|---|
| `get_net_position()` | GET | `/openapi/typea/portfolio/positions` | None | Fetch current net positions. |
| `get_holdings()` | GET | `/openapi/typea/portfolio/holdings` | None | Fetch long-term delivery holdings. |
| `get_fund_summary()` | GET | `/openapi/typea/user/fundsummary` | None | Fetch account funds and margin summary. |
| `get_trade_history(from_date, to_date)` | POST | `/openapi/typea/trades` | Form: `fromdate`, `todate` | Fetch executed trade history for a date range. |
| `convert_position(tradingsymbol, exchange, transaction_type, position_type, quantity, old_product, new_product)` | POST | `/openapi/typea/portfolio/convertposition` | Form position packet | Convert an existing position between products. |
| `get_health_statistics()` | GET | `/openapi/typea/Health/GetHealthStatistics` | None | Fetch account/API health statistics. The route exists in the SDK route table. |

## Derivatives and Market Utilities

| SDK call | HTTP | Route | Parameters | Purpose |
|---|---|---|---|---|
| `loser_gainer(exchange, security_id_code, segment, type_flag)` | POST | `/openapi/typea/losergainer` | Form: `Exchange`, `SecurityIdCode`, `segment`, `TypeFlag` | Fetch loser/gainer market data for the requested segment. |
| `get_option_chain_master(exchange_id)` | GET | `/openapi/typea/getoptionchainmaster/{exchange_id}` | Path `exchange_id` | Fetch option-chain master/expiry information. |
| `get_option_chain_data(exchange_id, expiry, token)` | GET | `/openapi/typea/GetOptionChain/{exchange_id}/{expiry}/{token}` | Path parameters | Fetch option-chain data for an exchange, expiry, and underlying token. |

## Basket APIs

| SDK call | HTTP | Route | Parameters | Purpose |
|---|---|---|---|---|
| `create_basket(basket_name, basket_description)` | POST | `/openapi/typea/CreateBasket` | Form: `BaskName`, `BaskDesc` | Create a basket. |
| `fetch_basket()` | GET | `/openapi/typea/FetchBasket` | None | List baskets. |
| `rename_basket(basket_name, basket_id)` | PUT | `/openapi/typea/RenameBasket` | Form: `basketName`, `BasketId` | Rename a basket. |
| `delete_basket(basket_id)` | DELETE | `/openapi/typea/DeleteBasket` | Form: `BasketId` | Delete a basket. |
| `calculate_basket(include_existing_position, order_product, disclosed_quantity, segment, trigger_price, script_code, order_type, basket_name, operation, order_validity, order_quantity, script_status, buy_sell_indicator, basket_priority, order_price, basket_id, exchange_id)` | POST | `/openapi/typea/CalculateBasket` | Form basket packet | Calculate or apply a basket operation. Review provider semantics before use. |

## Provider Example Coverage

The provider example also calls these methods directly:

- Authentication: `login`, `verify_totp`
- Orders: place, modify, cancel, cancel all, order book, order details
- Account: positions, margin calculation, holdings, funds, trade history
- Market data: historical chart, OHLC, LTP, instruments, intraday chart
- Utilities: loser/gainer, option-chain master, option-chain data
- Baskets: create, fetch, rename, delete, calculate
- Session: logout

The example contains hard-coded order IDs and live order-placement examples. Treat it as an API demonstration, not a script to run end-to-end.

## Project Mapping

The project wrapper currently uses the following live data path:

```text
MStockSource.get_candles()
  -> MStockClient.get_bars()
  -> scriptmaster lookup
  -> get_historical_chart route
  -> canonical lowercase OHLCV DataFrame
```

For forward testing, the next implementation should consume the historical response only after its timestamp and OHLCV fields have been verified against a real response. No order API is required for the first paper-trading stage.
