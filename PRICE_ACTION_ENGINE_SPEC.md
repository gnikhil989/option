# Price Action Consensus Engine - Complete Specification

## Overview

A **fully independent** trading engine that does NOT depend on the Option Chain WebSocket data flow. It fetches its own candle data via REST API (OpenAlgo `/history` endpoint), computes Heikin Ashi + Supertrend on three instruments (Spot, ATM CE, ATM PE), and enters/exits trades based on **triple consensus**.

---

## Architecture: Why Separate?

```
EXISTING (Option Chain Engine)              NEW (Price Action Engine)
================================           ================================
WebSocket ticks → data_lock →              REST API polling → own thread →
  Option Chain Manager →                     PriceActionEngine →
  Greeks/OI/IV scoring →                     HA + Supertrend on 3 scripts →
  V10 Strategies →                           Consensus check →
  TradeLoggerV10                             PriceActionTradeLogger
```

- **No shared locks** — fetches data via HTTP, no `data_lock` contention
- **No WebSocket dependency** — works even if WS is down; uses REST `/history` API
- **Own polling loop** — runs on interval matching the candle timeframe (e.g., every 3 min)
- **Own trade logger** — separate CSV/Excel files, no interference with V10 logs

---

## Data Flow

### Step 1: Symbol Discovery (One-time on start)

**API**: `POST http://127.0.0.1:5000/api/v1/search`

```json
{
  "apikey": "<from .env OPENALGO_API_KEY>",
  "query": "RELIANCE",
  "exchange": "NFO"
}
```

**Purpose**: Get list of all available futures & options contracts for the symbol.

**Processing**:
1. User enters symbol (e.g., "RELIANCE") and selects exchange (NSE/NFO/BFO)
2. Call search API → get all option contracts
3. Parse response to identify available expiries and strikes
4. User selects expiry from dropdown (or auto-pick nearest)
5. Determine ATM strike from spot price (fetched via history API on NSE exchange)

### Step 2: Fetch Candle Data (Periodic - every candle interval)

**API**: `POST http://127.0.0.1:5000/api/v1/history`

```json
{
  "apikey": "<from .env>",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "interval": "3m",
  "start_date": "2026-04-01",
  "end_date": "2026-04-02",
  "source": "api"
}
```

**Three parallel calls** every candle interval:

| # | Symbol | Exchange | Example |
|---|--------|----------|---------|
| 1 | Spot/Underlying | NSE | `RELIANCE` |
| 2 | ATM CE Option | NFO | `RELIANCE03APR25760CE` |
| 3 | ATM PE Option | NFO | `RELIANCE03APR25760PE` |

**Note on ATM tracking**: When spot moves and ATM strike changes:
- Fetch new ATM CE/PE history from scratch (need full candle history for Supertrend warmup)
- Old HA/ST state is discarded for the changed strike
- Consensus resets — no trade until new ST stabilizes (warmup period)

**Intervals supported**: `1m`, `3m`, `5m`, `15m`, `30m`, `1h`

**Data range**: `start_date` = yesterday (for warmup), `end_date` = today

### Step 3: Compute Indicators

#### 3a. Heikin Ashi Candles

For each of the 3 instruments, convert OHLC to Heikin Ashi:

```
HA_Close = (Open + High + Low + Close) / 4
HA_Open  = (prev_HA_Open + prev_HA_Close) / 2     # First candle: (Open + Close) / 2
HA_High  = max(High, HA_Open, HA_Close)
HA_Low   = min(Low, HA_Open, HA_Close)
```

**HA candle color**:
- GREEN (bullish): HA_Close > HA_Open
- RED (bearish): HA_Close < HA_Open

#### 3b. Supertrend on Heikin Ashi

Compute Supertrend using HA candles (NOT regular candles):

**Parameters** (configurable from UI):
- `period`: 10 (ATR lookback, default)
- `multiplier`: 3.0 (ATR multiplier, default)

