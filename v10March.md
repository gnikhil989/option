# V10 Safe Adaptive Guardian - Complete Architecture & Logic Reference

> **Version**: V10 (March 2026)
> **Codename**: Safe Adaptive Guardian
> **Files**: `utils/option_chain_v10_safe.py`, `utils/trade_logger_v10.py`, `utils/v10_strategies.py`, `templates/option_chain_v10.html`

---

## 1. DESIGN PRINCIPLES

V10 is built on 5 pillars:

| Principle | What it means | Implementation |
|-----------|--------------|----------------|
| **AUTONOMY** | Runs independently of the UI in a background thread | `_trade_engine_loop()` runs at 1Hz in a daemon thread |
| **CALM** | Signal smoothing to kill noise/whipsaw | `SignalAggregator` with 60-300s windows + majority voting |
| **COMMITMENT** | Once in a trade, ignores new entry signals | `active_trade` flag per strategy; only exit logic runs |
| **INERTIA** | Regime locking to prevent rapid flipping | `RegimeGuardian` with 15-minute lock duration |
| **SAFETY** | VWAP & Volatility filters, daily loss limits | Daily PnL caps, max trades/day, warmup period |

---

## 2. HIGH-LEVEL ARCHITECTURE

```
                        WebSocket (Market Data)
                               |
                               v
                    handle_quote_update()
                    (Updates option_data, underlying_ltp)
                               |
          +--------------------+--------------------+
          |                                         |
    Greek Monitor Thread                    Trade Engine Thread
    (_greek_monitor_loop)                   (_trade_engine_loop)
    - Refreshes Greeks every ~4s            - Runs every 1 second
    - Smart batching (ATM + rotation)       - Calls update_market_state()
    - API call to multioptiongreeks()       - Runs 4 strategies in parallel
          |                                         |
          v                                         v
    option_data updated                   generate_signals() -> 9 scoring functions
    (delta, gamma, theta, vega, iv, gex)  -> weighted composite score (-100 to +100)
                                                    |
                                                    v
                                          Strategy Hub (4 strategies)
                                          Each with its own:
                                            - SignalAggregator (entry + exit)
                                            - TradeLoggerv10 (CSV file)
                                            - Active trade state
                                                    |
                                                    v
                                              SSE Stream -> UI
                                          (get_option_chain() called by Flask)
```

---

## 3. THE TRADE ENGINE (`_trade_engine_loop`)

**Location**: `option_chain_v10_safe.py:441`

The engine is the heartbeat of V10. It runs in a background daemon thread at **1Hz** (1 tick per second).

### Per-tick flow:

1. **Daily Reset Check** (`_check_daily_reset`) - At 9:15-9:20 AM IST, resets all counters
2. **Data Freshness Check** - Waits if `option_data` is empty or `atm_strike == 0`
3. **Market State Update** (`update_market_state`) - Snapshots data, computes signals
4. **Strategy Hub** - Iterates all 4 strategies, calls `strategy.process_signals(manager, metrics)`
5. **State Persistence** - Every 10 seconds, saves state to JSON (`logs/v10_state_{underlying}.json`)

### State Persistence

State is saved/loaded to survive restarts:
- `trades_today`, `daily_pnl`, `safety_lock`
- Active trades per strategy (skips closed trades marked with `_closed=True`)
- Exit cooldown timestamps
- Last reset date (prevents double-reset)

---

## 4. SIGNAL GENERATION (The Quant Brain)

**Location**: `option_chain_v10_safe.py:531` (`generate_signals`)

### 9 Scoring Functions

Each returns a score (typically -30 to +30) and a human-readable reason string:

