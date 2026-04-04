"""
Option Chain Manager v8 - ANTI-WHIPSAW Edition
Built on v7 Adaptive with enhanced entry/exit logic to reduce false signals

KEY IMPROVEMENTS:
1. Stricter entry confirmation (15s + score threshold 40)
2. Hysteresis bands (different thresholds for entry vs hold)
3. Neutral signals do NOT trigger exit
4. Trailing stop with activation threshold
5. Signal quality tracking for post-trade analysis
"""

import json
import threading
import time
from datetime import datetime, timedelta
from collections import deque, Counter
from typing import Dict, List, Optional, Any
import logging
from cachetools import TTLCache
import pytz
from utils.trade_logger_v2 import TradeLoggerV2
import uuid
import statistics

logger = logging.getLogger(__name__)


class OptionChainCacheV8:
    """Zero-config cache for option chain data"""

    def __init__(self, maxsize=100, ttl=30):
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.cache.get(key)

    def set(self, key, value):
        with self.lock:
            self.cache[key] = value


class PaperTradeStateV2:
    """
    Enhanced trade state tracking with anti-whipsaw metrics
    """
    def __init__(self, trade_data: dict):
        # Core trade data
        self.trade_id = trade_data['Trade_ID']
        self.entry_time = datetime.now()
        self.symbol = trade_data['Symbol']
        self.contract = trade_data['Contract']
        self.strike = trade_data['Strike']
        self.option_type = trade_data['Type']  # CE/PE
        self.direction = trade_data.get('Direction', 'BUY')
        self.strategy = trade_data['Strategy']
        self.entry_price = trade_data['Price']
        self.quantity = trade_data.get('Quantity', 1)
        self.lot_size = trade_data.get('Lot_Size', 50)

        # Levels
        self.target = trade_data['Target']
        self.stoploss = trade_data['StopLoss']
        self.trailing_sl = trade_data['StopLoss']  # Starts at SL, moves up
        self.trailing_activated = False

        # Context at entry
        self.underlying_ltp = trade_data.get('Underlying_LTP', 0)
        self.entry_score = trade_data.get('Entry_Score', 0)
        self.entry_regime = trade_data.get('Regime', 'UNKNOWN')
        self.entry_confidence = trade_data.get('Confidence', 0)
        self.signal_confirm_time = trade_data.get('Signal_Confirm_Time', 0)

        # Greeks at entry
        self.entry_delta = trade_data.get('Entry_Delta', 0)
        self.entry_gamma = trade_data.get('Entry_Gamma', 0)
        self.entry_theta = trade_data.get('Entry_Theta', 0)
        self.entry_vega = trade_data.get('Entry_Vega', 0)

        # Tracking during trade
        self.max_price = self.entry_price
        self.min_price = self.entry_price
        self.score_history = [self.entry_score]
        self.regime_history = [self.entry_regime]
        self.opposite_signal_count = 0
        self.neutral_signal_count = 0

        # Signal history before entry (for analysis)
        self.pre_entry_signals = trade_data.get('Signal_History', [])

    def update_price(self, current_price):
        """Update max/min price tracking"""
        if current_price > self.max_price:
            self.max_price = current_price
        if current_price < self.min_price:
            self.min_price = current_price

    def update_score(self, score):
        """Track score during trade"""
        self.score_history.append(score)

    def update_regime(self, regime):
        """Track regime changes"""
        if regime != self.regime_history[-1]:
            self.regime_history.append(regime)

    def get_score_stats(self):
        """Calculate score statistics during trade"""
        if not self.score_history:
            return {'min': 0, 'max': 0, 'volatility': 0}

        return {
            'min': min(self.score_history),
            'max': max(self.score_history),
            'volatility': statistics.stdev(self.score_history) if len(self.score_history) > 1 else 0
        }

    def get_hold_duration(self):
        """Get trade duration in seconds"""
        return (datetime.now() - self.entry_time).total_seconds()

    def get_current_pnl_pct(self, current_price):
        """Calculate current ROI percentage"""
        if self.entry_price <= 0:
            return 0
        return ((current_price - self.entry_price) / self.entry_price) * 100

    def get_max_profit_pct(self):
        """Best ROI seen during trade"""
        if self.entry_price <= 0:
            return 0
        return ((self.max_price - self.entry_price) / self.entry_price) * 100

    def get_max_drawdown_pct(self):
        """Worst drawdown from entry"""
        if self.entry_price <= 0:
            return 0
        return ((self.entry_price - self.min_price) / self.entry_price) * 100


