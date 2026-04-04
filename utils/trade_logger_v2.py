"""
Enhanced Trade Logger v2
- Better analytics tracking
- Signal history for post-trade analysis
- Hysteresis and anti-whipsaw metrics
"""

import csv
import os
import json
from datetime import datetime
import threading
import logging

logger = logging.getLogger(__name__)


class TradeLoggerV2:
    """
    Enhanced trade logger with additional metrics for anti-whipsaw analysis.
    Tracks signal stability, score history, and trade quality metrics.
    """

    def __init__(self, filename="logs/v8_paper_trades.csv"):
        self.filename = filename
        self.lock = threading.Lock()

        # Enhanced headers with anti-whipsaw metrics
        self.headers = [
            # Identification
            'Trade_ID',
            'Timestamp',
            'Status',               # ENTRY / EXIT

            # Instrument
            'Symbol',               # Underlying
            'Contract',             # Option Symbol
            'Strike',
            'Type',                 # CE / PE
            'Direction',            # BUY / SELL

            # Strategy
            'Strategy',             # ADAPTIVE / MOMENTUM etc.
            'Regime',               # Detected market regime

            # Prices
            'Entry_Price',
            'Exit_Price',
            'Quantity',
            'Lot_Size',
            'Target',
            'StopLoss',
            'Trailing_SL',          # NEW: Trailing stop level
            'Underlying_LTP',

            # P&L
            'PnL_Points',
            'PnL_Amount',
            'ROI_Pct',

            # Signal Quality Metrics (NEW)
            'Entry_Score',          # Score at entry
            'Exit_Score',           # Score at exit
            'Min_Score_During',     # Lowest score during trade
            'Max_Score_During',     # Highest score during trade
            'Score_Volatility',     # Std dev of scores during trade
            'Signal_Confirm_Time',  # How long signal was confirmed before entry

            # Anti-Whipsaw Metrics (NEW)
            'Regime_At_Entry',
            'Regime_At_Exit',
            'Regime_Changes',       # Number of regime changes during trade
            'Opposite_Signals',     # Count of opposite signals during trade
            'Neutral_Signals',      # Count of neutral signals during trade

            # Market Context
            'Confidence',
            'Reason',
            'Exit_Reason',          # NEW: Specific exit reason
            'Snapshot_PCR',
            'Snapshot_IV',
            'Snapshot_GEX',         # NEW: Net GEX at trade time

            # Greeks
            'Entry_Delta',
            'Entry_Gamma',
            'Entry_Theta',
            'Entry_Vega',           # NEW
            'Exit_Delta',           # NEW

            # Timing
            'Time_Held_Sec',
            'Time_To_Target',       # NEW: Seconds to reach target (if hit)
            'Time_To_SL',           # NEW: Seconds to hit SL (if hit)

            # Price Tracking
            'Max_Price_Seen',
            'Min_Price_Seen',
            'Max_Profit_Pct',       # NEW: Best ROI during trade
            'Max_Drawdown_Pct',     # NEW: Worst drawdown during trade

            # Trade Quality Score (NEW)
            'Trade_Quality',        # Calculated quality score (0-100)
            'Signal_History',       # JSON: Last 10 signals before entry
        ]

        self._initialize_file()

    def _initialize_file(self):
        """Create file and write headers if not exists"""
        try:
            os.makedirs(os.path.dirname(self.filename), exist_ok=True)

            if not os.path.exists(self.filename):
                with open(self.filename, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(self.headers)
                logger.info(f"Created new trade log file: {self.filename}")
        except Exception as e:
            logger.error(f"Failed to initialize trade log: {e}")

    def log_trade(self, trade_data):
        """
        Log a trade event to CSV with enhanced metrics
        """
        try:
            row = []
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with self.lock:
                with open(self.filename, 'a', newline='') as f:
                    writer = csv.writer(f)

                    for header in self.headers:
                        val = trade_data.get(header, '')

                        if header == 'Timestamp' and not val:
                            val = current_time

                        # Handle JSON fields
                        if header == 'Signal_History' and isinstance(val, (list, dict)):
                            val = json.dumps(val)

                        # Format floats
                        if isinstance(val, float):
                            if 'Pct' in header or 'ROI' in header:
                                val = f"{val:.2f}"
                            elif 'Delta' in header or 'Gamma' in header:
                                val = f"{val:.4f}"
                            else:
                                val = f"{val:.2f}"

                        row.append(val)

                    writer.writerow(row)

            status = trade_data.get('Status', 'UNKNOWN')
            contract = trade_data.get('Contract', 'UNKNOWN')
            logger.info(f"[V2 Logger] {status} logged for {contract}")
            return True

        except Exception as e:
            logger.error(f"Error logging trade: {e}")
            return False

    def get_recent_trades(self, limit=10, status_filter=None):
        """
        Read back recent trades with optional filtering
        """
        trades = []
        try:
            if not os.path.exists(self.filename):
                return []

            with open(self.filename, 'r') as f:
                reader = csv.DictReader(f)
                all_rows = list(reader)

                if status_filter:
                    all_rows = [r for r in all_rows if r.get('Status') == status_filter]

                trades = all_rows[-limit:]
                trades.reverse()
        except Exception as e:
            logger.error(f"Error reading trade log: {e}")
        return trades

    def get_performance_summary(self):
        """
        Calculate performance metrics from trade history
        """
        try:
            if not os.path.exists(self.filename):
                return {'error': 'No trade history'}

            with open(self.filename, 'r') as f:
                reader = csv.DictReader(f)
                all_trades = list(reader)

            exits = [t for t in all_trades if t.get('Status') == 'EXIT']

            if not exits:
                return {'total_trades': 0, 'message': 'No completed trades'}

            # Calculate metrics
            total_pnl = sum(float(t.get('PnL_Amount', 0) or 0) for t in exits)
            winners = [t for t in exits if float(t.get('PnL_Amount', 0) or 0) > 0]
            losers = [t for t in exits if float(t.get('PnL_Amount', 0) or 0) < 0]

            win_rate = (len(winners) / len(exits) * 100) if exits else 0

            avg_win = sum(float(t.get('PnL_Amount', 0) or 0) for t in winners) / len(winners) if winners else 0
            avg_loss = sum(float(t.get('PnL_Amount', 0) or 0) for t in losers) / len(losers) if losers else 0

            # Profit factor
            gross_profit = sum(float(t.get('PnL_Amount', 0) or 0) for t in winners)
            gross_loss = abs(sum(float(t.get('PnL_Amount', 0) or 0) for t in losers))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

            # Average hold time
            avg_hold = sum(int(t.get('Time_Held_Sec', 0) or 0) for t in exits) / len(exits)

            # Exit reason breakdown
            exit_reasons = {}
            for t in exits:
                reason = t.get('Exit_Reason', 'Unknown')
                exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

            # Average trade quality
            avg_quality = sum(float(t.get('Trade_Quality', 0) or 0) for t in exits) / len(exits)

            return {
                'total_trades': len(exits),
                'winners': len(winners),
                'losers': len(losers),
                'win_rate': round(win_rate, 2),
                'total_pnl': round(total_pnl, 2),
                'avg_win': round(avg_win, 2),
                'avg_loss': round(avg_loss, 2),
                'profit_factor': round(profit_factor, 2),
                'avg_hold_time_min': round(avg_hold / 60, 2),
                'avg_trade_quality': round(avg_quality, 2),
                'exit_reasons': exit_reasons
            }

        except Exception as e:
            logger.error(f"Error calculating performance: {e}")
            return {'error': str(e)}

    def get_score_analysis(self):
        """
        Analyze relationship between entry scores and trade outcomes
        """
        try:
            if not os.path.exists(self.filename):
                return {}

            with open(self.filename, 'r') as f:
                reader = csv.DictReader(f)
                exits = [t for t in reader if t.get('Status') == 'EXIT']

            if not exits:
                return {}

            # Group by score ranges
            score_ranges = {
                '0-20': {'wins': 0, 'losses': 0, 'total_pnl': 0},
                '21-40': {'wins': 0, 'losses': 0, 'total_pnl': 0},
                '41-60': {'wins': 0, 'losses': 0, 'total_pnl': 0},
                '61-80': {'wins': 0, 'losses': 0, 'total_pnl': 0},
                '81-100': {'wins': 0, 'losses': 0, 'total_pnl': 0},
            }

            for t in exits:
                score = abs(float(t.get('Entry_Score', 0) or 0))
                pnl = float(t.get('PnL_Amount', 0) or 0)

                if score <= 20:
                    key = '0-20'
                elif score <= 40:
                    key = '21-40'
                elif score <= 60:
                    key = '41-60'
                elif score <= 80:
                    key = '61-80'
                else:
                    key = '81-100'

                if pnl > 0:
                    score_ranges[key]['wins'] += 1
                else:
                    score_ranges[key]['losses'] += 1
                score_ranges[key]['total_pnl'] += pnl

            # Calculate win rates per range
            for key in score_ranges:
                total = score_ranges[key]['wins'] + score_ranges[key]['losses']
                if total > 0:
                    score_ranges[key]['win_rate'] = round(score_ranges[key]['wins'] / total * 100, 2)
                    score_ranges[key]['avg_pnl'] = round(score_ranges[key]['total_pnl'] / total, 2)
                else:
                    score_ranges[key]['win_rate'] = 0
                    score_ranges[key]['avg_pnl'] = 0

            return score_ranges

        except Exception as e:
            logger.error(f"Error in score analysis: {e}")
            return {}
