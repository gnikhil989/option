"""
Option Chain Manager v10 - SAFE ADAPTIVE GUARDIAN
Autonomous, Committed, and Safe-First Trading System.

KEY DESIGN PRINCIPLES:
1. AUTONOMY: Runs in a dedicated background thread.
2. CALM: Signal smoothing (60s) to kill noise.
3. COMMITMENT: Once in a trade, ignores entry signals.
4. INERTIA: Regime locking (15m minimum) to prevent whipsaw.
5. SAFETY: VWAP & Volatility filters.
"""

import threading
import time
import json
import logging
from datetime import datetime, timedelta
from collections import deque, Counter
import statistics
import os
import uuid
import re
import pytz
from cachetools import TTLCache
from typing import Dict, List, Optional, Any

# V10 Specific Imports (no legacy dependencies)
from utils.trade_logger_v10 import TradeLoggerv10
from utils.daily_trade_book import DailyTradeBook
from utils.v10_strategies import (
    V10SafeStrategy,
    V10AggressiveStrategy,
    V10SidewaysStrategy,
    V10QuantMaxStrategy,
    V10ORBStrategy,
    V10OIWallStrategy,
    V10ExpiryMagnetStrategy,
    V10SpotFuturesStrategy,
    V10ThetaSellStrategy,
    V10IVCrushSellStrategy,
    V10AdaptiveStrategy,
    V10DTEAdaptiveStrategy,
)

logger = logging.getLogger(__name__)

class OptionChainCacheV10:
    """Zero-config cache for option chain data"""

    def __init__(self, maxsize=100, ttl=45):
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.cache.get(key)

    def set(self, key, value):
        with self.lock:
            self.cache[key] = value

class RegimeGuardian:
    """Enforces Inertia: Prevents rapid regime switching.

    Supports catastrophic reversal override: if score magnitude > 70 and
    opposes current regime, regime unlocks immediately to avoid being
    trapped during sharp reversals (e.g. flash crashes).

    Lock duration now scales with strategy timeframe via constructor param.
    """
    def __init__(self, lock_duration=300): # 5 minutes default (was 15m - too long for intraday)
        self.current_regime = 'NEUTRAL'
        self.lock_duration = lock_duration
        self.last_change_time = 0
        self.locked = False

    def update(self, detected_regime, confidence, score=0):
        now = time.time()

        if self.locked:
            time_in_lock = now - self.last_change_time

            # CATASTROPHIC REVERSAL OVERRIDE: unlock immediately on extreme opposite signal
            if abs(score) > 70:
                is_opposite = (
                    (self.current_regime == 'BULLISH' and detected_regime == 'BEARISH') or
                    (self.current_regime == 'BEARISH' and detected_regime == 'BULLISH')
                )
                if is_opposite:
                    logger.warning(f"[GUARDIAN] CATASTROPHIC REVERSAL: {self.current_regime} -> {detected_regime} (score={score}, unlocking early)")
                    self.current_regime = detected_regime
                    self.last_change_time = now
                    return self.current_regime

            if time_in_lock < self.lock_duration:
                return self.current_regime

            # Lock expired
            self.locked = False

        if detected_regime != self.current_regime:
            if confidence > 60:
                logger.info(f"[GUARDIAN] Regime Shift: {self.current_regime} -> {detected_regime} (Locked for {self.lock_duration/60:.1f}m)")
                self.current_regime = detected_regime
                self.last_change_time = now
                self.locked = True

        return self.current_regime

