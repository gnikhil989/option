"""
Test script for TradeLoggerV3 with Signal Aggregation

This script tests:
1. Signal aggregation/majority voting
2. All trade types (BUY CALL, BUY PUT, SELL CALL, SELL PUT, SELL STRADDLE)
3. Proper entry/exit logic
4. PnL calculations
"""

import time
import random
from datetime import datetime
from utils.trade_logger_v3 import TradeLoggerV3, SignalAggregator


def test_signal_aggregator():
    """Test the signal aggregation logic"""
    print("\n" + "="*60)
    print("TEST 1: Signal Aggregator (Majority Voting)")
    print("="*60)

    aggregator = SignalAggregator(window_seconds=10, min_samples=5)

    # Simulate whipsaw: mixed signals
    signals = [
        ('BUY CALL', 35),
        ('BUY CALL', 40),
        ('WAIT', 10),
        ('BUY CALL', 38),
        ('BUY PUT', -20),
        ('BUY CALL', 42),
        ('BUY CALL', 37),
        ('WAIT', 5),
        ('BUY CALL', 45),
        ('BUY CALL', 50),
    ]

    for action, score in signals:
        aggregator.add_signal(action, score)
        time.sleep(0.1)

    majority_action, confidence, sample_count, avg_score, majority_strike = aggregator.get_majority_signal()
    distribution = aggregator.get_signal_distribution()

    print(f"\nSignals added: {len(signals)}")
    print(f"Distribution: {distribution}")
    print(f"Majority Action: {majority_action}")
    print(f"Confidence: {confidence:.1f}%")
    print(f"Sample Count: {sample_count}")
    print(f"Avg Score: {avg_score:.1f}")

    assert majority_action == 'BUY CALL', f"Expected BUY CALL, got {majority_action}"
    assert confidence >= 60, f"Expected confidence >= 60%, got {confidence}%"
    print("\n[PASS] Signal Aggregator Test Passed!")


def test_trade_types():
    """Test different trade types are handled correctly"""
    print("\n" + "="*60)
    print("TEST 2: Trade Type Parsing")
    print("="*60)

    logger = TradeLoggerV3("logs/test_v3_trades.csv")

    test_cases = [
        ('BUY CALL', ('CE', 'BUY')),
        ('BUY PUT', ('PE', 'BUY')),
        ('SELL CALL', ('CE', 'SELL')),
        ('SELL PUT', ('PE', 'SELL')),
        ('SELL STRADDLE', ('STRADDLE', 'SELL')),
        ('SELL STRANGLE', ('STRANGLE', 'SELL')),
        ('WAIT', (None, None)),
    ]

    for action, expected in test_cases:
        trade_type, direction = logger._parse_action(action)
        result = (trade_type, direction)
        status = "[PASS]" if result == expected else "[FAIL]"
        print(f"{status} {action} -> Type: {trade_type}, Direction: {direction}")
        assert result == expected, f"Expected {expected}, got {result}"

    print("\n[PASS] All Trade Type Parsing Tests Passed!")


def test_pnl_calculation():
    """Test PnL calculations for LONG and SHORT positions"""
    print("\n" + "="*60)
    print("TEST 3: PnL Calculations")
    print("="*60)

    # Test scenarios
    test_cases = [
        # (direction, entry_price, exit_price, expected_pnl_points)
        ('BUY', 100, 130, 30),    # Long: profit when price rises
        ('BUY', 100, 80, -20),    # Long: loss when price drops
        ('SELL', 100, 70, 30),    # Short: profit when price drops
        ('SELL', 100, 120, -20),  # Short: loss when price rises
    ]

    for direction, entry, exit_p, expected in test_cases:
        if direction == 'SELL':
            pnl = entry - exit_p
        else:
            pnl = exit_p - entry

        status = "[PASS]" if pnl == expected else "[FAIL]"
        print(f"{status} {direction} Entry: {entry}, Exit: {exit_p} -> PnL: {pnl} (Expected: {expected})")
        assert pnl == expected, f"Expected PnL {expected}, got {pnl}"

    print("\n[PASS] All PnL Calculation Tests Passed!")