| # | Signal | Weight | What it measures | Score Range |
|---|--------|--------|-----------------|-------------|
| 1 | **Delta OI** (`_score_delta_oi`) | 1.5 | Net directional OI change across all strikes. CE OI build = bearish, PE OI build = bullish | -30 to +30 |
| 2 | **IV Skew** (`_score_iv_skew`) | 0.7 | ATM PE IV vs CE IV difference. PE IV > CE IV = bearish skew | -10 to +10 |
| 3 | **OI Unwind** (`_score_oi_unwind`) | 1.3 | Price trend vs OI trend correlation: Long buildup, Short buildup, Short covering, Long unwinding | -20 to +20 |
| 4 | **Max Pain** | 0.8 | Distance of spot from max pain strike. Pulls price toward max pain | -10 to +10 |
| 5 | **Gamma Exposure** (`_score_gamma_exposure`) | 1.0 | Net GEX across all strikes. Positive = stability, Negative = instability | -10 to +5 |
| 6 | **Vanna/Charm** (`_score_vanna_charm`) | 0.6 | IV crush detection (current IV < avg IV * 0.98 = bullish) | 0 to +10 |
| 7 | **Institutional Flow** (`_detect_institutional_flow`) | 1.2 | Bid/Ask volume imbalance across all strikes. >20% imbalance = directional | -10 to +10 |
| 8 | **Momentum Velocity** (`_score_momentum_velocity`) | 1.0 | 10-tick price velocity. Threshold: 0.01% for NIFTY sensitivity | -10 to +10 |
| 9 | **PCR Rate of Change** (`_score_pcr_roc`) | 1.2 | PCR trend over last 15 samples. Rising PCR = bullish (more puts being written) | -10 to +10 |

### Composite Score Calculation

```
weighted_score = sum(score_i * weight_i)  for each signal
max_possible = sum(all_weights) * 30
total_score = clamp((weighted_score / max_possible) * 100, -100, +100)
```

**Action mapping**:
- Score > +20 -> `BUY CALL`
- Score < -20 -> `BUY PUT`
- Otherwise -> `WAIT`

### Data Histories (deques)

| History | Max Length | Purpose |
|---------|-----------|---------|
| `price_history` | 600 | Momentum, volatility calculations |
| `atm_ce_history` / `atm_pe_history` | 600 | ATM option price tracking |
| `pcr_history` | 600 | PCR rate of change |
| `oi_history` | 600 | OI trend detection |
| `iv_history` | 120 | IV crush / vanna-charm |
| `gex_history` | 120 | GEX trend |
| `vanna_history` / `charm_history` | 120 | Second-order Greeks |

---

## 5. THE STRATEGY HUB (4 Parallel Strategies)

**Location**: `utils/v10_strategies.py`

All strategies inherit from `BaseStrategy` and differ only in parameters:

| Strategy | Entry Score | Agg Window | Min Confidence | Min Hold | Exit Style | CSV File |
|----------|------------|------------|---------------|----------|-----------|----------|
| **V10_Safe** | 20 | 180s (3min) | 60% | 60s | Conservative | `logs/v10_v10_safe_trades.csv` |
| **V10_Aggressive** | 15 | 60s (1min) | 55% | 10s | Aggressive | `logs/v10_v10_aggressive_trades.csv` |
| **V10_Sideways** | 35 | 120s (2min) | 65% | 30s | Scalp | `logs/v10_v10_sideways_trades.csv` |
| **V10_Quant_Max** | 50 | 300s (5min) | 70% | 300s (5min) | Conservative | `logs/v10_v10_quant_max_trades.csv` |

### Strategy Processing Flow (`process_signals`)

```
1. Determine instant action from raw_score vs entry_score_threshold
2. Feed action + score + regime + strike into entry_aggregator
3. Every 30 signals: print heartbeat log
4. If active trade exists -> _manage_active_trade()
   Else if under daily limit and past cooldown -> _check_entry()
```

### Entry Logic (`_check_entry`)

Sequential gates (all must pass):

