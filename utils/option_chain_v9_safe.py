"""
Option Chain Manager v9 - SAFE ADAPTIVE GUARDIAN
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
from datetime import datetime
from collections import deque
import statistics
import os

from utils.option_chain_v7_adaptive import OptionChainManagerV7Adaptive
from utils.trade_logger_v9 import TradeLoggerV9

logger = logging.getLogger(__name__)

# SignalSmoother removed in favor of TradeLoggerV9 Aggregator

class RegimeGuardian:
    """Enforces Inertia: Prevents rapid regime switching"""
    def __init__(self, lock_duration=900): # 15 minutes default
        self.current_regime = 'NEUTRAL'
        self.lock_duration = lock_duration
        self.last_change_time = 0
        self.locked = False
        
    def update(self, detected_regime, confidence):
        now = time.time()
        
        # If locked and time hasn't passed, ignore changes unless CATASTROPHIC reversal
        if self.locked:
            if now - self.last_change_time < self.lock_duration:
                # Exception: If we are BULLISH but detect severe BEARISH crash (-80 score), unlock immediately
                # This logic is handled by the caller or we can add it here if we pass score
                return self.current_regime
            
            # Lock expired
            self.locked = False
            
        if detected_regime != self.current_regime:
            # Only change if confidence is high enough to justify breaking inertia
            if confidence > 60:
                logger.info(f"[GUARDIAN] Regime Shift: {self.current_regime} -> {detected_regime} (Locked for {self.lock_duration/60}m)")
                self.current_regime = detected_regime
                self.last_change_time = now
                self.locked = True
                
        return self.current_regime

class OptionChainManagerV9Safe(OptionChainManagerV7Adaptive):
    """
    V9 SAFE Manager - The Autonomous Guardian
    Inherits data fetching from V7, but completely overrides decision logic.
    """
    
    def __init__(self, underlying, expiry, websocket_manager=None):
        super().__init__(underlying, expiry, websocket_manager)
        self.manager_id = f"{underlying}_{expiry}_V9_SAFE"
        
        # 1. AUTONOMY ENGINE
        self.engine_running = False
        self.engine_thread = None
        self.state_file = f"logs/v9_state_{underlying}.json"
        
        # 2. CALM LOGIC
        self.logger = TradeLoggerV9() # Handles Signal Aggregation (180s) & Logging
        self.guardian = RegimeGuardian(lock_duration=900) # 15 mins
        
        # 3. SAFETY FILTERS
        self.daily_loss_limit = -2.0 # % of capital (conceptual, tracking points here)
        self.max_trades_per_day = 5
        self.trades_today = 0
        self.daily_pnl_points = 0
        self.safety_lock = False # Locks system if daily limit hit
        
        # 4. STARTUP WARMUP - Prevent immediate trades after restart
        self.startup_time = time.time()
        self.min_warmup_seconds = 180  # 3 minutes warmup before first trade
        
        # 5. EXIT COOLDOWN - Prevent rapid re-entry after exit
        self.exit_cooldown_until = 0
        self.exit_cooldown_seconds = 60  # 60 seconds cooldown after each exit
        
        # 4. TRADE STATE (Persistent)
        self.active_trade = None # Dict storing current trade
        self.load_state()
        
        logger.info(f"Initialized V9 SAFE Guardian for {underlying}")
        
    def start_monitoring(self):
        """Override to start the Background Engine"""
        super().start_monitoring() # Starts V7 Greek loop
        self.start_engine()
        
    def start_engine(self):
        if self.engine_running:
            return
        self.engine_running = True
        self.engine_thread = threading.Thread(target=self._trade_engine_loop)
        self.engine_thread.daemon = True
        self.engine_thread.start()
        print(f"\n{'='*60}\n🟢 [V9 SAFETY GUARDIAN] ENGINE STARTED for {self.underlying}\n{'='*60}\n")
        logger.info(f"[V9 ENGINE] 🟢 Background Trading Engine Started for {self.underlying}")

    def stop_monitoring(self):
        super().stop_monitoring()
        self.engine_running = False

    def update_websocket_manager(self, new_ws_manager):
        """Update the websocket manager reference and resubscribe"""
        if not new_ws_manager or self.websocket_manager == new_ws_manager:
            return
            
        logger.info(f"[V9] Updating WebSocket manager reference for {self.underlying}")
        
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
        
    def _trade_engine_loop(self):
        """
        The Heartbeat of V9. Runs every 1 second.
        Independent of UI.
        """
        logger.info("[V9 ENGINE] Loop active...")
        while self.engine_running:
            try:
                # 1. Ensure Data is Fresh
                if not self.option_data or self.atm_strike == 0:
                    if int(time.time()) % 5 == 0:
                        print(f"⚠️ [V9 GUARDIAN] Waiting for Option Data... (LTP: {self.underlying_ltp})")
                    time.sleep(1)
                    continue
                    
                # 2. Calculate Metrics (Internal, no UI needed)
                metrics = self.calculate_market_metrics_internal()
                
                # V9 HEARTBEAT LOG (every 5 seconds)
                if int(time.time()) % 5 == 0:
                    raw_score = metrics.get('raw_score', 0)
                    print(f"\n{'='*70}")
                    print(f"🛡️ [V9 GUARDIAN] {self.underlying} @ {self.underlying_ltp:.2f} | ATM: {self.atm_strike}")
                    print(f"   Score: {raw_score:+d} | Regime: {self.guardian.current_regime} | Locked: {self.guardian.locked}")
                    print(f"   Active Trade: {'YES - ' + str(self.active_trade.get('contract', '')) if self.active_trade else 'NONE'}")
                    print(f"   Safety Lock: {self.safety_lock} | Trades Today: {self.trades_today}")
                    print(f"{'='*70}\n")
                
                # 3. Manage Active Trade (Commitment)
                if self.active_trade:
                    self._manage_active_trade(metrics)
                
                # 4. Scan for New Trades (If safe)
                elif not self.safety_lock:
                    self._scan_for_entry_opportunities(metrics)
                
                # 5. Persist State
                if int(time.time()) % 10 == 0: # Save every 10s
                    self.save_state()
                    
                time.sleep(1) # 1Hz heartbeat
                
            except Exception as e:
                logger.error(f"[V9 ENGINE] Error in loop: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5) # Backoff on error

    def calculate_market_metrics_internal(self):
        """Simplified metric calculation for internal engine use"""
        # Reuse V7 logic but strip UI formatting
        pcr = 0
        total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        if total_ce_oi > 0:
            pcr = total_pe_oi / total_ce_oi
            
        # Get Scores directly from V7 methods
        score_dict = {
            'delta_oi': self._score_delta_oi()[0],
            'iv_skew': self._score_iv_skew()[0],
            'oi_unwind': self._score_oi_unwind()[0],
            'max_pain': self._score_max_pain()[0],
            'gamma': self._score_gamma_exposure()[0],
            'vanna': self._score_vanna_charm()[0],
            'inst_flow': self._detect_institutional_flow()[0],
            'momentum': self._score_momentum_velocity()[0],
            'pcr_roc': self._score_pcr_roc()[0]
        }
        
        # Calculate Signals (Instantaneous)
        # TODO: Refactor V7 to expose a clean 'get_total_score' without side effects
        # For now, manually aggregating
        total_score = sum(score_dict.values())
        
        # NOTE: We do NOT smooth here anymore. We pass raw score to Logger which aggregates.
        
        return {
            'raw_score': total_score,
            'pcr': pcr,
            'ltp': self.underlying_ltp,
            # Pass all score details for logger context if needed
            'score_details': score_dict
        }

    def _manage_active_trade(self, metrics):
        """
        Manages an open trade with COMMITMENT.
        Ignores noise. Only exits on SL/Target/Major Reversal.
        """
        trade = self.active_trade
        # Ensure logger knows about active trade for console display
        self.logger.active_trade = trade
        
        current_ltp = 0
        
        # Get Current Price of Option
        try:
            strike = trade['strike']
            opt_type = trade['type']
            tag = 'ce_data' if opt_type == 'CE' else 'pe_data'
            current_ltp = self.option_data[strike][tag]['ltp']
        except KeyError:
            return # Data momentarily missing
            
        if current_ltp <= 0: return

        # Check Hard Exit Levels
        exit_reason = None
        
        if trade['direction'] == 'BUY':
            if current_ltp <= trade['sl']:
                exit_reason = "SL HIT"
            elif current_ltp >= trade['target']:
                exit_reason = "TARGET HIT"
        else: # SELL
            if current_ltp >= trade['sl']:
                exit_reason = "SL HIT"
            elif current_ltp <= trade['target']:
                exit_reason = "TARGET HIT"
                
        # Check Trailing Stop (ATR logic simplified to percent for now)
        # If we are 15% in profit, move SL to cost
        entry = trade['entry_price']
        profit_pct = (current_ltp - entry) / entry * 100 if trade['direction'] == 'BUY' else (entry - current_ltp) / entry * 100
        
        if profit_pct > 15 and not trade.get('sl_moved_to_cost'):
            trade['sl'] = entry
            trade['sl_moved_to_cost'] = True
            logger.info(f"[V9 TRADE] 🛡️ Moving SL to Cost {entry} (Profit {profit_pct:.1f}%)")
            
        # Execute Exit
        if exit_reason:
            self._close_trade(trade, current_ltp, exit_reason)
        else:
            # Check for Signal Exit via Logger
            # Create a mock signal data packet for the logger to check exit conditions
            # We map V9 metrics to what Logger expects
            signal_data = {
                'action': 'WAIT', # Default, will be filled by metrics score
                'score': metrics['raw_score'],
                'regime': self.guardian.current_regime
            }
            
            # Use Logger's sophisticated exit logic (Majority Vote, Signal Flip)
            # We need to bridge the gap: Logger expects to manage the trade, but here V9 manages it.
            # We will use the V3 Logic's helper: _check_signal_exit
            
            try:
                # Determine "Action" from raw score just for the logger's aggregator feed
                raw_score = metrics['raw_score']
                temp_action = 'WAIT'
                if raw_score > 20: temp_action = 'BUY CALL'
                elif raw_score < -20: temp_action = 'BUY PUT'
                
                signal_data['action'] = temp_action
                
                exit_signal = self.logger._check_signal_exit(signal_data, trade)
                
                # LIVE CONSOLE LOGGING (Active Trade Status)
                try:
                    self.logger._log_signal_status(
                        temp_action, 
                        raw_score, 
                        self.guardian.current_regime, 
                        self.underlying_ltp, 
                        self.underlying
                    )
                except Exception as log_e:
                    logger.error(f"[V9 LOGGING] Active trade log failed: {log_e}")
                
                if exit_signal:
                     self._close_trade(trade, current_ltp, f"SIGNAL: {exit_signal}")
                     
            except Exception as e:
                logger.error(f"[V9 EXIT CHECK] Error: {e}")

    def _close_trade(self, trade, exit_price, reason):
        pnl = (exit_price - trade['entry_price']) if trade['direction'] == 'BUY' else (trade['entry_price'] - exit_price)
        self.daily_pnl_points += pnl
        self.trades_today += 1
        
        logger.info(f"[V9 TRADE] 🔴 EXIT {trade['contract']} @ {exit_price} | {reason} | PnL: {pnl:.2f}")
        
        # Log to CSV via TradeLoggerV9
        self.logger._log_exit_event(trade, reason, exit_price, pnl, 0, 0) # Console log
        
        # Create full log packet for CSV
        log_entry = {
            'Trade_ID': trade.get('id', 'unknown'),
            'Status': 'EXIT',
            'Symbol': self.underlying,
            'Contract': trade['contract'],
            'Strike': trade.get('strike', ''),
            'Type': trade.get('type', ''),
            'Direction': trade.get('direction', ''),
            'Strategy': trade.get('Strategy', 'V9_SAFE'),
            'Regime': self.guardian.current_regime,
            'Entry_Price': trade['entry_price'],
            'Exit_Price': exit_price,
            'Quantity': trade.get('Quantity', 1),
            'Lot_Size': trade.get('Lot_Size', 50),
            'Target': trade.get('target', 0),
            'StopLoss': trade.get('sl', 0),
            'Underlying_LTP': self.underlying_ltp,
            'PnL_Points': pnl,
            'PnL_Amount': pnl * trade.get('Lot_Size', 50) * trade.get('Quantity', 1),
            'ROI_Pct': (pnl / trade['entry_price'] * 100) if trade['entry_price'] > 0 else 0,
            'Reason': reason,
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        self.logger.log_trade(log_entry)
        
        # Clear logger active trade so it stops showing "ACTIVE" status
        self.logger.active_trade = None
        self.active_trade = None
        
        # Set exit cooldown to prevent rapid re-entry
        self.exit_cooldown_until = time.time() + self.exit_cooldown_seconds
        print(f"⏸️ [V9 GUARDIAN] Cooldown started: {self.exit_cooldown_seconds}s before next entry allowed")
        
        self.save_state()
        
        # Daily Safety Check
        if self.trades_today >= self.max_trades_per_day:
            self.safety_lock = True
            logger.warning("[V9 GUARDIAN] 🔒 Max Trades Reached. System Locked for Day.")

    def _scan_for_entry_opportunities(self, metrics):
        """
        Scans for entries using TRADE LOGGER V9 AGGREGATION.
        """
        raw_score = metrics['raw_score']
        
        # WARMUP CHECK - Don't trade for first 3 minutes after startup
        warmup_remaining = self.min_warmup_seconds - (time.time() - self.startup_time)
        if warmup_remaining > 0:
            if int(time.time()) % 10 == 0:  # Log every 10 seconds
                print(f"⏳ [V9 WARMUP] {warmup_remaining:.0f}s remaining before trading enabled...")
            return
        
        # EXIT COOLDOWN CHECK - Don't re-enter immediately after exit
        if time.time() < self.exit_cooldown_until:
            cooldown_remaining = self.exit_cooldown_until - time.time()
            if int(time.time()) % 10 == 0:
                print(f"⏸️ [V9 COOLDOWN] {cooldown_remaining:.0f}s remaining after last exit...")
            return
        
        # DEBUG: Confirm V9 is scanning
        if int(time.time()) % 5 == 0:
            print(f"🔍 [V9 SCAN] Score: {raw_score:+d} | Checking entry conditions...")
        
        # 1. Determine Instantaneous Action (Input to Aggregator)
        # Using lighter threshold for input, Aggregator handles the filtering
        instant_action = 'WAIT'
        if raw_score > 20: instant_action = 'BUY CALL'
        elif raw_score < -20: instant_action = 'BUY PUT'
        
        # 2. Feed Aggregator
        # We feed the raw data. The logger maintains the 3-minute window logic.
        self.logger.entry_aggregator.add_signal(
            action=instant_action,
            score=raw_score,
            regime=self.guardian.current_regime
        )
        
        # LIVE CONSOLE LOGGING
        try:
            self.logger._log_signal_status(
                instant_action, 
                raw_score, 
                self.guardian.current_regime, 
                self.underlying_ltp, 
                self.underlying
            )
        except Exception as e:
            logger.error(f"[V9 LOGGING] Console log failed: {e}")
        
        # 3. Ask Aggregator for Majority Vote
        majority_action, confidence, sample_count, avg_score, _ = self.logger.entry_aggregator.get_majority_signal()
        
        # 4. Check V9 Strict Entry Conditions
        if majority_action == 'WAIT': return
        if confidence < 60: return # Require 60% agreement over 3 mins
        if sample_count < 60: return # Ensure enough data points (60 signals = ~1 minute)
        
        # 5. Check Regime Guardian (Inertia)
        # We only trade if the Guardian agrees or is UNLOCKED
        guardian_regime = self.guardian.update(
            'BULLISH' if avg_score > 0 else 'BEARISH', 
            confidence
        )
        
        # Conflict Check: Don't buy Calls if Guardian says BEARISH
        if 'CALL' in majority_action and guardian_regime == 'BEARISH':
            return
        if 'PUT' in majority_action and guardian_regime == 'BULLISH':
            return

        # 6. Daily Safety Check
        if self.safety_lock:
            return

        # 7. Execute
        self._execute_entry(majority_action, avg_score)



    def _execute_entry(self, action, score):
        # Select best strike (Clone logic from V7 or simplify)
        bias = 'BULLISH' if 'CALL' in action else 'BEARISH'
        contract_info = self._select_best_strike(bias)
        
        if not contract_info: return
        
        ltp = contract_info['ltp']
        if ltp <= 0: return

        # Calculate SAFE Levels (Fixed Risk Reward 1:2)
        # For BUY (long options): SL below entry, Target above entry
        # For SELL (short options): SL above entry, Target below entry
        sl_pts = ltp * 0.15 # 15% SL
        target_pts = ltp * 0.30 # 30% Target
        
        # V9 always BUYS options, so SL is always below entry, Target always above
        sl = ltp - sl_pts
        target = ltp + target_pts
        
        self.active_trade = {
            'id': f"V9_{int(time.time())}",
            'contract': contract_info['name'],
            'strike': contract_info['strike'],
            'type': contract_info['type'], # CE/PE
            'direction': 'BUY', # Always buying options for now
            'entry_price': ltp,
            'sl': sl,
            'target': target,
            'entry_time': datetime.now().isoformat(),
            'sl_moved_to_cost': False,
            # Add logger compatible fields for exit logic
            'Trade_ID': f"V9_{int(time.time())}",
            'Symbol': self.underlying,
            'Contract': contract_info['name'],
            'Strike': contract_info['strike'],
            'Type': contract_info['type'],
            'Direction': 'BUY',
            'Strategy': 'V9_SAFE',
            'Entry_Price': ltp,
            'Quantity': 1,
            'Lot_Size': 50, # Default, updated safely below
            'Target': target,
            'StopLoss': sl,
            'Entry_Underlying_LTP': self.underlying_ltp,
            'Max_Price_Seen': ltp,
            'Min_Price_Seen': ltp
        }
        
        # Safely get Lot Size
        try:
            self.active_trade['Lot_Size'] = self.logger._get_lot_size(self.underlying)
        except:
             self.active_trade['Lot_Size'] = 50
        
        # Sync with Logger for Console Status
        self.logger.active_trade = self.active_trade
        
        logger.info(f"[V9 TRADE] 🟢 ENTRY {action} on {contract_info['name']} @ {ltp} | Score: {score:.1f}")
        
        # Log Entry to CSV - Fully Populated
        # Get distribution stats if available
        dist_str = "N/A"
        confidence_val = 0
        try:
            distribution = self.logger.entry_aggregator.get_signal_distribution()
            dist_str = str(distribution)
            _, confidence_val, _, _, _ = self.logger.entry_aggregator.get_majority_signal()
        except:
            pass

        log_entry = {
            'Trade_ID': self.active_trade['Trade_ID'],
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Status': 'ENTRY',
            'Symbol': self.underlying,
            'Contract': contract_info['name'],
            'Strike': contract_info['strike'],
            'Type': contract_info['type'],
            'Direction': 'BUY',
            'Strategy': 'V9_SAFE',
            'Regime': self.guardian.current_regime,
            'Entry_Price': ltp,
            'Exit_Price': '',
            'Quantity': 1,
            'Lot_Size': self.active_trade.get('Lot_Size', 50),
            'Target': target,
            'StopLoss': sl,
            'Underlying_LTP': self.underlying_ltp,
            'PnL_Points': '',
            'PnL_Amount': '',
            'ROI_Pct': '',
            'Score': score,
            'Confidence': confidence_val,
            'Reason': f"Majority Vote: {action}",
            'Signal_Distribution': dist_str,
            'Entry_Confidence': confidence_val,
            'Snapshot_PCR': 0, # TODO: Pass metrics
            'Snapshot_IV': contract_info.get('iv', 0),
            'Entry_Delta': contract_info.get('delta', 0),
            'Entry_Gamma': contract_info.get('gamma', 0),
            'Entry_Theta': contract_info.get('theta', 0)
        }
        self.logger.log_trade(log_entry)
        
        self.save_state()

    def save_state(self):
        state = {
            'active_trade': self.active_trade,
            'trades_today': self.trades_today,
            'daily_pnl': self.daily_pnl_points,
            'safety_lock': self.safety_lock
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.active_trade = state.get('active_trade')
                    self.trades_today = state.get('trades_today', 0)
                    self.daily_pnl_points = state.get('daily_pnl', 0)
                    self.safety_lock = state.get('safety_lock', False)
                    logger.info(f"Loaded V9 State. Active Trade: {self.active_trade is not None}")
            except Exception as e:
                logger.error(f"Error loading state: {e}")