class OptionChainManagerV10Safe:
    """
    v10 SAFE Manager - The Autonomous Guardian
    Standalone implementation (Decoupled from V7).
    """
    
    def __init__(self, underlying, expiry, websocket_manager=None):
        """Initialize ADAPTIVE OptionChainManager V10"""
        self.underlying = underlying
        self.expiry = expiry

        # Strike step configuration
        if underlying == 'NIFTY':
            self.strike_step = 100
        elif underlying in ['BANKNIFTY', 'SENSEX']:
            self.strike_step = 100
        else:
            self.strike_step = 10

        self.option_data = {}
        self.subscription_map = {}
        self.underlying_ltp = 0
        self.underlying_bid = 0
        self.underlying_ask = 0
        self.underlying_open = 0
        self.underlying_high = 0
        self.underlying_low = 0
        self.underlying_close = 0
        self.underlying_avg = 0
        self.atm_strike = 0
        self.websocket_manager = websocket_manager
        self.cache = OptionChainCacheV10()
        self.data_lock = threading.Lock() # Protects option_data and histories
        self.monitoring_active = False
        self.initialized = False
        self.manager_id = f"{underlying}_{expiry}_v10_SAFE"
        self.option_mode = 'quote'

        # ADAPTIVE MODE
        self.strategy_mode = 'ADAPTIVE'
        self.current_regime = 'DETECTING'
        self.regime_confidence = 0

        # V10 Specific Components
        # 1. AUTONOMY ENGINE
        self.engine_running = False
        self.engine_thread = None
        self._state_underlying = underlying
        self.state_file = self._get_dated_state_file()

        # 2. CALM LOGIC
        self.logger = TradeLoggerv10() # Main logger for UI/Legacy compatibility
        self.guardian = RegimeGuardian(lock_duration=300) # 5 mins (was 15m - too long for intraday)

        # DAILY TRADE BOOK: Single Excel per day with all strategy sheets
        self.trade_book = DailyTradeBook(log_dir="logs")

        # 3. STRATEGY HUB (Multi-Method)
        self.strategies = [
            # BUY-ONLY (7 strategies)
            V10SafeStrategy(),          # Trend follower
            V10AggressiveStrategy(),     # Momentum scalper
            V10SidewaysStrategy(),       # Mean reversion at extremes
            V10QuantMaxStrategy(),       # High conviction swing
            V10ORBStrategy(),            # Opening range breakout
            V10OIWallStrategy(),         # OI wall bounce/breakout
            V10ExpiryMagnetStrategy(),   # Expiry day max pain magnet
            # SELL-ONLY (2 strategies)
            V10ThetaSellStrategy(),      # Theta collector in sideways markets
            V10IVCrushSellStrategy(),    # IV crush after high-IV periods
            # ADAPTIVE - BUY or SELL (1 strategy)
            V10AdaptiveStrategy(),       # Decides BUY/SELL based on regime, GEX, IV, OI
            # SPOT/FUTURES (1 strategy)
            V10SpotFuturesStrategy(),    # Underlying price paper trade
        ]
        
        # MARKET DATA
        self.underlying_ltp = 0
        self.underlying_open = 0.0
        self.underlying_high = 0.0
        self.underlying_low = 0.0
        self.underlying_close = 0.0
        self.underlying_avg = 0.0
        self.underlying_bid = 0.0
        self.underlying_ask = 0.0
        self.atm_strike = None
        self.market_metrics = {}

        # QUANT ENGINE STATE
        self.initial_state = {}
        self._initial_state_max_strikes = 50  # Cap to prevent unbounded growth
        self.start_price = None
        self.days_to_expiry = 0
        self.price_history = deque(maxlen=600)
        self.atm_ce_history = deque(maxlen=600)
        self.atm_pe_history = deque(maxlen=600)
        self.pcr_history = deque(maxlen=600)
        self.oi_history = deque(maxlen=600)
        self.ce_oi_history = deque(maxlen=600)  # Separate CE OI for pattern detection
        self.pe_oi_history = deque(maxlen=600)  # Separate PE OI for pattern detection
        self.oi_pattern = 'DETECTING'            # Current OI pattern name
        self.oi_pattern_strength = 0             # How strong the pattern is (0-100)
        self.vol_history = deque(maxlen=600)
        self.iv_history = deque(maxlen=120)
        self.gex_history = deque(maxlen=120)
        self.vanna_history = deque(maxlen=120)
        self.charm_history = deque(maxlen=120)
        self.volatility_history = deque(maxlen=120)
        self.action_history = deque(maxlen=10)
        self._backoff_until = 0

        # Greek Monitor State
        self._refresh_cycle = 0
        self._bg_greek_idx = 3

        # Max Pain Cache (avoid O(n^2) every tick)
        self._max_pain_cache = 0
        self._max_pain_cache_time = 0
        self._max_pain_cache_ttl = 5  # Recalc only every 5 seconds
        self._last_oi_hash = 0  # Track OI changes to skip redundant recalc

        # SAFETY FILTERS
        self.daily_loss_limit = -2.0
        self.max_trades_per_day = 5
        self.trades_today = 0
        self.daily_pnl_points = 0
        self.safety_lock = False

        # STARTUP WARMUP
        self.startup_time = time.time()
        self.min_warmup_seconds = 60

        # EXIT COOLDOWN
        self.exit_cooldown_until = 0
        self.exit_cooldown_seconds = 60

        # TRADE STATE (Persistent)
        self.active_trade = None
        
        # Signal reliability weights
        self.signal_weights = {
            'delta_oi': 1.5,
            'oi_unwind': 1.3,
            'pcr_roc': 1.2,
            'inst_flow': 1.2,
            'vwap': 1.3,          # NEW: VWAP is key institutional reference
            'oi_wall': 1.2,       # NEW: OI wall proximity
            'pcr_absolute': 0.9,  # NEW: Sentiment extreme detection
            'gamma': 1.0,
            'momentum': 1.0,
            'max_pain': 0.8,
            'iv_skew': 0.7,
            'vanna': 0.6,
        }

        # VWAP state
        self._vwap_numerator = 0.0
        self._vwap_denominator = 0
        self.vwap = 0.0

        # OI Wall state (updated each tick)
        self.oi_wall_ce_strike = 0  # Highest CE OI strike (resistance)
        self.oi_wall_pe_strike = 0  # Highest PE OI strike (support)
        self.oi_wall_ce_oi = 0
        self.oi_wall_pe_oi = 0

        # Opening Range state (for ORB strategy)
        self.opening_range_high = 0.0
        self.opening_range_low = 0.0
        self.opening_range_locked = False
        self._orb_lock_time = 0

        # ATM resubscription state
        self._chain_atm = None
        self._atm_mismatch_since = 0
        self._regeneration_needed = False
        self._last_resub_time = 0

        # MARKET REGIME DETECTOR
        # Identifies TRENDING vs SIDEWAYS/CHOPPY to throttle trading
        self.market_regime_type = 'DETECTING'  # TRENDING, SIDEWAYS, CHOPPY
        self._score_sign_history = deque(maxlen=1800)    # +1/-1 per tick for 30 min
        self._vwap_cross_history = deque(maxlen=1800)    # 1=crossed, 0=not
        self._regime_last_update = 0
        
        self.load_state()
        logger.info(f"Initialized v10 SAFE Guardian for {underlying}")

    # ============================================================================
    # V10 ENGINE & STATE MANAGEMENT
    # ============================================================================

    def start_monitoring(self):
        """Start Background Engine and Greek Monitor"""
        if self.monitoring_active:
            return
        
        # Start Greek Monitor (V7 Logic)
        self.monitoring_active = True
        self.greek_thread = threading.Thread(target=self._greek_monitor_loop)
        self.greek_thread.daemon = True
        self.greek_thread.start()
        
        # Start V10 Engine
        self.start_engine()
        
    def start_engine(self):
        if self.engine_running:
            return

        # Close any stale trades from previous session before starting
        self.close_stale_trades_on_startup()

        self.engine_running = True
        self.engine_thread = threading.Thread(target=self._trade_engine_loop)
        self.engine_thread.daemon = True
        self.engine_thread.start()
        print(f"\n{'='*60}\n[v10 SAFETY GUARDIAN] ENGINE STARTED for {self.underlying}\n{'='*60}\n")
        logger.info(f"[v10 ENGINE] Background Trading Engine Started for {self.underlying}")

    def stop_monitoring(self):
        """Stop background threads"""
        # Stop Greek Monitor
        if self.monitoring_active:
            logger.info(f"Stopping monitor for {self.underlying}")
            self.monitoring_active = False
            if self.websocket_manager:
                try:
                    self.websocket_manager.unregister_handler('quote', self.handle_quote_update)
                except Exception:
                    pass
        
        # Stop V10 Engine
        self.engine_running = False
        
    def stop(self):
        """Alias for stop_monitoring"""
        self.stop_monitoring()

    def close_all_open_trades(self, reason="SYSTEM SHUTDOWN"):
        """Close all active trades across all strategies before shutdown/startup.
        Logs EXIT to CSV with current LTP or last known price.
        """
        closed_count = 0
        for strategy in self.strategies:
            trade = strategy.active_trade
            if not trade or trade.get('_closed', False):
                continue

            # Get current LTP for the trade
            contract_type = trade.get('type', '')
            strike = trade.get('strike', 0)
            direction = trade.get('direction', 'BUY')
            exit_price = 0

            if direction in ('LONG', 'SHORT'):
                # Spot/Futures trade - use underlying LTP
                exit_price = self.underlying_ltp if self.underlying_ltp > 0 else trade.get('entry_price', 0)
            elif strike and strike in self.option_data:
                # Option trade - get option LTP
                key = 'ce_data' if contract_type == 'CE' else 'pe_data'
                exit_price = self.option_data[strike][key].get('ltp', 0)

            if exit_price <= 0:
                exit_price = trade.get('Max_Price_Seen', trade.get('entry_price', 0))

            # Build minimal metrics for close
            metrics = {
                'raw_score': 0,
                'pcr': self.pcr_history[-1] if self.pcr_history else 0,
                'ltp': self.underlying_ltp,
                'oi_pattern': '',
                'oi_pattern_strength': 0,
            }

            try:
                if direction == 'SELL':
                    strategy._close_sell_trade(self, exit_price, reason, metrics)
                elif direction in ('LONG', 'SHORT'):
                    strategy._close_spot_trade(self, exit_price, reason, metrics)
                else:
                    strategy._close_trade(self, exit_price, reason, metrics)

                closed_count += 1
                logger.info(f"[SHUTDOWN] Closed {strategy.name} trade {trade.get('Trade_ID')} @ {exit_price:.2f}")
            except Exception as e:
                logger.error(f"[SHUTDOWN] Error closing {strategy.name} trade: {e}")
                # Force clear even if close fails
                strategy.active_trade = None

        if closed_count > 0:
            self.save_state()
            logger.warning(f"[SHUTDOWN] Closed {closed_count} open trades. Reason: {reason}")
            print(f"\n{'='*60}\n[SHUTDOWN] Closed {closed_count} open trades ({reason})\n{'='*60}\n")
        else:
            logger.info("[SHUTDOWN] No open trades to close.")

        return closed_count

    def close_stale_trades_on_startup(self):
        """Called on startup: close any trades that were left open from previous session.
        These are stale because market conditions have changed since last run.
        """
        has_open = any(s.active_trade and not s.active_trade.get('_closed', False)
                       for s in self.strategies)
        if has_open:
            logger.warning("[STARTUP] Found open trades from previous session. Closing them.")
            print(f"\n{'='*60}\n[STARTUP] Found stale trades from previous session. Closing...\n{'='*60}\n")
            self.close_all_open_trades(reason="STALE TRADE (previous session)")
        
    def _greek_monitor_loop(self):
        """Background loop to refresh Greeks"""
        logger.info(f"Starting Smart Greek Monitor for {self.underlying}")
        while self.monitoring_active:
            try:
                # logger.info(f"Greek Monitor Check... {self.monitoring_active}")
                self.refresh_greeks()
                
                # Sleep with check
                for _ in range(8): # 4 seconds approx (0.5s * 8)
                    if not self.monitoring_active: return
                    time.sleep(0.5) 
            except Exception as e:
                logger.error(f"[{self.underlying}] Error in Greek monitoring loop: {e}")
                time.sleep(5)

    def refresh_greeks(self):
        """
        Smart Refresh Strategy for Greeks with Strict Batching
        """
        if self._backoff_until > time.time():
            return
        if not self.atm_strike:
            logger.warning("[GREEKS] No ATM Strike yet")
            return

        all_strikes = sorted(self.option_data.keys())
        
        # 1. Calculate Priority List (Distance from ATM)
        try:
             sorted_strikes = sorted(all_strikes, key=lambda s: abs(s - self.atm_strike))
        except ValueError:
            return

        # 2. Select Strikes to Update (Batch Size = 2)
        # Slot 1: ATM (Always)
        final_batch = []
        if len(sorted_strikes) > 0:
            final_batch.append(sorted_strikes[0])
            
        # Slot 2: Weighted Rotation to prioritize neighbors
        if len(sorted_strikes) > 1:
            if not hasattr(self, '_refresh_cycle'): self._refresh_cycle = 0
            if not hasattr(self, '_bg_greek_idx'): self._bg_greek_idx = 3 
            
            cycle_mode = self._refresh_cycle % 3
            target_strike = None
            
            if cycle_mode == 0:
                # Neighbor 1 (ATM+1 or closest) - Index 1
                target_strike = sorted_strikes[1]
            elif cycle_mode == 1 and len(sorted_strikes) > 2:
                # Neighbor 2 (ATM-1 or 2nd closest) - Index 2
                target_strike = sorted_strikes[2]
            else:
                # Background (Index 3+)
                if len(sorted_strikes) > 3:
                     # Rotate through the rest
                     bg_list = sorted_strikes[3:]
                     self._bg_greek_idx = (self._bg_greek_idx + 1) % len(bg_list)
                     target_strike = bg_list[self._bg_greek_idx]
                else:
                    target_strike = sorted_strikes[1]

            if target_strike and target_strike not in final_batch:
                final_batch.append(target_strike)
            
            self._refresh_cycle += 1

        # 3. Execute Batch
        if final_batch:
            logger.info(f"[GREEKS] Updating batch: {final_batch}")
            self._update_greeks_batch(final_batch)

    def _update_greeks_batch(self, strikes_list):
        """
        Fetch Greeks for multiple strikes in a single batch call.
        """
        if not strikes_list:
            return

        try:
            # 1. Prepare Request Payload
            symbols_to_fetch = []
            symbol_map = {} # symbol -> {strike, type}

            for strike in strikes_list:
                if strike not in self.option_data: continue
                
                # Add CE
                ce_sym = self.option_data[strike]['ce_symbol']
                symbols_to_fetch.append({"symbol": ce_sym, "exchange": "NFO"})
                symbol_map[ce_sym] = {'strike': strike, 'type': 'CE'}

                # Add PE
                pe_sym = self.option_data[strike]['pe_symbol']
                symbols_to_fetch.append({"symbol": pe_sym, "exchange": "NFO"})
                symbol_map[pe_sym] = {'strike': strike, 'type': 'PE'}

            if not symbols_to_fetch:
                return

            # 2. Execute Batch Call
            # logger.info(f"Fetching batch greeks for {len(symbols_to_fetch)} symbols")
            if hasattr(self, 'api_client') and self.api_client:
                 response = self.api_client.multioptiongreeks(symbols_list=symbols_to_fetch)
                 logger.info(f"[GREEKS DEBUG] Batch Response Status: {response.get('status')}")
            else:
                 logger.error("No API Client available for Greeks")
                 return


            # 3. Process Response
            if response and response.get('status') in ['success', 'partial']:
                data_list = response.get('data', [])
                if not data_list:
                     logger.warning("Greek Response empty data")
                     return

                # Bug #4 fix: Use data_lock for thread-safe greek updates
                with self.data_lock:
                    for item in data_list:
                        if item.get('status') == 'error': continue

                        symbol = item.get('symbol')
                        if symbol not in symbol_map: continue

                        details = symbol_map[symbol]
                        strike = details['strike']
                        option_type = details['type'] # CE or PE

                        if strike not in self.option_data: continue
                        target_dict = self.option_data[strike]['ce_data'] if option_type == 'CE' else self.option_data[strike]['pe_data']

                        greeks = item.get('greeks', {})
                        if not greeks:
                             # logger.debug(f"No greeks found for {symbol}")
                             continue

                        target_dict.update({
                            'delta': float(greeks.get('delta', 0) or 0),
                            'gamma': float(greeks.get('gamma', 0) or 0),
                            'theta': float(greeks.get('theta', 0) or 0),
                            'vega': float(greeks.get('vega', 0) or 0),
                            'rho': float(greeks.get('rho', 0) or 0),
                            'iv': float(item.get('implied_volatility', 0) or 0),
                            'gex': float(greeks.get('gamma', 0) or 0) * self.underlying_ltp * target_dict.get('oi', 0) * 100 * 0.01 # Approx GEX
                        })

                        # Capture DTE
                        if 'days_to_expiry' in item:
                            self.days_to_expiry = float(item.get('days_to_expiry', 0))

            elif response and response.get('code') == 429:
                logger.warning(f"[{self.underlying}] Batch Rate Limit (429). Backing off.")
                self._backoff_until = time.time() + 60

        except Exception as e:
            logger.error(f"Error in batch greeks update: {e}")

    def _trade_engine_loop(self):
        """
        The Heartbeat of v10. Runs every 1 second.
        Independent of UI.
        """
        logger.info("[v10 ENGINE] Loop active...")
        while self.engine_running:
            try:
                # 0. Bug #2 fix: Check for daily reset at market open
                self._check_daily_reset()

                # 1. Ensure Data is Fresh
                if not self.option_data or self.atm_strike == 0:
                    if int(time.time()) % 5 == 0:
                        print(f"⚠️ [v10 GUARDIAN] Waiting for Option Data... (LTP: {self.underlying_ltp})")
                    time.sleep(1)
                    continue

                # 2. DAILY SAFETY CHECKS: PnL limit + Total trade count limit
                # 2. DAILY SAFETY CHECKS - DISABLED FOR TESTING
                # TODO: Re-enable for live trading with:
                #   PnL limit: -50 points
                #   Trade limit: 20 trades
                # if not self.safety_lock:
                #     total_daily_pnl = sum(s.daily_pnl for s in self.strategies)
                #     total_daily_trades = sum(s.trades_today for s in self.strategies)
                #     if total_daily_pnl < -50:
                #         self.safety_lock = True
                #     elif total_daily_trades >= 20:
                #         self.safety_lock = True

                # 3. Calculate Metrics (Internal, no UI needed)
                metrics = self.update_market_state()

                # 3b. Detect market regime (TRENDING/SIDEWAYS/CHOPPY)
                self._detect_market_regime(metrics)
                metrics['market_regime_type'] = self.market_regime_type

                # 4. Run Strategy Hub
                for strategy in self.strategies:
                    try:
                        if False:  # Safety lock disabled for testing (was: self.safety_lock and not strategy.active_trade)
                            continue  # Skip strategies with no active trade when locked
                        strategy.process_signals(self, metrics)
                    except Exception as strat_e:
                        logger.error(f"Strategy {strategy.name} failed: {strat_e}")
                
                # 5. Persist State
                if int(time.time()) % 10 == 0: # Save every 10s
                    self.save_state()

                # 6. Consolidate Daily Excel (every 60s)
                self.trade_book.consolidate(self.strategies)

                time.sleep(1) # 1Hz heartbeat
                
            except Exception as e:
                logger.error(f"[v10 ENGINE] Error in loop: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5) # Backoff on error

    def update_market_state(self):
        """
        Updates internal market history and calculates signals.
        Called by Engine Loop (Writer).
        CRITICAL: Hold data_lock only briefly for snapshot, compute outside lock.
        """
        # Snapshot ALL data under lock (atomic read), then process outside
        with self.data_lock:
            ltp = self.underlying_ltp
            atm = self.atm_strike
            atm_data_snapshot = None
            if atm and atm in self.option_data:
                atm_entry = self.option_data[atm]
                atm_data_snapshot = {
                    'ce_ltp': atm_entry['ce_data'].get('ltp', 0),
                    'pe_ltp': atm_entry['pe_data'].get('ltp', 0),
                    'ce_iv': atm_entry['ce_data'].get('iv', 0),
                    'pe_iv': atm_entry['pe_data'].get('iv', 0),
                }

            total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
            total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
            total_oi = total_ce_oi + total_pe_oi

            # OI WALL DETECTION: find highest CE OI and PE OI strikes
            max_ce_oi, max_pe_oi = 0, 0
            wall_ce_strike, wall_pe_strike = 0, 0
            for strike, data in self.option_data.items():
                ce_oi = data['ce_data'].get('oi', 0)
                pe_oi = data['pe_data'].get('oi', 0)
                if ce_oi > max_ce_oi:
                    max_ce_oi = ce_oi
                    wall_ce_strike = strike
                if pe_oi > max_pe_oi:
                    max_pe_oi = pe_oi
                    wall_pe_strike = strike

            # Snapshot total volume for VWAP
            total_volume = sum(
                opt['ce_data'].get('volume', 0) + opt['pe_data'].get('volume', 0)
                for opt in self.option_data.values()
            )

            self._capture_initial_state()

        # Update OI walls (outside lock)
        self.oi_wall_ce_strike = wall_ce_strike
        self.oi_wall_pe_strike = wall_pe_strike
        self.oi_wall_ce_oi = max_ce_oi
        self.oi_wall_pe_oi = max_pe_oi

        # Update histories OUTSIDE lock
        if ltp > 0:
            self.price_history.append(ltp)

            # VWAP UPDATE: cumulative (price * volume) / cumulative volume
            tick_volume = max(total_volume - getattr(self, '_prev_total_volume', 0), 1)
            self._prev_total_volume = total_volume
            self._vwap_numerator += ltp * tick_volume
            self._vwap_denominator += tick_volume
            if self._vwap_denominator > 0:
                self.vwap = self._vwap_numerator / self._vwap_denominator

        if atm_data_snapshot:
            if atm_data_snapshot['ce_ltp'] > 0:
                self.atm_ce_history.append(atm_data_snapshot['ce_ltp'])
            if atm_data_snapshot['pe_ltp'] > 0:
                self.atm_pe_history.append(atm_data_snapshot['pe_ltp'])
            iv = (atm_data_snapshot['ce_iv'] + atm_data_snapshot['pe_iv']) / 2
            if iv > 0:
                self.iv_history.append(iv)

        if total_oi > 0: self.oi_history.append(total_oi)
        if total_ce_oi > 0:
            self.ce_oi_history.append(total_ce_oi)
            self.pcr_history.append(total_pe_oi / total_ce_oi)
        if total_pe_oi > 0:
            self.pe_oi_history.append(total_pe_oi)

        # OPENING RANGE CAPTURE (for ORB strategy)
        self._update_opening_range(ltp)

        # Heavy computation OUTSIDE lock
        pcr = self.pcr_history[-1] if self.pcr_history else 1.0
        max_pain = self.calculate_max_pain()

        # Generate full signal packet
        signals = self.generate_signals(pcr, max_pain)

        # Store for UI Reading (atomic assignment, no lock needed)
        self.latest_signals = signals

        # Return metrics for Engine Decision
        return {
            'raw_score': signals['score'],
            'pcr': pcr,
            'pcr_absolute': pcr,
            'ltp': self.underlying_ltp,
            'vwap': self.vwap,
            'days_to_expiry': self.days_to_expiry,
            'oi_wall_ce': wall_ce_strike,
            'oi_wall_pe': wall_pe_strike,
            'oi_pattern': self.oi_pattern,
            'oi_pattern_strength': self.oi_pattern_strength,
            'opening_range_high': self.opening_range_high,
            'opening_range_low': self.opening_range_low,
            'opening_range_locked': self.opening_range_locked,
            'quant_signal': signals
        }

    def _detect_market_regime(self, metrics):
        """Detect if market is TRENDING, SIDEWAYS, or CHOPPY.

        Uses 3 indicators measured over the last 30 minutes:
        1. Price range ratio: intraday range / price < 0.4% = tight/sideways
        2. VWAP crossings: how many times price crossed VWAP. >6 in 30 min = choppy
        3. Score sign flips: how many times composite score changed sign. >8 = choppy

        TRENDING = strong directional move, strategies should trade normally
        SIDEWAYS = tight range, only SELL strategies should be active
        CHOPPY = whipsaw, ALL strategies should reduce trading
        """
        now = time.time()

        # Only update every 30 seconds (not every tick)
        if now - self._regime_last_update < 30:
            return
        self._regime_last_update = now

        raw_score = metrics.get('raw_score', 0)
        ltp = metrics.get('ltp', 0)
        vwap = metrics.get('vwap', 0)

        # Track score sign (+1 or -1) for flip detection
        self._score_sign_history.append(1 if raw_score > 0 else -1 if raw_score < 0 else 0)

        # Track VWAP crossings
        if ltp > 0 and vwap > 0:
            # Check if price just crossed VWAP (compare to previous)
            if len(self.price_history) >= 2:
                prev_ltp = self.price_history[-2]
                crossed = (prev_ltp >= vwap and ltp < vwap) or (prev_ltp <= vwap and ltp > vwap)
                self._vwap_cross_history.append(1 if crossed else 0)

        # Need at least 10 minutes of data
        if len(self.price_history) < 600 or len(self._score_sign_history) < 20:
            self.market_regime_type = 'DETECTING'
            return

        # INDICATOR 1: Price range ratio
        recent_prices = list(self.price_history)[-1800:]  # Last 30 min
        price_high = max(recent_prices)
        price_low = min(recent_prices)
        price_avg = sum(recent_prices) / len(recent_prices)
        range_pct = ((price_high - price_low) / price_avg * 100) if price_avg > 0 else 0

        # INDICATOR 2: VWAP crossing count (last 30 min)
        vwap_crosses = sum(self._vwap_cross_history)

        # INDICATOR 3: Score sign flips (last 30 min)
        signs = list(self._score_sign_history)[-60:]  # Last 60 samples (at 30s update = 30 min)
        sign_flips = 0
        for i in range(1, len(signs)):
            if signs[i] != 0 and signs[i-1] != 0 and signs[i] != signs[i-1]:
                sign_flips += 1

        # CLASSIFY
        old_regime = self.market_regime_type

        if range_pct > 0.6 and sign_flips <= 4:
            self.market_regime_type = 'TRENDING'
        elif range_pct < 0.3 and vwap_crosses < 4:
            self.market_regime_type = 'SIDEWAYS'
        elif sign_flips > 6 or vwap_crosses > 8:
            self.market_regime_type = 'CHOPPY'
        elif range_pct > 0.4:
            self.market_regime_type = 'TRENDING'
        else:
            self.market_regime_type = 'SIDEWAYS'

        if old_regime != self.market_regime_type:
            logger.info(f"[REGIME] Market type: {old_regime} → {self.market_regime_type} | Range:{range_pct:.2f}% | VWAP crosses:{vwap_crosses} | Score flips:{sign_flips}")
            print(f"[REGIME] {self.market_regime_type} | Range:{range_pct:.2f}% | VWAP crosses:{vwap_crosses} | Score flips:{sign_flips}")

    def _update_opening_range(self, ltp):
        """Capture 9:15-9:30 high/low for ORB strategy.
        If server starts after 9:30, uses underlying_high/low as fallback
        (contains opening range within it) + open price for center.
        """
        if self.opening_range_locked or ltp <= 0:
            return

        try:
            now = datetime.now(pytz.timezone('Asia/Kolkata'))

            # Before 9:30: actively track high/low
            if now.hour == 9 and now.minute < 30:
                if self.opening_range_high == 0:
                    self.opening_range_high = ltp
                    self.opening_range_low = ltp
                else:
                    self.opening_range_high = max(self.opening_range_high, ltp)
                    self.opening_range_low = min(self.opening_range_low, ltp)

            # At 9:30: lock the range
            elif now.hour == 9 and now.minute >= 30 and not self.opening_range_locked:
                if self.opening_range_high > 0 and self.opening_range_low > 0:
                    # Normal case: we captured the range live
                    self.opening_range_locked = True
                    self._orb_lock_time = time.time()
                    logger.info(f"[ORB] Range locked LIVE: {self.opening_range_low:.2f} - {self.opening_range_high:.2f}")
                elif self.underlying_open > 0:
                    # LATE START FALLBACK: Server started after 9:30
                    # Use open price and current high/low to estimate the range
                    # The opening range is typically 0.3-0.5% of open price for NIFTY
                    open_price = self.underlying_open
                    if self.underlying_high > 0 and self.underlying_low > 0:
                        # high/low already contain the opening range
                        self.opening_range_high = self.underlying_high
                        self.opening_range_low = self.underlying_low
                    else:
                        # Pure estimation from open price
                        range_pct = 0.003  # 0.3% typical NIFTY opening range
                        self.opening_range_high = open_price * (1 + range_pct)
                        self.opening_range_low = open_price * (1 - range_pct)

                    self.opening_range_locked = True
                    self._orb_lock_time = time.time()
                    logger.info(f"[ORB] Range estimated (LATE START): {self.opening_range_low:.2f} - {self.opening_range_high:.2f} (open={open_price:.2f})")

            # After 10:00: if still not locked, opening range is stale - don't use it
            elif now.hour >= 10 and not self.opening_range_locked:
                self.opening_range_locked = True  # Mark as locked but with zero range = ORB won't trigger
                logger.info("[ORB] Past 10:00 AM without range capture. ORB disabled for today.")
        except Exception as e:
            logger.error(f"[ORB] Error in range capture: {e}")

    def generate_signals(self, pcr, max_pain):
        """
        Full V7 Signal Generation Logic
        (Recalculates scores based on history populated by update_market_state)
        """
        if not self.initial_state or not self.start_price:
            return {
                'action': 'WAIT',
                'confidence': '0%',
                'score': 0,
                'max_pain': max_pain,
                'pcr_signal': 'NEUTRAL',
                'reasons': ["Initializing..."],
                'regime': 'DETECTING',
                'regime_info': {}
            }

        # Calculate Scores
        score_delta, s_d_reason = self._score_delta_oi()
        score_iv, s_iv_reason = self._score_iv_skew()
        score_unwind, s_u_reason = self._score_oi_unwind()
        
        # Use passed max_pain for scoring
        mp = max_pain
        score_mp = 0
        s_mp_reason = "No Max Pain"
        if mp > 0 and self.underlying_ltp > 0:
            diff_pct = (self.underlying_ltp - mp) / mp * 100
            if diff_pct > 0.5: 
                score_mp, s_mp_reason = -10, f"Pulling Down to Max Pain {mp}"
            elif diff_pct < -0.5: 
                score_mp, s_mp_reason = 10, f"Pulling Up to Max Pain {mp}"
            else:
                s_mp_reason = "Aligned with Max Pain"
                
        score_gex, s_gex_reason, net_gex = self._score_gamma_exposure()
        score_vc, s_vc_reason = self._score_vanna_charm()
        score_flow, s_flow_reason = self._detect_institutional_flow()
        score_mom, s_mom_reason = self._score_momentum_velocity()
        score_pcr_roc, s_pcr_reason = self._score_pcr_roc()
        score_vwap, s_vwap_reason = self._score_vwap()
        score_oi_wall, s_wall_reason = self._score_oi_wall()
        score_pcr_abs, s_pcr_abs_reason = self._score_pcr_absolute(pcr)

        # Store GEX history
        self.gex_history.append(net_gex)

        # Store scores map
        scores_dict = {
            'delta_oi': score_delta,
            'iv_skew': score_iv,
            'oi_unwind': score_unwind,
            'max_pain': score_mp,
            'gamma': score_gex,
            'vanna': score_vc,
            'inst_flow': score_flow,
            'momentum': score_mom,
            'pcr_roc': score_pcr_roc,
            'vwap': score_vwap,
            'oi_wall': score_oi_wall,
            'pcr_absolute': score_pcr_abs,
        }

        # Weighting
        weighted_score = sum(
            scores_dict.get(key, 0) * weight
            for key, weight in self.signal_weights.items()
        )

        max_possible = sum(self.signal_weights.values()) * 30
        base_score = (weighted_score / max_possible) * 100
        
        # Cap score
        total_score = max(min(int(base_score), 100), -100)

        # Determine PCR Signal
        pcr_bias = "NEUTRAL"
        if score_pcr_roc > 0: pcr_bias = "BULLISH"
        elif score_pcr_roc < 0: pcr_bias = "BEARISH"
        
        # Determine Regime Action (Simple mapping for now or copy V7 detect_market_regime if needed)
        # Using simplified guardian status for V10 consistency or V7 logic?
        # Let's use V7-like reasoning strictly for UI display
        regime = self.guardian.current_regime
        confidence = 0 # Placeholder if not using full V7 detect_regime
        
        # Build Reasons for UI Transparency
        reasons = [
            f"Score: {total_score:.1f} | Regime: {regime}",
            f"Delta OI: {score_delta} ({s_d_reason})",
            f"IV Skew: {score_iv} ({s_iv_reason})",
            f"Momentum: {score_mom} ({s_mom_reason})",
            f"PCR ROC: {score_pcr_roc} ({s_pcr_reason})",
            f"GEX: {score_gex} ({s_gex_reason})",
            f"OI Pattern: {score_unwind} ({s_u_reason})",
            f"VWAP: {score_vwap} ({s_vwap_reason})",
            f"OI Wall: {score_oi_wall} ({s_wall_reason})",
            f"PCR Level: {score_pcr_abs} ({s_pcr_abs_reason})",
        ]

        # Extract Raw Metrics for Debugging
        try:
            raw_d_oi = s_d_reason.split('(')[1].split(')')[0] if '(' in s_d_reason else "0%"
            raw_iv = s_iv_reason.split('Diff ')[1] if 'Diff ' in s_iv_reason else "0.0"
            raw_mom = s_mom_reason.split('(')[1].split(')')[0] if '(' in s_mom_reason else "0%" # Note: s_mom_reason needs update
        except:
            raw_d_oi, raw_iv, raw_mom = "N/A", "N/A", "N/A"

        # PERIODIC DEBUG SUMMARY (Every 15 signals)
        if not hasattr(self, '_last_raw_log_time'): self._last_raw_log_time = 0
        if time.time() - self._last_raw_log_time > 15:
            self._last_raw_log_time = time.time()
            logger.info(f"🔍 [RAW SIGNALS] D_OI:{score_delta}({raw_d_oi}) | IV:{score_iv}({raw_iv}) | MOM:{score_mom} | PCR:{score_pcr_roc} | GEX:{score_gex} | F:{score_flow}")

        return {
            'action': 'BUY CALL' if total_score > 20 else 'BUY PUT' if total_score < -20 else 'WAIT',
            'confidence': f"{confidence}%",
            'score': total_score,
            'max_pain': max_pain,
            'pcr_signal': pcr_bias,
            'reasons': reasons,
            'scores_breakdown': scores_dict,  # Individual signal scores for strategy confluence checks
            'regime': regime,
            'regime_info': {'regime': regime, 'confidence': confidence},
            'flow_alerts': []
        }

    def calculate_market_metrics_internal(self):
        """Deprecated: Use update_market_state instead"""
        return self.update_market_state()




    def _get_dated_state_file(self):
        """State file in today's date folder: logs/DD-MM-YYYY/v10_state_NIFTY.json"""
        from datetime import datetime
        today = datetime.now().strftime("%d-%m-%Y")
        folder = os.path.join("logs", today)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"v10_state_{self._state_underlying}.json")

    def save_state(self):
        """Save complete state including active trades and strategy states"""
        # Update state file path for current date (handles midnight rollover)
        self.state_file = self._get_dated_state_file()
        # Build strategy states
        strategy_states = {}
        for s in self.strategies:
            # Only save active_trade if it exists and is NOT closed
            active_trade_to_save = None
            if s.active_trade and not s.active_trade.get('_closed', False):
                active_trade_to_save = s.active_trade

            strategy_states[s.name] = {
                'active_trade': active_trade_to_save,
                'trades_today': s.trades_today,
                'daily_pnl': s.daily_pnl,
                'exit_cooldown_until': s.exit_cooldown_until
            }

        state = {
            'trades_today': self.trades_today,
            'daily_pnl': self.daily_pnl_points,
            'safety_lock': self.safety_lock,
            'active_trade': self.active_trade,  # Bug #1 fix
            'exit_cooldown_until': self.exit_cooldown_until,  # Bug #3 fix
            'last_reset_date': getattr(self, '_last_reset_date', None),  # Bug #2 fix
            'strategy_states': strategy_states  # Bug #1 fix - strategy trades
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def load_state(self):
        """Load state from today's date folder. Falls back to old root location for migration."""
        # Check today's dated file first
        self.state_file = self._get_dated_state_file()

        # Fallback: if today's file doesn't exist, check old root location (migration)
        if not os.path.exists(self.state_file):
            old_path = os.path.join("logs", f"v10_state_{self._state_underlying}.json")
            if os.path.exists(old_path):
                logger.info(f"[STATE] Migrating state from {old_path} to {self.state_file}")
                import shutil
                os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
                shutil.copy2(old_path, self.state_file)

        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.trades_today = state.get('trades_today', 0)
                    self.daily_pnl_points = state.get('daily_pnl', 0)
                    self.safety_lock = state.get('safety_lock', False)

                    # Bug #1 fix: Load active_trade
                    self.active_trade = state.get('active_trade', None)

                    # Bug #3 fix: Load exit_cooldown_until
                    self.exit_cooldown_until = state.get('exit_cooldown_until', 0)

                    # Bug #2 fix: Load last reset date
                    self._last_reset_date = state.get('last_reset_date', None)

                    # Bug #1 fix: Load strategy states
                    strategy_states = state.get('strategy_states', {})
                    for s in self.strategies:
                        if s.name in strategy_states:
                            ss = strategy_states[s.name]
                            s.active_trade = ss.get('active_trade', None)
                            s.trades_today = ss.get('trades_today', 0)
                            s.daily_pnl = ss.get('daily_pnl', 0)
                            s.exit_cooldown_until = ss.get('exit_cooldown_until', 0)
                            if s.active_trade:
                                logger.info(f"[{s.name}] Restored active trade: {s.active_trade.get('Contract')}")

                    logger.info(f"Loaded v10 State. Active trades restored for {len([s for s in self.strategies if s.active_trade])} strategies.")
            except Exception as e:
                logger.error(f"Error loading state: {e}")

    def _check_daily_reset(self):
        """Bug #2 fix: Reset daily counters at market open (9:15 AM IST)"""
        try:
            ist = pytz.timezone('Asia/Kolkata')
            now = datetime.now(ist)
            today_str = now.strftime('%Y-%m-%d')

            # Only reset between 9:15 and 9:20 AM to avoid multiple resets
            if now.hour == 9 and 15 <= now.minute < 20:
                if getattr(self, '_last_reset_date', None) != today_str:
                    # Reset manager counters
                    self.trades_today = 0
                    self.daily_pnl_points = 0
                    self.safety_lock = False

                    # Reset strategy counters
                    for s in self.strategies:
                        s.trades_today = 0
                        s.daily_pnl = 0

                    self._last_reset_date = today_str
                    logger.info(f"[v10 ENGINE] Daily counters reset for {today_str}")
                    self.save_state()
        except Exception as e:
            logger.error(f"Error in daily reset check: {e}")

    # ============================================================================
    # V7 LOGIC REPLICATION (The Core Intelligence)
    # ============================================================================

    def _capture_initial_state(self):
        """Capture initial OI state and handle dynamic strike additions.

        Capped to prevent unbounded growth: only tracks strikes currently
        in option_data. Prunes stale strikes that are no longer in the chain.
        """
        if not self.option_data: return

        if self.start_price is None and self.underlying_ltp > 0:
            self.start_price = self.underlying_ltp

        # Prune strikes no longer in option_data (prevents unbounded growth)
        stale_strikes = [s for s in self.initial_state if s not in self.option_data]
        for s in stale_strikes:
            del self.initial_state[s]

        # Add new strikes
        new_strikes_found = False
        for strike, data in self.option_data.items():
            if strike not in self.initial_state:
                self.initial_state[strike] = {
                    'ce_oi': data['ce_data'].get('oi', 0),
                    'pe_oi': data['pe_data'].get('oi', 0)
                }
                new_strikes_found = True

        if new_strikes_found:
            logger.debug(f"[V10] Synchronized initial_state with {len(self.initial_state)} strikes")

    def _get_recent_volatility(self):
        """Calculate recent realized volatility using 1-second returns.

        Returns annualized volatility percentage (comparable to IV).
        Previous implementation used high-low range which doesn't scale
        correctly for option SL/Target calculations.
        """
        if len(self.price_history) < 60:
            return 15.0  # Default ~15% annualized (typical NIFTY)

        recent_prices = list(self.price_history)[-60:]

        # Calculate 1-second log returns
        returns = []
        for i in range(1, len(recent_prices)):
            if recent_prices[i - 1] > 0:
                ret = (recent_prices[i] - recent_prices[i - 1]) / recent_prices[i - 1]
                returns.append(ret)

        if len(returns) < 10:
            return 15.0

        # Standard deviation of returns, annualized
        # Trading seconds per year: ~6.25 hrs * 3600 * 252 days = ~5,670,000
        import math
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance) if variance > 0 else 0

        # Annualize: multiply by sqrt(trading_seconds_per_year)
        annualized_vol = std_dev * math.sqrt(5_670_000) * 100  # as percentage

        return max(5.0, min(80.0, annualized_vol))  # Clamp to 5-80%

    def _score_delta_oi(self):
        """Score based on net directional OI change across all strikes.

        Compares current OI against PREVIOUS 3-MINUTE SNAPSHOT (not session start).
        This catches reversals that session-baseline comparison misses.

        Rolling snapshot: Every 180 ticks (~3 min), saves per-strike OI.
        Comparison is always against the previous snapshot.
        """
        # Maintain rolling OI snapshot (updated every 180 ticks)
        if not hasattr(self, '_oi_snapshot'):
            self._oi_snapshot = {}
            self._oi_snapshot_prev = {}
            self._oi_snapshot_tick = 0

        self._oi_snapshot_tick += 1

        # Take new snapshot every 180 ticks (~3 minutes)
        if self._oi_snapshot_tick >= 180:
            self._oi_snapshot_prev = dict(self._oi_snapshot)
            self._oi_snapshot = {}
            for strike, data in self.option_data.items():
                self._oi_snapshot[strike] = {
                    'ce_oi': data['ce_data'].get('oi', 0),
                    'pe_oi': data['pe_data'].get('oi', 0),
                }
            self._oi_snapshot_tick = 0

        # Use previous snapshot as baseline (if available), else fall back to initial_state
        baseline = self._oi_snapshot_prev if self._oi_snapshot_prev else self.initial_state

        net_delta_oi = 0
        current_total_oi_change = 0
        for strike, data in self.option_data.items():
            if strike not in baseline: continue
            base = baseline[strike]
            ce_oi_change = data['ce_data'].get('oi', 0) - base.get('ce_oi', 0)
            pe_oi_change = data['pe_data'].get('oi', 0) - base.get('pe_oi', 0)
            net_delta_oi += (ce_oi_change * 0.5) + (pe_oi_change * -0.5)
            current_total_oi_change += abs(ce_oi_change) + abs(pe_oi_change)

        if current_total_oi_change == 0: return 0, "No OI Change"

        # Minimum OI change filter: ignore tiny movements
        total_oi = sum(
            d['ce_data'].get('oi', 0) + d['pe_data'].get('oi', 0)
            for d in self.option_data.values()
        )
        if total_oi > 0 and (current_total_oi_change / total_oi) < 0.005:
            return 0, "OI change < 0.5% of total (noise)"

        directional_pct = (abs(net_delta_oi) / current_total_oi_change) * 100

        # DEBUG LOG (Every 30s)
        if not hasattr(self, '_d_oi_log_count'): self._d_oi_log_count = 0
        self._d_oi_log_count += 1
        if self._d_oi_log_count % 30 == 0:
            src = "3min" if self._oi_snapshot_prev else "session"
            logger.info(f"[D_OI] ({src}) Total Change: {current_total_oi_change} | Net: {net_delta_oi} | Dir%: {directional_pct:.1f}%")

        # Graduated scoring based on directional concentration
        if directional_pct < 10:
            return 0, f"Balanced Delta OI ({directional_pct:.1f}%)"

        # Determine direction and graduated score
        if directional_pct >= 40:
            score = 30
            strength = "Strong"
        elif directional_pct >= 20:
            score = 20
            strength = "Moderate"
        else:  # 10-20%
            score = 10
            strength = "Weak"

        if net_delta_oi < 0:
            return score, f"{strength} Put Writing ({directional_pct:.1f}% directional)"
        else:
            return -score, f"{strength} Call Writing ({directional_pct:.1f}% directional)"

    def _score_iv_skew(self):
        """Score based on ATM IV skew between PE and CE.

        Threshold raised from 0.5 to 1.5 IV points - the old threshold was
        within bid-ask noise for NIFTY options (ATM IVs range 12-25).
        """
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return 0, "No ATM Data"
        atm_data = self.option_data[self.atm_strike]
        ce_iv = atm_data['ce_data'].get('iv', 0)
        pe_iv = atm_data['pe_data'].get('iv', 0)

        if ce_iv == 0 or pe_iv == 0: return 0, "IV Missing"

        diff = pe_iv - ce_iv
        if diff > 2.0: return -15, f"Put IV Skew (Bearish: Diff {diff:.2f})"
        elif diff > 1.5: return -10, f"Mild Put IV Skew (Diff {diff:.2f})"
        elif diff < -2.0: return 15, f"Call IV Skew (Bullish: Diff {diff:.2f})"
        elif diff < -1.5: return 10, f"Mild Call IV Skew (Diff {diff:.2f})"
        return 0, f"Balanced IV Skew (Diff {diff:.2f})"

    def _score_oi_unwind(self):
        """Detect OI patterns using SEPARATE CE and PE OI trends.

        The 6 OI patterns and what they mean:

        STRONG SIGNALS (fresh money entering):
        - Long Buildup:  Price UP + PE OI UP (put writers betting bullish) → Strong BULLISH (+25)
        - Short Buildup: Price DOWN + CE OI UP (call writers betting bearish) → Strong BEARISH (-25)

        MODERATE SIGNALS (existing money exiting):
        - Short Covering:  Price UP + CE OI DOWN (call writers buying back under pressure) → Moderate BULLISH (+15)
        - Long Unwinding:  Price DOWN + PE OI DOWN (put holders exiting/profit booking) → Moderate BEARISH (-15)

        CONTINUATION SIGNALS (confirmation of ongoing move):
        - Put Unwinding:  Price UP + PE OI DOWN (puts closed as market moves away) → Bullish continuation (+10)
        - Call Unwinding:  Price DOWN + CE OI DOWN (calls closed as market drops) → Bearish continuation (-10)

        Uses 60-second lookback window for trend detection (not first/last which spans entire session).
        """
        lookback = 180  # 3-minute window (institutional orders take 2-3 min to fill)

        if (len(self.price_history) < lookback or
                len(self.ce_oi_history) < lookback or
                len(self.pe_oi_history) < lookback):
            self.oi_pattern = 'DETECTING'
            self.oi_pattern_strength = 0
            return 0, "Building History"

        # Price trend over last 60 seconds
        price_now = self.price_history[-1]
        price_prev = self.price_history[-lookback]
        price_change = price_now - price_prev
        price_pct = (price_change / price_prev * 100) if price_prev > 0 else 0

        # CE OI trend over last 60 seconds
        ce_oi_now = self.ce_oi_history[-1]
        ce_oi_prev = self.ce_oi_history[-lookback]
        ce_oi_change = ce_oi_now - ce_oi_prev

        # PE OI trend over last 60 seconds
        pe_oi_now = self.pe_oi_history[-1]
        pe_oi_prev = self.pe_oi_history[-lookback]
        pe_oi_change = pe_oi_now - pe_oi_prev

        # Minimum thresholds to filter noise
        price_is_up = price_pct > 0.01    # > 0.01% move
        price_is_down = price_pct < -0.01
        ce_oi_up = ce_oi_change > 0
        ce_oi_down = ce_oi_change < 0
        pe_oi_up = pe_oi_change > 0
        pe_oi_down = pe_oi_change < 0

        # Calculate strength based on magnitude of OI change
        total_oi = ce_oi_now + pe_oi_now
        oi_change_magnitude = (abs(ce_oi_change) + abs(pe_oi_change))
        strength = min(100, int((oi_change_magnitude / max(total_oi, 1)) * 10000)) if total_oi > 0 else 0

        score = 0
        pattern = "Mixed"

        if price_is_up:
            if pe_oi_up and (pe_oi_change > abs(ce_oi_change) or not ce_oi_up):
                # Price UP + PE OI increasing = LONG BUILDUP (fresh put writing = bullish)
                score = 25
                pattern = f"Long Buildup (PE OI +{pe_oi_change:,})"
            elif ce_oi_down and abs(ce_oi_change) > pe_oi_change:
                # Price UP + CE OI decreasing significantly = SHORT COVERING
                score = 15
                pattern = f"Short Covering (CE OI {ce_oi_change:,})"
            elif pe_oi_down:
                # Price UP + PE OI decreasing = PUT UNWINDING (continuation)
                score = 10
                pattern = f"Put Unwinding (PE OI {pe_oi_change:,})"
            else:
                score = 5
                pattern = "Mild Bullish OI"

        elif price_is_down:
            if ce_oi_up and (ce_oi_change > abs(pe_oi_change) or not pe_oi_up):
                # Price DOWN + CE OI increasing = SHORT BUILDUP (fresh call writing = bearish)
                score = -25
                pattern = f"Short Buildup (CE OI +{ce_oi_change:,})"
            elif pe_oi_down and abs(pe_oi_change) > ce_oi_change:
                # Price DOWN + PE OI decreasing significantly = LONG UNWINDING
                score = -15
                pattern = f"Long Unwinding (PE OI {pe_oi_change:,})"
            elif ce_oi_down:
                # Price DOWN + CE OI decreasing = CALL UNWINDING (continuation)
                score = -10
                pattern = f"Call Unwinding (CE OI {ce_oi_change:,})"
            else:
                score = -5
                pattern = "Mild Bearish OI"

        else:
            # Price flat
            if ce_oi_up and pe_oi_up:
                pattern = "OI Buildup Both Sides (Range Forming)"
            elif ce_oi_down and pe_oi_down:
                pattern = "OI Unwinding Both Sides (Breakout Coming)"
            else:
                pattern = "Neutral OI"

        # Update manager-level state for strategies to use
        self.oi_pattern = pattern
        self.oi_pattern_strength = strength

        # Debug log every 30s
        if not hasattr(self, '_oi_pattern_log_count'): self._oi_pattern_log_count = 0
        self._oi_pattern_log_count += 1
        if self._oi_pattern_log_count % 30 == 0:
            logger.info(f"[OI PATTERN] {pattern} | CE OI: {ce_oi_change:+,} | PE OI: {pe_oi_change:+,} | Price: {price_pct:+.3f}% | Strength: {strength}")

        return score, pattern

    def _score_max_pain(self):
        mp = self.calculate_max_pain()
        if mp == 0 or self.underlying_ltp == 0: return 0, "No Max Pain"
        
        diff_pct = (self.underlying_ltp - mp) / mp * 100
        if diff_pct > 0.5: return -10, f"Pulling Down to Max Pain {mp}"
        elif diff_pct < -0.5: return 10, f"Pulling Up to Max Pain {mp}"
        return 0, "Aligned with Max Pain"

    def _score_gamma_exposure(self):
        """Score based on net Gamma Exposure across all strikes.

        Fixes:
        - Includes underlying price in GEX formula (was missing)
        - Uses correct lot sizes per underlying
        - Graduated scoring instead of binary
        - Positive GEX = market maker long gamma = price stability (mean-reverting)
        - Negative GEX = market maker short gamma = price instability (trending)
        """
        total_gex = 0
        lot_sizes = {
            'NIFTY': 75, 'BANKNIFTY': 15, 'SENSEX': 10,
            'RELIANCE': 250, 'HDFCBANK': 550, 'ICICIBANK': 700,
            'SBIN': 1500, 'INFY': 300, 'BHARTIARTL': 458
        }
        lot_size = lot_sizes.get(self.underlying, 50)
        spot = self.underlying_ltp if self.underlying_ltp > 0 else 1

        for strike, data in self.option_data.items():
            ce_gex = data['ce_data'].get('gamma', 0) * data['ce_data'].get('oi', 0) * lot_size * spot * 0.01
            pe_gex = data['pe_data'].get('gamma', 0) * data['pe_data'].get('oi', 0) * lot_size * spot * 0.01
            total_gex += (ce_gex - pe_gex)

        if total_gex == 0: return 0, "No GEX", 0

        gex_m = total_gex / 1e6

        # Positive GEX = stability (mean reversion) - mild bullish in range-bound market
        # Negative GEX = instability (trend amplification) - indicates big move possible
        if total_gex > 0:
            if gex_m > 10:
                return 5, f"High GEX Stability ({gex_m:.1f}M) - Range Bound", total_gex
            return 3, f"Positive GEX ({gex_m:.1f}M) - Mild Stability", total_gex
        else:
            if gex_m < -10:
                return -15, f"High Negative GEX ({gex_m:.1f}M) - Volatile", total_gex
            elif gex_m < -5:
                return -10, f"Negative GEX ({gex_m:.1f}M) - Unstable", total_gex
            return -5, f"Mild Negative GEX ({gex_m:.1f}M)", total_gex

    def _score_vanna_charm(self):
        if len(self.iv_history) < 20: return 0, "Wait"
        current_iv = self.iv_history[-1]
        avg_iv = sum(self.iv_history)/len(self.iv_history)
        if current_iv < avg_iv * 0.98: return 10, "IV Crush (Bullish)"
        return 0, "Normal"

    def _detect_institutional_flow(self):
        """ Institutional Flow V1: Bid/Ask Volume Imbalance """
        total_bid_vol = 0
        total_ask_vol = 0
        
        for strike, data in self.option_data.items():
            total_bid_vol += data['ce_data'].get('bid_qty', 0) + data['pe_data'].get('bid_qty', 0)
            total_ask_vol += data['ce_data'].get('ask_qty', 0) + data['pe_data'].get('ask_qty', 0)
            
        if total_bid_vol == 0 or total_ask_vol == 0:
            return 0, "No Flow Data"
            
        imbalance = (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol) * 100
        
        # DEBUG LOG (Every 30s)
        if not hasattr(self, '_flow_log_count'): self._flow_log_count = 0
        self._flow_log_count += 1
        if self._flow_log_count % 30 == 0:
            logger.info(f"📊 [FLOW DEBUG] Imbalance: {imbalance:.1f}% | BidVol: {total_bid_vol} | AskVol: {total_ask_vol}")

        if imbalance > 20: return 10, "Institutional Buying Pressure"
        elif imbalance < -20: return -10, "Institutional Selling Pressure"
        return 0, "Balanced Flow"

    def _score_momentum_velocity(self):
        """Score based on price velocity over 60 seconds.

        Uses acceleration (2nd derivative) alongside velocity to distinguish
        genuine momentum from random walk. Window: 60s full, 30s midpoint.
        """
        if len(self.price_history) < 60: return 0, "No Velocity"
        p_now = self.price_history[-1]
        p_mid = self.price_history[-30]
        p_prev = self.price_history[-60]

        velocity = (p_now - p_prev) / p_prev * 100
        # Acceleration over 30s halves (was 15s - too noisy)
        vel_recent = (p_now - p_mid) / p_mid * 100 if p_mid > 0 else 0
        vel_old = (p_mid - p_prev) / p_prev * 100 if p_prev > 0 else 0
        acceleration = vel_recent - vel_old

        # DEBUG LOG (Every 30s)
        if not hasattr(self, '_mom_log_count'): self._mom_log_count = 0
        self._mom_log_count += 1
        if self._mom_log_count % 30 == 0:
            logger.info(f"[MOM] Velocity: {velocity:.4f}% | Accel: {acceleration:.4f}% | History: {len(self.price_history)}")

        # Score based on velocity + acceleration bonus
        score = 0
        if velocity > 0.05:
            score = 15
        elif velocity > 0.03:
            score = 10
        elif velocity < -0.05:
            score = -15
        elif velocity < -0.03:
            score = -10

        # Acceleration bonus: accelerating momentum gets extra weight
        if score > 0 and acceleration > 0.01:
            score = min(score + 5, 20)
        elif score < 0 and acceleration < -0.01:
            score = max(score - 5, -20)

        direction = "Positive" if velocity > 0 else "Negative" if velocity < 0 else "Stable"
        return score, f"{direction} Momentum ({velocity:.4f}%, accel: {acceleration:.4f}%)"

    def _score_pcr_roc(self):
        """Score based on PCR rate of change over 60 seconds.

        Window: 3 minutes (180 samples at 1Hz). Matches OI pattern window.
        60-second PCR still has bid-ask oscillation noise.
        Added magnitude check: PCR change must be > 1% to be meaningful.
        """
        if len(self.pcr_history) < 180: return 0, "Wait"
        pcr_now = self.pcr_history[-1]
        pcr_prev = self.pcr_history[-180]
        if pcr_prev == 0: return 0, "PCR Div/0"

        roc_pct = ((pcr_now - pcr_prev) / pcr_prev) * 100

        # Need > 1% change to be meaningful (filter bid-ask oscillations)
        if roc_pct > 3: return 15, f"PCR Surging ({roc_pct:+.1f}%)"
        elif roc_pct > 1: return 10, f"PCR Rising ({roc_pct:+.1f}%)"
        elif roc_pct < -3: return -15, f"PCR Crashing ({roc_pct:+.1f}%)"
        elif roc_pct < -1: return -10, f"PCR Falling ({roc_pct:+.1f}%)"
        return 0, f"PCR Stable ({roc_pct:+.1f}%)"

    def _score_vwap(self):
        """Score based on price position relative to VWAP.
        Price above VWAP = bullish (institutional buying).
        Price below VWAP = bearish (institutional selling).
        Distance from VWAP measures conviction.
        """
        if self.vwap <= 0 or self.underlying_ltp <= 0:
            return 0, "VWAP not ready"

        distance_pct = ((self.underlying_ltp - self.vwap) / self.vwap) * 100

        if distance_pct > 0.3:
            return 15, f"Above VWAP +{distance_pct:.2f}% (Strong Bull)"
        elif distance_pct > 0.1:
            return 10, f"Above VWAP +{distance_pct:.2f}%"
        elif distance_pct < -0.3:
            return -15, f"Below VWAP {distance_pct:.2f}% (Strong Bear)"
        elif distance_pct < -0.1:
            return -10, f"Below VWAP {distance_pct:.2f}%"
        return 0, f"At VWAP ({distance_pct:+.2f}%)"

    def _score_oi_wall(self):
        """Score based on price proximity to OI walls.
        Highest CE OI strike = resistance (call writers defend).
        Highest PE OI strike = support (put writers defend).
        Price near support = bullish (likely bounce). Near resistance = bearish.
        Price breaking through wall = strong directional signal.
        """
        ltp = self.underlying_ltp
        if ltp <= 0 or self.oi_wall_ce_strike == 0 or self.oi_wall_pe_strike == 0:
            return 0, "OI Walls not ready"

        ce_wall = self.oi_wall_ce_strike  # Resistance
        pe_wall = self.oi_wall_pe_strike  # Support

        # Distance to walls as % of price
        dist_to_resistance = ((ce_wall - ltp) / ltp) * 100 if ce_wall > 0 else 999
        dist_to_support = ((ltp - pe_wall) / ltp) * 100 if pe_wall > 0 else 999

        # BREAKOUT: price above resistance wall
        if ltp > ce_wall:
            return 20, f"ABOVE CE Wall {ce_wall} (Breakout!)"
        # BREAKDOWN: price below support wall
        if ltp < pe_wall:
            return -20, f"BELOW PE Wall {pe_wall} (Breakdown!)"

        # Near support = bullish bounce zone
        if dist_to_support < 0.2:
            return 10, f"Near PE Wall {pe_wall} (Support)"
        # Near resistance = bearish rejection zone
        if dist_to_resistance < 0.2:
            return -10, f"Near CE Wall {ce_wall} (Resistance)"

        # In the middle = range-bound, no signal
        return 0, f"Between Walls {pe_wall}-{ce_wall}"

    def _score_pcr_absolute(self, pcr):
        """Score based on PCR absolute level (sentiment extremes).
        This is different from PCR ROC which measures direction of change.

        PCR > 1.3 = extreme put buying = bearish crowd = contrarian bullish
        PCR < 0.7 = extreme call buying = bullish crowd = contrarian bearish
        PCR 0.8-1.2 = normal range = no edge
        """
        if pcr <= 0:
            return 0, "No PCR"

        # Extreme bearish sentiment (high PCR) = contrarian bullish
        if pcr > 1.5:
            return 15, f"Extreme Put Heavy PCR {pcr:.2f} (Contrarian Bull)"
        elif pcr > 1.3:
            return 10, f"High PCR {pcr:.2f} (Mild Contrarian Bull)"
        # Extreme bullish sentiment (low PCR) = contrarian bearish
        elif pcr < 0.5:
            return -15, f"Extreme Call Heavy PCR {pcr:.2f} (Contrarian Bear)"
        elif pcr < 0.7:
            return -10, f"Low PCR {pcr:.2f} (Mild Contrarian Bear)"

        return 0, f"Normal PCR {pcr:.2f}"

    def calculate_max_pain(self):
        """Calculate max pain strike with TTL caching and OI-change detection.

        Avoids O(n^2) recalculation every tick. Only recalculates if:
        - Cache TTL expired (5 seconds), AND
        - OI distribution has actually changed (hash check)
        """
        now = time.time()

        # Build OI snapshot and hash to detect real changes
        ce_oi = {k: v['ce_data'].get('oi', 0) for k, v in self.option_data.items()}
        pe_oi = {k: v['pe_data'].get('oi', 0) for k, v in self.option_data.items()}
        oi_hash = hash(tuple(sorted(ce_oi.items())) + tuple(sorted(pe_oi.items())))

        # Return cached value if TTL not expired or OI unchanged
        if (now - self._max_pain_cache_time < self._max_pain_cache_ttl
                and oi_hash == self._last_oi_hash
                and self._max_pain_cache > 0):
            return self._max_pain_cache

        strikes = sorted(self.option_data.keys())
        if not strikes:
            return 0

        # Filter out strikes with negligible OI (reduces n for O(n^2) loop)
        max_oi = max(max(ce_oi.values(), default=0), max(pe_oi.values(), default=0))
        if max_oi > 0:
            relevant_strikes = [s for s in strikes
                                if ce_oi.get(s, 0) > max_oi * 0.01 or pe_oi.get(s, 0) > max_oi * 0.01]
        else:
            relevant_strikes = strikes

        if not relevant_strikes:
            return 0

        min_loss = float('inf')
        max_pain = 0
        for strike in relevant_strikes:
            loss = 0
            for k in relevant_strikes:
                if strike > k: loss += (strike - k) * ce_oi.get(k, 0)
                if strike < k: loss += (k - strike) * pe_oi.get(k, 0)
            if loss < min_loss:
                min_loss = loss
                max_pain = strike

        self._max_pain_cache = max_pain
        self._max_pain_cache_time = now
        self._last_oi_hash = oi_hash
        return max_pain

    def calculate_atm(self):
        """Calculate ATM strike from underlying LTP. RETURNS value only, does NOT update self.atm_strike.
        Caller is responsible for deciding when to update self.atm_strike (after cooldown check).
        """
        try:
            # 1. Use WebSocket cached LTP if available
            if self.underlying_ltp > 0:
                return round(self.underlying_ltp / self.strike_step) * self.strike_step

            # 2. Fallback to API if WebSocket hasn't delivered LTP yet
            if self.api_client:
                if self.underlying == 'SENSEX':
                    exchange = 'BSE_INDEX'
                elif self.underlying == 'NIFTY':
                    exchange = 'NSE_INDEX'
                else:
                    exchange = 'NSE'

                logger.info(f"[{self.underlying}] LTP is 0. Polling API fallback...")
                response = self.api_client.quotes(symbol=self.underlying, exchange=exchange)
                
                if response.get('status') == 'success':
                    data = response.get('data', {})
                    ltp = float(data.get('ltp', 0) or 0)
                    if ltp > 0:
                        self.underlying_ltp = ltp
                        calculated = round(ltp / self.strike_step) * self.strike_step
                        logger.info(f"[{self.underlying}] API Fallback success: {ltp} -> ATM {calculated}")
                        return calculated

            return self.atm_strike or 0  # Return current ATM if can't calculate new one
        except Exception as e:
            logger.error(f"Error calculating ATM for {self.underlying}: {e}")
            return self.atm_strike or 0
            return 0

    def generate_strikes(self):
        if not self.atm_strike: return
        strikes = []
        for i in range(6, 0, -1): strikes.append({'strike': self.atm_strike - (i*self.strike_step), 'tag': f'ITM{i}', 'position': -i})
        strikes.append({'strike': self.atm_strike, 'tag': 'ATM', 'position': 0})
        for i in range(1, 7): strikes.append({'strike': self.atm_strike + (i*self.strike_step), 'tag': f'OTM{i}', 'position': i})
        
        for s in strikes:
            strike = s['strike']
            self.option_data[strike] = {
                'strike': strike, 'tag': s['tag'], 
                'ce_symbol': self.construct_option_symbol(strike, 'CE'),
                'pe_symbol': self.construct_option_symbol(strike, 'PE'),
                'is_gamma_wall': False,
                'ce_data': {
                    'oi': 0, 'volume': 0, 'ltp': 0, 'iv': 0, 'delta': 0.5,
                    'gamma': 0, 'theta': 0, 'vega': 0, 'gex': 0, 'spread': 0,
                    'oi_intensity': 0, 'oi_sentiment': 'NEUTRAL',
                    'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0
                }, 
                'pe_data': {
                    'oi': 0, 'volume': 0, 'ltp': 0, 'iv': 0, 'delta': -0.5,
                    'gamma': 0, 'theta': 0, 'vega': 0, 'gex': 0, 'spread': 0,
                    'oi_intensity': 0, 'oi_sentiment': 'NEUTRAL',
                    'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0,
                    'iv_status': 'normal'
                }
            }
            self.subscription_map[self.option_data[strike]['ce_symbol']] = {'strike': strike, 'type': 'CE'}
            self.subscription_map[self.option_data[strike]['pe_symbol']] = {'strike': strike, 'type': 'PE'}

    def construct_option_symbol(self, strike, option_type):
        """Construct OpenAlgo option symbol"""
        # Format: [Base Symbol][Expiration Date][Strike Price][Option Type]
        
        # Parse expiry date to proper format (DDMMMYY or DDMMM)
        expiry_formatted = None
        expiry_year = '25'  # Default year
        
        if isinstance(self.expiry, str):
            try:
                # Handle format like "28-AUG-25" -> "28AUG" and extract year "25"
                parts = self.expiry.split('-')
                if len(parts) >= 3:
                    day = parts[0].zfill(2)
                    month = parts[1].upper()[:3]
                    expiry_formatted = f"{day}{month}"
                    expiry_year = parts[2][-2:] 
                elif len(parts) >= 2:
                    day = parts[0].zfill(2)
                    month = parts[1].upper()[:3]
                    expiry_formatted = f"{day}{month}"
                else:
                    expiry_formatted = '28AUG' 
            except Exception as e:
                logger.error(f"Error parsing expiry: {e}")
                expiry_formatted = '28AUG'
        elif isinstance(self.expiry, datetime):
            expiry_formatted = self.expiry.strftime('%d%b').upper()
            expiry_year = self.expiry.strftime('%y')
        else:
            expiry_formatted = '28AUG'
        
        # Remove decimal if whole number
        strike_str = str(int(strike)) if strike == int(strike) else str(strike)
        
        # Construct symbol
        # Example: NIFTY28AUG2524500CE
        symbol = f"{self.underlying}{expiry_formatted}{expiry_year}{strike_str}{option_type}"
        return symbol

    def initialize(self, api_client):
        if self.initialized: return
        self.api_client = api_client
        self.atm_strike = self.calculate_atm()  # Direct set on initialization (no cooldown needed)
        self._chain_atm = self.atm_strike
        self.generate_strikes()
        self.setup_subscriptions()
        self.initialized = True
        return True

    def update_websocket_manager(self, new_ws_manager):
        """Update the websocket manager reference and resubscribe"""
        if not new_ws_manager or self.websocket_manager == new_ws_manager:
            return
            
        logger.info(f"[V10] Updating WebSocket manager reference for {self.underlying}")
        
        # 1. Unregister from OLD if possible
        if self.websocket_manager:
            try:
                self.websocket_manager.unregister_handler('quote', self.handle_quote_update)
            except:
                pass
                
        # 2. Update reference
        self.websocket_manager = new_ws_manager
        
        # 3. Setup subscriptions with NEW manager
        self.setup_subscriptions()


    def setup_subscriptions(self):
        """Configure WebSocket subscriptions with handler first approach"""
        if not self.websocket_manager: return
        
        # 1. Register Handler FIRST
        self.websocket_manager.register_handler('quote', self.handle_quote_update)
        
        # 2. Subscribe Underlying
        # Use V7 logic for correct exchange selection
        if self.underlying == 'SENSEX':
            exchange = 'BSE_INDEX'
        elif self.underlying == 'NIFTY':
            exchange = 'NSE_INDEX'
        else:
            exchange = 'NSE'
            
        logger.info(f"[V10] Subscribing to Underlying: {self.underlying} on {exchange}")
        self.websocket_manager.subscribe({'exchange': exchange, 'symbol': self.underlying, 'mode': 'quote'})
        
        # 3. Initial Strike Generation (if possible)
        if self.atm_strike:
            self.batch_subscribe_options()

    def batch_subscribe_options(self):
        if not self.websocket_manager: return
        exchange = 'NFO'
        instruments = []
        for d in self.option_data.values():
            instruments.append({'symbol': d['ce_symbol'], 'exchange': exchange})
            instruments.append({'symbol': d['pe_symbol'], 'exchange': exchange})
        
        logger.warning(f"[V10] Subscribing to {len(instruments)} options. First: {instruments[0]['symbol'] if instruments else 'None'}")
        self.websocket_manager.subscribe_batch(instruments, mode='quote')

    def handle_quote_update(self, data):
        """Handle quote updates with robust key extraction (Thread-Safe)"""
        # 0. Handle both symbol and Symbol keys
        symbol = (data.get('symbol') or data.get('Symbol') or data.get('trading_symbol') or '').upper()

        # 1. Handle Underlying Update
        if symbol == self.underlying:
            # Bug #4 fix: Use data_lock for thread-safe underlying data updates
            with self.data_lock:
                # Capture OHLC and avg
                self.underlying_ltp = float(data.get('ltp') or data.get('last_price', 0) or 0)
                self.underlying_bid = float(data.get('bid_price') or data.get('bid', 0) or 0)
                self.underlying_ask = float(data.get('ask_price') or data.get('ask', 0) or 0)
                self.underlying_open = float(data.get('open', 0) or 0)
                self.underlying_high = float(data.get('high', 0) or 0)
                self.underlying_low = float(data.get('low', 0) or 0)
                self.underlying_close = float(data.get('close', 0) or 0)

                if 'average_price' in data:
                    self.underlying_avg = float(data.get('average_price', 0) or 0)
                else:
                    self.underlying_avg = (self.underlying_high + self.underlying_low) / 2 if (self.underlying_high and self.underlying_low) else 0

            # ATM TRACKING
            calculated_atm = self.calculate_atm()
            now = time.time()

            if not calculated_atm:
                return

            # Always update for UI display
            self.atm_strike = calculated_atm

            # FIRST TIME: No option data yet → generate + subscribe
            if not self.option_data:
                self._chain_atm = calculated_atm
                self.generate_strikes()
                if self.websocket_manager:
                    self.batch_subscribe_options()
                self._last_resub_time = now
                self.update_option_tags()
                logger.info(f"[V10-ATM] First time chain built: chain_atm={self._chain_atm}")
                return

            # If chain_atm was never set (shouldn't happen, but safety)
            if self._chain_atm is None:
                self._chain_atm = calculated_atm
                logger.info(f"[V10-ATM] chain_atm was None, setting to {calculated_atm}")

            # ATM matches chain ATM → no mismatch, just update tags
            if calculated_atm == self._chain_atm:
                if self._atm_mismatch_since != 0:
                    logger.info(f"[V10-ATM] Mismatch resolved: ATM back to chain_atm={self._chain_atm}")
                self._atm_mismatch_since = 0
                self.update_option_tags()
                return

            # ATM differs from chain ATM → track mismatch duration
            if self._atm_mismatch_since == 0:
                # First time seeing mismatch → start timer
                self._atm_mismatch_since = now
                logger.info(f"[V10-ATM] Mismatch STARTED: chain_atm={self._chain_atm} calculated={calculated_atm}")

            elapsed = now - self._atm_mismatch_since

            # Log every 10 seconds while waiting
            if not hasattr(self, '_last_mismatch_log'): self._last_mismatch_log = 0
            if now - self._last_mismatch_log >= 10:
                logger.info(f"[V10-ATM] Mismatch ongoing: chain={self._chain_atm} calc={calculated_atm} elapsed={elapsed:.0f}s / 60s")
                self._last_mismatch_log = now

            # Check if mismatch persisted for 60 seconds
            if elapsed >= 60:
                logger.warning(f"[V10-ATM] 60s reached! chain={self._chain_atm} → new={calculated_atm}. Setting regeneration_needed=True")
                self._regeneration_needed = True

            self.update_option_tags()
            return

        # 2. Handle Option Update
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']
            strike = strike_info['strike']

            # Debug: log first option quote received (only once)
            if not getattr(self, '_first_option_quote_logged', False):
                logger.info(f"[V10] First option quote received: {symbol} | LTP: {data.get('ltp')} | OI: {data.get('oi')}")
                self._first_option_quote_logged = True

            # Map quote fields robustly
            quote_data = {
                'ltp': float(data.get('ltp') or data.get('last_price', 0) or 0),
                'bid': float(data.get('bid_price') or data.get('bid', 0) or 0),
                'ask': float(data.get('ask_price') or data.get('ask', 0) or 0),
                'bid_qty': int(data.get('bid_size') or data.get('buy_quantity', 0) or 0),
                'ask_qty': int(data.get('ask_size') or data.get('sell_quantity', 0) or 0),
                'volume': int(data.get('volume') or data.get('v') or data.get('v_24h', 0) or 0),
                'oi': int(data.get('oi') or data.get('o') or data.get('oi_change', 0) or 0),
                'open': float(data.get('open', 0) or 0),
                'high': float(data.get('high', 0) or 0),
                'low': float(data.get('low', 0) or 0),
                'close': float(data.get('close', 0) or 0),
                'avg_price': float(data.get('average_price') or data.get('avg_price') or data.get('atp', 0) or 0)
            }

            if quote_data['bid'] > 0 and quote_data['ask'] > 0:
                quote_data['spread'] = quote_data['ask'] - quote_data['bid']
            
            self.update_option_depth(strike, option_type, quote_data)

    def update_option_depth(self, strike, option_type, depth_data):
        """Update option chain with data (NOW THREAD SAFE)"""
        if strike in self.option_data:
            target = 'ce_data' if option_type == 'CE' else 'pe_data'
            with self.data_lock:
                current_data = self.option_data[strike][target]
                for key, value in depth_data.items():
                    if key in ['volume', 'oi', 'avg_price'] and value == 0 and current_data.get(key, 0) > 0:
                         continue
                    current_data[key] = value

    def _expand_strikes_if_needed(self):
        """Add new strikes at edges when ATM shifts 2+ steps.

        Ensures we always have at least 6 ITM and 4 OTM strikes around current ATM.
        Removes strikes that are now very far (>8 steps) from ATM to keep chain tight.
        Subscribes to new strikes via WebSocket.
        """
        if not self.atm_strike:
            return

        # Target range: ATM-6 to ATM+4
        target_min = self.atm_strike - (6 * self.strike_step)
        target_max = self.atm_strike + (4 * self.strike_step)

        new_strikes_added = []

        # Add missing strikes in range
        strike = target_min
        while strike <= target_max:
            if strike not in self.option_data:
                position = self.get_strike_position(strike)
                tag = self.get_position_tag(position)
                self.option_data[strike] = {
                    'strike': strike, 'tag': tag,
                    'ce_symbol': self.construct_option_symbol(strike, 'CE'),
                    'pe_symbol': self.construct_option_symbol(strike, 'PE'),
                    'is_gamma_wall': False,
                    'ce_data': {
                        'oi': 0, 'volume': 0, 'ltp': 0, 'iv': 0, 'delta': 0.5,
                        'gamma': 0, 'theta': 0, 'vega': 0, 'gex': 0, 'spread': 0,
                        'oi_intensity': 0, 'oi_sentiment': 'NEUTRAL',
                        'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0
                    },
                    'pe_data': {
                        'oi': 0, 'volume': 0, 'ltp': 0, 'iv': 0, 'delta': -0.5,
                        'gamma': 0, 'theta': 0, 'vega': 0, 'gex': 0, 'spread': 0,
                        'oi_intensity': 0, 'oi_sentiment': 'NEUTRAL',
                        'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0,
                        'iv_status': 'normal'
                    }
                }
                self.subscription_map[self.option_data[strike]['ce_symbol']] = {'strike': strike, 'type': 'CE'}
                self.subscription_map[self.option_data[strike]['pe_symbol']] = {'strike': strike, 'type': 'PE'}
                new_strikes_added.append(strike)
            strike += self.strike_step

        # Remove strikes too far from ATM (>8 steps)
        far_limit = 8 * self.strike_step
        stale_strikes = [s for s in self.option_data.keys()
                         if abs(s - self.atm_strike) > far_limit]
        for s in stale_strikes:
            data = self.option_data.pop(s, None)
            if data:
                self.subscription_map.pop(data.get('ce_symbol', ''), None)
                self.subscription_map.pop(data.get('pe_symbol', ''), None)

        # Subscribe to new strikes
        if new_strikes_added and self.websocket_manager:
            instruments = []
            for s in new_strikes_added:
                if s in self.option_data:
                    instruments.append({'symbol': self.option_data[s]['ce_symbol'], 'exchange': 'NFO'})
                    instruments.append({'symbol': self.option_data[s]['pe_symbol'], 'exchange': 'NFO'})
            if instruments:
                self.websocket_manager.subscribe_batch(instruments, mode='quote')
                logger.info(f"[V10] Expanded chain: +{len(new_strikes_added)} strikes, -{len(stale_strikes)} stale. New range: {target_min}-{target_max}")

    def _do_chain_regeneration(self):
        """Full option chain regeneration around current ATM strike."""
        old_strikes = sorted(self.option_data.keys())
        range_str = f"{old_strikes[0]}-{old_strikes[-1]}" if old_strikes else "empty"
        logger.warning(f"[V10] Regenerating chain: ATM {self.atm_strike} | Old range: {range_str}")
        print(f"[V10] Option chain regenerating: ATM {self.atm_strike} | Old range: {range_str}")

        self.option_data.clear()
        self.subscription_map.clear()
        self.initial_state.clear()
        self.generate_strikes()
        if self.websocket_manager:
            self.batch_subscribe_options()

        self._chain_atm = self.atm_strike
        self._atm_mismatch_since = 0
        self._regeneration_needed = False
        self._last_resub_time = time.time()

    def update_option_tags(self):
        """Update option tags when ATM changes"""
        for strike_data in self.option_data.values():
            strike = strike_data['strike']
            position = self.get_strike_position(strike)
            strike_data['position'] = position
            strike_data['tag'] = self.get_position_tag(position)

    def get_strike_position(self, strike):
        if not self.atm_strike: return 0
        return (strike - self.atm_strike) // self.strike_step

    def get_position_tag(self, position):
        if position == 0: return 'ATM'
        elif position > 0: return f'OTM{abs(position)}'
        else: return f'ITM{abs(position)}'

    def _select_best_strike(self, bias):
        # Simple selection: ATM for now, can be sophisticated later
        if self.atm_strike in self.option_data:
            opt_type = 'CE' if bias == 'BULLISH' else 'PE'
            data = self.option_data[self.atm_strike]['ce_data'] if opt_type == 'CE' else self.option_data[self.atm_strike]['pe_data']
            return {
                'strike': self.atm_strike,
                'name': self.option_data[self.atm_strike][f'{opt_type.lower()}_symbol'],
                'ltp': data.get('ltp', 0),
                'type': opt_type,
                'iv': data.get('iv', 0),
                'delta': data.get('delta', 0)
            }
        return None

    def get_option_chain(self):
        # Return data structure for UI
        # CRITICAL: Only hold data_lock for the quick in-memory snapshot.
        # CSV reads (get_paper_trade_summary) happen OUTSIDE the lock
        # to prevent blocking WebSocket, engine, and Greek threads.
        with self.data_lock:
            # Calculate aggregates
            total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
            total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
            total_ce_vol = sum(opt['ce_data'].get('volume', 0) for opt in self.option_data.values())
            total_pe_vol = sum(opt['pe_data'].get('volume', 0) for opt in self.option_data.values())

            metrics = {}
            if hasattr(self, 'latest_signals'):
                 metrics = {
                     'quant_signal': self.latest_signals,
                     'pcr': self.pcr_history[-1] if self.pcr_history else 0,
                     'max_pain': self.latest_signals.get('max_pain', 0),
                     'total_ce_oi': total_ce_oi,
                     'total_pe_oi': total_pe_oi,
                     'total_ce_volume': total_ce_vol,
                     'total_pe_volume': total_pe_vol
                 }
            else:
                 # Fallback if engine hasn't run yet
                 metrics = {
                     'quant_signal': {'score': 0, 'action': 'WAIT', 'regime': 'Initializing'},
                     'pcr': 0,
                     'total_ce_oi': total_ce_oi,
                     'total_pe_oi': total_pe_oi,
                     'total_ce_volume': total_ce_vol,
                     'total_pe_volume': total_pe_vol
                 }

            # Snapshot in-memory data under lock (fast)
            options_snapshot = [dict(v) for v in self.option_data.values()]
            underlying_ltp = self.underlying_ltp
            underlying_open = getattr(self, 'underlying_open', 0.0)
            underlying_high = getattr(self, 'underlying_high', 0.0)
            underlying_low = getattr(self, 'underlying_low', 0.0)
            underlying_close = getattr(self, 'underlying_close', 0.0)
            underlying_avg = getattr(self, 'underlying_avg', 0.0)
            underlying_bid = getattr(self, 'underlying_bid', 0.0)
            underlying_ask = getattr(self, 'underlying_ask', 0.0)
            atm_strike = self.atm_strike
            regime = self.guardian.current_regime

        # OUTSIDE lock: Strategy info and CSV reads (slow, but won't block other threads)
        strategies_info = [
            {
                'name': s.name,
                'active_trade': s.active_trade,
                'daily_pnl': s.daily_pnl,
                'trades_today': s.trades_today
            } for s in self.strategies
        ]
        paper_summary = self.get_paper_trade_summary()

        result = {
            'underlying': self.underlying,
            'underlying_ltp': underlying_ltp,
            'underlying_open': underlying_open,
            'underlying_high': underlying_high,
            'underlying_low': underlying_low,
            'underlying_close': underlying_close,
            'underlying_avg': underlying_avg,
            'underlying_bid': underlying_bid,
            'underlying_ask': underlying_ask,
            'atm_strike': atm_strike,
            'expiry': self.expiry,
            'options': options_snapshot,
            'mode': 'V10_MULTI_STRAT',
            'regime': regime,
            'market_metrics': metrics,
            'strategies': strategies_info,
            'paper_trade_summary': paper_summary,
            'chain_atm': self._chain_atm,
            'atm_mismatch_since': self._atm_mismatch_since,
            'regeneration_needed': self._regeneration_needed,
        }

        # Auto-reset after signaling regeneration — ensures only ONE refresh
        if self._regeneration_needed:
            logger.info(f"[V10-ATM] Regeneration signaled, resetting state. New chain_atm={self.atm_strike}")
            self._regeneration_needed = False
            self._chain_atm = self.atm_strike
            self._atm_mismatch_since = 0

        return result

    def get_paper_trade_summary(self):
        """Aggregate summary from all strategies"""
        combined = {
            'total_trades': 0,
            'total_pnl': 0,
            'strategies': {}
        }
        for s in self.strategies:
            summary = s.logger.get_performance_summary()
            combined['strategies'][s.name] = summary
            combined['total_trades'] += summary.get('total_trades', 0)
            combined['total_pnl'] += summary.get('total_pnl', 0)
            
        return combined