1. **Majority Vote**: Entry aggregator must NOT say WAIT
2. **Sample Check**: `sample_count >= entry_min_samples` (derived from `agg_window // 3`)
3. **Confidence Check**: Majority confidence >= `min_confidence` threshold
4. **Regime Check**: Signal direction must align with `RegimeGuardian` regime
   - BUY CALL blocked if regime is BEARISH
   - BUY PUT blocked if regime is BULLISH

### Entry Execution (`_execute_entry`)

- Selects **ATM strike** via `_select_best_strike(bias)` (always ATM for now)
- Calculates **SL/Target** based on exit style and recent volatility:
  - `vol_multiplier = clamp(volatility / 0.15, 0.8, 2.0)`
  - Conservative: SL 15%, Target 30% (adjusted by vol_multiplier)
  - Aggressive: SL 12%, Target 40%
  - Scalp: SL 8%, Target 15%
- All BUY trades: SL below entry, Target above entry
- Logs ENTRY row to strategy-specific CSV
- Clears entry aggregator

### Exit Logic (`_manage_active_trade`)

Three exit triggers checked every tick:

1. **Stop Loss Hit**: `current_ltp <= trade['sl']`
2. **Target Hit**: `current_ltp >= trade['target']`
3. **Signal Flip**: Raw score flips significantly (CE trade + score < -15, or PE trade + score > 15)
   - Flip signals fed into `exit_aggregator`
   - Needs `exit_min_samples` with >= 70% confidence to trigger

### Trade Close (`_close_trade`)

**Critical anti-double-close logic**:
1. Mark trade as `_closed = True` immediately
2. Set `self.active_trade = None` immediately
3. Set exit cooldown (120s conservative, 60s aggressive)
4. Calculate PnL = exit_price - entry_price
5. Log EXIT row to CSV
6. Clear both aggregators

---

## 6. REGIME GUARDIAN

**Location**: `option_chain_v10_safe.py:55` (`RegimeGuardian`)

Prevents rapid regime switching (whipsaw protection):

- **Lock Duration**: 15 minutes (900 seconds)
- States: `NEUTRAL`, `BULLISH`, `BEARISH`
- Once a regime is set, it cannot change for 15 minutes
- `update(proposed_regime, confidence)` returns current (possibly locked) regime
- Acts as a gate for strategy entries (blocks counter-trend trades)

---

## 7. SIGNAL AGGREGATION (Majority Voting)

**Location**: `utils/trade_logger_v10.py:24` (`SignalAggregator`)

The core anti-whipsaw mechanism. Instead of reacting to every signal, it collects signals over a time window and uses majority voting.

### How it works:

1. **Collect**: Each tick, `add_signal(action, score, regime, strike, contract)` is called
2. **Window**: Only signals within the last `window_seconds` are considered
3. **Cleanup**: Signals older than 2x window are purged (prevents memory leak)
4. **Vote**: `get_majority_signal()` returns:
   - `majority_action`: Most common action in the window
   - `confidence`: Percentage of signals agreeing (e.g., 75% say BUY CALL)
   - `sample_count`: Total signals in window
   - `avg_score`: Average score for the majority action
   - `majority_strike`: Most recent strike associated with the majority action

### Two Aggregators Per Strategy:

| Aggregator | Window | Min Samples | Purpose |
|-----------|--------|-------------|---------|
| `entry_aggregator` | Strategy's `agg_window` | `agg_window // 3` | Confirms entry signals |
| `exit_aggregator` | `agg_window // 2` | `max(5, min_samples // 2)` | Confirms signal flip exits |

---

## 8. TRADE LOGGER V10

**Location**: `utils/trade_logger_v10.py:171` (`TradeLoggerv10`)

### CSV Structure (34 columns)