class OptionChainManagerV8AntiWhipsaw:
    """
    ANTI-WHIPSAW Manager - Designed to reduce false signals in volatile markets
    """

    def __init__(self, underlying, expiry, websocket_manager=None):
        """Initialize Anti-Whipsaw OptionChainManager"""
        self.underlying = underlying
        self.expiry = expiry

        # Strike step configuration
        if underlying == 'NIFTY':
            self.strike_step = 50
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
        self.cache = OptionChainCacheV8()
        self.monitoring_active = False
        self.initialized = False
        self.manager_id = f"{underlying}_{expiry}_V8_ANTIWHIPSAW"
        self.option_mode = 'quote'

        # QUANT ENGINE STATE
        self.initial_state = {}
        self.start_price = None
        self.price_history = deque(maxlen=600)
        self.atm_ce_history = deque(maxlen=600)
        self.atm_pe_history = deque(maxlen=600)
        self.pcr_history = deque(maxlen=600)
        self.oi_history = deque(maxlen=600)
        self.vol_history = deque(maxlen=600)
        self.iv_history = deque(maxlen=120)
        self.gex_history = deque(maxlen=120)
        self.action_history = deque(maxlen=20)  # Track recent actions
        self.score_history = deque(maxlen=60)   # Track recent scores
        self.trade_state = None
        self.days_to_expiry = 0
        self._backoff_until = 0
        self.manager_instance_id = id(self)

        # ADAPTIVE MODE
        self.strategy_mode = 'ADAPTIVE'
        self.current_regime = 'DETECTING'
        self.regime_confidence = 0

        # ============================================================
        # ANTI-WHIPSAW PAPER TRADING CONFIGURATION
        # ============================================================

        self.logger = TradeLoggerV2("logs/v8_paper_trades.csv")
        self.paper_trade_state: Optional[PaperTradeStateV2] = None
        self.paper_trading_enabled = True
        self.last_signal_action = 'WAIT'

        # ENTRY CONFIGURATION (Stricter)
        self.signal_confirm_time = 15           # Seconds signal must persist (was 10)
        self.min_score_threshold = 40           # Minimum |score| to enter (was 15)
        self.min_confidence_threshold = 60      # Minimum regime confidence to enter

        # HOLD CONFIGURATION (More lenient - hysteresis)
        self.hold_score_threshold = 20          # Lower threshold to stay in trade
        self.min_hold_time = 45                 # Minimum seconds before any signal-based exit

        # EXIT CONFIGURATION
        self.exit_signal_confirm_time = 10      # Seconds for opposite signal confirmation
        self.opposite_score_threshold = 30      # Minimum |score| for opposite signal to trigger exit
        self.ignore_neutral_signals = True      # WAIT signal does NOT trigger exit

        # COOLDOWN
        self.trade_cooldown_duration = 60       # Seconds after exit before new entry (was 30)
        self.trade_cooldown = 0

        # TRAILING STOP CONFIGURATION
        self.trailing_activation_pct = 10       # Activate trailing after 10% profit
        self.trailing_distance_pct = 5          # Trail 5% below peak
        self.move_sl_to_cost_at_pct = 8         # Move SL to cost at 8% profit

        # SIGNAL TRACKING
        self.pending_signal = None
        self.exit_signal_start = None
        self.pre_entry_signal_buffer = deque(maxlen=10)  # Store last 10 signals before entry

        # Signal reliability weights
        self.signal_weights = {
            'delta_oi': 1.5,
            'oi_unwind': 1.3,
            'pcr_roc': 1.2,
            'inst_flow': 1.2,
            'gamma': 1.0,
            'momentum': 1.0,
            'max_pain': 0.8,
            'iv_skew': 0.7,
            'vanna': 0.6,
        }

        # Available modes
        self.available_modes = [
            'ADAPTIVE',
            'MOMENTUM_RIDER',
            'THETA_HUNTER',
            'CONTRARIAN_REVERSAL',
            'INSTITUTIONAL_SHADOW'
        ]

        logger.info(f"Initialized ANTI-WHIPSAW OptionChainManagerV8 for {underlying}")
        logger.info(f"  Entry: {self.signal_confirm_time}s confirm, score >= {self.min_score_threshold}")
        logger.info(f"  Hold: score >= {self.hold_score_threshold}, min hold {self.min_hold_time}s")
        logger.info(f"  Exit: Ignore neutral={self.ignore_neutral_signals}, opposite confirm {self.exit_signal_confirm_time}s")

    def set_strategy_mode(self, mode):
        """Update strategy mode dynamically"""
        if mode in self.available_modes:
            old_mode = self.strategy_mode
            self.strategy_mode = mode
            logger.info(f"Strategy Mode: {old_mode} -> {mode} for {self.manager_id}")
            return True
        return False

    # ============================================================================
    # ANTI-WHIPSAW PAPER TRADING ENGINE
    # ============================================================================

    def _manage_paper_trade(self, total_score, regime_data, metrics):
        """
        ANTI-WHIPSAW Trade Management

        Key differences from v7:
        1. Stricter entry (15s confirm + score >= 40)
        2. Neutral signals IGNORED
        3. Opposite signals need score threshold + confirmation
        4. Trailing stop with activation
        5. Trade quality tracking
        """
        if not self.paper_trading_enabled:
            return

        current_time = datetime.now()
        current_action = regime_data.get('action', 'WAIT')
        current_regime = regime_data.get('regime', 'UNKNOWN')
        current_confidence = regime_data.get('confidence', 0)

        # Track signal history for analysis
        self.pre_entry_signal_buffer.append({
            'action': current_action,
            'score': total_score,
            'regime': current_regime,
            'timestamp': time.time()
        })

        # Store score history
        self.score_history.append(total_score)

        # Debug logging
        if current_action != self.last_signal_action:
            logger.info(f"[V8 PAPER] Signal: {self.last_signal_action} -> {current_action} | Score: {total_score} | Regime: {current_regime}")
            self.last_signal_action = current_action

        # ================================================================
        # 1. MONITOR ACTIVE TRADE
        # ================================================================
        if self.paper_trade_state:
            trade = self.paper_trade_state

            # Get current LTP
            current_ltp = 0
            if trade.strike in self.option_data:
                tag = 'ce_data' if trade.option_type == 'CE' else 'pe_data'
                current_ltp = self.option_data[trade.strike][tag].get('ltp', 0)

            if current_ltp <= 0:
                return

            # Update trade tracking
            trade.update_price(current_ltp)
            trade.update_score(total_score)
            trade.update_regime(current_regime)

            hold_duration = trade.get_hold_duration()
            current_pnl_pct = trade.get_current_pnl_pct(current_ltp)

            # ============================================================
            # EXIT LOGIC (Priority Order)
            # ============================================================
            exit_reason = None
            exit_price = current_ltp

            # A. HARD STOPLOSS - Immediate, non-negotiable
            if current_ltp <= trade.stoploss:
                exit_reason = "SL_HIT"
                exit_price = trade.stoploss
                logger.info(f"[V8 PAPER] STOPLOSS HIT @ {exit_price}")

            # B. TARGET HIT - Immediate
            elif current_ltp >= trade.target:
                exit_reason = "TARGET_HIT"
                exit_price = trade.target
                logger.info(f"[V8 PAPER] TARGET HIT @ {exit_price}")

            # C. TRAILING STOP HIT
            elif trade.trailing_activated and current_ltp <= trade.trailing_sl:
                exit_reason = "TRAILING_SL_HIT"
                exit_price = trade.trailing_sl
                logger.info(f"[V8 PAPER] TRAILING SL HIT @ {exit_price}")

            # D. SIGNAL-BASED EXIT (Only after min_hold_time)
            elif hold_duration >= self.min_hold_time:
                is_signal_valid = self._is_signal_valid_for_trade(trade.option_type, current_action)

                if not is_signal_valid:
                    # Check if it's NEUTRAL or OPPOSITE
                    if 'WAIT' in current_action:
                        # NEUTRAL SIGNAL - IGNORE (key anti-whipsaw feature)
                        if self.ignore_neutral_signals:
                            trade.neutral_signal_count += 1
                            logger.debug(f"[V8 PAPER] Ignoring NEUTRAL signal (count: {trade.neutral_signal_count})")
                            # Reset exit timer since we're ignoring
                            self.exit_signal_start = None
                        else:
                            # Old behavior: exit on neutral
                            exit_reason = "SIGNAL_NEUTRAL"
                    else:
                        # OPPOSITE SIGNAL - Needs confirmation
                        trade.opposite_signal_count += 1

                        # Check if opposite signal is strong enough
                        opposite_score_ok = abs(total_score) >= self.opposite_score_threshold

                        if not opposite_score_ok:
                            logger.debug(f"[V8 PAPER] Opposite signal weak (|{total_score}| < {self.opposite_score_threshold})")
                            self.exit_signal_start = None
                        else:
                            # Start or continue confirmation timer
                            if self.exit_signal_start is None:
                                self.exit_signal_start = time.time()
                                logger.info(f"[V8 PAPER] Opposite signal detected ({current_action}). Confirming for {self.exit_signal_confirm_time}s...")

                            elif time.time() - self.exit_signal_start >= self.exit_signal_confirm_time:
                                exit_reason = f"SIGNAL_FLIP_{current_action.replace(' ', '_')}"
                                logger.info(f"[V8 PAPER] Signal flip confirmed after {self.exit_signal_confirm_time}s")
                            else:
                                remaining = self.exit_signal_confirm_time - (time.time() - self.exit_signal_start)
                                logger.debug(f"[V8 PAPER] Confirming flip: {remaining:.1f}s remaining")
                else:
                    # Signal still valid - reset exit timer
                    if self.exit_signal_start is not None:
                        logger.info(f"[V8 PAPER] Exit cancelled - signal valid again")
                    self.exit_signal_start = None

            # ============================================================
            # TRAILING STOP UPDATE (No exit triggered above)
            # ============================================================
            if not exit_reason:
                # Activate trailing stop
                if not trade.trailing_activated and current_pnl_pct >= self.trailing_activation_pct:
                    trade.trailing_activated = True
                    trade.trailing_sl = current_ltp * (1 - self.trailing_distance_pct / 100)
                    logger.info(f"[V8 PAPER] Trailing stop ACTIVATED @ {trade.trailing_sl:.2f}")

                # Update trailing stop (only moves up)
                elif trade.trailing_activated:
                    new_trailing = current_ltp * (1 - self.trailing_distance_pct / 100)
                    if new_trailing > trade.trailing_sl:
                        trade.trailing_sl = new_trailing
                        logger.debug(f"[V8 PAPER] Trailing SL updated to {trade.trailing_sl:.2f}")

                # Move SL to cost at threshold
                if current_pnl_pct >= self.move_sl_to_cost_at_pct:
                    if trade.stoploss < trade.entry_price:
                        trade.stoploss = trade.entry_price
                        logger.info(f"[V8 PAPER] SL moved to cost @ {trade.entry_price}")

            # ============================================================
            # EXECUTE EXIT
            # ============================================================
            if exit_reason:
                self._execute_exit(trade, exit_price, exit_reason, total_score, regime_data, metrics)
                return

        # ================================================================
        # 2. CHECK ENTRY CONDITIONS
        # ================================================================
        if not self.paper_trade_state and time.time() > self.trade_cooldown:
            if 'BUY' in current_action:
                # A. SCORE THRESHOLD CHECK
                if abs(total_score) < self.min_score_threshold:
                    if self.pending_signal:
                        logger.debug(f"[V8 PAPER] Score dropped below threshold, resetting pending signal")
                        self.pending_signal = None
                    return

                # B. CONFIDENCE CHECK
                if current_confidence < self.min_confidence_threshold:
                    logger.debug(f"[V8 PAPER] Confidence {current_confidence}% < {self.min_confidence_threshold}% threshold")
                    return

                # C. SIGNAL CONFIRMATION
                current_timestamp = time.time()

                if self.pending_signal is None or self.pending_signal.get('action') != current_action:
                    # New signal - start confirmation timer
                    self.pending_signal = {
                        'action': current_action,
                        'first_seen': current_timestamp,
                        'score': total_score,
                        'regime': current_regime
                    }
                    logger.info(f"[V8 PAPER] Signal confirmation started: {current_action} | Score: {total_score} | Need {self.signal_confirm_time}s")
                    return

                # Check confirmation duration
                signal_duration = current_timestamp - self.pending_signal['first_seen']
                if signal_duration < self.signal_confirm_time:
                    logger.debug(f"[V8 PAPER] Confirming: {signal_duration:.1f}s / {self.signal_confirm_time}s")
                    return

                # D. SIGNAL CONFIRMED - EXECUTE ENTRY
                logger.info(f"[V8 PAPER] Signal CONFIRMED after {signal_duration:.1f}s: {current_action}")
                self._execute_entry(current_action, total_score, regime_data, metrics, signal_duration)
                self.pending_signal = None

            else:
                # Non-BUY signal - reset pending
                if self.pending_signal:
                    logger.debug(f"[V8 PAPER] Non-BUY signal, resetting pending")
                    self.pending_signal = None

        elif time.time() <= self.trade_cooldown:
            remaining = self.trade_cooldown - time.time()
            logger.debug(f"[V8 PAPER] Cooldown: {remaining:.1f}s remaining")

    def _is_signal_valid_for_trade(self, option_type, current_action):
        """Check if current signal supports the trade direction"""
        if option_type == 'CE':
            return 'BUY CALL' in current_action
        elif option_type == 'PE':
            return 'BUY PUT' in current_action
        return False

    def _execute_entry(self, action, total_score, regime_data, metrics, confirm_duration):
        """Execute paper trade entry with enhanced tracking"""
        current_time = datetime.now()
        bias = 'BULLISH' if 'CALL' in action else 'BEARISH'
        contract_type = 'CE' if bias == 'BULLISH' else 'PE'

        # Select strike
        best_contract = self._select_best_strike(bias)
        if not best_contract or best_contract['ltp'] <= 0:
            logger.warning(f"[V8 PAPER] Entry blocked: Invalid contract for {bias}")
            return

        # Calculate levels
        levels = self._calculate_trade_levels(best_contract['ltp'], action)
        trade_id = f"V8_{uuid.uuid4().hex[:8]}"
        lot_size = 50 if self.underlying == 'NIFTY' else 15 if self.underlying == 'BANKNIFTY' else 250

        # Get Greeks
        strike = best_contract['strike']
        tag = 'ce_data' if contract_type == 'CE' else 'pe_data'
        entry_delta = self.option_data[strike][tag].get('delta', 0) if strike in self.option_data else 0
        entry_gamma = self.option_data[strike][tag].get('gamma', 0) if strike in self.option_data else 0
        entry_theta = self.option_data[strike][tag].get('theta', 0) if strike in self.option_data else 0
        entry_vega = self.option_data[strike][tag].get('vega', 0) if strike in self.option_data else 0

        # Create trade state
        trade_data = {
            'Trade_ID': trade_id,
            'Symbol': self.underlying,
            'Contract': best_contract['name'],
            'Strike': strike,
            'Type': contract_type,
            'Direction': 'BUY',
            'Strategy': self.strategy_mode,
            'Price': best_contract['ltp'],
            'Quantity': 1,
            'Lot_Size': lot_size,
            'Target': levels['target'],
            'StopLoss': levels['sl'],
            'Underlying_LTP': self.underlying_ltp,
            'Entry_Score': total_score,
            'Regime': regime_data.get('regime', 'UNKNOWN'),
            'Confidence': regime_data.get('confidence', 0),
            'Signal_Confirm_Time': confirm_duration,
            'Entry_Delta': entry_delta,
            'Entry_Gamma': entry_gamma,
            'Entry_Theta': entry_theta,
            'Entry_Vega': entry_vega,
            'Signal_History': list(self.pre_entry_signal_buffer)
        }

        self.paper_trade_state = PaperTradeStateV2(trade_data)

        # Log entry
        log_data = {
            'Trade_ID': trade_id,
            'Status': 'ENTRY',
            'Timestamp': current_time.strftime("%Y-%m-%d %H:%M:%S"),
            'Symbol': self.underlying,
            'Contract': best_contract['name'],
            'Strike': strike,
            'Type': contract_type,
            'Direction': 'BUY',
            'Strategy': self.strategy_mode,
            'Regime': regime_data.get('regime', 'UNKNOWN'),
            'Entry_Price': best_contract['ltp'],
            'Exit_Price': '',
            'Quantity': 1,
            'Lot_Size': lot_size,
            'Target': levels['target'],
            'StopLoss': levels['sl'],
            'Trailing_SL': levels['sl'],
            'Underlying_LTP': self.underlying_ltp,
            'Entry_Score': total_score,
            'Signal_Confirm_Time': round(confirm_duration, 1),
            'Regime_At_Entry': regime_data.get('regime', 'UNKNOWN'),
            'Confidence': regime_data.get('confidence', 0),
            'Reason': regime_data.get('reason', 'Signal Generated'),
            'Snapshot_PCR': metrics.get('pcr', 0),
            'Snapshot_IV': metrics.get('iv', 0),
            'Snapshot_GEX': metrics.get('gex', 0),
            'Entry_Delta': round(entry_delta, 4),
            'Entry_Gamma': round(entry_gamma, 6),
            'Entry_Theta': round(entry_theta, 4),
            'Entry_Vega': round(entry_vega, 4),
        }

        self.logger.log_trade(log_data)
        logger.info(f"[V8 PAPER] ENTRY: {action} on {best_contract['name']} @ {best_contract['ltp']} | Score: {total_score} | Confirm: {confirm_duration:.1f}s")

    def _execute_exit(self, trade: PaperTradeStateV2, exit_price, exit_reason, total_score, regime_data, metrics):
        """Execute paper trade exit with comprehensive metrics"""
        current_time = datetime.now()

        pnl_points = exit_price - trade.entry_price
        pnl_amount = pnl_points * trade.lot_size * trade.quantity
        roi_pct = trade.get_current_pnl_pct(exit_price)
        hold_duration = trade.get_hold_duration()
        score_stats = trade.get_score_stats()

        # Calculate trade quality score (0-100)
        trade_quality = self._calculate_trade_quality(trade, exit_reason, roi_pct)

        # Get exit delta
        exit_delta = 0
        if trade.strike in self.option_data:
            tag = 'ce_data' if trade.option_type == 'CE' else 'pe_data'
            exit_delta = self.option_data[trade.strike][tag].get('delta', 0)

        log_data = {
            'Trade_ID': trade.trade_id,
            'Status': 'EXIT',
            'Timestamp': current_time.strftime("%Y-%m-%d %H:%M:%S"),
            'Symbol': trade.symbol,
            'Contract': trade.contract,
            'Strike': trade.strike,
            'Type': trade.option_type,
            'Direction': trade.direction,
            'Strategy': trade.strategy,
            'Regime': regime_data.get('regime', 'UNKNOWN'),
            'Entry_Price': trade.entry_price,
            'Exit_Price': exit_price,
            'Quantity': trade.quantity,
            'Lot_Size': trade.lot_size,
            'Target': trade.target,
            'StopLoss': trade.stoploss,
            'Trailing_SL': trade.trailing_sl,
            'Underlying_LTP': self.underlying_ltp,
            'PnL_Points': round(pnl_points, 2),
            'PnL_Amount': round(pnl_amount, 2),
            'ROI_Pct': round(roi_pct, 2),
            'Entry_Score': trade.entry_score,
            'Exit_Score': total_score,
            'Min_Score_During': score_stats['min'],
            'Max_Score_During': score_stats['max'],
            'Score_Volatility': round(score_stats['volatility'], 2),
            'Signal_Confirm_Time': trade.signal_confirm_time,
            'Regime_At_Entry': trade.entry_regime,
            'Regime_At_Exit': regime_data.get('regime', 'UNKNOWN'),
            'Regime_Changes': len(trade.regime_history) - 1,
            'Opposite_Signals': trade.opposite_signal_count,
            'Neutral_Signals': trade.neutral_signal_count,
            'Confidence': regime_data.get('confidence', 0),
            'Reason': regime_data.get('reason', ''),
            'Exit_Reason': exit_reason,
            'Snapshot_PCR': metrics.get('pcr', 0),
            'Snapshot_IV': metrics.get('iv', 0),
            'Snapshot_GEX': metrics.get('gex', 0),
            'Entry_Delta': round(trade.entry_delta, 4),
            'Entry_Gamma': round(trade.entry_gamma, 6),
            'Entry_Theta': round(trade.entry_theta, 4),
            'Entry_Vega': round(trade.entry_vega, 4),
            'Exit_Delta': round(exit_delta, 4),
            'Time_Held_Sec': int(hold_duration),
            'Max_Price_Seen': trade.max_price,
            'Min_Price_Seen': trade.min_price,
            'Max_Profit_Pct': round(trade.get_max_profit_pct(), 2),
            'Max_Drawdown_Pct': round(trade.get_max_drawdown_pct(), 2),
            'Trade_Quality': trade_quality,
            'Signal_History': trade.pre_entry_signals
        }

        self.logger.log_trade(log_data)

        # Reset state
        self.paper_trade_state = None
        self.exit_signal_start = None

        # Set cooldown
        if "FLIP" in exit_reason:
            self.trade_cooldown = time.time() + 15  # Shorter for flips
        else:
            self.trade_cooldown = time.time() + self.trade_cooldown_duration

        logger.info(f"[V8 PAPER] EXIT: {exit_reason} | PnL: {pnl_amount:.2f} | ROI: {roi_pct:.2f}% | Quality: {trade_quality}")

    def _calculate_trade_quality(self, trade: PaperTradeStateV2, exit_reason, roi_pct):
        """
        Calculate trade quality score (0-100)
        Higher = better quality trade
        """
        score = 50  # Base score

        # ROI contribution (+/- 20)
        if roi_pct > 20:
            score += 20
        elif roi_pct > 10:
            score += 15
        elif roi_pct > 5:
            score += 10
        elif roi_pct > 0:
            score += 5
        elif roi_pct > -5:
            score -= 5
        elif roi_pct > -10:
            score -= 10
        else:
            score -= 20

        # Exit reason contribution (+/- 15)
        if exit_reason == 'TARGET_HIT':
            score += 15
        elif exit_reason == 'TRAILING_SL_HIT':
            score += 10  # Good - locked profits
        elif exit_reason == 'SL_HIT':
            score -= 10
        elif 'FLIP' in exit_reason:
            score -= 5

        # Signal stability contribution (+/- 10)
        score_volatility = trade.get_score_stats()['volatility']
        if score_volatility < 10:
            score += 10  # Stable signal
        elif score_volatility < 20:
            score += 5
        elif score_volatility > 30:
            score -= 5

        # Regime stability contribution (+/- 5)
        regime_changes = len(trade.regime_history) - 1
        if regime_changes == 0:
            score += 5
        elif regime_changes > 2:
            score -= 5

        return max(0, min(100, score))

    def get_paper_trade_summary(self):
        """Returns comprehensive paper trading performance summary"""
        summary = self.logger.get_performance_summary()
        summary['active_trade'] = None

        if self.paper_trade_state:
            trade = self.paper_trade_state
            current_ltp = 0
            if trade.strike in self.option_data:
                tag = 'ce_data' if trade.option_type == 'CE' else 'pe_data'
                current_ltp = self.option_data[trade.strike][tag].get('ltp', 0)

            summary['active_trade'] = {
                'trade_id': trade.trade_id,
                'contract': trade.contract,
                'entry_price': trade.entry_price,
                'current_ltp': current_ltp,
                'pnl_pct': trade.get_current_pnl_pct(current_ltp),
                'hold_duration': trade.get_hold_duration(),
                'trailing_activated': trade.trailing_activated,
                'trailing_sl': trade.trailing_sl
            }

        summary['score_analysis'] = self.logger.get_score_analysis()
        summary['config'] = {
            'entry_confirm_time': self.signal_confirm_time,
            'min_score_threshold': self.min_score_threshold,
            'ignore_neutral': self.ignore_neutral_signals,
            'trailing_activation': self.trailing_activation_pct
        }

        return summary

    # ============================================================================
    # SIGNAL GENERATION (Same as V7 Adaptive)
    # ============================================================================

    def _capture_initial_state(self):
        """Capture initial OI state"""
        if not self.initial_state and self.option_data:
            for strike, data in self.option_data.items():
                self.initial_state[strike] = {
                    'ce_oi': data['ce_data'].get('oi', 0),
                    'pe_oi': data['pe_data'].get('oi', 0)
                }
            if self.underlying_ltp > 0:
                self.start_price = self.underlying_ltp
            logger.info(f"Captured initial state. Strikes: {len(self.initial_state)}")

    def _get_recent_volatility(self):
        """Calculate recent price volatility"""
        if len(self.price_history) < 60:
            return 0.1

        recent_prices = list(self.price_history)[-60:]
        avg_price = sum(recent_prices) / len(recent_prices)
        price_range = max(recent_prices) - min(recent_prices)
        volatility = (price_range / avg_price) * 100

        return max(0.05, volatility)

    def _check_divergence(self):
        """Detect Spot vs Option Divergence (Traps)"""
        if len(self.price_history) < 10 or len(self.atm_ce_history) < 10:
            return None, ""

        current_spot = self.price_history[-1]
        current_ce = self.atm_ce_history[-1]
        current_pe = self.atm_pe_history[-1]

        spot_high = max(self.price_history)
        spot_low = min(self.price_history)
        ce_high = max(self.atm_ce_history)
        pe_high = max(self.atm_pe_history)

        if current_spot >= spot_high * 0.998:
            if current_ce < ce_high * 0.98:
                return "BEARISH", f"BULL TRAP: Spot at {current_spot:.1f} (High) but Call Weak"

        if current_spot <= spot_low * 1.002:
            if current_pe < pe_high * 0.98:
                return "BULLISH", f"BEAR TRAP: Spot at {current_spot:.1f} (Low) but Put Weak"

        return None, ""

    def _score_momentum_velocity(self):
        """Momentum Velocity with dynamic thresholds"""
        if len(self.price_history) < 10:
            return 0, "No Velocity Data"

        p_now = self.price_history[-1]
        p_prev = self.price_history[-10]
        velocity_pct = ((p_now - p_prev) / p_prev) * 100

        volatility = self._get_recent_volatility()
        threshold = max(0.1, volatility * 2)

        if velocity_pct > threshold:
            if velocity_pct > threshold * 3:
                return 25, f"MOMENTUM BURST (+{velocity_pct:.2f}%)"
            return 10, f"Positive Momentum (+{velocity_pct:.2f}%)"
        elif velocity_pct < -threshold:
            if velocity_pct < -threshold * 3:
                return -25, f"MOMENTUM CRASH ({velocity_pct:.2f}%)"
            return -10, f"Negative Momentum ({velocity_pct:.2f}%)"

        return 0, f"Stable Velocity ({velocity_pct:+.2f}%)"

    def _get_expiry_multiplier(self):
        """Time-Decay Awareness"""
        if self.days_to_expiry <= 0:
            return 1.0, "Expiry Unknown"

        if self.days_to_expiry <= 1:
            return 0.4, "EXPIRY DAY: High Gamma Risk"
        elif self.days_to_expiry <= 2:
            return 0.6, "Near Expiry (2 days)"
        elif self.days_to_expiry <= 3:
            return 0.8, "Approaching Expiry (3 days)"

        return 1.0, "Normal Expiry Window"

    def _score_pcr_roc(self):
        """PCR Rate-of-Change with longer window"""
        if len(self.pcr_history) < 60:
            return 0, f"Building PCR History ({len(self.pcr_history)}/60)"

        pcr_now = self.pcr_history[-1]
        pcr_prev = self.pcr_history[-60]

        if pcr_prev <= 0:
            return 0, "Invalid PCR Data"

        roc = ((pcr_now - pcr_prev) / pcr_prev) * 100

        if roc > 3:
            return 20, f"PCR Surge +{roc:.2f}% (Strong Support)"
        elif roc > 1.5:
            return 10, f"PCR Rising +{roc:.2f}% (Bullish)"
        elif roc < -3:
            return -20, f"PCR Crash {roc:.2f}% (Strong Resistance)"
        elif roc < -1.5:
            return -10, f"PCR Falling {roc:.2f}% (Bearish)"

        return 0, f"Stable PCR (ROC: {roc:+.2f}%)"

    def _verify_volume_liquidity(self):
        """Volume Confirmation Filter"""
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return False, "No ATM Data"

        atm_data = self.option_data[self.atm_strike]
        ce_vol = atm_data['ce_data'].get('volume', 0)
        pe_vol = atm_data['pe_data'].get('volume', 0)
        total_vol = ce_vol + pe_vol

        if self.underlying in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            min_volume = 10000
        else:
            min_volume = 2000

        if total_vol < min_volume:
            return False, f"Low Volume ({total_vol:,})"
        elif total_vol < min_volume * 2:
            return True, f"Moderate Volume ({total_vol:,})"

        return True, f"High Volume ({total_vol:,})"

    def _score_gamma_exposure(self):
        """Gamma Exposure with pinning detection"""
        total_gex = 0
        gamma_walls = []

        lot_size = 50 if self.underlying == 'NIFTY' else 15 if self.underlying == 'BANKNIFTY' else 1

        for strike, data in self.option_data.items():
            ce_gamma = data['ce_data'].get('gamma', 0)
            ce_oi = data['ce_data'].get('oi', 0)
            pe_gamma = data['pe_data'].get('gamma', 0)
            pe_oi = data['pe_data'].get('oi', 0)

            ce_gex = ce_gamma * ce_oi * lot_size
            pe_gex = pe_gamma * pe_oi * lot_size

            strike_gex = ce_gex - pe_gex
            total_gex += strike_gex

            data['ce_data']['gex'] = ce_gex
            data['pe_data']['gex'] = pe_gex
            data['net_gex'] = strike_gex
            data['is_gamma_wall'] = False

            gamma_walls.append((strike, abs(strike_gex)))

        gamma_walls.sort(key=lambda x: x[1], reverse=True)

        if not gamma_walls:
            return 0, "No Gamma Data", total_gex

        top_wall_strike = gamma_walls[0][0]
        self.option_data[top_wall_strike]['is_gamma_wall'] = True

        score = 0
        reason = "Neutral Gamma"

        if total_gex > 0:
            distance_pct = abs(self.underlying_ltp - top_wall_strike) / top_wall_strike * 100

            if distance_pct < 0.5:
                score = 0
                reason = f"Gamma Pin at {top_wall_strike} (Mean Reversion)"
            else:
                if self.underlying_ltp > top_wall_strike:
                    score = -5
                    reason = f"Gamma Pull Down toward {top_wall_strike}"
                else:
                    score = 5
                    reason = f"Gamma Pull Up toward {top_wall_strike}"

        elif total_gex < 0:
            if len(self.price_history) > 5:
                is_uptrend = self.price_history[-1] > self.price_history[-5]
                score = 10 if is_uptrend else -10
                reason = f"Negative GEX: Trend Amplification ({'Up' if is_uptrend else 'Down'})"
            else:
                score = 0
                reason = "Negative GEX (Volatile Regime)"

        return score, reason, total_gex

    def _score_vanna_charm(self):
        """Vanna/Charm Bias"""
        if len(self.iv_history) < 20 or len(self.price_history) < 20:
            return 0, "Wait for IV History"

        current_iv = self.iv_history[-1]
        avg_iv = sum(self.iv_history) / len(self.iv_history)

        current_price = self.price_history[-1]
        prev_price = self.price_history[-20]

        score = 0
        reason = "Vanna/Charm Neutral"

        if current_price > prev_price * 1.001 and current_iv < avg_iv * 0.98:
            score = 15
            reason = "Bullish Vanna Squeeze (Dealers Covering)"
        elif current_price < prev_price * 0.999 and current_iv > avg_iv * 1.02:
            score = -15
            reason = "Bearish Vanna Crush (Hedging Spike)"

        return score, reason

    def _detect_institutional_flow(self):
        """Institutional Flow Detection"""
        alerts = []
        score = 0
        reason = "Normal Flow"

        for strike, data in self.option_data.items():
            ce_vol = data['ce_data'].get('volume', 0)
            ce_oi = data['ce_data'].get('oi', 0)
            pe_vol = data['pe_data'].get('volume', 0)
            pe_oi = data['pe_data'].get('oi', 0)

            ce_oi_change = 0
            pe_oi_change = 0
            if strike in self.initial_state:
                ce_oi_change = ce_oi - self.initial_state[strike]['ce_oi']
                pe_oi_change = pe_oi - self.initial_state[strike]['pe_oi']

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

    def _scan_iv_surface(self):
        """Identify IV Anomalies"""
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return

        atm_data = self.option_data[self.atm_strike]
        atm_iv = (atm_data['ce_data'].get('iv', 0) + atm_data['pe_data'].get('iv', 0)) / 2

        if atm_iv <= 0:
            return

        for strike, data in self.option_data.items():
            for side in ['ce_data', 'pe_data']:
                iv = data[side].get('iv', 0)
                if iv <= 0:
                    continue

                ratio = iv / atm_iv
                data[side]['iv_status'] = 'normal'

                if ratio < 0.85:
                    data[side]['iv_status'] = 'cheap'
                elif ratio > 1.35:
                    data[side]['iv_status'] = 'expensive'

    def _calculate_heatmap_intensity(self):
        """Calculate OI Change Intensity"""
        if not self.initial_state:
            return

        max_change = 1
        changes = []

        for strike, data in self.option_data.items():
            if strike not in self.initial_state:
                continue

            ce_chg = abs(data['ce_data'].get('oi', 0) - self.initial_state[strike]['ce_oi'])
            pe_chg = abs(data['pe_data'].get('oi', 0) - self.initial_state[strike]['pe_oi'])
            changes.extend([ce_chg, pe_chg])

        if changes:
            max_change = max(changes) if max(changes) > 0 else 1

        for strike, data in self.option_data.items():
            if strike not in self.initial_state:
                continue

            ce_chg = data['ce_data'].get('oi', 0) - self.initial_state[strike]['ce_oi']
            pe_chg = data['pe_data'].get('oi', 0) - self.initial_state[strike]['pe_oi']

            data['ce_data']['oi_intensity'] = min(1.0, abs(ce_chg) / max_change)
            data['pe_data']['oi_intensity'] = min(1.0, abs(pe_chg) / max_change)

            data['ce_data']['oi_sentiment'] = 'build' if ce_chg >= 0 else 'unwind'
            data['pe_data']['oi_sentiment'] = 'build' if pe_chg >= 0 else 'unwind'

    def _score_delta_oi(self):
        """Delta OI with dynamic thresholds"""
        net_delta_oi = 0
        current_total_oi_change = 0

        for strike, data in self.option_data.items():
            if strike not in self.initial_state:
                continue

            init = self.initial_state[strike]

            ce_oi_change = data['ce_data'].get('oi', 0) - init['ce_oi']
            ce_delta = data['ce_data'].get('delta', 0.5)
            if ce_delta == 0:
                ce_delta = 0.5

            pe_oi_change = data['pe_data'].get('oi', 0) - init['pe_oi']
            pe_delta = data['pe_data'].get('delta', -0.5)
            if pe_delta == 0:
                pe_delta = -0.5

            net_delta_oi += (ce_oi_change * ce_delta) + (pe_oi_change * pe_delta)
            current_total_oi_change += abs(ce_oi_change) + abs(pe_oi_change)

        score = 0
        reason = "Balanced Delta OI"

        if current_total_oi_change == 0:
            return 0, "No OI Change"

        directional_pct = (abs(net_delta_oi) / current_total_oi_change) * 100 if current_total_oi_change > 0 else 0

        if net_delta_oi < 0 and directional_pct > 30:
            score = 30
            reason = f"Strong Put Writing ({directional_pct:.1f}% directional - Bullish)"
        elif net_delta_oi > 0 and directional_pct > 30:
            score = -30
            reason = f"Strong Call Writing ({directional_pct:.1f}% directional - Bearish)"
        elif directional_pct > 15:
            if net_delta_oi < 0:
                score = 15
                reason = f"Moderate Put Writing ({directional_pct:.1f}%)"
            else:
                score = -15
                reason = f"Moderate Call Writing ({directional_pct:.1f}%)"
        else:
            score = 0
            reason = f"Balanced Delta OI ({directional_pct:.1f}% directional)"

        return score, reason

    def _score_iv_skew(self):
        """IV Skew with context"""
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return 0, "No ATM Data"

        atm_data = self.option_data[self.atm_strike]
        ce_iv = atm_data['ce_data'].get('iv', 0)
        pe_iv = atm_data['pe_data'].get('iv', 0)

        if ce_iv == 0 or pe_iv == 0:
            return 0, "IV Missing"

        diff = pe_iv - ce_iv

        is_falling = False
        if len(self.price_history) > 20:
            price_change_pct = ((self.price_history[-1] - self.price_history[-20]) / self.price_history[-20]) * 100
            is_falling = price_change_pct < -0.1

        if diff > 2:
            if is_falling:
                return -15, f"Put IV Spike - Justified Fear (Bearish)"
            else:
                return 10, f"Put IV Spike - Excess Fear (Contrarian Bullish)"

        elif diff < -2:
            return -10, f"Call IV Spike - FOMO/Greed (Bearish)"

        return 0, f"Balanced IV Skew"

    def _score_oi_unwind(self):
        """OI Build-up vs Unwinding"""
        if len(self.price_history) < 2 or len(self.oi_history) < 2:
            return 0, "Building History"

        price_trend = self.price_history[-1] - self.price_history[0]
        oi_trend = self.oi_history[-1] - self.oi_history[0]

        price_move_sig = (self.price_history[-1] * 0.0005)
        oi_move_sig = 1000

        if price_trend > price_move_sig and oi_trend > oi_move_sig:
            return 20, "Long Build-up (Price up, OI up)"
        elif price_trend < -price_move_sig and oi_trend > oi_move_sig:
            return -20, "Short Build-up (Price down, OI up)"
        elif price_trend > price_move_sig and oi_trend < -oi_move_sig:
            return 5, "Short Covering (Price up, OI down)"
        elif price_trend < -price_move_sig and oi_trend < -oi_move_sig:
            return -5, "Long Unwinding (Price down, OI down)"

        return 0, "Mixed Position Flow"

    def _score_max_pain(self):
        """Max Pain with correct magnetism"""
        mp = self.calculate_max_pain()
        if mp == 0 or self.underlying_ltp == 0:
            return 0, "No Max Pain"

        diff_pct = (self.underlying_ltp - mp) / mp * 100

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

    def _check_signal_correlation(self, scores_dict):
        """Correlation Filter"""
        correlation_penalty = 1.0

        score_iv = scores_dict.get('iv_skew', 0)
        score_vc = scores_dict.get('vanna', 0)
        score_delta = scores_dict.get('delta_oi', 0)
        score_flow = scores_dict.get('inst_flow', 0)

        if abs(score_iv) > 10 and abs(score_vc) > 10:
            if (score_iv > 0 and score_vc > 0) or (score_iv < 0 and score_vc < 0):
                correlation_penalty *= 0.85

        if abs(score_delta) > 20 and abs(score_flow) > 15:
            if (score_delta > 0 and score_flow > 0) or (score_delta < 0 and score_flow < 0):
                correlation_penalty *= 0.90

        return correlation_penalty

    def calculate_max_pain(self):
        """Calculate Max Pain"""
        strikes = []
        ce_oi = {}
        pe_oi = {}

        for strike_data in self.option_data.values():
            strike = strike_data['strike']
            strikes.append(strike)
            ce_oi[strike] = strike_data['ce_data'].get('oi', 0)
            pe_oi[strike] = strike_data['pe_data'].get('oi', 0)

        if not strikes:
            return 0

        strikes.sort()

        min_loss = float('inf')
        max_pain = 0

        for strike_price in strikes:
            total_loss = 0
            for k in strikes:
                if strike_price > k:
                    total_loss += (strike_price - k) * ce_oi[k]

                if k > strike_price:
                    total_loss += (k - strike_price) * pe_oi[k]

            if total_loss < min_loss:
                min_loss = total_loss
                max_pain = strike_price

        return max_pain

    # ============================================================================
    # REGIME DETECTION
    # ============================================================================

    def detect_market_regime(self, total_score, net_gex, volatility, scores_dict):
        """Auto-detect market regime and select optimal strategy"""
        inst_score = scores_dict.get('inst_flow', 0)
        trap_type, trap_msg = self._check_divergence()

        # THETA_HUNTER
        if net_gex > 0 and -30 <= total_score <= 30:
            return {
                'regime': 'THETA_HUNTER',
                'confidence': 85,
                'action': 'SELL STRADDLE',
                'reason': 'Gamma Pinning + Range-Bound Market',
                'description': 'Market pinned. Selling premium.',
                'thresholds': {'entry': 'ATM', 'target': '50-80%', 'sl': '40%'},
            }

        # MOMENTUM_RIDER
        elif net_gex < 0 and abs(total_score) > 60:
            direction = 'BULLISH' if total_score > 0 else 'BEARISH'
            action = 'BUY CALL' if total_score > 0 else 'BUY PUT'

            return {
                'regime': 'MOMENTUM_RIDER',
                'confidence': 80,
                'action': action,
                'reason': f'Negative GEX + Strong {direction} Trend',
                'description': 'Dealers amplifying moves. Ride momentum.',
                'thresholds': {'entry': 'ATM/ITM1', 'target': '+30%', 'sl': '-15%'},
            }

        # CONTRARIAN_REVERSAL
        elif trap_type:
            action = 'BUY PUT' if trap_type == 'BEARISH' else 'BUY CALL'

            return {
                'regime': 'CONTRARIAN_REVERSAL',
                'confidence': 75,
                'action': action,
                'reason': trap_msg,
                'description': 'Divergence detected. Fading fake move.',
                'thresholds': {'entry': 'ATM', 'target': '+20%', 'sl': '-10%'},
            }

        # INSTITUTIONAL_SHADOW
        elif abs(inst_score) > 15:
            action = 'BUY CALL' if inst_score > 0 else 'BUY PUT'

            return {
                'regime': 'INSTITUTIONAL_SHADOW',
                'confidence': 70,
                'action': action,
                'reason': f'Following Institutional Flow (Score: {inst_score:+d})',
                'description': 'Large fresh positions detected.',
                'thresholds': {'entry': 'Same as institutions', 'target': '+25%', 'sl': '-12%'},
            }

        # RISK_OFF
        elif volatility > 0.3 or abs(total_score) < 20:
            return {
                'regime': 'RISK_OFF',
                'confidence': 50,
                'action': 'WAIT',
                'reason': 'High Volatility or No Clear Setup',
                'description': 'Conditions unclear. Waiting.',
                'thresholds': {},
            }

        # CONSERVATIVE
        elif abs(total_score) > 40:
            direction = 'BULLISH' if total_score > 0 else 'BEARISH'
            action = 'BUY CALL' if total_score > 0 else 'BUY PUT'

            return {
                'regime': 'CONSERVATIVE',
                'confidence': 60,
                'action': action,
                'reason': f'Moderate {direction} Signal (Score: {total_score:+d})',
                'description': 'Moderate conviction trade.',
                'thresholds': {'entry': 'ATM', 'target': '+20%', 'sl': '-12%'},
            }

        # NEUTRAL
        else:
            return {
                'regime': 'NEUTRAL',
                'confidence': 40,
                'action': 'WAIT',
                'reason': 'No Clear Opportunity',
                'description': 'Waiting for clearer direction.',
                'thresholds': {},
            }

    def generate_signals(self, pcr, max_pain):
        """Generate trading signals with anti-whipsaw awareness"""
        self._capture_initial_state()

        # Update histories
        if self.underlying_ltp > 0:
            self.price_history.append(self.underlying_ltp)

        if self.atm_strike in self.option_data:
            atm_data = self.option_data[self.atm_strike]
            self.atm_ce_history.append(atm_data['ce_data'].get('ltp', 0))
            self.atm_pe_history.append(atm_data['pe_data'].get('ltp', 0))

            atm_iv = (atm_data['ce_data'].get('iv', 0) + atm_data['pe_data'].get('iv', 0)) / 2
            if atm_iv > 0:
                self.iv_history.append(atm_iv)

        total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        total_oi = total_ce_oi + total_pe_oi

        self.oi_history.append(total_oi)

        if total_ce_oi > 0:
            self.pcr_history.append(total_pe_oi / total_ce_oi)

        if not self.initial_state or not self.start_price:
            return {
                'action': 'WAIT',
                'confidence': '0%',
                'score': 0,
                'max_pain': max_pain,
                'pcr_signal': 'NEUTRAL',
                'reasons': ["Initializing..."],
                'trade_setup': {},
                'warning': None,
                'mode': self.strategy_mode,
                'regime': 'DETECTING',
                'regime_info': {}
            }

        # Calculate scores
        score_delta, s_d_reason = self._score_delta_oi()
        score_iv, s_iv_reason = self._score_iv_skew()
        score_unwind, s_u_reason = self._score_oi_unwind()
        score_mp, s_mp_reason = self._score_max_pain()
        score_gex, s_gex_reason, net_gex = self._score_gamma_exposure()

        self.gex_history.append(net_gex)

        score_vc, s_vc_reason = self._score_vanna_charm()
        score_flow, s_flow_reason = self._detect_institutional_flow()
        score_mom, s_mom_reason = self._score_momentum_velocity()
        score_pcr_roc, s_pcr_reason = self._score_pcr_roc()

        is_liquid, liquidity_reason = self._verify_volume_liquidity()
        expiry_mult, expiry_reason = self._get_expiry_multiplier()

        scores_dict = {
            'delta_oi': score_delta,
            'iv_skew': score_iv,
            'oi_unwind': score_unwind,
            'max_pain': score_mp,
            'gamma': score_gex,
            'vanna': score_vc,
            'inst_flow': score_flow,
            'momentum': score_mom,
            'pcr_roc': score_pcr_roc
        }

        # Weighted score
        weighted_score = (
            score_delta * self.signal_weights['delta_oi'] +
            score_iv * self.signal_weights['iv_skew'] +
            score_unwind * self.signal_weights['oi_unwind'] +
            score_mp * self.signal_weights['max_pain'] +
            score_gex * self.signal_weights['gamma'] +
            score_vc * self.signal_weights['vanna'] +
            score_flow * self.signal_weights['inst_flow'] +
            score_mom * self.signal_weights['momentum'] +
            score_pcr_roc * self.signal_weights['pcr_roc']
        )

        max_possible = sum(self.signal_weights.values()) * 30
        base_score = (weighted_score / max_possible) * 100

        correlation_penalty = self._check_signal_correlation(scores_dict)
        base_score *= correlation_penalty

        total_score = int(base_score * expiry_mult)
        total_score = max(min(total_score, 100), -100)

        # Update OI changes
        if self.initial_state:
            for strike, data in self.option_data.items():
                if strike in self.initial_state:
                    data['ce_data']['oi_change'] = data['ce_data'].get('oi', 0) - self.initial_state[strike]['ce_oi']
                    data['pe_data']['oi_change'] = data['pe_data'].get('oi', 0) - self.initial_state[strike]['pe_oi']

        self._scan_iv_surface()
        self._calculate_heatmap_intensity()

        volatility = self._get_recent_volatility()

        # Regime detection
        if self.strategy_mode == 'ADAPTIVE':
            regime_info = self.detect_market_regime(total_score, net_gex, volatility, scores_dict)
            action = regime_info['action']
            self.current_regime = regime_info['regime']
            self.regime_confidence = regime_info['confidence']
        else:
            regime_info = {
                'regime': self.strategy_mode,
                'confidence': 0,
                'action': 'WAIT',
                'reason': f'Manual mode: {self.strategy_mode}',
            }
            if total_score > 65:
                action = 'BUY CALL'
            elif total_score < -65:
                action = 'BUY PUT'
            else:
                action = 'WAIT'

        # Track action history
        self.action_history.append(action)

        # Build reasons
        reasons = [
            f"Regime: {regime_info['regime']} (Confidence: {regime_info['confidence']}%)",
            f"Reason: {regime_info['reason']}",
            f"Score: {total_score} | Net GEX: {net_gex:,.0f}",
            f"Delta OI: {score_delta} ({s_d_reason})",
            f"IV Skew: {score_iv} ({s_iv_reason})",
            f"OI Flow: {score_unwind} ({s_u_reason})",
            f"Max Pain: {score_mp} ({s_mp_reason})",
            f"Gamma: {score_gex} ({s_gex_reason})",
            f"Inst. Flow: {score_flow} ({s_flow_reason})",
            f"Momentum: {score_mom} ({s_mom_reason})",
            f"PCR ROC: {score_pcr_roc} ({s_pcr_reason})",
        ]

        if expiry_mult < 1.0:
            reasons.insert(0, f"{expiry_reason} (Score x{expiry_mult:.1f})")

        if not is_liquid:
            reasons.insert(0, f"{liquidity_reason}")

        # Trade setup
        trade_setup = {}
        if action != 'WAIT':
            best_contract = self._select_best_strike('BULLISH' if 'CALL' in action else 'BEARISH' if 'PUT' in action else 'NEUTRAL', action)
            if best_contract:
                levels = self._calculate_trade_levels(best_contract['ltp'], action)
                trade_setup = {
                    'contract': best_contract['name'],
                    'entry': best_contract['ltp'],
                    'sl': levels['sl'],
                    'target': levels['target'],
                    'rr': '1:2',
                }

        pcr_bias = "NEUTRAL"
        if score_pcr_roc > 0:
            pcr_bias = "BULLISH"
        elif score_pcr_roc < 0:
            pcr_bias = "BEARISH"

        return {
            'action': action,
            'confidence': f"{regime_info['confidence']}%",
            'score': total_score,
            'max_pain': max_pain,
            'pcr_signal': pcr_bias,
            'pcr_momentum': {'score': score_pcr_roc, 'reason': s_pcr_reason},
            'reasons': reasons,
            'trade_setup': trade_setup,
            'warning': None,
            'mode': self.strategy_mode,
            'regime': regime_info['regime'],
            'regime_info': regime_info,
            'flow_alerts': s_flow_reason.split(", ") if "Institutional" in s_flow_reason else []
        }

    # ============================================================================
    # HELPER METHODS
    # ============================================================================

    def _select_best_strike(self, bias, action="BUY"):
        """Select best strike for trade"""
        if not self.atm_strike:
            return None

        if action == "SELL STRADDLE":
            strike = self.atm_strike
            if strike not in self.option_data:
                return None
            ce_data = self.option_data[strike].get('ce_data', {})
            pe_data = self.option_data[strike].get('pe_data', {})
            combined_ltp = ce_data.get('ltp', 0) + pe_data.get('ltp', 0)

            return {
                'is_multi': True,
                'strike': strike,
                'name': f"STRADDLE {strike}",
                'ltp': combined_ltp,
            }

        target_strike = None

        if "SELL" in action:
            contract_type = 'PE' if bias == "BULLISH" else 'CE'
            if bias == "BULLISH":
                target_strike = self.atm_strike - (2 * self.strike_step)
            elif bias == "BEARISH":
                target_strike = self.atm_strike + (2 * self.strike_step)
        else:
            contract_type = 'CE' if bias == "BULLISH" else 'PE'
            target_strike = self.atm_strike

        if target_strike in self.option_data:
            data = self.option_data[target_strike].get(f"{contract_type.lower()}_data")
            if not data:
                return None
            return {
                'strike': target_strike,
                'name': self.option_data[target_strike].get(f"{contract_type.lower()}_symbol"),
                'ltp': data.get('ltp', 0),
                'type': contract_type
            }
        return None

    def _calculate_trade_levels(self, entry_price, action_type="BUY"):
        """Calculate SL and Target"""
        if entry_price <= 0:
            return {'sl': 0, 'target': 0}

        if "SELL" in action_type:
            sl = round(entry_price * 1.40, 1)
            target = round(entry_price * 0.20, 1)
        else:
            sl = round(entry_price * 0.85, 1)
            target = round(entry_price * 1.30, 1)

        return {'sl': sl, 'target': target}

    def calculate_market_metrics(self):
        """Calculate PCR and metrics"""
        total_ce_volume = sum(opt['ce_data'].get('volume', 0) for opt in self.option_data.values())
        total_pe_volume = sum(opt['pe_data'].get('volume', 0) for opt in self.option_data.values())
        total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())

        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0

        max_pain = self.calculate_max_pain()
        signals = self.generate_signals(pcr, max_pain)

        inst_score, inst_reason = self._detect_institutional_flow()
        flow_alerts = inst_reason.split(", ") if inst_reason != "Normal Flow" else []

        # ATM IV
        atm_iv = 0
        if self.atm_strike and self.atm_strike in self.option_data:
            atm_data = self.option_data[self.atm_strike]
            atm_iv = (atm_data['ce_data'].get('iv', 0) + atm_data['pe_data'].get('iv', 0)) / 2

        # Net GEX
        net_gex = self.gex_history[-1] if self.gex_history else 0

        trade_metrics = {
            'pcr': round(pcr, 8),
            'iv': atm_iv,
            'gex': net_gex
        }

        # Execute paper trading engine
        self._manage_paper_trade(
            total_score=signals['score'],
            regime_data=signals['regime_info'],
            metrics=trade_metrics
        )

        return {
            'days_to_expiry': self.days_to_expiry,
            'total_ce_volume': total_ce_volume,
            'total_pe_volume': total_pe_volume,
            'total_volume': total_ce_volume + total_pe_volume,
            'total_ce_oi': total_ce_oi,
            'total_pe_oi': total_pe_oi,
            'pcr': round(pcr, 8),
            'max_pain': max_pain,
            'quant_signal': signals,
            'flow_alerts': flow_alerts
        }

    # ============================================================================
    # INFRASTRUCTURE METHODS
    # ============================================================================

    def initialize(self, api_client):
        """Setup option chain with subscriptions"""
        if self.initialized:
            logger.info(f"Option chain already initialized for {self.underlying}")
            return True

        self.api_client = api_client
        self.calculate_atm()
        self.generate_strikes()
        self.setup_subscriptions()
        self.initialized = True
        return True

    def calculate_atm(self):
        """Determine ATM strike from underlying LTP"""
        try:
            if self.underlying_ltp and self.underlying_ltp > 0:
                self.atm_strike = round(self.underlying_ltp / self.strike_step) * self.strike_step
                return self.atm_strike

            if self.underlying == 'SENSEX':
                exchange = 'BSE_INDEX'
            elif self.underlying == 'NIFTY':
                exchange = 'NSE_INDEX'
            else:
                exchange = 'NSE'
            response = self.api_client.quotes(symbol=self.underlying, exchange=exchange)

            if response.get('status') == 'success':
                data = response.get('data', {})
                self.underlying_ltp = data.get('ltp', 0)
                self.underlying_bid = data.get('bid', self.underlying_ltp)
                self.underlying_ask = data.get('ask', self.underlying_ltp)

                if self.underlying_ltp > 0:
                    self.atm_strike = round(self.underlying_ltp / self.strike_step) * self.strike_step
                    return self.atm_strike
                else:
                    logger.warning(f"Invalid LTP for {self.underlying}: {self.underlying_ltp}")
                    return 0
            else:
                logger.warning(f"Failed to fetch quote for {self.underlying}")
                return 0
        except Exception as e:
            logger.error(f"Error calculating ATM: {e}")
            return 0

    def generate_strikes(self):
        """Create strike list with proper tagging"""
        if not self.atm_strike:
            logger.warning("generate_strikes skipped: ATM is 0")
            return

        strikes = []
        for i in range(6, 0, -1):
            strike = self.atm_strike - (i * self.strike_step)
            strikes.append({'strike': strike, 'tag': f'ITM{i}', 'position': -i})

        strikes.append({'strike': self.atm_strike, 'tag': 'ATM', 'position': 0})

        for i in range(1, 5):
            strike = self.atm_strike + (i * self.strike_step)
            strikes.append({'strike': strike, 'tag': f'OTM{i}', 'position': i})

        for strike_info in strikes:
            strike = strike_info['strike']
            self.option_data[strike] = {
                'strike': strike,
                'tag': strike_info['tag'],
                'position': strike_info['position'],
                'ce_symbol': self.construct_option_symbol(strike, 'CE'),
                'pe_symbol': self.construct_option_symbol(strike, 'PE'),
                'ce_data': {
                    'ltp': 0, 'bid': 0, 'ask': 0, 'bid_qty': 0,
                    'ask_qty': 0, 'spread': 0, 'volume': 0, 'oi': 0,
                    'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0,
                    'gex': 0, 'delta': 0.5, 'gamma': 0, 'theta': 0, 'vega': 0, 'iv': 0
                },
                'pe_data': {
                    'ltp': 0, 'bid': 0, 'ask': 0, 'bid_qty': 0,
                    'ask_qty': 0, 'spread': 0, 'volume': 0, 'oi': 0,
                    'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0,
                    'gex': 0, 'delta': -0.5, 'gamma': 0, 'theta': 0, 'vega': 0, 'iv': 0
                },
                'net_gex': 0,
                'is_gamma_wall': False
            }

            self.subscription_map[self.option_data[strike]['ce_symbol']] = {'strike': strike, 'type': 'CE'}
            self.subscription_map[self.option_data[strike]['pe_symbol']] = {'strike': strike, 'type': 'PE'}

        logger.info(f"Generated {len(strikes)} strikes for {self.underlying}. ATM: {self.atm_strike}")

    def _update_greeks_for_strike(self, strike):
        """Fetch and update Greeks for a specific strike"""
        if strike not in self.option_data:
            return

        try:
            def process_response(response, target_dict):
                if response and response.get('status') == 'success':
                    greeks = response.get('greeks', {})
                    target_dict.update({
                        'delta': float(greeks.get('delta', 0) or 0),
                        'gamma': float(greeks.get('gamma', 0) or 0),
                        'theta': float(greeks.get('theta', 0) or 0),
                        'vega': float(greeks.get('vega', 0) or 0),
                        'rho': float(greeks.get('rho', 0) or 0),
                        'iv': float(response.get('implied_volatility', 0) or 0)
                    })
                    if 'days_to_expiry' in response:
                        self.days_to_expiry = float(response.get('days_to_expiry', 0))
                    return True
                elif response and response.get('code') == 429:
                    logger.warning(f"[{self.underlying}] Rate Limited for {strike}")
                    return False
                return True

            ce_symbol = self.option_data[strike]['ce_symbol']
            ce_response = self.api_client.optiongreeks(symbol=ce_symbol, exchange='NFO')

            if not process_response(ce_response, self.option_data[strike]['ce_data']):
                self._backoff_until = time.time() + 60
                return

            time.sleep(2.0)

            pe_symbol = self.option_data[strike]['pe_symbol']
            pe_response = self.api_client.optiongreeks(symbol=pe_symbol, exchange='NFO')

            if not process_response(pe_response, self.option_data[strike]['pe_data']):
                self._backoff_until = time.time() + 60
                return

            time.sleep(2.0)

        except Exception as e:
            logger.error(f"Error fetching Greeks for strike {strike}: {e}")

    def refresh_greeks(self):
        """Smart Refresh Strategy for Greeks"""
        if self._backoff_until > time.time():
            return

        if not self.atm_strike:
            return

        all_strikes = sorted(self.option_data.keys())

        atm_idx = -1
        try:
            atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - self.atm_strike))
        except ValueError:
            return

        focus_indices = set(range(max(0, atm_idx - 1), min(len(all_strikes), atm_idx + 2)))

        for idx in focus_indices:
            if not self.monitoring_active:
                return
            strike = all_strikes[idx]
            self._update_greeks_for_strike(strike)

        if not hasattr(self, '_bg_greek_idx'):
            self._bg_greek_idx = 0

        attempts = 0
        while attempts < len(all_strikes):
            self._bg_greek_idx = (self._bg_greek_idx + 1) % len(all_strikes)
            if self._bg_greek_idx not in focus_indices:
                strike = all_strikes[self._bg_greek_idx]
                self._update_greeks_for_strike(strike)
                break
            attempts += 1

    def _greek_monitor_loop(self):
        """Background loop to refresh Greeks"""
        logger.info(f"Starting Smart Greek Monitor for {self.underlying}")
        while self.monitoring_active:
            try:
                self.refresh_greeks()
                for _ in range(20):
                    if not self.monitoring_active:
                        return
                    time.sleep(0.5)
            except Exception as e:
                logger.error(f"[{self.underlying}] Error in Greek monitoring loop: {e}")
                time.sleep(5)

    def construct_option_symbol(self, strike, option_type):
        """Construct OpenAlgo option symbol"""
        expiry_formatted = None
        expiry_year = '25'

        if isinstance(self.expiry, str):
            try:
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

        strike_str = str(int(strike)) if strike == int(strike) else str(strike)
        symbol = f"{self.underlying}{expiry_formatted}{expiry_year}{strike_str}{option_type}"
        return symbol

    def setup_subscriptions(self):
        """Configure WebSocket subscriptions"""
        if not self.websocket_manager:
            logger.warning("WebSocket manager not available")
            return

        self.websocket_manager.register_handler('quote', self.handle_quote_update)
        self.subscribe_underlying_quote()
        self.batch_subscribe_options()

    def subscribe_underlying_quote(self):
        """Subscribe to underlying index"""
        if self.websocket_manager:
            if self.underlying == 'SENSEX':
                exchange = 'BSE_INDEX'
            elif self.underlying == 'NIFTY':
                exchange = 'NSE_INDEX'
            else:
                exchange = 'NSE'
            subscription = {'exchange': exchange, 'symbol': self.underlying, 'mode': 'quote'}
            self.websocket_manager.subscribe(subscription)

    def batch_subscribe_options(self):
        """Batch subscribe to all option strikes"""
        if not self.websocket_manager:
            return

        exchange = 'BFO' if self.underlying == 'SENSEX' else 'NFO'
        instruments = []
        for strike_data in self.option_data.values():
            instruments.append({'symbol': strike_data['ce_symbol'], 'exchange': exchange})
            instruments.append({'symbol': strike_data['pe_symbol'], 'exchange': exchange})

        self.websocket_manager.subscribe_batch(instruments, mode='quote')
        logger.info(f"Subscribed to {len(instruments)} instruments")

    def handle_quote_update(self, data):
        """Handle quote updates"""
        symbol = data.get('symbol', '')

        if symbol == self.underlying:
            self.underlying_ltp = float(data.get('ltp', 0) or 0)
            self.underlying_bid = float(data.get('bid', 0) or 0)
            self.underlying_ask = float(data.get('ask', 0) or 0)
            self.underlying_open = float(data.get('open', 0) or 0)
            self.underlying_high = float(data.get('high', 0) or 0)
            self.underlying_low = float(data.get('low', 0) or 0)
            self.underlying_close = float(data.get('close', 0) or 0)

            if 'average_price' in data:
                self.underlying_avg = float(data.get('average_price', 0) or 0)
            else:
                self.underlying_avg = (self.underlying_high + self.underlying_low) / 2 if (self.underlying_high and self.underlying_low) else 0

            old_atm = self.atm_strike
            self.atm_strike = self.calculate_atm()
            if old_atm != self.atm_strike:
                if not self.option_data:
                    self.generate_strikes()
                    if self.websocket_manager and getattr(self.websocket_manager, 'authenticated', False):
                        self.batch_subscribe_options()
                else:
                    self.update_option_tags()
            return

        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']
            strike = strike_info['strike']

            quote_data = {
                'ltp': float(data.get('ltp', 0) or 0),
                'bid': float(data.get('bid_price', data.get('bid', 0)) or 0),
                'ask': float(data.get('ask_price', data.get('ask', 0)) or 0),
                'bid_qty': int(data.get('bid_size', data.get('buy_quantity', 0)) or 0),
                'ask_qty': int(data.get('ask_size', data.get('sell_quantity', 0)) or 0),
                'total_buy_qty': int(data.get('total_buy_quantity', 0) or 0),
                'total_sell_qty': int(data.get('total_sell_quantity', 0) or 0),
                'volume': int(data.get('volume', 0) or 0),
                'oi': int(data.get('oi', 0) or 0),
                'open': float(data.get('open', 0) or 0),
                'high': float(data.get('high', 0) or 0),
                'low': float(data.get('low', 0) or 0),
                'close': float(data.get('close', 0) or 0),
                'avg_price': float(data.get('average_price', 0) or 0)
            }

            if quote_data['bid'] > 0 and quote_data['ask'] > 0:
                quote_data['spread'] = quote_data['ask'] - quote_data['bid']

            self.update_option_depth(strike, option_type, quote_data)

    def update_option_depth(self, strike, option_type, depth_data):
        """Update option chain with data"""
        if strike in self.option_data:
            target = 'ce_data' if option_type == 'CE' else 'pe_data'
            current_data = self.option_data[strike][target]

            for key, value in depth_data.items():
                if key in ['volume', 'oi'] and value == 0 and current_data.get(key, 0) > 0:
                    continue
                current_data[key] = value

    def get_option_chain(self):
        """Return formatted option chain data"""
        if self.atm_strike == 0:
            if self.calculate_atm() > 0:
                self.generate_strikes()
                self.setup_subscriptions()

        data = {
            'underlying': self.underlying,
            'underlying_ltp': self.underlying_ltp,
            'underlying_bid': self.underlying_bid,
            'underlying_ask': self.underlying_ask,
            'underlying_open': self.underlying_open,
            'underlying_high': self.underlying_high,
            'underlying_low': self.underlying_low,
            'underlying_close': self.underlying_close,
            'underlying_avg': self.underlying_avg,
            'atm_strike': self.atm_strike,
            'expiry': self.expiry,
            'timestamp': datetime.now(pytz.timezone('Asia/Kolkata')).isoformat(),
            'options': list(self.option_data.values()),
            'market_metrics': self.calculate_market_metrics(),
            'mode': self.strategy_mode,
            'regime': self.current_regime,
            'paper_trade_summary': self.get_paper_trade_summary()
        }
        return data

    def update_option_tags(self):
        """Update option tags when ATM changes"""
        for strike_data in self.option_data.values():
            strike = strike_data['strike']
            position = self.get_strike_position(strike)
            strike_data['position'] = position
            strike_data['tag'] = self.get_position_tag(position)

    def get_strike_position(self, strike):
        if not self.atm_strike:
            return 0
        return (strike - self.atm_strike) // self.strike_step

    def get_position_tag(self, position):
        if position == 0:
            return 'ATM'
        elif position > 0:
            return f'OTM{abs(position)}'
        else:
            return f'ITM{abs(position)}'

    def start_monitoring(self):
        if self.monitoring_active:
            return
        self.monitoring_active = True
        self.greek_thread = threading.Thread(target=self._greek_monitor_loop)
        self.greek_thread.daemon = True
        self.greek_thread.start()

    def stop_monitoring(self):
        """Stop background threads"""
        if not self.monitoring_active:
            return
        logger.info(f"Stopping monitor for {self.underlying}")
        self.monitoring_active = False
        if self.websocket_manager:
            try:
                self.websocket_manager.unregister_handler('quote', self.handle_quote_update)
            except Exception:
                pass

    def stop(self):
        """Alias for stop_monitoring"""
        self.stop_monitoring()
