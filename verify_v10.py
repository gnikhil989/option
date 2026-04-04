
import sys
import os
import time
import logging
from unittest.mock import MagicMock

# Mocking the folder structure for import
sys.path.append(os.getcwd())

from utils.option_chain_v10_safe import OptionChainManagerV10Safe

def test_v10_multi_strat():
    print("Testing V10 Multi-Strategy Hub...")
    
    # Mock API and WS
    mock_api = MagicMock()
    mock_ws = MagicMock()
    
    # Initialize Manager
    manager = OptionChainManagerV10Safe("NIFTY", "28-AUG-25", websocket_manager=mock_ws)
    manager.api_client = mock_api
    
    # Setup some mock data
    manager.underlying_ltp = 24500
    manager.atm_strike = 24500
    manager.option_data = {
        24500: {
            'strike': 24500,
            'ce_symbol': 'NIFTY28AUG2524500CE',
            'pe_symbol': 'NIFTY28AUG2524500PE',
            'ce_data': {'ltp': 100, 'oi': 10000, 'delta': 0.5},
            'pe_data': {'ltp': 100, 'oi': 10000, 'delta': -0.5}
        }
    }
    manager.initial_state = {
        24500: {'ce_oi': 9000, 'pe_oi': 9000}
    }
    manager.start_price = 24500
    manager.guardian.current_regime = 'NEUTRAL'
    
    # Simulate a STRONG BULLISH Signal
    # raw_score used in process_signals is from metrics['raw_score']
    metrics = {'raw_score': 60} 
    
    print(f"Injecting Score: {metrics['raw_score']}")
    
    # Run once
    for strategy in manager.strategies:
        print(f"Processing Strategy: {strategy.name}")
        # Feed it signals for 100 iterations to get samples
        for _ in range(100): 
            strategy.process_signals(manager, metrics)
            
        print(f"  Aggregator Status: {strategy.logger.entry_aggregator.get_majority_signal()}")
        
    print("Check if log files exist in logs/")
    for s in manager.strategies:
        if os.path.exists(s.logger.filename):
            print(f"✅ Log file created for {s.name}: {s.logger.filename}")
        else:
            print(f"❌ Log file MISSING for {s.name}: {s.logger.filename}")

if __name__ == "__main__":
    test_v10_multi_strat()