**Algorithm**:
```
ATR = EMA(TrueRange(HA_High, HA_Low, prev_HA_Close), period)

Upper_Band = (HA_High + HA_Low) / 2 + (multiplier * ATR)
Lower_Band = (HA_High + HA_Low) / 2 - (multiplier * ATR)

# Band continuity
if Upper_Band < prev_Upper_Band and prev_HA_Close > prev_Upper_Band:
    Upper_Band = prev_Upper_Band
if Lower_Band > prev_Lower_Band and prev_HA_Close < prev_Lower_Band:
    Lower_Band = prev_Lower_Band

# Direction
if prev_Supertrend == prev_Upper_Band:
    if HA_Close > Upper_Band:
        Supertrend = Lower_Band   # Flip to BUY
    else:
        Supertrend = Upper_Band   # Stay SELL
else:
    if HA_Close < Lower_Band:
        Supertrend = Upper_Band   # Flip to SELL
    else:
        Supertrend = Lower_Band   # Stay BUY

Signal:
  BUY  = HA_Close > Supertrend (price above lower band)
  SELL = HA_Close < Supertrend (price below upper band)
```

### Step 4: Consensus Decision

```
Spot_ST_Signal    = BUY or SELL
ATM_CE_ST_Signal  = BUY or SELL
ATM_PE_ST_Signal  = BUY or SELL (INVERTED for consensus — see below)
```

**Critical: PE signal inversion**

When market is bullish:
- Spot price goes UP → Spot ST = BUY
- CE price goes UP → CE ST = BUY
- PE price goes DOWN → PE ST = SELL

So for consensus, PE's signal must be **inverted**:
- PE ST = SELL means PE agrees with bullish direction → treat as BUY for consensus
- PE ST = BUY means PE disagrees → treat as SELL for consensus

**Consensus Matrix**:

| Spot ST | CE ST | PE ST (raw) | PE (inverted) | Consensus | Action |
|---------|-------|-------------|---------------|-----------|--------|
| BUY | BUY | SELL | BUY | ALL AGREE BULLISH | **BUY CALL** |
| SELL | SELL | BUY | SELL | ALL AGREE BEARISH | **BUY PUT** |
| Any mismatch | — | — | — | NO CONSENSUS | **NO TRADE / EXIT** |

### Step 5: Trade Entry/Exit Logic

**Entry Conditions** (ALL must be true):
1. Triple consensus achieved (all 3 agree)
2. No existing open trade (one trade at a time)
3. Current time >= user-configured start time
4. Current time < user-configured force-exit time
5. Warmup period passed (need at least `period` candles for valid Supertrend)
6. Optional: Daily trade limit not exceeded
7. Optional: Daily loss limit not exceeded

**Exit Conditions** (ANY triggers exit):
1. **Consensus broken** — any one of the 3 instruments flips → immediate exit
2. **Force exit time** — e.g., 3:20 PM for intraday → close all open trades
3. **Stop loss hit** — configurable % or points from entry
4. **Target hit** — configurable % or points from entry
5. **Manual exit** — user clicks exit button on UI

**Trade Details on Entry**:
- Contract: ATM CE (if BUY CALL) or ATM PE (if BUY PUT)
- Entry Price: LTP of the contract at consensus time (from latest candle close)
- Stop Loss: configurable (default 2% of entry price, or X points)
- Target: configurable (default 4% of entry price, or X points)

---

## Trade Logging

### CSV File Structure

**File**: `logs/DD-MM-YYYY/price_action_trades.csv`

**Columns**:
```
Trade_ID, Timestamp, Symbol, Expiry, Strike, Type (CE/PE), Direction (BUY/SELL),
Entry_Price, Exit_Price, Target, StopLoss,
PnL_Points, PnL_Pct, Quantity, PnL_Amount,
Entry_Time, Exit_Time, Duration_Seconds,
Exit_Reason (CONSENSUS_BROKEN / SL_HIT / TARGET_HIT / FORCE_EXIT / MANUAL),
Spot_ST_Direction, CE_ST_Direction, PE_ST_Direction,
Spot_Price_At_Entry, Spot_Price_At_Exit,
Timeframe, ST_Period, ST_Multiplier
```

### Excel Consolidation

**File**: `logs/price_action_tradebook_DD-MM-YYYY.xlsx`

Sheets:
1. **Trades** — all trades with full details
2. **Summary** — daily P&L, win rate, avg hold time, max drawdown
3. **Signal Log** — every consensus check with all 3 ST values (for backtesting review)

---