```
Trade_ID, Timestamp, Status (ENTRY/EXIT), Symbol, Contract, Strike, Type (CE/PE/STRADDLE/STRANGLE),
Direction (BUY/SELL), Strategy, Regime, Entry_Price, Exit_Price, Quantity, Lot_Size, Target, StopLoss,
Underlying_LTP, PnL_Points, PnL_Amount, ROI_Pct, Score, Confidence, Reason, Signal_Distribution,
Entry_Confidence, Snapshot_PCR, Snapshot_IV, Entry_Delta, Entry_Gamma, Entry_Theta,
Time_Held_Sec, Max_Profit_Seen, Max_Loss_Seen
```

### Key Features:

- **Thread-safe writes**: `self.lock` wraps all file I/O
- **Performance cache**: 30-second TTL cache on `get_performance_summary()` to avoid CSV re-reads
- **Lot sizes**: Built-in mapping (NIFTY=75, BANKNIFTY=15, RELIANCE=250, etc.)
- **Verbose console logging**: Heartbeat logs showing signal status, majority vote, distribution
- **Entry/Exit event banners**: Prominent console prints with trade details and PnL

### The `process_signal` Method (Legacy/UI Path)

This is the older, more complete signal processing path used for UI-driven signals:
- Handles BUY/SELL CALL/PUT/STRADDLE/STRANGLE
- Validates trade_setup consistency with majority action
- Calculates entry levels from option_data if trade_setup is missing
- Manages SL/Target for both LONG and SHORT positions
- Tracks Max_Price_Seen / Min_Price_Seen for watermark PnL

### Performance Summary (`get_performance_summary`)

Aggregates from CSV:
- Total/Winning/Losing trades, Win rate
- Total PnL, Average win/loss amounts
- Average hold time
- Breakdown by trade type (CE/PE/STRADDLE/STRANGLE)
- Cached for 30 seconds to avoid repeated CSV reads

---

## 9. GREEK MONITORING

**Location**: `option_chain_v10_safe.py:281` (`_greek_monitor_loop`)

Runs in a separate daemon thread, refreshing Greeks every ~4 seconds.

### Smart Batching Strategy:

- **Slot 1**: Always refreshes ATM strike (highest priority)
- **Slot 2**: Rotates through neighbors:
  - Cycle 0: ATM+1 (nearest neighbor)
  - Cycle 1: ATM-1 (second nearest)
  - Cycle 2: Background rotation (strikes 3+ away)
- **Rate limit handling**: 429 response triggers 60-second backoff
- **Thread safety**: Greek updates happen under `data_lock`

### Greeks Stored Per Strike:

`delta`, `gamma`, `theta`, `vega`, `rho`, `iv`, `gex` (computed from gamma * spot * OI * 100 * 0.01)

---

## 10. WEBSOCKET DATA FLOW

**Location**: `option_chain_v10_safe.py:1142` (`handle_quote_update`)

### Subscription Setup:

1. Register handler with `websocket_manager.register_handler('quote', self.handle_quote_update)`
2. Subscribe to underlying (NSE_INDEX for NIFTY, BSE_INDEX for SENSEX, NSE for stocks)
3. Batch subscribe all option strikes on NFO exchange

### Quote Update Handling:

**Underlying updates** (under `data_lock`):
- Updates: `ltp`, `bid`, `ask`, `open`, `high`, `low`, `close`, `avg`
- Recalculates ATM strike; if changed, regenerates strikes or updates tags

**Option updates**:
- Maps incoming symbol to strike via `subscription_map`
- Extracts: `ltp`, `bid`, `ask`, `bid_qty`, `ask_qty`, `volume`, `oi`, `open`, `high`, `low`, `close`, `avg_price`, `spread`
- Zero-protection: Ignores zero values for volume/OI if existing value is non-zero (prevents data regression)

### ATM Calculation:

```python
atm_strike = round(underlying_ltp / strike_step) * strike_step
```

Strike steps: NIFTY=50, BANKNIFTY/SENSEX=100, Stocks=10

---

## 11. UI (Frontend)

**Location**: `templates/option_chain_v10.html`

