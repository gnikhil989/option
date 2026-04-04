import sys
import os
import time
import logging

# Configure logging to show everything
logging.basicConfig(level=logging.DEBUG)

sys.path.append(os.getcwd())

from utils.option_chain_v9_safe import OptionChainManagerV9Safe

def debug_active_trade():
    print("Initializing Manager...")
    try:
        manager = OptionChainManagerV9Safe('NIFTY', '28JAN2026')
        print("Manager Initialized.")
    except Exception as e:
        print(f"❌ Init failed: {e}")
        return

    # Mock Data
    manager.option_data = {
        25000: {
            'ce_data': {'oi': 1000, 'volume': 500, 'iv': 15, 'ltp': 102, 'delta': 0.5, 'gamma': 0.01, 'theta': -5},
            'pe_data': {'oi': 2000, 'volume': 800, 'iv': 14, 'ltp': 80, 'delta': -0.4, 'gamma': 0.01, 'theta': -4},
            'strike': 25000,
            'ce_symbol': 'NIFTY26JAN25000CE',
            'pe_symbol': 'NIFTY26JAN25000PE'
        }
    }
    manager.atm_strike = 25000
    manager.underlying_ltp = 25000
    manager.safety_lock = False
    
    # Manually set an ACTIVE trade
    manager.active_trade = {
        'id': 'TEST_TRADE',
        'Trade_ID': 'TEST_TRADE',
        'contract': 'NIFTY26JAN25000CE',
        'Contract': 'NIFTY26JAN25000CE',
        'strike': 25000,
        'Strike': 25000,
        'type': 'CE',
        'Type': 'CE',
        'direction': 'BUY',
        'Direction': 'BUY',
        'entry_price': 100,
        'Entry_Price': 100,
        'sl': 90,
        'StopLoss': 90,
        'target': 120,
        'Target': 120,
        'Lot_Size': 50
    }
    
    # Force logger settings
    manager.logger.verbose_logging = True
    manager.logger.log_interval = 0
    manager.logger.active_trade = manager.active_trade

    print("Running _manage_active_trade manual test...")
    try:
        metrics = {
            'raw_score': 0, # Neutral score
            'pcr': 1.0,
            'ltp': 25000
        }
        
        manager._manage_active_trade(metrics)
        print("✅ _manage_active_trade finished.")
        
    except Exception as e:
        print(f"❌ _manage_active_trade failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_active_trade()