## File Structure (New Files to Create)

```
option-chain/
├── utils/
│   ├── price_action_engine.py      # Core engine: data fetch, HA, Supertrend, consensus
│   ├── price_action_logger.py      # Trade logging to CSV/Excel
│   └── price_action_config.py      # Default config, validation
├── config/
│   └── price_action_config.json    # User-editable config (ST params, risk, timers)
├── templates/
│   └── price_action.html           # New UI page
└── app.py                          # Add new routes (2 routes + 1 SSE endpoint)
```

---

## Module: `utils/price_action_engine.py`

### Class: `PriceActionEngine`

```python
class PriceActionEngine:
    def __init__(self, api_key, host, symbol, exchange, expiry, timeframe,
                 start_time, force_exit_time, st_period=10, st_multiplier=3.0,
                 sl_pct=2.0, target_pct=4.0, quantity=1, max_trades=10, daily_loss_limit=5000):
        # Config
        self.api_key = api_key
        self.host = host
        self.symbol = symbol          # e.g., "RELIANCE"
        self.exchange = exchange      # e.g., "NSE" for spot, "NFO" for options
        self.expiry = expiry          # e.g., "03APR25" (used to build option symbol)
        self.timeframe = timeframe    # e.g., "3m"
        self.start_time = start_time  # e.g., "09:20"
        self.force_exit_time = force_exit_time  # e.g., "15:20"

        # Supertrend params
        self.st_period = st_period
        self.st_multiplier = st_multiplier

        # Risk params
        self.sl_pct = sl_pct
        self.target_pct = target_pct
        self.quantity = quantity
        self.max_trades = max_trades
        self.daily_loss_limit = daily_loss_limit

        # State
        self.running = False
        self.current_trade = None
        self.atm_strike = None
        self.spot_data = {}           # {candles: [], ha: [], supertrend: []}
        self.ce_data = {}
        self.pe_data = {}
        self.trade_logger = PriceActionTradeLogger(symbol)
        self.lock = threading.RLock()
        self.engine_thread = None
        self.last_consensus = None    # For UI display

    # --- Public Methods ---

    def start(self):
        """Start the engine loop in a background thread"""
        self.running = True
        self.engine_thread = threading.Thread(target=self._engine_loop, daemon=True)
        self.engine_thread.start()

    def stop(self):
        """Stop engine and close any open trade"""
        self.running = False
        if self.current_trade:
            self._exit_trade("ENGINE_STOPPED")

    def get_state(self):
        """Return current state dict for UI (called by SSE endpoint)"""
        # Returns: spot_price, atm_strike, spot_st, ce_st, pe_st,
        #          consensus, current_trade, trade_history, ha_candles, etc.

    # --- Private Methods ---

    def _engine_loop(self):
        """Main loop - runs every candle interval"""
        self._initial_warmup()  # Fetch yesterday + today's data, compute indicators
        while self.running:
            sleep_until_next_candle()
            self._fetch_latest_candles()
            self._update_atm_if_needed()
            self._compute_indicators()
            self._check_consensus_and_trade()
            self._check_exit_conditions()
            self._check_force_exit_time()

    def _fetch_history(self, symbol, exchange, start_date, end_date):
        """Call /api/v1/history REST endpoint"""
        # Returns list of OHLC candles

    def _search_symbols(self, query, exchange):
        """Call /api/v1/search REST endpoint"""
        # Returns list of available contracts

    def _compute_heikin_ashi(self, ohlc_candles):
        """Convert OHLC candles to Heikin Ashi"""
        # Returns list of HA candles

    def _compute_supertrend(self, ha_candles, period, multiplier):
        """Compute Supertrend on HA candles"""
        # Returns list of {value, direction: 'BUY'/'SELL'} per candle

    def _determine_atm_strike(self, spot_price, strike_gap):
        """Round spot to nearest strike"""
        # e.g., spot=1260 with gap=20 → ATM=1260

    def _build_option_symbol(self, strike, option_type):
        """Build NFO symbol string"""
        # e.g., "RELIANCE03APR251260CE"

    def _check_consensus(self):
        """Check if all 3 Supertrends agree"""
        # Returns: ('BUY_CALL', confidence) or ('BUY_PUT', confidence) or ('NO_TRADE', 0)

    def _enter_trade(self, direction, entry_price, contract_symbol):
        """Log trade entry"""

    def _exit_trade(self, reason):
        """Log trade exit with P&L"""

    def _check_exit_conditions(self):
        """Check SL/Target/Consensus on every tick"""

    def _check_force_exit_time(self):
        """Close trade if past force exit time"""
```