### Tech Stack:
- **CSS Framework**: TailwindCSS + DaisyUI
- **Data Transport**: Server-Sent Events (SSE) via `/trading/api/option-chain/stream-v10/{underlying}`
- **Template Engine**: Jinja2 (Flask)

### Page Layout (top to bottom):

1. **Header**: Underlying selector, Expiry selector, Refresh button
2. **Market Stats Bar**: Spot LTP/Bid/Ask, ATM Strike, PCR, Total Volume (CE/PE), Total OI (CE/PE), OHLC
3. **Quant Decision Engine Dashboard**:
   - Recommended Action (BUY CALL/PUT/WAIT) with score meter (-100 to +100)
   - Active Trade Setup (Contract, Entry, SL, Target, R:R badge)
   - Analysis reasons list
   - Flow & Momentum alerts with PCR momentum badge
   - Strategy mode selector (Sniper/Scalper radio buttons)
   - Opportunity ticker (scrolling marquee)
4. **Option Chain Table**: Full CE/PE data with:
   - Findings column (icons: Gamma Wall, Institutional Flow, Momentum)
   - Greeks: GEX, Vega, Theta, Gamma, Delta, IV
   - Market data: OI (with delta OI), Volume, LTP, OHLC, Avg, Spread
   - ATM row highlighted, ITM/OTM color-coded
5. **Connection Status**: WebSocket badge + last update timestamp

### Real-Time Update Flow:

```
1. Page Load -> preloadAllExpiries() (fetches expiry dates for all underlyings)
2. -> startRealTimeUpdates() (creates EventSource SSE connection)
3. SSE message arrives -> JSON parsed
4. requestAnimationFrame() throttling (prevents render queue backup)
5. updateOptionChain(data):
   a. updateMarketInfo(data)  - Updates stats bar, quant engine dashboard
   b. updateOptionRow(option) - Updates each strike row with animations
6. Price change animations: green flash (up), red flash (down)
```

### Session Management:

- `createOptionChainSession()` on page load (2s delay)
- `destroyOptionChainSession()` on underlying change
- Heartbeat interval keeps session alive
- Strategy mode switch sends POST to `/trading/api/option-chain-v10/set-mode`

---

## 12. THREAD SAFETY & LOCK STRATEGY

**Critical Rule**: Never do I/O (CSV reads, API calls) while holding `data_lock`.

### Lock Map:

| Lock | Protects | Location |
|------|----------|----------|
| `data_lock` | `option_data`, underlying prices, OI histories | `OptionChainManagerV10` |
| `cache.lock` | TTL cache reads/writes | `OptionChainCacheV10` |
| `logger.lock` | CSV file writes | `TradeLoggerv10` |
| `aggregator.lock` (RLock) | Signal history deque | `SignalAggregator` |

### Pattern Used:

```python
# CORRECT: Snapshot under lock, compute outside
with self.data_lock:
    snapshot = {quick_copy_of_data}

# Heavy computation outside lock
result = expensive_calculation(snapshot)
```

### Known Bug Fixes Applied:

1. **Bug #1**: Active trades were lost on restart -> Added strategy state persistence
2. **Bug #2**: Daily counters never reset -> Added `_check_daily_reset()` at market open
3. **Bug #3**: Cooldown lost on restart -> Persisted `exit_cooldown_until`
4. **Bug #4**: Greek updates not thread-safe -> Added `data_lock` to `_update_greeks_batch`
5. **UI Freeze**: CSV reads under `data_lock` -> Moved outside lock

---

## 13. CSV LOG FILES

Each strategy writes to its own CSV file:

```
logs/v10_v10_safe_trades.csv
logs/v10_v10_aggressive_trades.csv
logs/v10_v10_sideways_trades.csv
logs/v10_v10_quant_max_trades.csv
```

Each file has the same 34-column schema (see Section 8).

**Two rows per trade**: One ENTRY row, one EXIT row (matched by `Trade_ID`).

### Performance Summary (aggregated for UI):