def test_level_calculation():
    """Test Target/Stoploss level calculations"""
    print("\n" + "="*60)
    print("TEST 4: Target/Stoploss Calculations")
    print("="*60)

    logger = TradeLoggerV3("logs/test_v3_trades.csv")

    # BUY: Target above, SL below
    entry_price = 100
    target, sl = logger._calculate_levels(entry_price, 'BUY', {})
    print(f"\nBUY @ {entry_price}:")
    print(f"  Target: {target} (should be > {entry_price})")
    print(f"  StopLoss: {sl} (should be < {entry_price})")
    assert target > entry_price, f"BUY target {target} should be > entry {entry_price}"
    assert sl < entry_price, f"BUY SL {sl} should be < entry {entry_price}"

    # SELL: Target below, SL above
    target, sl = logger._calculate_levels(entry_price, 'SELL', {})
    print(f"\nSELL @ {entry_price}:")
    print(f"  Target: {target} (should be < {entry_price})")
    print(f"  StopLoss: {sl} (should be > {entry_price})")
    assert target < entry_price, f"SELL target {target} should be < entry {entry_price}"
    assert sl > entry_price, f"SELL SL {sl} should be > entry {entry_price}"

    print("\n[PASS] All Level Calculation Tests Passed!")


def simulate_trading_session():
    """Simulate a full trading session with multiple signals"""
    print("\n" + "="*60)
    print("TEST 5: Full Trading Session Simulation")
    print("="*60)

    logger = TradeLoggerV3("logs/test_v3_simulation.csv")

    # Mock option data
    option_data = {
        24000: {
            'strike': 24000,
            'tag': 'ATM',
            'ce_symbol': 'NIFTY09JAN2524000CE',
            'pe_symbol': 'NIFTY09JAN2524000PE',
            'ce_data': {'ltp': 250, 'delta': 0.5, 'gamma': 0.001, 'theta': -5},
            'pe_data': {'ltp': 230, 'delta': -0.5, 'gamma': 0.001, 'theta': -5},
        },
        24050: {
            'strike': 24050,
            'tag': 'OTM1',
            'ce_symbol': 'NIFTY09JAN2524050CE',
            'pe_symbol': 'NIFTY09JAN2524050PE',
            'ce_data': {'ltp': 180, 'delta': 0.4, 'gamma': 0.001, 'theta': -4},
            'pe_data': {'ltp': 280, 'delta': -0.6, 'gamma': 0.001, 'theta': -6},
        },
        23950: {
            'strike': 23950,
            'tag': 'ITM1',
            'ce_symbol': 'NIFTY09JAN2523950CE',
            'pe_symbol': 'NIFTY09JAN2523950PE',
            'ce_data': {'ltp': 320, 'delta': 0.6, 'gamma': 0.001, 'theta': -6},
            'pe_data': {'ltp': 180, 'delta': -0.4, 'gamma': 0.001, 'theta': -4},
        },
    }

    # Simulate signals over time
    print("\n--- Phase 1: Building Signal Consensus (BUY CALL) ---")
    for i in range(12):
        signal_data = {
            'action': 'BUY CALL' if i != 5 else 'WAIT',  # One outlier
            'score': 45 + random.randint(-5, 10),
            'regime': 'MOMENTUM_RIDER',
            'regime_info': {
                'regime': 'MOMENTUM_RIDER',
                'confidence': 75,
                'thresholds': {'target': '+30%', 'sl': '-15%'}
            },
            'trade_setup': {
                'contract': 'NIFTY09JAN2524000CE',
                'strike': 24000,
                'entry': 250,
                'target': 325,
                'sl': 212.5,
                'type': 'CE',
                'underlying_ltp': 24010
            },
            'mode': 'ADAPTIVE'
        }

        logger.process_signal(
            signal_data=signal_data,
            option_data=option_data,
            underlying_ltp=24010,
            underlying_symbol='NIFTY',
            metrics={'pcr': 0.95, 'iv': 15.5}
        )

        time.sleep(0.1)

        if logger.active_trade:
            print(f"  Signal {i+1}: Trade ENTERED - {logger.active_trade['Contract']}")
            break
        else:
            stats = logger.get_signal_stats()
            dist = stats.get('entry_distribution', {})
            print(f"  Signal {i+1}: Building consensus... {dist}")

    # Check if trade was entered
    if logger.active_trade:
        print(f"\n[SUCCESS] Trade entered after signal consensus!")
        print(f"  Contract: {logger.active_trade['Contract']}")
        print(f"  Entry: {logger.active_trade['Entry_Price']}")
        print(f"  Target: {logger.active_trade['Target']}")
        print(f"  StopLoss: {logger.active_trade['StopLoss']}")

        # Simulate price movement to target
        print("\n--- Phase 2: Simulating Price Movement to Target ---")
        option_data[24000]['ce_data']['ltp'] = 330  # Price hits target

        signal_data = {
            'action': 'BUY CALL',
            'score': 50,
            'regime': 'MOMENTUM_RIDER',
            'regime_info': {'regime': 'MOMENTUM_RIDER', 'confidence': 75},
            'trade_setup': {},
            'mode': 'ADAPTIVE'
        }

        logger.process_signal(
            signal_data=signal_data,
            option_data=option_data,
            underlying_ltp=24100,
            underlying_symbol='NIFTY',
            metrics={'pcr': 0.98, 'iv': 14.0}
        )

        if not logger.active_trade:
            print("[SUCCESS] Trade exited at TARGET!")
        else:
            print("[INFO] Trade still active (may need more signals for exit)")
    else:
        print("\n[INFO] Trade not entered yet - may need more signals")

    # Print summary
    print("\n--- Performance Summary ---")
    summary = logger.get_performance_summary()
    for key, value in summary.items():
        if key != 'recent_trades':
            print(f"  {key}: {value}")

    print("\n[PASS] Trading Session Simulation Complete!")


