# Quick Integration Guide

## Step-by-Step Integration

### Option 1: Direct Replacement (Recommended for Testing)

```bash
# 1. Backup original
cp utils/option_chain_v7.py utils/option_chain_v7_backup.py

# 2. Replace class name in refactored file
# Edit utils/option_chain_v7_refactored.py
# Change: class OptionChainManagerV7Refactored
# To: class OptionChainManagerV7

# 3. Replace file
mv utils/option_chain_v7.py utils/option_chain_v7_old.py
mv utils/option_chain_v7_refactored.py utils/option_chain_v7.py

# 4. Restart app
python app.py
```

### Option 2: Side-by-Side Comparison (Recommended for Validation)

```python
# In your app.py or route handler

from utils.option_chain_v7 import OptionChainManagerV7 as OriginalManager
from utils.option_chain_v7_refactored import OptionChainManagerV7Refactored as RefactoredManager

# Create both managers
original_mgr = OriginalManager(
    underlying='NIFTY',
    expiry='28-AUG-25',
    websocket_manager=ws_manager
)

refactored_mgr = RefactoredManager(
    underlying='NIFTY',
    expiry='28-AUG-25',
    websocket_manager=ws_manager
)

# Compare signals
def compare_signals():
    # Get chain data (they share same WebSocket updates)
    chain_data = original_mgr.get_option_chain()

    # Calculate metrics
    pcr = chain_data['market_metrics']['pcr']
    max_pain = chain_data['market_metrics']['max_pain']

    # Generate signals from both
    original_signal = original_mgr.generate_signals(pcr, max_pain)
    refactored_signal = refactored_mgr.generate_signals(pcr, max_pain)

    # Log comparison
    print(f"Original: {original_signal['action']} | Score: {original_signal['score']}")
    print(f"Refactored: {refactored_signal['action']} | Score: {refactored_signal['score']}")
    print(f"Difference: {refactored_signal['score'] - original_signal['score']}")
    print("-" * 50)

    return original_signal, refactored_signal

# Run comparison every minute
import schedule
schedule.every(1).minutes.do(compare_signals)
```

### Option 3: Feature Flag (Recommended for Production)

```python
# In config.py
USE_REFACTORED_SIGNALS = True  # Feature flag

# In your manager initialization
if USE_REFACTORED_SIGNALS:
    from utils.option_chain_v7_refactored import OptionChainManagerV7Refactored as ManagerClass
else:
    from utils.option_chain_v7 import OptionChainManagerV7 as ManagerClass

manager = ManagerClass(
    underlying='NIFTY',
    expiry='28-AUG-25',
    websocket_manager=ws_manager
)
```

---

## Cherry-Picking Individual Fixes

If you want to apply fixes gradually, here's how to copy individual methods:

### Fix 1: Max Pain (CRITICAL - Apply First)

```python
# In your original option_chain_v7.py, replace _score_max_pain() method:

def _score_max_pain(self):
    """
    REFACTORED: Signal 4 - Max Pain Drift
    FIXED: Price is ATTRACTED to Max Pain, not repelled
    """
    mp = self.calculate_max_pain()
    if mp == 0 or self.underlying_ltp == 0:
        return 0, "No Max Pain"

    diff_pct = (self.underlying_ltp - mp) / mp * 100

    # FIXED: Max Pain acts as MAGNET
    if diff_pct > 0.5:
        if len(self.price_history) > 5 and self.price_history[-1] > self.price_history[-5]:
            return -5, f"Above Max Pain {mp} (Bearish Pull Expected)"
        else:
            return -10, f"Pulling Down to Max Pain {mp}"

    elif diff_pct < -0.5:
        if len(self.price_history) > 5 and self.price_history[-1] < self.price_history[-5]:
            return 5, f"Below Max Pain {mp} (Bullish Pull Expected)"
        else:
            return 10, f"Pulling Up to Max Pain {mp}"

    return 0, f"Aligned with Max Pain {mp}"
```

### Fix 2: IV Skew Context (HIGH - Apply Second)

```python
# Replace _score_iv_skew() method:

def _score_iv_skew(self):
    """
    REFACTORED: Signal 2 - IV Skew with Context
    """
    if not self.atm_strike or self.atm_strike not in self.option_data:
        return 0, "No ATM Data"

    atm_data = self.option_data[self.atm_strike]
    ce_iv = atm_data['ce_data'].get('iv', 0)
    pe_iv = atm_data['pe_data'].get('iv', 0)

    if ce_iv == 0 or pe_iv == 0:
        return 0, "IV Missing"

    diff = pe_iv - ce_iv

    # Check if price is falling
    is_falling = False
    if len(self.price_history) > 20:
        price_change_pct = ((self.price_history[-1] - self.price_history[-20]) / self.price_history[-20]) * 100
        is_falling = price_change_pct < -0.1

    if diff > 2:
        if is_falling:
            return -15, f"Put IV Spike - Justified Fear (Bearish: PE {pe_iv:.1f} vs CE {ce_iv:.1f})"
        else:
            return 10, f"Put IV Spike - Excess Fear (Contrarian Bullish: PE {pe_iv:.1f} vs CE {ce_iv:.1f})"

    elif diff < -2:
        return -10, f"Call IV Spike - FOMO/Greed (Bearish: CE {ce_iv:.1f} vs PE {pe_iv:.1f})"

    return 0, f"Balanced IV Skew (PE {pe_iv:.1f} vs CE {ce_iv:.1f})"
```

