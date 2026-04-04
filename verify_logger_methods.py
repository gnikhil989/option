import sys
import os
sys.path.append(os.getcwd())
from utils.trade_logger_v9 import TradeLoggerV9

def test_logger():
    print("Initializing Logger...")
    logger = TradeLoggerV9()
    
    print("Testing _get_lot_size...")
    try:
        lot_size = logger._get_lot_size('NIFTY')
        print(f"Lot Size for NIFTY: {lot_size}")
    except AttributeError as e:
        print(f"❌ _get_lot_size failed: {e}")
    except Exception as e:
        print(f"❌ _get_lot_size error: {e}")

    print("Testing _log_signal_status...")
    try:
        logger._log_signal_status('BUY CALL', 10, 'BULLISH', 25000, 'NIFTY')
        print("✅ _log_signal_status executed")
    except Exception as e:
        print(f"❌ _log_signal_status error: {e}")

    print("Testing log_trade...")
    try:
        data = {
            'Trade_ID': 'TEST_123',
            'Status': 'ENTRY', 
            'Symbol': 'NIFTY',
            'Contract': 'NIFTY20JAN2625600PE',
            'Lot_Size': 50
        }
        success = logger.log_trade(data)
        print(f"Log Trade Success: {success}")
    except Exception as e:
        print(f"❌ log_trade error: {e}")

if __name__ == "__main__":
    test_logger()