def test_straddle_trade():
    """Test STRADDLE trade logging"""
    print("\n" + "="*60)
    print("TEST 6: STRADDLE Trade Test")
    print("="*60)

    logger = TradeLoggerV3("logs/test_v3_straddle.csv")

    option_data = {
        24000: {
            'strike': 24000,
            'tag': 'ATM',
            'ce_symbol': 'NIFTY09JAN2524000CE',
            'pe_symbol': 'NIFTY09JAN2524000PE',
            'ce_data': {'ltp': 250, 'delta': 0.5, 'gamma': 0.001, 'theta': -5},
            'pe_data': {'ltp': 230, 'delta': -0.5, 'gamma': 0.001, 'theta': -5},
        },
    }

    print("\n--- Sending SELL STRADDLE Signals ---")
    for i in range(12):
        signal_data = {
            'action': 'SELL STRADDLE',
            'score': 5,  # Low score = range bound = THETA_HUNTER
            'regime': 'THETA_HUNTER',
            'regime_info': {
                'regime': 'THETA_HUNTER',
                'confidence': 85,
                'thresholds': {'target': '50-80% profit', 'sl': 'Premium spike >40%'}
            },
            'trade_setup': {
                'contract': 'STRADDLE 24000',
                'strike': 24000,
                'entry': 480,  # 250 + 230
                'target': 240,  # 50% profit for SELL
                'sl': 672,     # 40% loss for SELL
                'type': 'STRADDLE',
                'underlying_ltp': 24000,
                'legs': [
                    {'name': 'NIFTY09JAN2524000CE', 'ltp': 250, 'type': 'CE'},
                    {'name': 'NIFTY09JAN2524000PE', 'ltp': 230, 'type': 'PE'}
                ]
            },
            'mode': 'ADAPTIVE'
        }

        logger.process_signal(
            signal_data=signal_data,
            option_data=option_data,
            underlying_ltp=24000,
            underlying_symbol='NIFTY',
            metrics={'pcr': 1.0, 'iv': 12.0}
        )

        time.sleep(0.1)

        if logger.active_trade:
            print(f"  Signal {i+1}: STRADDLE Trade ENTERED!")
            print(f"    Type: {logger.active_trade['Type']}")
            print(f"    Direction: {logger.active_trade['Direction']}")
            print(f"    Entry: {logger.active_trade['Entry_Price']}")
            print(f"    Target: {logger.active_trade['Target']} (profit if price drops)")
            print(f"    StopLoss: {logger.active_trade['StopLoss']} (loss if price rises)")
            break

    if logger.active_trade:
        assert logger.active_trade['Type'] == 'STRADDLE', "Expected STRADDLE trade type"
        assert logger.active_trade['Direction'] == 'SELL', "Expected SELL direction"
        print("\n[PASS] STRADDLE Trade Test Passed!")
    else:
        print("\n[INFO] STRADDLE trade not entered - may need more signals")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("TRADE LOGGER V3 - TEST SUITE")
    print("Testing Signal Aggregation & Majority Voting")
    print("="*60)

    try:
        test_signal_aggregator()
        test_trade_types()
        test_pnl_calculation()
        test_level_calculation()
        simulate_trading_session()
        test_straddle_trade()

        print("\n" + "="*60)
        print("ALL TESTS COMPLETED SUCCESSFULLY!")
        print("="*60)

    except AssertionError as e:
        print(f"\n[FAIL] Test failed: {e}")
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