`get_paper_trade_summary()` in the manager iterates all 4 strategy loggers, calls `get_performance_summary()` on each, and combines:
- `total_trades` (sum across strategies)
- `total_pnl` (sum across strategies)
- Per-strategy breakdown with win rate, avg win/loss, hold time

---

## 14. STATE FILE

**Path**: `logs/v10_state_{underlying}.json`

```json
{
  "trades_today": 2,
  "daily_pnl": 45.5,
  "safety_lock": false,
  "active_trade": null,
  "exit_cooldown_until": 1709500000,
  "last_reset_date": "2026-03-15",
  "strategy_states": {
    "V10_Safe": {
      "active_trade": { "Trade_ID": "...", "Contract": "...", ... },
      "trades_today": 1,
      "daily_pnl": 30.0,
      "exit_cooldown_until": 0
    },
    "V10_Aggressive": { ... },
    "V10_Sideways": { ... },
    "V10_Quant_Max": { ... }
  }
}
```

---

## 15. KEY CONFIGURATION CONSTANTS

| Constant | Value | Location |
|----------|-------|----------|
| Engine tick rate | 1 second | `_trade_engine_loop` |
| Greek refresh rate | ~4 seconds | `_greek_monitor_loop` |
| Regime lock duration | 900s (15 min) | `RegimeGuardian` |
| Warmup period | 60 seconds | `min_warmup_seconds` |
| Exit cooldown | 60-120 seconds | Per strategy exit_style |
| Max trades/day (strategy) | 50 | `BaseStrategy.max_trades_per_day` |
| Max trades/day (manager) | 5 | `OptionChainManagerV10.max_trades_per_day` |
| State save interval | 10 seconds | `_trade_engine_loop` |
| Performance cache TTL | 30 seconds | `TradeLoggerv10._perf_cache_ttl` |
| Price history length | 600 ticks | `price_history` deque maxlen |
| IV/GEX history length | 120 ticks | `iv_history`, `gex_history` deque maxlen |
| Signal aggregator maxlen | 2000 signals | `SignalAggregator.signal_history` |
| Cleanup interval | 2x window | `_cleanup_old_signals` |

---

## 16. SUPPORTED UNDERLYINGS

| Symbol | Exchange | Strike Step |
|--------|----------|------------|
| NIFTY | NSE_INDEX | 50 |
| BANKNIFTY | NSE_INDEX -> NFO | 100 |
| SENSEX | BSE_INDEX | 100 |
| RELIANCE, HDFCBANK, ICICIBANK, SBIN, INFY, BHARTIARTL | NSE | 10 |

### Strike Generation:

From ATM: **6 ITM + ATM + 4 OTM** = 11 strikes total

Symbol format: `{UNDERLYING}{DD}{MON}{YY}{STRIKE}{CE/PE}`
Example: `NIFTY28AUG2524500CE`

---

## 17. IMPORTANT GOTCHAS FOR DEVELOPERS

1. **Legacy imports**: `TradeLogger` and `TradeLoggerV3` are imported but only used for the legacy `self.logger` instance. The actual V10 strategies use `TradeLoggerv10`.

2. **`process_signal` on TradeLoggerv10**: This is the older, full-featured entry/exit manager. V10 strategies bypass it entirely, using only `entry_aggregator`, `exit_aggregator`, and `log_trade()` directly. The method exists for backward compatibility.

3. **Manager vs Strategy trade limits**: Manager has `max_trades_per_day=5`, each strategy has `max_trades_per_day=50`. These are checked independently. The manager limit is not enforced in the V10 strategy flow (strategies check their own limit).

4. **SSE endpoint**: The UI connects to `/trading/api/option-chain/stream-v10/{underlying}` which is defined in `app.py` (not in the files analyzed here).

---

## 18. MARCH 2026 FIXES APPLIED

### Memory Leaks & UI Freeze Fixes

