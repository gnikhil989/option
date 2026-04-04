# V9 SAFE GUARDIAN - System Flow Analysis & Bug Report

## Executive Summary

The V9 "SAFE ADAPTIVE GUARDIAN" is an autonomous options trading system that:
1. Runs a background trading engine independent of the UI
2. Uses signal aggregation (3-minute majority voting) to filter noise
3. Implements regime locking (15-minute inertia) to prevent whipsaw
4. Persists state for crash recovery
5. Enforces safety limits (max trades/day, warmup, cooldowns)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              FRONTEND (UI)                               │
│                     templates/option_chain_v9.html                       │
│  ┌─────────────────────────────────────────────────────────────────────┐│
│  │  - Creates SSE connection to /stream-v9/<underlying>                ││
│  │  - Creates session via /api/option-chain-session/create-v9          ││
│  │  - Displays option chain table + quant decision panel               ││
│  │  - Updates every 1 second via SSE                                   ││
│  └─────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ SSE (Server-Sent Events)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           FLASK APP (app.py)                             │
│  ┌─────────────────────────────────────────────────────────────────────┐│
│  │  Routes:                                                             ││
│  │  - /trading/option-chain-v9           → Renders HTML page            ││
│  │  - /trading/api/option-chain/stream-v9/<underlying>                  ││
│  │      → SSE generator, calls manager.get_option_chain() every 1s      ││
│  │  - /trading/api/option-chain-session/create-v9                       ││
│  │      → Creates/reuses OptionChainManagerV9Safe instance              ││
│  └─────────────────────────────────────────────────────────────────────┘│
│                                                                          │
│  Manager Lifecycle:                                                      │
│  - active_managers dict stores all running managers                      │
│  - cleanup_inactive_managers() stops old managers when switching symbols │
│  - manager_lock prevents race conditions                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ Uses
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              OPTION CHAIN MANAGER V9 SAFE                                │
│              utils/option_chain_v9_safe.py                               │
│  ┌─────────────────────────────────────────────────────────────────────┐│
│  │  Inherits from: OptionChainManagerV7Adaptive                        ││
│  │  - Data fetching (WebSocket subscriptions)                          ││
│  │  - Greek calculations (Delta, Gamma, Theta, Vega, GEX)              ││
│  │  - Score calculations (9 scoring methods)                           ││
│  └─────────────────────────────────────────────────────────────────────┘│
│                                                                          │
│  V9 Additions:                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐│
│  │  1. Background Trading Engine (_trade_engine_loop)                  ││
│  │  2. RegimeGuardian (15-min lock)                                    ││
│  │  3. TradeLoggerV9 (Signal Aggregation)                              ││
│  │  4. State Persistence (JSON)                                        ││
│  │  5. Safety Controls                                                 ││
│  └─────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ Uses
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    TRADE LOGGER V9                                       │
│                    utils/trade_logger_v9.py                              │
│  ┌─────────────────────────────────────────────────────────────────────┐│
│  │  SignalAggregator:                                                  ││
│  │  - 3-minute sliding window for entry signals                        ││
│  │  - 2-minute sliding window for exit signals                         ││
│  │  - Majority voting with confidence %                                ││
│  │                                                                      ││
│  │  CSV Logging:                                                       ││
│  │  - logs/v9_paper_trades.csv                                         ││
│  │  - Entry/Exit events with full context                              ││
│  └─────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Detailed Flow Analysis

### 1. Initialization Flow

```
User visits: /trading/option-chain-v9?underlying=NIFTY&expiry=24-FEB-26
                    │
                    ▼
app.py: option_chain_v9()
    │
    ├── get_api_client() → OpenAlgo API client
    │
    ├── If expiry not provided:
    │       └── Fetch expiries from API
    │
    ├── manager_key = "NIFTY_24-FEB-26_v9_safe"
    │
    ├── with manager_lock:
    │       │
    │       ├── If manager_key in active_managers:
    │       │       └── Reuse existing manager
    │       │
    │       └── Else:
    │               ├── get_or_create_websocket_manager()
    │               ├── OptionChainManagerV9Safe(underlying, expiry, ws_manager)
    │               │       │
    │               │       ├── super().__init__() → V7 initialization
    │               │       │       ├── Set strike_step (50 for NIFTY)
    │               │       │       ├── Initialize data structures
    │               │       │       ├── Initialize histories (price, PCR, OI, IV, etc.)
    │               │       │       └── Create TradeLogger instances
    │               │       │
    │               │       ├── Create TradeLoggerV9()
    │               │       ├── Create RegimeGuardian(lock_duration=900)
    │               │       ├── Set safety params (max_trades=5, warmup=180s, cooldown=60s)
    │               │       └── load_state() → Restore active_trade from JSON
    │               │
    │               └── manager.initialize(client)
    │                       ├── Fetch option chain symbols
    │                       ├── Calculate Greeks via py_vollib_vectorized
    │                       └── Set initialized = True
    │
    ├── manager.start_monitoring()
    │       │
    │       ├── super().start_monitoring() → V7 monitoring thread
    │       │       └── Starts _greek_refresh_loop() (30-second cycle)
    │       │
    │       └── start_engine()
    │               └── Starts _trade_engine_loop() (1-second cycle)
    │
    ├── cleanup_inactive_managers(manager_key)
    │       └── Stops all OTHER managers (quota protection)
    │
    └── Render option_chain_v9.html with chain_data
```