---

## Module: `utils/price_action_logger.py`

### Class: `PriceActionTradeLogger`

```python
class PriceActionTradeLogger:
    def __init__(self, symbol):
        self.symbol = symbol
        self.log_dir = f"logs/{datetime.now().strftime('%d-%m-%Y')}"
        self.csv_file = f"{self.log_dir}/price_action_trades.csv"
        self.signal_log_file = f"{self.log_dir}/price_action_signals.csv"
        self.trades = []

    def log_entry(self, trade_id, symbol, expiry, strike, option_type, direction,
                  entry_price, target, stoploss, spot_price, spot_st, ce_st, pe_st,
                  timeframe, st_period, st_multiplier, quantity):
        """Write entry row to CSV"""

    def log_exit(self, trade_id, exit_price, exit_reason, spot_price,
                 spot_st, ce_st, pe_st):
        """Update CSV row with exit details, compute P&L"""

    def log_signal(self, timestamp, spot_st_dir, spot_st_val, ce_st_dir, ce_st_val,
                   pe_st_dir, pe_st_val, consensus, spot_price, ce_price, pe_price):
        """Log every consensus check to signal CSV (for backtesting review)"""

    def get_summary(self):
        """Return daily summary: total trades, wins, losses, net P&L, win rate"""

    def export_excel(self):
        """Consolidate CSVs into Excel workbook with Trades + Summary + Signals sheets"""
```

---

## Flask Routes (additions to `app.py`)

```python
# --- Price Action Engine Routes ---

@app.route('/price_action')
def price_action_page():
    """Render the Price Action trading UI"""
    return render_template('price_action.html')

@app.route('/api/price_action/search', methods=['POST'])
def price_action_search():
    """Proxy search to OpenAlgo and parse option contracts"""
    # Input: {symbol, exchange}
    # Calls OpenAlgo /api/v1/search
    # Returns: {expiries: [...], strikes: [...], contracts: [...]}

@app.route('/api/price_action/start', methods=['POST'])
def price_action_start():
    """Start the price action engine"""
    # Input: {symbol, exchange, expiry, timeframe, start_time, force_exit_time,
    #         st_period, st_multiplier, sl_pct, target_pct, quantity, max_trades}
    # Creates PriceActionEngine instance, calls .start()
    # Returns: {status: 'started'}

@app.route('/api/price_action/stop', methods=['POST'])
def price_action_stop():
    """Stop the engine, close open trades"""
    # Returns: {status: 'stopped', final_trade: {...}}

@app.route('/api/price_action/stream')
def price_action_stream():
    """SSE endpoint for real-time UI updates"""
    def generate():
        while True:
            state = engine.get_state()
            yield f"data: {json.dumps(state)}\n\n"
            time.sleep(1)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/price_action/manual_exit', methods=['POST'])
def price_action_manual_exit():
    """Manually exit current trade"""
    # Returns: {status: 'exited', trade: {...}}
```

---

## UI: `templates/price_action.html`

### Layout Sections

