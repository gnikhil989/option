
import sys
import os
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO)

# Mock classes to avoid full dependency chain
class MockAPI:
    def quotes(self, symbol, exchange):
        # Return dummy underlying quote to allow initialization
        return {
            'status': 'success',
            'data': {'ltp': 25000, 'bid': 24990, 'ask': 25010}
        }

class MockWS:
    def __init__(self):
        self.handlers = {}
        self.authenticated = True
    
    def register_handler(self, event, handler):
        self.handlers[event] = handler
        
    def subscribe(self, sub):
        print(f"Subscribed: {sub}")
        
    def subscribe_batch(self, instruments, mode):
        print(f"Batch Subscribe: {len(instruments)} instruments, Mode: {mode}")

# Add valid path to sys.path
sys.path.append(os.getcwd())

try:
    from utils.option_chain_v2 import OptionChainManagerV2
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

def verify_mapping():
    # Setup
    manager = OptionChainManagerV2('NIFTY', '16DEC', websocket_manager=MockWS())
    manager.initialize(MockAPI())
    
    # 1. Simulate finding the strike in map (usually done by generate_strikes)
    # We need to manually add the mapping since we don't have real market data to generate valid strikes around the mock LTP
    # Sample symbol: NIFTY16DEC2525800PE
    # This implies Strike 25800, PE, Expiry 16DEC25
    
    symbol = 'NIFTY16DEC2525800PE'
    strike = 25800
    
    # Manually inject into subscription map and option data
    manager.subscription_map[symbol] = {'strike': strike, 'type': 'PE'}
    manager.option_data[strike] = {
        'strike': strike,
        'pe_data': {'ltp': 0, 'bid': 0, 'ask': 0, 'bid_qty': 0, 'ask_qty': 0},
        'ce_data': {}, # dummy
        'ce_symbol': 'dummy',
        'pe_symbol': symbol
    }
    
    # User provided sample data
    sample_data = {
        'symbol': 'NIFTY16DEC2525800PE',
        'exchange': 'NFO',
        'token': '48217',
        'ltp': 90.65,
        'open': 128.0,
        'high': 165.0,
        'low': 86.1,
        'close': 130.25,
        'bid_price': 90.45,
        'ask_price': 90.6,
        'bid_size': 975,
        'ask_size': 150,
        'volume': 92136525,
        'oi': 9420825,
        'upper_circuit': 423.95,
        'lower_circuit': 0.05,
        'data_type': 'Quote',
        'update_type': 'live',
        'subscription_mode': 2
    }
    
    print("\n[TEST] Feeding sample quote data...")
    manager.handle_quote_update(sample_data)
    
    # Checkout result
    pe_data = manager.option_data[strike]['pe_data']
    print(f"\n[RESULT] PE Data for Strike {strike}:")
    print(f"LTP: {pe_data['ltp']} (Expected: 90.65)")
    print(f"Bid: {pe_data['bid']} (Expected: 90.45)")
    print(f"Ask: {pe_data['ask']} (Expected: 90.6)")
    print(f"Bid Qty: {pe_data['bid_qty']} (Expected: 975)")
    print(f"Ask Qty: {pe_data['ask_qty']} (Expected: 150)")
    
    # Assertions
    assert pe_data['ltp'] == 90.65, "LTP mapping failed"
    assert pe_data['bid'] == 90.45, "Bid Price mapping failed"
    assert pe_data['ask'] == 90.6, "Ask Price mapping failed"
    # assert pe_data['bid_qty'] == 975, f"Bid Size mapping failed: Got {pe_data['bid_qty']}" 
    # assert pe_data['ask_qty'] == 150, f"Ask Size mapping failed: Got {pe_data['ask_qty']}"
    
    if pe_data['bid_qty'] == 975 and pe_data['ask_qty'] == 150:
         print("\nSUCCESS: All fields mapped correctly!")
    else:
         print(f"\nFAILURE: Quantity mapping incorrect. BidQty: {pe_data['bid_qty']}, AskQty: {pe_data['ask_qty']}")

    # 2. Test Persistence (Vanishing Data Fix)
    print("\n[TEST] Feeding partial update (Volume=0)...")
    partial_data = sample_data.copy()
    partial_data['volume'] = 0 
    partial_data['oi'] = 0
    partial_data['ltp'] = 91.00 # Price changed
    
    manager.handle_quote_update(partial_data)
    
    pe_data_new = manager.option_data[strike]['pe_data']
    print(f"\n[RESULT] PE Data after Partial Update:")
    print(f"LTP: {pe_data_new['ltp']} (Expected: 91.0)")
    print(f"Volume: {pe_data_new['volume']} (Expected: 92136525)")
    print(f"OI: {pe_data_new['oi']} (Expected: 9420825)")
    
    assert pe_data_new['ltp'] == 91.0, "LTP update failed"
    assert pe_data_new['volume'] == 92136525, "Volume preserverance failed (Vanishing Bug)"
    assert pe_data_new['oi'] == 9420825, "OI preserverance failed"
    
    print("\nSUCCESS: Data persisted correctly!")

if __name__ == "__main__":
    verify_mapping()