### 2. Background Trading Engine Flow

```
_trade_engine_loop() [Runs every 1 second]
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 1: Data Validation                                           │
│   - Check if option_data exists                                   │
│   - Check if atm_strike > 0                                       │
│   - If not ready, wait and continue                               │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 2: Calculate Market Metrics                                  │
│   calculate_market_metrics_internal()                             │
│   │                                                               │
│   ├── Calculate PCR = total_pe_oi / total_ce_oi                  │
│   │                                                               │
│   ├── Calculate 9 scores from V7:                                │
│   │   ├── delta_oi:    OI directional bias              (±30)    │
│   │   ├── iv_skew:     Put vs Call IV imbalance         (±15)    │
│   │   ├── oi_unwind:   Price vs OI correlation          (±20)    │
│   │   ├── max_pain:    Distance from max pain           (±10)    │
│   │   ├── gamma:       GEX and pinning detection        (±10)    │
│   │   ├── vanna:       IV vs Price divergence           (±15)    │
│   │   ├── inst_flow:   Institutional position detection (±20)    │
│   │   ├── momentum:    Price velocity                   (±25)    │
│   │   └── pcr_roc:     PCR rate of change               (±20)    │
│   │                                                               │
│   └── Return { raw_score: sum(), pcr, ltp, score_details }       │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 3: Heartbeat Log (every 5 seconds)                          │
│   Print: Score, Regime, Lock Status, Active Trade, Safety Lock    │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 4: Decision Branch                                           │
│                                                                   │
│   IF active_trade exists:                                         │
│       └── _manage_active_trade(metrics)                           │
│                                                                   │
│   ELSE IF NOT safety_lock:                                        │
│       └── _scan_for_entry_opportunities(metrics)                  │
│                                                                   │
│   ELSE:                                                           │
│       └── Do nothing (safety lock active)                         │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 5: Persist State (every 10 seconds)                          │
│   save_state() → logs/v9_state_{underlying}.json                  │
└───────────────────────────────────────────────────────────────────┘
```

### 3. Entry Decision Flow

```
_scan_for_entry_opportunities(metrics)
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ GATE 1: Warmup Check                                              │
│   IF time.time() - startup_time < 180s:                          │
│       └── RETURN (still warming up)                               │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ GATE 2: Exit Cooldown Check                                       │
│   IF time.time() < exit_cooldown_until:                          │
│       └── RETURN (cooling down after last exit)                   │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 3: Determine Instantaneous Action                            │
│   raw_score = metrics['raw_score']                                │
│                                                                   │
│   IF raw_score > 20:    instant_action = 'BUY CALL'              │
│   ELIF raw_score < -20: instant_action = 'BUY PUT'               │
│   ELSE:                 instant_action = 'WAIT'                   │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 4: Feed Signal Aggregator                                    │
│   entry_aggregator.add_signal(                                    │
│       action=instant_action,                                      │
│       score=raw_score,                                            │
│       regime=guardian.current_regime                              │
│   )                                                               │
│                                                                   │
│   Aggregator maintains 3-minute sliding window of signals         │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 5: Get Majority Vote                                         │
│   majority_action, confidence, sample_count, avg_score, _         │
│       = entry_aggregator.get_majority_signal()                    │
│                                                                   │
│   This counts signals by ACTION in the window and returns         │
│   the most common action with its confidence percentage           │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ GATE 6: Strict Entry Conditions                                   │
│                                                                   │
│   IF majority_action == 'WAIT': RETURN                            │
│   IF confidence < 60%:          RETURN  (Need 60% agreement)      │
│   IF sample_count < 60:         RETURN  (Need ~1 min of data)     │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ GATE 7: Regime Guardian Check                                     │
│                                                                   │
│   detected_regime = 'BULLISH' if avg_score > 0 else 'BEARISH'    │
│   guardian_regime = guardian.update(detected_regime, confidence)  │
│                                                                   │
│   Regime Guardian Logic:                                          │
│   - If locked and lock_duration (15m) not passed: keep old regime│
│   - If not locked and confidence > 60%: update regime & lock      │
│                                                                   │
│   Conflict Check:                                                 │
│   IF 'CALL' in action AND guardian_regime == 'BEARISH': RETURN   │
│   IF 'PUT' in action AND guardian_regime == 'BULLISH': RETURN    │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ GATE 8: Safety Lock Check                                         │
│   IF safety_lock: RETURN                                          │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 9: Execute Entry                                             │
│   _execute_entry(majority_action, avg_score)                      │
│                                                                   │
│   1. Select best strike via _select_best_strike()                 │
│   2. Get current LTP of the option                                │
│   3. Calculate SL = LTP * 0.85 (15% loss)                        │
│   4. Calculate Target = LTP * 1.30 (30% profit)                  │
│   5. Create active_trade dict                                     │
│   6. Log to CSV                                                   │
│   7. save_state()                                                 │
└───────────────────────────────────────────────────────────────────┘
```

