
import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure utils can be imported
sys.path.append(os.getcwd())

from utils.option_chain_v9_safe import OptionChainManagerV9Safe
from utils.trade_logger_v9 import TradeLoggerV9

class TestV9SignalLogic(unittest.TestCase):
    
    def setUp(self):
        # Mock dependencies
        self.mock_logger = MagicMock(spec=TradeLoggerV9)
        self.mock_logger.entry_aggregator = MagicMock()
        
        # Patch TradeLoggerV9 inside OptionChainManagerV9Safe
        with patch('utils.option_chain_v9_safe.TradeLoggerV9', return_value=self.mock_logger):
            self.manager = OptionChainManagerV9Safe("TEST", "2024-01-01")
            
        # Mock internal components
        self.manager.option_data = {'mock': 'data'}
        self.manager.underlying_ltp = 20000
        self.manager._execute_entry = MagicMock()
        
    def test_whipsaw_rejection(self):
        """Test that inconsistent signals (WAITS) result in NO entry"""
        print("\nTesting Whipsaw Rejection...")
        
        # Setup mock aggregator to return lack of consensus
        # majority_action, confidence, sample_count, avg_score, majority_strike
        self.mock_logger.entry_aggregator.get_majority_signal.return_value = ('WAIT', 40, 20, 10, None)
        
        metrics = {'raw_score': 50, 'pcr': 0.8, 'ltp': 20000}
        self.manager._scan_for_entry_opportunities(metrics)
        
        self.manager._execute_entry.assert_not_called()
        print("✅ Correctly rejected entry due to WAIT/Low Confidence")

    def test_strong_trend_entry(self):
        """Test that consistent STRONG signals result in entry"""
        print("\nTesting Strong Trend Entry...")
        
        # Setup mock aggregator to return strong consensus
        self.mock_logger.entry_aggregator.get_majority_signal.return_value = ('BUY CALL', 80, 20, 70, None)
        
        metrics = {'raw_score': 80, 'pcr': 1.2, 'ltp': 20000}
        self.manager._scan_for_entry_opportunities(metrics)
        
        self.manager._execute_entry.assert_called_with('BUY CALL', 70)
        print("✅ Correctly executed entry for Strong Call Signal")
        
    def test_insufficient_samples(self):
        """Test that we wait for enough samples"""
        print("\nTesting Insufficient Samples...")
        
        self.mock_logger.entry_aggregator.get_majority_signal.return_value = ('BUY CALL', 90, 5, 80, None)
        
        metrics = {'raw_score': 80, 'pcr': 1.2, 'ltp': 20000}
        self.manager._scan_for_entry_opportunities(metrics)
        
        self.manager._execute_entry.assert_not_called()
        print("✅ Correctly rejected entry due to insufficient samples")

if __name__ == '__main__':
    unittest.main()