### Fix 3: Institutional Flow (HIGH - Apply Third)

```python
# Replace _detect_institutional_flow() method:

def _detect_institutional_flow(self):
    """
    REFACTORED: Signal 7 - Institutional Flow
    FIXED: Stricter thresholds (1.5x OI + OI increasing)
    """
    alerts = []
    score = 0
    reason = "Normal Flow"

    for strike, data in self.option_data.items():
        ce_vol = data['ce_data'].get('volume', 0)
        ce_oi = data['ce_data'].get('oi', 0)
        pe_vol = data['pe_data'].get('volume', 0)
        pe_oi = data['pe_data'].get('oi', 0)

        # Get OI change
        ce_oi_change = 0
        pe_oi_change = 0
        if strike in self.initial_state:
            ce_oi_change = ce_oi - self.initial_state[strike]['ce_oi']
            pe_oi_change = pe_oi - self.initial_state[strike]['pe_oi']

        # FIXED: Volume > 1.5x OI + OI increasing
        if ce_oi > 0 and ce_vol > ce_oi * 1.5 and ce_oi_change > 500:
            alerts.append(f"Fresh Institutional CE at {strike}")
            data['ce_data']['institutional_flow'] = True
            if strike > self.underlying_ltp:
                score += 5
        else:
            data['ce_data']['institutional_flow'] = False

        if pe_oi > 0 and pe_vol > pe_oi * 1.5 and pe_oi_change > 500:
            alerts.append(f"Fresh Institutional PE at {strike}")
            data['pe_data']['institutional_flow'] = True
            if strike < self.underlying_ltp:
                score -= 5
        else:
            data['pe_data']['institutional_flow'] = False

    if alerts:
        reason = ", ".join(alerts[:3])

    score = max(-20, min(20, score))
    return score, reason
```

---

## Validation Tests

### Test 1: Max Pain Direction Test

```python
def test_max_pain_fix():
    """Test that Max Pain logic is fixed"""

    # Setup
    manager.underlying_ltp = 22500
    manager.price_history.extend([22450, 22460, 22480, 22490, 22500])  # Rising

    # Mock max pain at 22000 (500 below current)
    max_pain = 22000

    score, reason = manager._score_max_pain()

    # Should be NEGATIVE (bearish pull down)
    assert score <= 0, f"Max Pain fix failed: Expected negative score, got {score}"
    assert "Pull" in reason or "Bearish" in reason

    print("✅ Max Pain test PASSED")
```

### Test 2: IV Skew Context Test

```python
def test_iv_skew_context():
    """Test that IV Skew is context-aware"""

    # Setup: Put IV spike but price is FLAT (not falling)
    manager.atm_strike = 22000
    manager.option_data[22000] = {
        'ce_data': {'iv': 15},
        'pe_data': {'iv': 20}  # 5 points higher
    }
    manager.price_history.extend([22000] * 25)  # Flat price

    score, reason = manager._score_iv_skew()

    # Should be POSITIVE (contrarian bullish)
    assert score > 0, f"IV Skew context test failed: Expected positive, got {score}"
    assert "Contrarian" in reason or "Excess Fear" in reason

    print("✅ IV Skew context test PASSED")
```

### Test 3: Institutional Flow Filter Test

```python
def test_institutional_flow_filter():
    """Test that low institutional flow is filtered"""

    # Setup: Active strike but not institutional (Volume = 0.9x OI)
    strike = 22000
    manager.option_data[strike] = {
        'ce_data': {'volume': 9000, 'oi': 10000},  # 0.9x
        'pe_data': {'volume': 0, 'oi': 0}
    }
    manager.initial_state[strike] = {'ce_oi': 9950, 'pe_oi': 0}  # Minimal OI change

    score, reason = manager._detect_institutional_flow()

    # Should have NO institutional flow flag
    assert manager.option_data[strike]['ce_data']['institutional_flow'] == False

    print("✅ Institutional flow filter test PASSED")
```

### Run All Tests

```python
def run_validation_tests():
    """Run all validation tests"""
    tests = [
        test_max_pain_fix,
        test_iv_skew_context,
        test_institutional_flow_filter
    ]

    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"❌ Test FAILED: {e}")
        except Exception as e:
            print(f"❌ Test ERROR: {e}")

    print("\n✅ All tests completed!")

# Run tests before deploying
run_validation_tests()
```