### 4. Trade Management Flow

```
_manage_active_trade(metrics)
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 1: Get Current Option Price                                  │
│   strike = trade['strike']                                        │
│   opt_type = trade['type']  (CE or PE)                           │
│   current_ltp = option_data[strike][tag]['ltp']                  │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 2: Check Hard Exit Levels                                    │
│                                                                   │
│   IF direction == 'BUY':                                          │
│       IF current_ltp <= SL:     exit_reason = "SL HIT"           │
│       ELIF current_ltp >= TGT:  exit_reason = "TARGET HIT"       │
│                                                                   │
│   (Note: V9 always BUYS options, never SELLs)                     │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 3: Trailing Stop Logic                                       │
│                                                                   │
│   Calculate profit_pct:                                           │
│     = (current_ltp - entry) / entry * 100                        │
│                                                                   │
│   IF profit_pct > 15% AND NOT sl_moved_to_cost:                  │
│       trade['sl'] = entry_price  (Move SL to breakeven)          │
│       trade['sl_moved_to_cost'] = True                           │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 4: Signal-Based Exit Check                                   │
│                                                                   │
│   IF no hard exit triggered:                                      │
│       temp_action = 'BUY CALL' if score > 20 else                │
│                     'BUY PUT' if score < -20 else 'WAIT'         │
│                                                                   │
│       exit_signal = logger._check_signal_exit(signal_data, trade)│
│                                                                   │
│       Signal flip detection:                                      │
│       - If we're in a CALL and signals flip to PUT               │
│       - If we're in a PUT and signals flip to CALL               │
│       - Uses 2-minute exit aggregator window                      │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ STEP 5: Execute Exit (if triggered)                               │
│   _close_trade(trade, exit_price, reason)                        │
│                                                                   │
│   1. Calculate PnL = exit_price - entry_price                    │
│   2. Update daily_pnl_points                                      │
│   3. Increment trades_today                                       │
│   4. Log to CSV                                                   │
│   5. Clear active_trade                                           │
│   6. Set exit_cooldown_until = now + 60s                         │
│   7. save_state()                                                 │
│   8. IF trades_today >= 5: safety_lock = True                    │
└───────────────────────────────────────────────────────────────────┘
```

---

## Score Calculation Details

### Scoring Methods (from V7 Parent)

| Method | Range | Bullish Trigger | Bearish Trigger |
|--------|-------|-----------------|-----------------|
| `_score_delta_oi` | ±30 | Net negative delta OI (Put writing) | Net positive delta OI (Call writing) |
| `_score_iv_skew` | ±15 | Put IV spike + price not falling | Call IV spike (FOMO) |
| `_score_oi_unwind` | ±20 | Price ↑ + OI ↑ (Long buildup) | Price ↓ + OI ↑ (Short buildup) |
| `_score_max_pain` | ±10 | Below max pain (pull up expected) | Above max pain (pull down expected) |
| `_score_gamma_exposure` | ±10 | Negative GEX + uptrend | Negative GEX + downtrend |
| `_score_vanna_charm` | ±15 | Price ↑ + IV ↓ (dealer covering) | Price ↓ + IV ↑ (hedging spike) |
| `_detect_institutional_flow` | ±20 | Fresh PE positions below spot | Fresh CE positions above spot |
| `_score_momentum_velocity` | ±25 | +0.3% move (burst) | -0.3% move (crash) |
| `_score_pcr_roc` | ±20 | PCR rising >3% | PCR falling >3% |

