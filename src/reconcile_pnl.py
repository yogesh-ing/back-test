#!/usr/bin/env python
"""
PnL Reconciliation & Strategy Verification Report

This script:
1. Validates PnL calculations by checking if charges/STT are properly applied
2. Creates a single verification file with all strategy trades including quantity, time, price
3. Identifies anomalies in PnL calculations

Usage:
    python src/reconcile_pnl.py [--date YYYY-MM-DD] [--all]
"""

import argparse
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from backtest.simulator.fees import IndiaEquityFees, CommissionCalculator


def calculate_expected_charges(
    symbol: str,
    quantity: float,
    entry_price: float,
    exit_price: float,
    holding_period_minutes: int,
    instrument_type: str = "equity",
) -> dict:
    """Calculate expected statutory charges for a trade."""
    
    from backtest.simulator.fees import TradeSegment, OrderSide
    from decimal import Decimal
    
    # Determine if intraday or delivery
    is_intraday = holding_period_minutes < 480  # Less than 8 hours
    
    segment = TradeSegment.EQUITY_INTRADAY if is_intraday else TradeSegment.EQUITY_DELIVERY
    
    calc = CommissionCalculator.for_broker("zerodha")
    
    qty_dec = Decimal(str(quantity))
    entry_dec = Decimal(str(entry_price))
    exit_dec = Decimal(str(exit_price))
    
    # Calculate charges for buy side
    buy_breakdown = calc.calculate(
        quantity=qty_dec,
        fill_price=entry_dec,
        side=OrderSide.BUY,
        segment=segment,
    )
    
    # Calculate charges for sell side
    sell_breakdown = calc.calculate(
        quantity=qty_dec,
        fill_price=exit_dec,
        side=OrderSide.SELL,
        segment=segment,
    )
    
    total_commission = float(buy_breakdown.brokerage + sell_breakdown.brokerage)
    total_stt = float(buy_breakdown.get("stt") + sell_breakdown.get("stt"))
    total_exchange = float(buy_breakdown.exchange_fees + sell_breakdown.exchange_fees)
    total_sebi = float(buy_breakdown.get("sebi_turnover") + sell_breakdown.get("sebi_turnover"))
    total_ipft = float(buy_breakdown.get("ipft") + sell_breakdown.get("ipft"))
    total_stamp = float(buy_breakdown.get("stamp_duty") + sell_breakdown.get("stamp_duty"))
    total_gst = float(buy_breakdown.get("gst") + sell_breakdown.get("gst"))
    total_dp = float(buy_breakdown.get("dp_charges") + sell_breakdown.get("dp_charges"))
    
    total_charges = (
        total_commission
        + total_stt
        + total_exchange
        + total_sebi
        + total_ipft
        + total_stamp
        + total_gst
        + total_dp
    )
    
    return {
        "brokerage": total_commission,
        "stt": total_stt,
        "exchange": total_exchange,
        "sebi": total_sebi,
        "ipft": total_ipft,
        "stamp": total_stamp,
        "gst": total_gst,
        "dp": total_dp,
        "total_charges": total_charges,
        "is_intraday": is_intraday,
    }


