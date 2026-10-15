"""Export closed and open trades to CSV for audit, tax, and performance analysis.

Usage:
    python scripts/export_trades.py [--mode paper|live] [--output-dir tradeData]

Outputs:
    - tradeData/closed_trades.csv  (all historically closed trades)
    - tradeData/open_positions.csv (currently open positions with MTM)
"""

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def get_db_connection() -> sqlite3.Connection:
    """Connect to the forward test database."""
    conn = sqlite3.connect("forward_test.db")
    conn.row_factory = sqlite3.Row
    return conn


def export_closed_trades(output_dir: Path, mode: str = None) -> int:
    """Export all closed trades from live_trades table.
    
    Args:
        output_dir: Directory to write CSV file
        mode: Filter by 'paper' or 'live', None = all
        
    Returns:
        Number of trades exported
    """
    conn = get_db_connection()
    
    query = """
        SELECT 
            t.strategy_name,
            t.symbol,
            t.direction,
            t.entry_time,
            t.entry_price,
            t.exit_time,
            t.exit_price,
            t.quantity,
            t.net_pnl,
            t.gross_pnl,
            t.commission_total as total_charges,
            t.portfolio_id,
            t.exit_reason
        FROM trades t
    """
    params = []
    
    if mode:
        # Join with portfolios to filter by mode
        query = query.replace("FROM trades t", "FROM trades t JOIN portfolios p ON t.portfolio_id = p.portfolio_id")
        query += " WHERE p.mode = ?"
        params.append(mode)
    
    query += " ORDER BY t.exit_time DESC"
    
    trades = conn.execute(query, params).fetchall()
    
    output_file = output_dir / "closed_trades.csv"
    
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Strategy",
            "Instrument", 
            "Timeframe",
            "Entry DateTime",
            "Entry Price",
            "Exit DateTime", 
            "Exit Price",
            "Quantity",
            "Net PnL (Rs)",
            "Side",
            "Gross PnL (Rs)",
            "Total Charges (Rs)",
            "Portfolio ID",
            "Instance ID"
        ])
        
        for trade in trades:
            writer.writerow([
                trade["strategy_name"] or "N/A",
                trade["symbol"],
                "N/A",  # timeframe not stored in trades table
                trade["entry_time"],
                f"{trade['entry_price']:.2f}",
                trade["exit_time"],
                f"{trade['exit_price']:.2f}",
                trade["quantity"],
                f"{trade['net_pnl']:.2f}",
                trade["direction"],
                f"{trade['gross_pnl']:.2f}",
                f"{trade['total_charges']:.2f}",
                trade["portfolio_id"],
                "N/A",  # instance_id not directly stored
            ])
    
    conn.close()
    
    total_pnl = sum(t["net_pnl"] for t in trades)
    print(f"[OK] Exported {len(trades)} closed trades to {output_file}")
    print(f"   Total Net PnL: Rs{total_pnl:+,.2f}")
    
    return len(trades)


def export_open_positions(output_dir: Path, mode: str = None) -> int:
    """Export currently open positions from portfolio state.
    
    This queries the running runners via API since open positions live in memory,
    not in the DB (only closed trades persist to DB).
    
    Args:
        output_dir: Directory to write CSV file
        mode: Filter by 'paper' or 'live', None = all
        
    Returns:
        Number of positions exported
    """
    import requests
    
    # Get portfolio summary which includes all runners and their positions
    resp = requests.get("http://127.0.0.1:5000/api/portfolio/summary")
    if resp.status_code != 200:
        print(f"❌ Failed to fetch portfolio summary: {resp.status_code}")
        return 0
    
    data = resp.json()
    runners = data.get("runners", [])
    
    positions = []
    for runner in runners:
        if mode and runner.get("mode") != mode:
            continue
            
        instance_id = runner.get("instance_id")
        strategy = runner.get("strategy_name")
        
        # Fetch detailed runner state for positions
        detail_resp = requests.get(
            f"http://127.0.0.1:5000/api/portfolio/runner/{instance_id}"
        )
        if detail_resp.status_code == 200:
            detail = detail_resp.json().get("runner", {})
            runner_positions = detail.get("positions", {})
            
            for symbol, pos in runner_positions.items():
                positions.append({
                    "strategy": strategy,
                    "instance_id": instance_id[:12] + "...",
                    "symbol": symbol,
                    "quantity": pos.get("qty", 0),
                    "entry_price": pos.get("avg_price", 0),
                    "current_price": pos.get("last_price", 0),
                    "unrealized_pnl": pos.get("unrealized_pnl", 0),
                    "entry_time": pos.get("entry_ts", ""),
                    "deployed_capital": pos.get("qty", 0) * pos.get("avg_price", 0),
                })
    
    output_file = output_dir / "open_positions.csv"
    
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Strategy",
            "Instance ID",
            "Symbol",
            "Quantity",
            "Entry Price",
            "Current Price",
            "Unrealized PnL (Rs)",
            "Entry DateTime",
            "Deployed Capital (Rs)",
        ])
        
        for pos in positions:
            writer.writerow([
                pos["strategy"],
                pos["instance_id"],
                pos["symbol"],
                pos["quantity"],
                f"{pos['entry_price']:.2f}",
                f"{pos['current_price']:.2f}",
                f"{pos['unrealized_pnl']:+.2f}",
                pos["entry_time"],
                f"{pos['deployed_capital']:.2f}",
            ])
    
    total_unrealized = sum(p["unrealized_pnl"] for p in positions)
    total_deployed = sum(p["deployed_capital"] for p in positions)
    
    print(f"[OK] Exported {len(positions)} open positions to {output_file}")
    print(f"   Total Unrealized PnL: Rs{total_unrealized:+,.2f}")
    print(f"   Total Deployed Capital: Rs{total_deployed:,.2f}")
    
    return len(positions)


def main():
    parser = argparse.ArgumentParser(description="Export trades to CSV")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        help="Filter by trading mode (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tradeData"),
        help="Output directory (default: tradeData)",
    )
    
    args = parser.parse_args()
    
    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 80)
    print("TRADE DATA EXPORT")
    print("=" * 80)
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"Mode: {args.mode or 'all'}")
    print(f"Output: {args.output_dir.absolute()}")
    print("=" * 80)
    print()
    
    # Export closed trades
    print("[1/2] Exporting closed trades...")
    closed_count = export_closed_trades(args.output_dir, args.mode)
    print()
    
    # Export open positions
    print("[2/2] Exporting open positions...")
    open_count = export_open_positions(args.output_dir, args.mode)
    print()
    
    print("=" * 80)
    print(f"SUMMARY: {closed_count} closed trades, {open_count} open positions")
    print("=" * 80)


if __name__ == "__main__":
    main()