**Total Score Range: -165 to +165**

### Entry Thresholds

```
Score > +20  →  BUY CALL candidate
Score < -20  →  BUY PUT candidate
-20 to +20   →  WAIT

Then filtered by:
- 60% majority confidence over 3 minutes
- Minimum 60 signals in window
- Regime Guardian alignment
```

---

## Bugs & Logical Issues Identified

### Critical Bugs

#### 1. **`trades_today` Never Resets** (Severity: HIGH)
**Location:** `option_chain_v9_safe.py:319, 363-366`
```python
# Problem: trades_today increments on exit but never resets at day start
self.trades_today += 1

# After 5 trades, safety_lock becomes True forever
if self.trades_today >= self.max_trades_per_day:
    self.safety_lock = True
```
**Impact:** After 5 trades, system permanently locks. Requires manual restart or state file deletion.
**Fix:** Add daily reset logic in `_trade_engine_loop()`:
```python
# Check if new trading day
current_date = datetime.now().date()
if hasattr(self, '_last_trade_date') and self._last_trade_date != current_date:
    self.trades_today = 0
    self.safety_lock = False
    self.daily_pnl_points = 0
self._last_trade_date = current_date
```

#### 2. **Exit Cooldown Not Persisted** (Severity: MEDIUM)
**Location:** `option_chain_v9_safe.py:555-566`
```python
# save_state() does NOT save exit_cooldown_until
state = {
    'active_trade': self.active_trade,
    'trades_today': self.trades_today,
    'daily_pnl': self.daily_pnl_points,
    'safety_lock': self.safety_lock
    # MISSING: 'exit_cooldown_until'
}
```
**Impact:** After restart, cooldown is bypassed. Could cause rapid re-entry.
**Fix:** Add to state dict and restore in `load_state()`.

#### 3. **Warmup Resets on Every Restart** (Severity: MEDIUM)
**Location:** `option_chain_v9_safe.py:87-88`
```python
self.startup_time = time.time()  # Always set to NOW
self.min_warmup_seconds = 180
```
**Impact:** Hot restart during market hours waits 3 minutes even if system was running fine.
**Consideration:** This might be intentional for safety, but a "warm restart" option could be useful.

### Logic Bugs

#### 4. **Regime Guardian Confidence Check Edge Case** (Severity: LOW)
**Location:** `option_chain_v9_safe.py:52`
```python
# Guardian requires confidence > 60
if confidence > 60:  # Strict greater than

# But entry requires confidence >= 60
if confidence < 60: return  # Line 423
```
**Impact:** If confidence is exactly 60%, entry is allowed but regime won't update. Minor edge case.

#### 5. **Signal Aggregator Strike Tracking Mismatch** (Severity: MEDIUM)
**Location:** `option_chain_v9_safe.py:400-404`
```python
# V9 feeds aggregator WITHOUT strike
self.logger.entry_aggregator.add_signal(
    action=instant_action,
    score=raw_score,
    regime=self.guardian.current_regime
    # MISSING: strike, trade_type, direction
)
```
**Impact:** Logger's sophisticated strike-based filtering (to handle ATM changes) is bypassed.
**Fix:** Pass strike information:
```python
self.logger.entry_aggregator.add_signal(
    action=instant_action,
    score=raw_score,
    regime=self.guardian.current_regime,
    strike=self.atm_strike,
    trade_type='CE' if 'CALL' in instant_action else 'PE' if 'PUT' in instant_action else None,
    direction='BUY'
)
```

#### 6. **Trade Dict Key Case Mismatch** (Severity: MEDIUM)
**Location:** `option_chain_v9_safe.py:468-494`
```python
self.active_trade = {
    'direction': 'BUY',  # lowercase
    # ... later ...
    'Direction': 'BUY',  # UPPERCASE (duplicate!)
}
```
**Impact:** Logger's `_check_signal_exit()` uses `trade['Direction']` (uppercase). Confusion and potential KeyError in edge cases.
**Fix:** Use consistent casing throughout.

#### 7. **PCR Not Passed to Signal Data** (Severity: LOW)
**Location:** `option_chain_v9_safe.py:277-281`
```python
signal_data = {
    'action': 'WAIT',
    'score': metrics['raw_score'],
    'regime': self.guardian.current_regime
    # MISSING: 'pcr' from metrics
}
```
**Impact:** Logger's exit logic can use PCR for context, but it's missing.

### Architectural Issues

#### 8. **V7 `_select_best_strike()` May Not Align with V9 Philosophy**
**Location:** Inherited from `option_chain_v7_adaptive.py`
**Concern:** V9's "safe" approach might want more conservative strike selection (deeper ITM for less risk), but it uses V7's strike selection which may prefer ATM or slight OTM for higher gamma.