---

## Monitoring After Deployment

### Log Signal Changes

```python
import logging

logger = logging.getLogger('signal_monitor')

def monitor_signals(old_signal, new_signal):
    """Monitor significant signal changes"""

    old_score = old_signal['score']
    new_score = new_signal['score']
    diff = new_score - old_score

    # Log if difference > 20 points
    if abs(diff) > 20:
        logger.warning(f"Large signal change detected!")
        logger.warning(f"  Old: {old_signal['action']} ({old_score})")
        logger.warning(f"  New: {new_signal['action']} ({new_score})")
        logger.warning(f"  Diff: {diff:+d}")
        logger.warning(f"  Reasons: {new_signal['reasons'][:3]}")

    # Log action changes
    if old_signal['action'] != new_signal['action']:
        logger.info(f"Action changed: {old_signal['action']} → {new_signal['action']}")
```

### Track Win Rate (Paper Trading)

```python
class SignalTracker:
    def __init__(self):
        self.signals = []
        self.outcomes = []

    def record_signal(self, signal, entry_price):
        """Record a signal when it's generated"""
        self.signals.append({
            'timestamp': datetime.now(),
            'action': signal['action'],
            'score': signal['score'],
            'entry_price': entry_price,
            'target': signal['trade_setup'].get('target'),
            'sl': signal['trade_setup'].get('sl')
        })

    def record_outcome(self, signal_id, exit_price, result):
        """Record outcome (hit target, hit SL, manual exit)"""
        self.outcomes.append({
            'signal_id': signal_id,
            'exit_price': exit_price,
            'result': result,  # 'target', 'sl', 'manual'
            'timestamp': datetime.now()
        })

    def calculate_win_rate(self):
        """Calculate win rate"""
        if not self.outcomes:
            return 0

        wins = sum(1 for o in self.outcomes if o['result'] == 'target')
        total = len(self.outcomes)

        return (wins / total) * 100

# Usage
tracker = SignalTracker()

# When signal is generated
signal = manager.generate_signals(pcr, max_pain)
if signal['action'] != 'WAIT':
    tracker.record_signal(signal, current_price)

# When trade exits
tracker.record_outcome(signal_id=0, exit_price=120, result='target')

# Check win rate
print(f"Win Rate: {tracker.calculate_win_rate():.1f}%")
```

---

## Rollback Plan

If you need to revert:

```bash
# 1. Stop application
pkill -f app.py

# 2. Restore original
mv utils/option_chain_v7_backup.py utils/option_chain_v7.py

# 3. Restart
python app.py

# 4. Verify
curl http://localhost:5000/trading/api/option-chain/NIFTY
```

---

## Performance Benchmarking

```python
import time

def benchmark_signal_generation():
    """Benchmark signal generation speed"""

    # Setup
    manager = OptionChainManagerV7Refactored(...)
    pcr = 1.05
    max_pain = 22000

    # Warm up
    for _ in range(10):
        manager.generate_signals(pcr, max_pain)

    # Benchmark
    iterations = 100
    start = time.time()

    for _ in range(iterations):
        manager.generate_signals(pcr, max_pain)

    end = time.time()

    avg_time = ((end - start) / iterations) * 1000  # ms

    print(f"Average signal generation time: {avg_time:.2f}ms")
    print(f"Expected: <5ms (acceptable: <10ms)")

    assert avg_time < 10, f"Performance regression: {avg_time:.2f}ms"

benchmark_signal_generation()
```

---

## FAQ

**Q: Can I use both versions simultaneously?**
A: Yes, see "Option 2: Side-by-Side Comparison" above.

**Q: Will this affect my WebSocket connections?**
A: No, WebSocket handling is unchanged. Only signal generation logic is refactored.

**Q: How do I know if it's working correctly?**
A: Run the validation tests above. Check that Max Pain signals are reversed.

**Q: What if I only want the Max Pain fix?**
A: Copy just the `_score_max_pain()` method (see "Cherry-Picking" section).

**Q: Can I adjust signal weights?**
A: Yes, edit `self.signal_weights` in `__init__()` method.

**Q: How do I tune for more/fewer signals?**
A: Adjust threshold in `generate_signals()`:
```python
if total_score > 55:  # Looser (was 65)
    bias = "BULLISH"
```

---

## Next Steps

1. ✅ Read REFACTORING_SUMMARY.md for complete details
2. ✅ Read BEFORE_AFTER_EXAMPLES.md for concrete scenarios
3. ✅ Choose integration approach (Option 1, 2, or 3)
4. ✅ Run validation tests
5. ✅ Deploy in test environment first
6. ✅ Monitor for 24-48 hours
7. ✅ Compare signals against original
8. ✅ Deploy to production with feature flag
9. ✅ Monitor win rate and signal quality
10. ✅ Gradually increase % of traffic to refactored version

**Good luck! 🚀**