```
+------------------------------------------------------------------+
| NAVBAR (existing)                          [Price Action Engine]  |
+------------------------------------------------------------------+
|                                                                    |
| [1] CONFIGURATION PANEL (collapsible, shown on start)             |
| ┌──────────────────────────────────────────────────────────────┐  |
| │ Symbol: [___RELIANCE___] [Search]  Exchange: [NSE/NFO/BFO]  │  |
| │ Expiry: [dropdown - populated after search]                   │  |
| │ Timeframe: [1m | 3m | 5m | 15m | 30m]                       │  |
| │ Supertrend Period: [10]  Multiplier: [3.0]                    │  |
| │ Start Time: [09:20]  Force Exit: [15:20]                      │  |
| │ SL %: [2.0]  Target %: [4.0]  Qty: [1]                       │  |
| │ Max Trades/Day: [10]  Daily Loss Limit: [5000]                │  |
| │                                                                │  |
| │              [START ENGINE]  [STOP ENGINE]                     │  |
| └──────────────────────────────────────────────────────────────┘  |
|                                                                    |
| [2] STATUS BAR                                                     |
| ┌──────────────────────────────────────────────────────────────┐  |
| │ Status: RUNNING | Spot: 1262.50 | ATM: 1260 | Timeframe: 3m │  |
| │ Next candle in: 45s | Candles processed: 312                  │  |
| └──────────────────────────────────────────────────────────────┘  |
|                                                                    |
| [3] TRIPLE CONSENSUS PANEL (main visual)                           |
| ┌──────────────┬──────────────┬──────────────┬────────────────┐  |
| │   SPOT       │   ATM CE     │   ATM PE     │  CONSENSUS     │  |
| │   RELIANCE   │  1260CE      │  1260PE      │                │  |
| │              │              │              │                │  |
| │  ST: BUY ▲   │  ST: BUY ▲   │  ST: SELL ▼  │  ✅ BUY CALL  │  |
| │  HA: GREEN   │  HA: GREEN   │  HA: RED     │  (ALL AGREE)   │  |
| │  LTP: 1262   │  LTP: 45.30  │  LTP: 22.10  │               │  |
| │  ST Val: 1248 │  ST Val: 39  │  ST Val: 28  │               │  |
| └──────────────┴──────────────┴──────────────┴────────────────┘  |
|                                                                    |
| [4] CURRENT TRADE (if active)                                      |
| ┌──────────────────────────────────────────────────────────────┐  |
| │ OPEN: BUY CALL | RELIANCE 1260CE @ 45.30                     │  |
| │ Current: 47.80 | P&L: +2.50 (+5.5%) | SL: 44.39 | T: 47.12 │  |
| │ Duration: 6m 30s | Entry: 10:15:00                            │  |
| │                                    [MANUAL EXIT]              │  |
| └──────────────────────────────────────────────────────────────┘  |
|                                                                    |
| [5] TRADE LOG TABLE                                                |
| ┌────┬──────┬────────┬───────┬──────┬────────┬────────┬────────┐ |
| │ #  │ Time │Contract│ Entry │ Exit │  P&L   │  Dur   │ Reason │ |
| ├────┼──────┼────────┼───────┼──────┼────────┼────────┼────────┤ |
| │ 1  │10:15 │1260CE  │ 45.30 │47.80 │ +2.50  │ 6m 30s │TARGET  │ |
| │ 2  │11:00 │1260PE  │ 22.10 │20.50 │ +1.60  │ 3m 00s │CONSNS  │ |
| └────┴──────┴────────┴───────┴──────┴────────┴────────┴────────┘ |
|                                                                    |
| [6] DAILY SUMMARY                                                  |
| ┌──────────────────────────────────────────────────────────────┐  |
| │ Trades: 5 | Wins: 3 | Losses: 2 | Win Rate: 60%              │  |
| │ Net P&L: +4.30 pts (₹2,150) | Max Drawdown: -1.80 pts       │  |
| └──────────────────────────────────────────────────────────────┘  |
|                                                                    |
+------------------------------------------------------------------+
```

### UI Behavior

1. **On page load**: Show configuration panel, everything else hidden
2. **On Search click**: Call `/api/price_action/search` → populate expiry dropdown and show available strikes
3. **On Start click**: 
   - Validate all fields
   - Call `/api/price_action/start` with all params
   - Collapse config panel
   - Start SSE connection to `/api/price_action/stream`
   - Show status bar, consensus panel, trade sections
4. **SSE updates (every 1s)**: Update all live values — spot price, ST directions, HA colors, current trade P&L, countdown to next candle
5. **On Stop click**: Call `/api/price_action/stop`, show final summary
6. **On Manual Exit**: Call `/api/price_action/manual_exit`, update trade log

### UI Tech Stack
- Same as existing: Tailwind CSS + DaisyUI (via `compiled.css`)
- Extends `layout.html` → `base.html`
- No additional JS libraries needed (vanilla JS + SSE)

---

## Config File: `config/price_action_config.json`