#### 9. **Lot Size Inconsistency**
**Location:** `option_chain_v9_safe.py:489, 499`
```python
'Lot_Size': 50,  # Hardcoded default

# Then attempts to get from logger
self.active_trade['Lot_Size'] = self.logger._get_lot_size(self.underlying)
```
**Impact:** If logger method fails (which it catches with try/except), lot size stays as 50 even for BANKNIFTY (should be 15).

#### 10. **No Market Hours Check**
**Concern:** The trading engine runs 24/7 if the app is running. Should check if within market hours (9:15 AM - 3:30 PM IST).

### UI/UX Issues

#### 11. **SSE Stream Creates Duplicate Managers**
**Location:** `app.py:1151-1164`
If the SSE stream endpoint (`/stream-v9/<underlying>`) is called directly without first visiting the page, it creates a new manager. The page creates one via the page route, and the SSE creates another.

#### 12. **Session ID Not Available for Mode Switch**
**Location:** `option_chain_v9.html:1099-1104`
```javascript
const sessionId = optionChainSessionId;
if (!sessionId) {
    console.error("No active session ID found for mode switch");
    alert("Session not active. Please wait or refresh.");
    return;
}
```
**Impact:** If user tries to switch mode before session is created (first 2 seconds), it fails.

---

## Performance Considerations

### 1. **Score Calculation Frequency**
Currently calculates all 9 scores every 1 second. Some scores (like max_pain) could be cached for 30+ seconds.

### 2. **Signal History Memory**
```python
self.signal_history = deque(maxlen=2000)  # 2000 signals
```
At 1 signal/second, this is ~33 minutes of history. With 3-minute window, most of this is unused.

### 3. **State File I/O**
State saved every 10 seconds. Consider buffering or reducing frequency.

---

## Recommendations

### Immediate Fixes
1. Add daily reset logic for `trades_today` and `safety_lock`
2. Persist `exit_cooldown_until` in state
3. Fix trade dict key casing consistency
4. Pass strike info to signal aggregator

### Enhancements
1. Add market hours check
2. Add configurable warmup behavior (full vs warm restart)
3. Cache expensive calculations (max_pain, GEX)
4. Add WebSocket reconnection handling

### Testing Needed
1. Multi-day operation (does it reset properly?)
2. Crash recovery (does state restore correctly?)
3. Symbol switching (are old managers properly cleaned up?)
4. High volatility scenarios (does signal aggregation work?)

---

## File Reference

| File | Purpose | Key Functions |
|------|---------|---------------|
| `app.py:658-711` | V9 route handler | `option_chain_v9()` |
| `app.py:1133-1177` | V9 SSE stream | `option_chain_stream_v9()` |
| `option_chain_v9_safe.py:60-98` | V9 initialization | `__init__()` |
| `option_chain_v9_safe.py:139-185` | Trading engine | `_trade_engine_loop()` |
| `option_chain_v9_safe.py:187-222` | Score calculation | `calculate_market_metrics_internal()` |
| `option_chain_v9_safe.py:224-314` | Trade management | `_manage_active_trade()` |
| `option_chain_v9_safe.py:316-366` | Exit execution | `_close_trade()` |
| `option_chain_v9_safe.py:368-444` | Entry scanning | `_scan_for_entry_opportunities()` |
| `option_chain_v9_safe.py:448-553` | Entry execution | `_execute_entry()` |
| `trade_logger_v9.py:24-163` | Signal aggregation | `SignalAggregator` |
| `trade_logger_v9.py:165-1139` | Trade logging | `TradeLoggerV9` |
| `option_chain_v7_adaptive.py:46-164` | V7 base init | Inherited by V9 |
| `option_chain_v7_adaptive.py:203-673` | Score methods | All `_score_*` methods |

---

## Glossary

| Term | Definition |
|------|------------|
| ATM | At-The-Money - strike closest to current price |
| GEX | Gamma Exposure - aggregate gamma × OI across strikes |
| PCR | Put-Call Ratio - total PE OI / total CE OI |
| OI | Open Interest - number of outstanding contracts |
| IV | Implied Volatility |
| Regime | Market state (BULLISH/BEARISH/NEUTRAL) |
| Majority Vote | Most common signal over 3-minute window |
| Guardian Lock | 15-minute freeze on regime changes |
| Safety Lock | System halt after max daily trades |

---

*Document generated: 2026-02-05*
*V9 Engine Version: SAFE ADAPTIVE GUARDIAN*