def reconcile_trades(conn, target_date: date) -> list:
    """Reconcile trades for a given date and identify anomalies."""
    
    cur = conn.cursor()
    
    # Get all trades for the date
    cur.execute("""
        SELECT 
            t.trade_id,
            t.symbol,
            t.strategy_name,
            t.direction,
            t.quantity,
            t.entry_price,
            t.exit_price,
            t.entry_time AT TIME ZONE 'Asia/Kolkata' as entry_ist,
            t.exit_time AT TIME ZONE 'Asia/Kolkata' as exit_ist,
            EXTRACT(EPOCH FROM (t.exit_time - t.entry_time)) / 60 as holding_minutes,
            t.gross_pnl,
            t.net_pnl,
            t.commission_total,
            t.slippage_total,
            t.exit_reason
        FROM trades t
        WHERE DATE(t.entry_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata') = %s
        ORDER BY t.entry_time
    """, (target_date,))
    
    trades = cur.fetchall()
    anomalies = []
    
    print(f"\n{'='*160}")
    print(f"PnL RECONCILIATION REPORT FOR {target_date}")
    print(f"{'='*160}\n")
    print(f"Total trades: {len(trades)}\n")
    
    # Summary counters
    total_gross = Decimal('0')
    total_net = Decimal('0')
    total_recorded_commission = Decimal('0')
    total_expected_charges = Decimal('0')
    
    trades_with_missing_charges = 0
    trades_with_anomalies = []
    
    for idx, trade in enumerate(trades, 1):
        trade_id, symbol, strategy, direction, qty, entry_px, exit_px, entry_time, exit_time, holding_min, gross_pnl, net_pnl, comm_total, slippage, exit_reason = trade
        
        qty = float(qty)
        entry_px = float(entry_px)
        exit_px = float(exit_px)
        gross_pnl = float(gross_pnl)
        net_pnl = float(net_pnl)
        comm_total = float(comm_total) if comm_total else 0.0
        slippage = float(slippage) if slippage else 0.0
        holding_min = float(holding_min) if holding_min else 0
        
        # Calculate expected charges
        expected = calculate_expected_charges(
            symbol=symbol,
            quantity=qty,
            entry_price=entry_px,
            exit_price=exit_px,
            holding_period_minutes=holding_min,
        )
        
        # Check for anomalies
        charge_anomaly = abs(comm_total - expected['total_charges']) > 0.01
        pnl_consistency = abs((gross_pnl - comm_total - slippage) - net_pnl) > 0.01
        
        if charge_anomaly or comm_total == 0:
            trades_with_missing_charges += 1
            
        if pnl_consistency:
            trades_with_anomalies.append({
                'trade_id': trade_id,
                'symbol': symbol,
                'issue': f'PnL inconsistency: gross({gross_pnl:.2f}) - comm({comm_total:.2f}) - slippage({slippage:.2f}) = {gross_pnl - comm_total - slippage:.2f} != net({net_pnl:.2f})'
            })
        
        # Accumulate totals
        total_gross += Decimal(str(gross_pnl))
        total_net += Decimal(str(net_pnl))
        total_recorded_commission += Decimal(str(comm_total))
        total_expected_charges += Decimal(str(expected['total_charges']))
        
        # Print trade details
        print(f"{idx:3}. {symbol:12} | {strategy:20} | {direction:6} | Qty: {qty:8.2f}")
        print(f"     Entry: Rs{entry_px:10.2f} @ {entry_time.strftime('%H:%M:%S')} | Exit: Rs{exit_px:10.2f} @ {exit_time.strftime('%H:%M:%S')} | Hold: {holding_min:.0f}min ({'Intraday' if expected['is_intraday'] else 'Delivery'})")
        print(f"     Gross PnL: Rs{gross_pnl:10.2f} | Net PnL: Rs{net_pnl:10.2f}")
        print(f"     Recorded Charges: Rs{comm_total:10.2f} | Expected Charges: Rs{expected['total_charges']:10.2f} | Diff: Rs{comm_total - expected['total_charges']:10.2f}")
        
        if expected['total_charges'] > 0:
            print(f"       -> Brokerage: Rs{expected['brokerage']:.2f}, STT: Rs{expected['stt']:.2f}, Exchange: Rs{expected['exchange']:.2f}, GST: Rs{expected['gst']:.2f}, DP: Rs{expected['dp']:.2f}")
        
        if charge_anomaly:
            print(f"     WARNING: Charge mismatch! Expected Rs{expected['total_charges']:.2f}, got Rs{comm_total:.2f}")
        
        if pnl_consistency:
            print(f"     WARNING: PnL formula broken!")
        
        print()
    
    # Summary
    print(f"\n{'='*160}")
    print("RECONCILIATION SUMMARY")
    print(f"{'='*160}")
    print(f"Total Gross PnL:         Rs{float(total_gross):12.2f}")
    print(f"Total Net PnL:           Rs{float(total_net):12.2f}")
    print(f"Recorded Commissions:    Rs{float(total_recorded_commission):12.2f}")
    print(f"Expected Total Charges:  Rs{float(total_expected_charges):12.2f}")
    print(f"Missing Charges:         Rs{float(total_expected_charges - total_recorded_commission):12.2f}")
    print(f"\nTrades without charges:  {trades_with_missing_charges}/{len(trades)} ({100*trades_with_missing_charges/max(len(trades),1):.1f}%)")
    print(f"Trades with PnL anomalies: {len(trades_with_anomalies)}")
    
    if trades_with_anomalies:
        print(f"\nPnL Anomaly Details:")
        for a in trades_with_anomalies:
            print(f"  - {a['trade_id']}: {a['symbol']} - {a['issue']}")
    
    # Generate verification file
    generate_verification_file(trades, target_date)
    
    cur.close()
    
    return anomalies


