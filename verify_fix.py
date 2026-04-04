
import sys
import os
from unittest.mock import MagicMock

# Mock cachetools
sys.modules['cachetools'] = MagicMock()
sys.modules['utils.trade_logger'] = MagicMock()

# Add the project directory to sys.path
sys.path.append('/home/e-stone62/Desktop/others/test-folder/option-chain')

try:
    from utils.option_chain_v7_adaptive import OptionChainManagerV7Adaptive
    
    print("Successfully imported OptionChainManagerV7Adaptive")
    
    if hasattr(OptionChainManagerV7Adaptive, 'initialize'):
        print("PASS: OptionChainManagerV7Adaptive has 'initialize' method.")
    else:
        print("FAIL: OptionChainManagerV7Adaptive MISSING 'initialize' method.")
        
    # Check other critical methods
    methods = ['calculate_atm', 'generate_strikes', 'setup_subscriptions', 'generate_signals']
    for m in methods:
        if hasattr(OptionChainManagerV7Adaptive, m):
            print(f"PASS: Has '{m}' method.")
        else:
            print(f"FAIL: Missing '{m}' method.")

except ImportError as e:
    print(f"ImportError: {e}")
except Exception as e:
    print(f"An error occurred: {e}")
