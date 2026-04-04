
import csv
import os
import time
from datetime import datetime
import threading
import logging

logger = logging.getLogger(__name__)

class TradeLogger:
    """
    Handles CSV logging for paper trades and signals.
    Designed for post-trade analysis and strategy refinement.
    """
    
    def __init__(self, filename="logs/v7_paper_trades.csv"):
        self.filename = filename
        self.lock = threading.Lock()
        self.headers = [
            'Trade_ID',
            'Timestamp',
            'Status',           # ENTRY / EXIT
            'Symbol',           # Underlying
            'Contract',         # Option Symbol
            'Strike',
            'Type',             # CE / PE
            'Direction',        # BUY / SELL (NEW)
            'Strategy',         # ADAPTIVE / SNIPER etc.
            'Regime',           # DETECTED REGIME
            'Entry_Price',      # Price at entry (NEW)
            'Exit_Price',       # Price at exit (NEW)
            'Quantity',         # Number of Lots
            'Lot_Size',         # Actual lot size (NEW)
            'Target',
            'StopLoss',
            'Underlying_LTP',   # Spot price at trade time (NEW)
            'PnL_Points',       # Calculated on Exit
            'PnL_Amount',       # Calculated on Exit
            'ROI_Pct',          # Return on Investment % (NEW)
            'Score',            # Quant Score
            'Confidence',
            'Reason',           # Logic Description
            'Snapshot_PCR',     # Metric snapshot for analysis
            'Snapshot_IV',
            'Entry_Delta',      # Greeks at entry (NEW)
            'Entry_Gamma',      # Greeks at entry (NEW)
            'Entry_Theta',      # Greeks at entry (NEW)
            'Time_Held_Sec',    # Duration
            'Max_Profit_Seen',  # Best price during trade (NEW)
            'Max_Loss_Seen'     # Worst price during trade (NEW)
        ]
        
        self._initialize_file()

    def _initialize_file(self):
        """Create file and write headers if not exists"""
        try:
            # Ensure directory exists
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
        Log a trade event to CSV
        :param trade_data: Dict containing key-value pairs matching headers
        """
        try:
            row = []
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            with self.lock:
                with open(self.filename, 'a', newline='') as f:
                    writer = csv.writer(f)
                    
                    for header in self.headers:
                        # Use default values if key missing
                        val = trade_data.get(header, '')
                        
                        # Auto-fill Timestamp if missing
                        if header == 'Timestamp' and not val:
                            val = current_time
                            
                        # Format floats
                        if isinstance(val, float):
                            val = f"{val:.2f}"
                            
                        row.append(val)
                        
                    writer.writerow(row)
                    
            logger.info(f"Logged {trade_data.get('Status')} for {trade_data.get('Contract')}")
            return True
            
        except Exception as e:
            logger.error(f"Error logging trade: {e}")
            return False

    def get_recent_trades(self, limit=10):
        """Read back recent trades for UI display"""
        trades = []
        try:
            if not os.path.exists(self.filename):
                return []
                
            with open(self.filename, 'r') as f:
                reader = csv.DictReader(f)
                all_rows = list(reader)
                trades = all_rows[-limit:]
                trades.reverse() # Newest first
        except Exception as e:
            logger.error(f"Error reading trade log: {e}")
        return trades