```json
{
  "supertrend": {
    "period": 10,
    "multiplier": 3.0
  },
  "timeframe": "3m",
  "risk": {
    "sl_pct": 2.0,
    "target_pct": 4.0,
    "quantity": 1,
    "max_trades_per_day": 10,
    "daily_loss_limit": 5000
  },
  "timers": {
    "start_time": "09:20",
    "force_exit_time": "15:20"
  },
  "consensus": {
    "invert_pe": true,
    "require_all_three": true,
    "warmup_candles": 15
  },
  "symbols": {
    "default_symbol": "RELIANCE",
    "default_exchange": "NSE",
    "option_exchange": "NFO",
    "strike_gap": {
      "RELIANCE": 20,
      "NIFTY": 50,
      "BANKNIFTY": 100,
      "HDFCBANK": 20,
      "SBIN": 5,
      "INFY": 20,
      "BHARTIARTL": 20,
      "ICICIBANK": 20,
      "SENSEX": 100
    }
  }
}
```

---

## API Call Examples (for reference during implementation)

### 1. Search for option contracts
```
POST http://127.0.0.1:5000/api/v1/search
{
  "apikey": "2ca06c6ce6873ab27c6b771f86fbd03a854c0240926e266867434343eb309707",
  "query": "RELIANCE",
  "exchange": "NFO"
}
```
Response: List of contracts like `RELIANCE03APR251260CE`, `RELIANCE03APR251260PE`, etc.

### 2. Fetch spot candles
```
POST http://127.0.0.1:5000/api/v1/history
{
  "apikey": "2ca06c6ce6873ab27c6b771f86fbd03a854c0240926e266867434343eb309707",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "interval": "3m",
  "start_date": "2026-04-01",
  "end_date": "2026-04-02",
  "source": "api"
}
```

### 3. Fetch ATM CE candles
```
POST http://127.0.0.1:5000/api/v1/history
{
  "apikey": "...",
  "symbol": "RELIANCE03APR251260CE",
  "exchange": "NFO",
  "interval": "3m",
  "start_date": "2026-04-01",
  "end_date": "2026-04-02",
  "source": "api"
}
```

### 4. Fetch ATM PE candles
Same as above but with `RELIANCE03APR251260PE`.

---

## Edge Cases & Handling

| Scenario | Handling |
|----------|----------|
| ATM strike changes mid-trade | Do NOT change contract mid-trade. Only update ATM for next trade entry. |
| ATM changes when no trade | Fetch new CE/PE history, recompute ST from scratch, wait for warmup. |
| API returns error/timeout | Retry once after 2s. If still fails, skip this candle cycle, log warning. |
| Market closed (no new candles) | Engine sleeps until next candle; no false signals from stale data. |
| Less candles than ST period | Mark as "WARMING UP" in UI, no trades allowed until enough candles. |
| All 3 agree but then immediately disagree | Exit on the very next candle check (consensus broken). |
| Force exit time reached with open trade | Close at market price, log reason as "FORCE_EXIT". |
| Daily loss limit reached | Stop entering new trades, allow existing trade to play out with SL/Target. |
| Option illiquidity (wide spread) | Log warning in UI; optionally skip entry if spread > configurable threshold. |

---

## Implementation Order

1. **`utils/price_action_engine.py`** — Core engine with data fetching, HA, Supertrend, consensus logic
2. **`utils/price_action_logger.py`** — CSV/Excel trade logging
3. **`config/price_action_config.json`** — Default configuration
4. **`templates/price_action.html`** — Full UI with all 6 sections
5. **`app.py` additions** — Routes for search, start, stop, stream, manual exit
6. **Testing** — Verify with RELIANCE on 3m timeframe, check HA and ST calculations against known values

---

## Key Design Decisions

1. **REST polling over WebSocket** — Simpler, no lock contention, sufficient for 3m+ candles
2. **PE signal inversion** — Essential for correct consensus (PE moves opposite to spot)
3. **One trade at a time** — Keeps risk simple; no overlapping positions
4. **Warmup from yesterday's data** — Supertrend needs ~15 candles history to stabilize
5. **ATM re-determination only between trades** — Prevents mid-trade contract switching chaos
6. **Separate log files** — No interference with V10 option chain trade logs
7. **All config from UI** — No need to edit JSON files; UI sends all params on start
