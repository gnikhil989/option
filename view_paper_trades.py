#!/usr/bin/env python3
"""
Paper Trade Analysis Dashboard
View performance statistics and trade logs from CSV
"""

import csv
import os
from datetime import datetime
from collections import defaultdict

def read_trades(filename="logs/v7_paper_trades.csv"):
    """Read trades from CSV file"""
    if not os.path.exists(filename):
        return []

    trades = []
    try:
        with open(filename, 'r') as f:
            reader = csv.DictReader(f)
            trades = list(reader)
    except Exception as e:
        print(f"Error reading file: {e}")
    return trades

def analyze_trades(trades):
    """Analyze trade performance"""
    if not trades:
        return None

    # Separate entry and exit records
    entries = [t for t in trades if t.get('Status') == 'ENTRY']
    exits = [t for t in trades if t.get('Status') == 'EXIT']

    if not exits:
        print("\n📊 PAPER TRADE ANALYSIS")
        print("=" * 60)
        print(f"✅ Entries Logged: {len(entries)}")
        print(f"⏳ Waiting for first trade to complete...")
        print(f"\n💡 Tip: Let the system run during market hours to capture trades.")

        if entries:
            latest = entries[-1]
            print(f"\n🔄 Last Entry:")
            print(f"   Contract: {latest.get('Contract', 'N/A')}")
            print(f"   Time: {latest.get('Timestamp', 'N/A')}")
            print(f"   Signal: {latest.get('Reason', 'N/A')}")
        return None

    # Calculate statistics
    total_trades = len(exits)
    total_pnl = sum(float(t.get('PnL_Amount', 0)) for t in exits)

    winning_trades = [t for t in exits if float(t.get('PnL_Amount', 0)) > 0]
    losing_trades = [t for t in exits if float(t.get('PnL_Amount', 0)) < 0]

    win_count = len(winning_trades)
    loss_count = len(losing_trades)
    win_rate = (win_count / total_trades * 100) if total_trades else 0

    avg_win = sum(float(t.get('PnL_Amount', 0)) for t in winning_trades) / win_count if win_count else 0
    avg_loss = sum(float(t.get('PnL_Amount', 0)) for t in losing_trades) / loss_count if loss_count else 0

    # Calculate max win/loss
    pnls = [float(t.get('PnL_Amount', 0)) for t in exits]
    max_win = max(pnls) if pnls else 0
    max_loss = min(pnls) if pnls else 0

    # Average holding time
    avg_hold = sum(int(t.get('Time_Held_Sec', 0)) for t in exits) / total_trades if total_trades else 0

    # Breakdown by strategy/regime
    regime_stats = defaultdict(lambda: {'count': 0, 'pnl': 0})
    for t in exits:
        regime = t.get('Regime', 'UNKNOWN')
        regime_stats[regime]['count'] += 1
        regime_stats[regime]['pnl'] += float(t.get('PnL_Amount', 0))

    # Breakdown by signal type
    signal_stats = defaultdict(lambda: {'count': 0, 'pnl': 0})
    for t in exits:
        reason = t.get('Reason', 'UNKNOWN')
        # Extract primary reason (before parentheses or first phrase)
        primary_reason = reason.split('(')[0].split(',')[0].strip()
        signal_stats[primary_reason]['count'] += 1
        signal_stats[primary_reason]['pnl'] += float(t.get('PnL_Amount', 0))

    return {
        'total_trades': total_trades,
        'total_pnl': total_pnl,
        'win_count': win_count,
        'loss_count': loss_count,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'max_win': max_win,
        'max_loss': max_loss,
        'avg_hold_min': avg_hold / 60,
        'regime_stats': dict(regime_stats),
        'signal_stats': dict(signal_stats),
        'recent_trades': exits[-10:]
    }

def display_analysis(stats):
    """Display formatted analysis"""
    if not stats:
        return

    print("\n" + "=" * 80)
    print("📊 PAPER TRADE PERFORMANCE ANALYSIS")
    print("=" * 80)

    print(f"\n📈 OVERALL PERFORMANCE")
    print(f"   Total Trades: {stats['total_trades']}")
    print(f"   Total P&L: ₹{stats['total_pnl']:,.2f}")
    print(f"   Win Rate: {stats['win_rate']:.2f}%")
    print(f"   Winning Trades: {stats['win_count']} | Losing Trades: {stats['loss_count']}")

    print(f"\n💰 TRADE STATISTICS")
    print(f"   Average Win: ₹{stats['avg_win']:,.2f}")
    print(f"   Average Loss: ₹{stats['avg_loss']:,.2f}")
    print(f"   Max Win: ₹{stats['max_win']:,.2f}")
    print(f"   Max Loss: ₹{stats['max_loss']:,.2f}")
    print(f"   Avg Hold Time: {stats['avg_hold_min']:.2f} minutes")

    if stats['avg_loss'] != 0:
        profit_factor = abs(stats['avg_win'] * stats['win_count']) / abs(stats['avg_loss'] * stats['loss_count'])
        print(f"   Profit Factor: {profit_factor:.2f}")

    print(f"\n🎯 PERFORMANCE BY REGIME")
    for regime, data in stats['regime_stats'].items():
        print(f"   {regime:20s}: {data['count']:2d} trades | P&L: ₹{data['pnl']:,.2f}")

    print(f"\n🔔 PERFORMANCE BY EXIT REASON")
    sorted_signals = sorted(stats['signal_stats'].items(), key=lambda x: x[1]['count'], reverse=True)
    for reason, data in sorted_signals[:5]:  # Top 5
        reason_short = reason[:35] + '...' if len(reason) > 35 else reason
        print(f"   {reason_short:38s}: {data['count']:2d} trades | P&L: ₹{data['pnl']:,.2f}")

    print(f"\n📜 RECENT TRADES (Last 10)")
    print("   " + "-" * 76)
    for t in stats['recent_trades'][-10:]:
        contract = t.get('Contract', 'N/A')[:15]
        pnl = float(t.get('PnL_Amount', 0))
        pnl_str = f"₹{pnl:,.2f}"
        pnl_emoji = "✅" if pnl > 0 else "❌"
        reason = t.get('Reason', 'N/A')[:25]
        timestamp = t.get('Timestamp', 'N/A')[-8:]  # Just time part
        print(f"   {pnl_emoji} {timestamp} | {contract:15s} | {pnl_str:12s} | {reason}")

    print("\n" + "=" * 80)

def main():
    print("🚀 Loading paper trades from CSV...")

    trades = read_trades()

    if not trades:
        print("❌ No trades found. File may not exist or is empty.")
        print(f"   Expected location: logs/v7_paper_trades.csv")
        print(f"\n💡 Tips:")
        print(f"   1. Make sure the system is running")
        print(f"   2. Signals need to be generated for trades to occur")
        print(f"   3. Check that paper_trading_enabled = True in the code")
        return

    print(f"✅ Loaded {len(trades)} trade events from CSV\n")

    stats = analyze_trades(trades)
    if stats:
        display_analysis(stats)

if __name__ == "__main__":
    main()