| Fix | File | What Changed |
|-----|------|-------------|
| **`initial_state` unbounded growth** | `option_chain_v10_safe.py` | Added pruning of stale strikes not in `option_data`. Dict now stays bounded to current chain size. |
| **`calculate_max_pain` O(n^2) every tick** | `option_chain_v10_safe.py` | Added 5-second TTL cache + OI hash change detection. Filters low-OI strikes to reduce N. |
| **CSV reads on every UI refresh** | `trade_logger_v10.py` | `get_recent_trades()` now uses `deque(reader, maxlen=limit)` tail-read instead of loading entire CSV into memory. |
| **Duplicate deque initialization** | `option_chain_v10_safe.py` | Removed first block (lines 124-135). Single initialization remains. |
| **Duplicate `_select_best_strike`** | `option_chain_v10_safe.py` | Removed first duplicate definition. |
| **Race condition in `update_market_state`** | `option_chain_v10_safe.py` | All data now snapshotted atomically under `data_lock`. Deque appends happen outside lock with snapshot values. |
| **RAF stuck flag (browser tab hidden)** | `option_chain_v10.html` | Added 3-second watchdog timer to reset `rafPending` if RAF never fires. |
| **Orphaned SSE reconnection timers** | `option_chain_v10.html` | Track reconnect timer, cancel previous before scheduling new one. |
| **Orphaned server sessions** | `option_chain_v10.html` | Re-enabled `navigator.sendBeacon` session destroy on `beforeunload`. Clears heartbeat interval. |
| **innerHTML += DOM leak** | `option_chain_v10.html` | Gamma wall badge now uses `appendChild()` instead of `innerHTML +=` which re-parsed all children. |

### Strategy Logic Fixes

| Fix | File | What Changed |
|-----|------|-------------|
| **Entry score thresholds too high** | `v10_strategies.py` | Reduced: Safe 20->15, Aggressive 15->10, Sideways 35->25, QuantMax 50->35 |
| **Aggregation windows too long** | `v10_strategies.py` | Reduced: Safe 180s->90s, Aggressive 60s->45s, Sideways 120s->60s, QuantMax 300s->180s |
| **Confidence thresholds misaligned** | `v10_strategies.py` | Aggressive 55%->45% (appropriate for short window), QuantMax 70%->65% |
| **Regime lock 15 min** | `option_chain_v10_safe.py` | Reduced to 5 min. Added catastrophic reversal override (score > 70 unlocks immediately). |
| **Delta OI binary scoring** | `option_chain_v10_safe.py` | Graduated: 10-20% = score 10, 20-40% = score 20, 40%+ = score 30. Added min OI change filter. |
| **IV Skew 0.5pt threshold** | `option_chain_v10_safe.py` | Raised to 1.5pt minimum (was bid-ask noise). Added graduated scoring at 1.5 and 2.0 levels. |
| **Momentum 0.01% threshold** | `option_chain_v10_safe.py` | Window 10->30 ticks, threshold 0.01%->0.03%. Added acceleration (2nd derivative) scoring. |
| **GEX missing price component** | `option_chain_v10_safe.py` | Added `spot * 0.01` to formula. Fixed lot sizes per underlying. Graduated scoring. |
| **PCR ROC 15-sample window** | `option_chain_v10_safe.py` | Raised to 60 samples (60s). Added magnitude threshold (>1% change). Graduated scoring. |
| **SL/Target on option %** | `v10_strategies.py` | Now uses IV-based underlying move converted to option points via delta. Min SL ensures stops aren't tighter than spread. |
| **Signal flip at -15 (too aggressive)** | `v10_strategies.py` | Two-level exit: catastrophic flip (immediate, score crosses opposite entry threshold) + signal fade (aggregated, score goes neutral). |
| **Volatility calculation** | `option_chain_v10_safe.py` | Changed from range/price to annualized standard deviation of 1-second returns (comparable to IV). |