def generate_verification_file(trades: list, target_date: date):
    """Generate a single CSV file with all trade details for strategy verification."""
    
    output_dir = Path("tools/out")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    filename = output_dir / f"strategy_trades_{target_date}.csv"
    
    with open(filename, 'w') as f:
        # Header
        f.write("Strategy Verification Report\n")
        f.write(f"Date: {target_date}\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        # Column headers
        f.write(",".join([
            "#",
            "Symbol",
            "Strategy",
            "Direction",
            "Quantity",
            "Entry Price",
            "Entry Time (IST)",
            "Exit Price",
            "Exit Time (IST)",
            "Holding Period (min)",
            "Trade Type",
            "Gross PnL",
            "Net PnL",
            "Commission",
            "Slippage",
            "Exit Reason",
            "Expected Charges",
            "Charge Status"
        ]) + "\n")
        
        for idx, trade in enumerate(trades, 1):
            trade_id, symbol, strategy, direction, qty, entry_px, exit_px, entry_time, exit_time, holding_min, gross_pnl, net_pnl, comm_total, slippage, exit_reason = trade
            
            qty = float(qty)
            entry_px = float(entry_px)
            exit_px = float(exit_px)
            gross_pnl = float(gross_pnl)
            net_pnl = float(net_pnl)
            comm_total = float(comm_total) if comm_total else 0.0
            slippage = float(slippage) if slippage else 0.0
            holding_min = float(holding_min) if holding_min else 0
            
            # Calculate expected charges
            expected = calculate_expected_charges(
                symbol=symbol,
                quantity=qty,
                entry_price=entry_px,
                exit_price=exit_px,
                holding_period_minutes=holding_min,
            )
            
            trade_type = "Intraday" if expected['is_intraday'] else "Delivery"
            charge_status = "OK" if abs(comm_total - expected['total_charges']) < 0.01 else "MISSING"
            
            f.write(",".join([
                str(idx),
                symbol,
                strategy or "",
                direction,
                f"{qty:.2f}",
                f"{entry_px:.2f}",
                entry_time.strftime('%Y-%m-%d %H:%M:%S'),
                f"{exit_px:.2f}",
                exit_time.strftime('%Y-%m-%d %H:%M:%S'),
                f"{holding_min:.0f}",
                trade_type,
                f"{gross_pnl:.2f}",
                f"{net_pnl:.2f}",
                f"{comm_total:.2f}",
                f"{slippage:.2f}",
                exit_reason or "",
                f"{expected['total_charges']:.2f}",
                charge_status
            ]) + "\n")
    
    print(f"\nVerification file written to: {filename.absolute()}")


def main():
    parser = argparse.ArgumentParser(description="PnL Reconciliation & Strategy Verification")
    parser.add_argument("--date", type=str, help="Target date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--all", action="store_true", help="Process all dates with trading activity")
    args = parser.parse_args()
    
    load_dotenv()
    db_url = os.getenv('FORWARD_TEST_DB_URL').replace('postgresql+psycopg2://', 'postgresql://')
    
    conn = psycopg2.connect(db_url)
    
    try:
        if args.all:
            # Get all dates with trades
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT DATE(entry_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata') as trade_date
                FROM trades
                ORDER BY trade_date DESC
            """)
            dates = [row[0] for row in cur.fetchall()]
            cur.close()
            
            print(f"Found {len(dates)} dates with trading activity")
            for d in dates[:5]:  # Process last 5 days
                print(f"\n{'#'*160}")
                print(f"# Processing date: {d}")
                print(f"{'#'*160}")
                reconcile_trades(conn, d)
        else:
            target_date = date.fromisoformat(args.date) if args.date else date.today()
            reconcile_trades(conn, target_date)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
