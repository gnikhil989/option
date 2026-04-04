"""
Trade Logger V3 - Enhanced Paper Trading with Signal Aggregation

Key Features:
1. Signal Aggregation (Majority Voting) - Handles whipsaw by averaging signals over time window
2. Proper handling of ALL trade types: BUY CALL, BUY PUT, SELL CALL, SELL PUT, SELL STRADDLE, SELL STRANGLE
3. Consistent Entry/Target/Stoploss with UI values
4. Proper PnL calculations for both LONG and SHORT positions
5. Exit logic with signal confirmation and majority voting
"""

import csv
import os
import time
from datetime import datetime
from collections import deque, Counter
import threading
import logging
import uuid

logger = logging.getLogger(__name__)


class SignalAggregator:
    """
    Aggregates signals over a time window and provides majority vote.
    This handles whipsaw by not reacting to every signal change.
    NOW ALSO TRACKS: strike, type, direction for proper filtering
    """

    def __init__(self, window_seconds=180, min_samples=15):
        """
        Args:
            window_seconds: Time window to aggregate signals (default 180s = 3 min)
            min_samples: Minimum samples needed before providing a signal
        """
        self.window_seconds = window_seconds
        self.min_samples = min_samples
        self.signal_history = deque(maxlen=2000)  # Increased for longer window
        self.lock = threading.Lock()
        self.current_strike = None  # Track current ATM strike

    def add_signal(self, action, score, regime=None, strike=None, trade_type=None, direction=None, contract=None):
        """Add a new signal to the history with full context"""
        with self.lock:
            # If strike changed significantly, clear old signals
            if strike and self.current_strike and abs(strike - self.current_strike) > 0:
                # Strike changed - log it
                if strike != self.current_strike:
                    logger.info(f"[SIGNAL AGG] Strike changed: {self.current_strike} → {strike}")
                self.current_strike = strike

            elif strike and not self.current_strike:
                self.current_strike = strike

            self.signal_history.append({
                'timestamp': time.time(),
                'action': action,
                'score': score,
                'regime': regime,
                'strike': strike,
                'type': trade_type,
                'direction': direction,
                'contract': contract
            })

    def get_majority_signal(self, current_strike=None):
        """
        Get the majority signal from recent history.
        Uses ACTION + STRIKE as composite key for majority calculation.
        This ensures "SELL STRADDLE @ 24000" and "SELL STRADDLE @ 24050" are counted separately.
        Returns: (action, confidence_pct, sample_count, score_avg, majority_strike)
        """
        with self.lock:
            current_time = time.time()
            cutoff = current_time - self.window_seconds

            # Filter signals within window
            recent_signals = [
                s for s in self.signal_history
                if s['timestamp'] >= cutoff
            ]

            if len(recent_signals) < self.min_samples:
                return 'WAIT', 0, len(recent_signals), 0, None

            total_count = len(recent_signals)

            # Create composite key: ACTION + STRIKE
            # e.g., "SELL STRADDLE_24000", "BUY CALL_24050"
            def get_composite_key(s):
                action = s['action']
                strike = s.get('strike')
                if strike:
                    return f"{action}_{strike}"
                return action

            # Count by composite key (action + strike)
            composite_counter = Counter(get_composite_key(s) for s in recent_signals)

            # Get most common composite key
            most_common_composite, count = composite_counter.most_common(1)[0]
            confidence = (count / total_count) * 100

            # Parse back the action and strike from composite key
            if '_' in most_common_composite:
                parts = most_common_composite.rsplit('_', 1)
                most_common_action = parts[0]
                try:
                    majority_strike = int(parts[1])
                except:
                    majority_strike = None
            else:
                most_common_action = most_common_composite
                majority_strike = None

            # Calculate average score for the majority action+strike
            majority_signals = [s for s in recent_signals if get_composite_key(s) == most_common_composite]
            avg_score = sum(s['score'] for s in majority_signals) / len(majority_signals) if majority_signals else 0

            return most_common_action, confidence, total_count, avg_score, majority_strike

    def get_signal_distribution(self, current_strike=None):
        """
        Get distribution of signals in current window for debugging.
        Shows ACTION + STRIKE combinations.
        """
        with self.lock:
            current_time = time.time()
            cutoff = current_time - self.window_seconds

            recent_signals = [
                s for s in self.signal_history
                if s['timestamp'] >= cutoff
            ]

            if not recent_signals:
                return {}

            # Create composite key: ACTION + STRIKE
            def get_composite_key(s):
                action = s['action']
                strike = s.get('strike')
                if strike:
                    return f"{action}@{strike}"  # Using @ for display clarity
                return action

            composite_counter = Counter(get_composite_key(s) for s in recent_signals)
            total = len(recent_signals)

            return {
                key: {
                    'count': count,
                    'percentage': round((count / total) * 100, 1)
                }
                for key, count in composite_counter.items()
            }

    def clear(self):
        """Clear signal history (useful after trade entry/exit)"""
        with self.lock:
            self.signal_history.clear()


class TradeLoggerV3:
    """
    Enhanced Trade Logger with Signal Aggregation and proper trade management.
    """

    def __init__(self, filename="logs/v7_paper_trades_v3.csv"):
        self.filename = filename
        self.lock = threading.Lock()

        # Signal Aggregators - CONFIGURABLE WINDOWS
        # Increased to 3 min for entry, 2 min for exit to capture more signals
        self.entry_window_seconds = 180  # 3 minutes
        self.exit_window_seconds = 120   # 2 minutes
        self.entry_min_samples = 15      # Min signals for entry decision
        self.exit_min_samples = 10       # Min signals for exit decision

        self.entry_aggregator = SignalAggregator(
            window_seconds=self.entry_window_seconds,
            min_samples=self.entry_min_samples
        )
        self.exit_aggregator = SignalAggregator(
            window_seconds=self.exit_window_seconds,
            min_samples=self.exit_min_samples
        )

        # Track current strike for filtering
        self.current_strike = None

        # Trade State
        self.active_trade = None
        self.trade_cooldown_until = 0

        # Configuration
        self.min_entry_confidence = 60  # Minimum % of signals agreeing for entry
        self.min_exit_confidence = 50   # Minimum % for exit (lower since we want to protect capital)
        self.min_hold_seconds = 60      # Minimum time to hold before signal-based exit
        self.cooldown_after_exit = 30   # Seconds to wait after exit before new entry

        # Console Logging Configuration
        self.verbose_logging = True     # Enable/disable live console logs
        self.log_interval = 5           # Log every N seconds (to reduce spam)
        self._last_log_time = 0         # Track last log time
        self._signal_count = 0          # Count signals since last log

        # Headers for CSV
        self.headers = [
            'Trade_ID',
            'Timestamp',
            'Status',           # ENTRY / EXIT
            'Symbol',           # Underlying
            'Contract',         # Option Symbol
            'Strike',
            'Type',             # CE / PE / STRADDLE / STRANGLE
            'Direction',        # BUY / SELL
            'Strategy',         # ADAPTIVE / etc
            'Regime',           # Market regime
            'Entry_Price',
            'Exit_Price',
            'Quantity',
            'Lot_Size',
            'Target',
            'StopLoss',
            'Underlying_LTP',
            'PnL_Points',
            'PnL_Amount',
            'ROI_Pct',
            'Score',
            'Confidence',
            'Reason',
            'Signal_Distribution',  # NEW: Shows signal counts
            'Entry_Confidence',     # NEW: Majority vote confidence at entry
            'Snapshot_PCR',
            'Snapshot_IV',
            'Entry_Delta',
            'Entry_Gamma',
            'Entry_Theta',
            'Time_Held_Sec',
            'Max_Profit_Seen',
            'Max_Loss_Seen'
        ]

        self._initialize_file()

    def _initialize_file(self):
        """Create file and write headers if not exists"""
        try:
            os.makedirs(os.path.dirname(self.filename), exist_ok=True)

            if not os.path.exists(self.filename):
                with open(self.filename, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(self.headers)
                logger.info(f"Created new trade log file: {self.filename}")
        except Exception as e:
            logger.error(f"Failed to initialize trade log: {e}")

    def log_trade(self, trade_data):
        """Log a trade event to CSV"""
        try:
            row = []
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with self.lock:
                with open(self.filename, 'a', newline='') as f:
                    writer = csv.writer(f)

                    for header in self.headers:
                        val = trade_data.get(header, '')

                        if header == 'Timestamp' and not val:
                            val = current_time

                        if isinstance(val, float):
                            val = f"{val:.2f}"
                        elif isinstance(val, dict):
                            val = str(val)

                        row.append(val)

                    writer.writerow(row)

            logger.info(f"Logged {trade_data.get('Status')} for {trade_data.get('Contract')}")
            return True

        except Exception as e:
            logger.error(f"Error logging trade: {e}")
            return False

    def process_signal(self, signal_data, option_data, underlying_ltp, underlying_symbol, metrics=None):
        """
        Main method to process incoming signals.
        Uses majority voting to decide on entry/exit.

        Args:
            signal_data: Dict from generate_signals() containing action, score, trade_setup, etc.
            option_data: Dict of option chain data
            underlying_ltp: Current underlying price
            underlying_symbol: e.g. 'NIFTY', 'BANKNIFTY'
            metrics: Additional metrics like PCR, IV
        """
        current_time = time.time()
        action = signal_data.get('action', 'WAIT')
        score = signal_data.get('score', 0)
        regime = signal_data.get('regime', 'UNKNOWN')
        regime_info = signal_data.get('regime_info', {})
        trade_setup = signal_data.get('trade_setup', {})

        # Extract strike, type, direction from trade_setup for proper tracking
        current_strike = trade_setup.get('strike')
        current_type = trade_setup.get('type')
        current_contract = trade_setup.get('contract', '')

        # Parse direction from action
        current_direction = 'SELL' if 'SELL' in action else 'BUY' if 'BUY' in action else None

        # Update current strike for filtering
        if current_strike:
            self.current_strike = current_strike

        # Add signal to aggregator WITH strike, type, direction info
        self.entry_aggregator.add_signal(
            action=action,
            score=score,
            regime=regime,
            strike=current_strike,
            trade_type=current_type,
            direction=current_direction,
            contract=current_contract
        )

        # LIVE CONSOLE LOGGING - Signal Tracking (now with strike info)
        self._log_signal_status(action, score, regime, underlying_ltp, underlying_symbol, current_strike, current_contract)

        # If we have an active trade, manage it
        if self.active_trade:
            self._manage_active_trade(
                signal_data, option_data, underlying_ltp,
                underlying_symbol, metrics, current_time
            )
        else:
            # Check for new entry
            self._check_entry(
                signal_data, option_data, underlying_ltp,
                underlying_symbol, metrics, current_time, trade_setup
            )

    def _log_signal_status(self, current_action, current_score, regime, underlying_ltp, symbol, current_strike=None, current_contract=None):
        """Log live signal status to console with strike tracking"""
        if not self.verbose_logging:
            return

        self._signal_count += 1
        current_time = time.time()

        # Only log at specified interval (unless it's time-critical)
        time_since_last_log = current_time - self._last_log_time
        if time_since_last_log < self.log_interval:
            return

        self._last_log_time = current_time

        # Get majority signal - FILTERED BY CURRENT STRIKE
        majority_action, confidence, sample_count, avg_score, majority_strike = self.entry_aggregator.get_majority_signal(current_strike=current_strike)
        distribution = self.entry_aggregator.get_signal_distribution(current_strike=current_strike)

        # Format distribution for display
        dist_str = " | ".join([f"{k}: {v['count']}({v['percentage']:.0f}%)" for k, v in distribution.items()])

        # Get strike-specific sample info
        strike_info = f"Strike: {current_strike}" if current_strike else "Strike: N/A"

        # Determine status
        if self.active_trade:
            # Show current P&L if we have active trade
            trade_info = f"ACTIVE: {self.active_trade['Direction']} {self.active_trade['Type']} @ {self.active_trade['Entry_Price']}"
            status_icon = "🔵"
        elif current_time < self.trade_cooldown_until:
            remaining = self.trade_cooldown_until - current_time
            trade_info = f"COOLDOWN: {remaining:.0f}s remaining"
            status_icon = "⏸️"
        elif sample_count < self.entry_aggregator.min_samples:
            trade_info = f"COLLECTING: {sample_count}/{self.entry_aggregator.min_samples} samples for {strike_info}"
            status_icon = "🔄"
        elif confidence >= self.min_entry_confidence and majority_action != 'WAIT':
            trade_info = f"READY TO ENTER: {majority_action} @ {majority_strike or current_strike}"
            status_icon = "🟢"
        else:
            trade_info = f"WAITING: Need {self.min_entry_confidence}% confidence"
            status_icon = "🟡"

        # Print formatted log with STRIKE INFO
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"\n{'='*75}")
        print(f"{status_icon} [V7] [{timestamp}] {symbol} @ {underlying_ltp:.2f} | {strike_info} | Signals: {self._signal_count}")
        print(f"{'='*75}")
        print(f"   📊 Current Signal: {current_action} | Score: {current_score:+d} | Regime: {regime}")
        print(f"   📄 Contract:       {current_contract if current_contract else 'N/A'}")
        print(f"   📈 Majority Vote:  {majority_action} ({confidence:.1f}% confidence) | Samples: {sample_count}")
        if majority_strike:
            print(f"   🎯 Majority Strike: {majority_strike}")
        print(f"   📉 Distribution:   {dist_str if dist_str else 'No data yet'}")

        # If trade is active, SHOW EXIT AGGREGATOR STATS TOO
        if self.active_trade:
            print(f"{'-'*75}")
            exit_action, exit_conf, exit_samples, exit_score, _ = self.exit_aggregator.get_majority_signal()
            exit_dist = self.exit_aggregator.get_signal_distribution()
            exit_dist_str = " | ".join([f"{k}: {v['count']}({v['percentage']:.0f}%)" for k, v in exit_dist.items()])
            
            print(f"   📉 EXIT VOTE (Fast): {exit_action} ({exit_conf:.1f}%) | Score: {exit_score:.1f}")
            print(f"      Distribution:     {exit_dist_str}")
            print(f"{'-'*75}")

        print(f"   💼 Status:         {trade_info}")

        # Show active trade details
        if self.active_trade:
            self._log_active_trade_status(underlying_ltp)

    def _log_active_trade_status(self, underlying_ltp):
        """Log current active trade P&L status"""
        trade = self.active_trade
        entry_price = trade['Entry_Price']
        target = trade['Target']
        stoploss = trade['StopLoss']
        direction = trade['Direction']

        # Calculate unrealized P&L (would need current option LTP for accurate calc)
        print(f"\n   📋 Active Trade Details:")
        print(f"      Contract: {trade['Contract']}")
        print(f"      Entry: {entry_price} | Target: {target} | SL: {stoploss}")
        print(f"      Underlying Entry: {trade.get('Entry_Underlying_LTP', 0):.2f} | Now: {underlying_ltp:.2f}")

    def _log_entry_event(self, trade, confidence, distribution):
        """Log trade entry prominently"""
        print(f"\n{'*'*70}")
        print(f"{'*'*70}")
        print(f"   🚀 [V7] TRADE ENTRY EXECUTED!")
        print(f"{'*'*70}")
        print(f"   📝 Trade ID:   {trade['Trade_ID']}")
        print(f"   📄 Contract:   {trade['Contract']}")
        print(f"   🎯 Direction:  {trade['Direction']} {trade['Type']}")
        print(f"   💰 Entry:      {trade['Entry_Price']}")
        print(f"   🎯 Target:     {trade['Target']}")
        print(f"   🛑 StopLoss:   {trade['StopLoss']}")
        print(f"   📊 Confidence: {confidence:.1f}%")
        print(f"   📈 Distribution: {distribution}")
        print(f"{'*'*70}")
        print(f"{'*'*70}\n")

    def _log_exit_event(self, trade, exit_reason, exit_price, pnl_amount, roi_pct, hold_duration):
        """Log trade exit prominently"""
        pnl_icon = "✅" if pnl_amount > 0 else "❌"
        print(f"\n{'#'*70}")
        print(f"{'#'*70}")
        print(f"   {pnl_icon} [V7] TRADE EXIT - {exit_reason}")
        print(f"{'#'*70}")
        print(f"   📝 Trade ID:   {trade['Trade_ID']}")
        print(f"   📄 Contract:   {trade['Contract']}")
        print(f"   💰 Entry:      {trade['Entry_Price']} → Exit: {exit_price}")
        print(f"   📊 PnL:        ₹{pnl_amount:,.2f} ({roi_pct:+.2f}%)")
        print(f"   ⏱️ Duration:   {hold_duration/60:.1f} minutes")
        print(f"{'#'*70}")
        print(f"{'#'*70}\n")

    def _check_entry(self, signal_data, option_data, underlying_ltp,
                     underlying_symbol, metrics, current_time, trade_setup):
        """Check if we should enter a new trade based on majority voting"""

        # Cooldown check
        if current_time < self.trade_cooldown_until:
            remaining = self.trade_cooldown_until - current_time
            logger.debug(f"[TRADE LOGGER] Cooldown active: {remaining:.1f}s remaining")
            return

        # Get current strike from trade_setup for filtering
        current_strike = trade_setup.get('strike') if trade_setup else self.current_strike

        # Get majority signal - FILTERED BY CURRENT STRIKE
        majority_action, confidence, sample_count, avg_score, majority_strike = self.entry_aggregator.get_majority_signal(current_strike=current_strike)

        # Log signal distribution for debugging
        distribution = self.entry_aggregator.get_signal_distribution(current_strike=current_strike)
        if distribution:
            logger.debug(f"[TRADE LOGGER] Signal Distribution for Strike {current_strike}: {distribution}")

        # Check if we have enough confidence and it's an actionable signal
        if majority_action == 'WAIT':
            logger.debug(f"[TRADE LOGGER] Majority says WAIT (confidence: {confidence:.1f}%, samples: {sample_count})")
            return

        if confidence < self.min_entry_confidence:
            logger.debug(f"[TRADE LOGGER] Confidence too low: {confidence:.1f}% < {self.min_entry_confidence}%")
            return

        if sample_count < self.entry_aggregator.min_samples:
            logger.debug(f"[TRADE LOGGER] Not enough samples: {sample_count} < {self.entry_aggregator.min_samples}")
            return

        # Parse the action to determine trade type
        trade_type, direction = self._parse_action(majority_action)
        if not trade_type:
            logger.warning(f"[TRADE LOGGER] Could not parse action: {majority_action}")
            return

        # Use trade_setup from UI if available AND consistent with majority action
        # If majority action differs significantly (e.g. BUY vs SELL), disregard setup to avoid bad levels
        use_provided_setup = False
        if trade_setup:
            setup_action = signal_data.get('action', '')
            # Simple consistency check: Directions must match
            if 'BUY' in majority_action and 'BUY' in setup_action:
                use_provided_setup = True
            elif 'SELL' in majority_action and 'SELL' in setup_action:
                use_provided_setup = True
            # Allow WAIT to be overridden
            elif setup_action == 'WAIT': 
                use_provided_setup = False
            
            if not use_provided_setup:
                logger.warning(f"[TRADE LOGGER] Signal Conflict: Majority {majority_action} vs Setup {setup_action}. Recalculating levels.")

        if use_provided_setup and trade_setup.get('entry'):
            entry_price = trade_setup['entry']
            target = trade_setup.get('target', 0)
            stoploss = trade_setup.get('sl', 0)
            contract_name = trade_setup.get('contract', '')
            # Use strike from trade_setup directly if available
            strike = trade_setup.get('strike') or self._extract_strike_from_contract(contract_name, option_data)
            legs = trade_setup.get('legs', [])
            # Use type from trade_setup if available (for STRADDLE/STRANGLE detection)
            if trade_setup.get('type'):
                setup_type = trade_setup['type']
                if setup_type in ['STRADDLE', 'STRANGLE']:
                    trade_type = setup_type
        else:
            # Fallback: calculate from option_data
            # This handles the "Conflict" case by generating fresh valid levels for the Majority Action
            entry_info = self._get_entry_info(trade_type, direction, option_data, underlying_symbol)
            if not entry_info:
                logger.warning(f"[TRADE LOGGER] Could not get entry info for {trade_type} {direction}")
                return

            entry_price = entry_info['ltp']
            strike = entry_info['strike']
            contract_name = entry_info['contract']
            
            # Recalculate levels for the TRUE action (Majority)
            # We pass empty regime_info to force default level calculation
            target, stoploss = self._calculate_levels(entry_price, direction, {})
            legs = entry_info.get('legs', [])

        if entry_price <= 0:
            logger.warning(f"[TRADE LOGGER] Invalid entry price: {entry_price}")
            return

        # Determine lot size
        lot_size = self._get_lot_size(underlying_symbol)

        # Get Greeks if available
        entry_delta, entry_gamma, entry_theta = self._get_greeks(strike, trade_type, option_data)

        # Create trade
        trade_id = f"TRD_{uuid.uuid4().hex[:8]}"

        self.active_trade = {
            'Trade_ID': trade_id,
            'Entry_Time': datetime.now().isoformat(),
            'Symbol': underlying_symbol,
            'Contract': contract_name,
            'Strike': strike,
            'Type': trade_type,
            'Direction': direction,
            'Strategy': signal_data.get('mode', 'ADAPTIVE'),
            'Entry_Price': entry_price,
            'Quantity': 1,
            'Lot_Size': lot_size,
            'Target': target,
            'StopLoss': stoploss,
            'Entry_Underlying_LTP': underlying_ltp,
            'Entry_Delta': entry_delta,
            'Entry_Gamma': entry_gamma,
            'Entry_Theta': entry_theta,
            'Max_Price_Seen': entry_price,
            'Min_Price_Seen': entry_price,
            'Entry_Confidence': confidence,
            'Entry_Distribution': distribution,
            'Legs': legs
        }

        # Log entry to CSV
        log_data = {
            'Trade_ID': trade_id,
            'Status': 'ENTRY',
            'Symbol': underlying_symbol,
            'Contract': contract_name,
            'Strike': strike,
            'Type': trade_type,
            'Direction': direction,
            'Strategy': signal_data.get('mode', 'ADAPTIVE'),
            'Regime': signal_data.get('regime', 'UNKNOWN'),
            'Entry_Price': entry_price,
            'Exit_Price': '',
            'Quantity': 1,
            'Lot_Size': lot_size,
            'Target': target,
            'StopLoss': stoploss,
            'Underlying_LTP': underlying_ltp,
            'PnL_Points': '',
            'PnL_Amount': '',
            'ROI_Pct': '',
            'Score': int(avg_score),
            'Confidence': signal_data.get('regime_info', {}).get('confidence', 0),
            'Reason': f"Majority: {majority_action} ({confidence:.1f}% confidence)",
            'Signal_Distribution': str(distribution),
            'Entry_Confidence': round(confidence, 1),
            'Snapshot_PCR': metrics.get('pcr', 0) if metrics else 0,
            'Snapshot_IV': metrics.get('iv', 0) if metrics else 0,
            'Entry_Delta': round(entry_delta, 4),
            'Entry_Gamma': round(entry_gamma, 6),
            'Entry_Theta': round(entry_theta, 4),
            'Time_Held_Sec': '',
            'Max_Profit_Seen': '',
            'Max_Loss_Seen': ''
        }

        self.log_trade(log_data)

        # Clear entry aggregator after entry
        self.entry_aggregator.clear()

        # CONSOLE LOG - Prominent Entry Notification
        if self.verbose_logging:
            self._log_entry_event(self.active_trade, confidence, distribution)

        logger.info(f"[TRADE LOGGER] ENTRY: {direction} {trade_type} @ {entry_price} | "
                   f"Target: {target} | SL: {stoploss} | Confidence: {confidence:.1f}%")

    def _manage_active_trade(self, signal_data, option_data, underlying_ltp,
                             underlying_symbol, metrics, current_time):
        """Monitor and manage an active trade"""

        trade = self.active_trade

        # Get current LTP for the position
        current_ltp = self._get_current_ltp(trade, option_data)
        if current_ltp <= 0:
            logger.debug(f"[TRADE LOGGER] No LTP data for {trade['Contract']}")
            return

        # Update max/min seen
        if current_ltp > trade['Max_Price_Seen']:
            trade['Max_Price_Seen'] = current_ltp
        if current_ltp < trade['Min_Price_Seen']:
            trade['Min_Price_Seen'] = current_ltp

        # Calculate hold duration
        try:
            entry_time = datetime.fromisoformat(trade['Entry_Time'])
            hold_duration = (datetime.now() - entry_time).total_seconds()
        except:
            hold_duration = 0

        exit_reason = None
        exit_price = current_ltp

        direction = trade['Direction']

        # Check hard stops (Target/StopLoss)
        if direction == 'SELL':
            # SHORT: Profit if price drops, Loss if price rises
            if current_ltp >= trade['StopLoss']:
                exit_reason = "SL HIT"
                exit_price = trade['StopLoss']
            elif current_ltp <= trade['Target']:
                exit_reason = "TARGET HIT"
                exit_price = trade['Target']
        else:
            # LONG: Profit if price rises, Loss if price drops
            if current_ltp <= trade['StopLoss']:
                exit_reason = "SL HIT"
                exit_price = trade['StopLoss']
            elif current_ltp >= trade['Target']:
                exit_reason = "TARGET HIT"
                exit_price = trade['Target']

        # Signal-based exit (only if no hard stop hit and min hold time passed)
        if not exit_reason and hold_duration >= self.min_hold_seconds:
            exit_reason = self._check_signal_exit(signal_data, trade)

        # Execute exit if needed
        if exit_reason:
            self._execute_exit(trade, exit_reason, exit_price, current_ltp,
                              signal_data, metrics, hold_duration)

    def _check_signal_exit(self, signal_data, trade):
        """Check if we should exit based on signal changes using majority voting"""

        current_action = signal_data.get('action', 'WAIT')
        score = signal_data.get('score', 0)

        # Add to exit aggregator
        self.exit_aggregator.add_signal(current_action, score)

        majority_action, confidence, sample_count, avg_score, _ = self.exit_aggregator.get_majority_signal()

        if sample_count < self.exit_aggregator.min_samples:
            return None

        if confidence < self.min_exit_confidence:
            return None

        trade_type = trade['Type']
        trade_dir = trade['Direction']

        # Determine if signal has flipped
        is_signal_valid = False

        if trade_dir == 'SELL':
            # SHORT positions
            if trade_type in ['STRADDLE', 'STRANGLE']:
                # Stay in theta trades if market remains range-bound
                if 'SELL' in majority_action or majority_action == 'WAIT':
                    is_signal_valid = True
                elif signal_data.get('regime') != 'CONTRARIAN_REVERSAL' and abs(avg_score) < 40:  # Still range-bound
                    is_signal_valid = True
            elif trade_type == 'CE':
                # Short CE valid on bearish/neutral
                if 'SELL' in majority_action:
                    is_signal_valid = True
                # Only use score if NOT in Contrarian mode (Contrarian ignores score sign)
                elif signal_data.get('regime') != 'CONTRARIAN_REVERSAL' and avg_score <= 0:
                    is_signal_valid = True
            elif trade_type == 'PE':
                # Short PE valid on bullish/neutral
                if 'SELL' in majority_action:
                    is_signal_valid = True
                elif signal_data.get('regime') != 'CONTRARIAN_REVERSAL' and avg_score >= 0:
                    is_signal_valid = True
        else:
            # LONG positions
            if trade_type == 'CE':
                # Long CE valid on bullish
                if 'BUY CALL' in majority_action:
                    is_signal_valid = True
                elif signal_data.get('regime') != 'CONTRARIAN_REVERSAL' and avg_score > 0:
                    is_signal_valid = True
            elif trade_type == 'PE':
                # Long PE valid on bearish
                if 'BUY PUT' in majority_action:
                    is_signal_valid = True
                elif signal_data.get('regime') != 'CONTRARIAN_REVERSAL' and avg_score < 0:
                    is_signal_valid = True

        if not is_signal_valid and majority_action != 'WAIT':
            distribution = self.exit_aggregator.get_signal_distribution()
            return f"SIGNAL FLIP ({majority_action} @ {confidence:.0f}%)"

        return None

    def _execute_exit(self, trade, exit_reason, exit_price, current_ltp,
                      signal_data, metrics, hold_duration):
        """Execute trade exit and log to CSV"""

        direction = trade['Direction']

        # Calculate PnL based on direction
        if direction == 'SELL':
            # SHORT: PnL = Entry - Exit
            pnl_points = trade['Entry_Price'] - exit_price
        else:
            # LONG: PnL = Exit - Entry
            pnl_points = exit_price - trade['Entry_Price']

        lot_size = trade['Lot_Size']
        pnl_amount = pnl_points * lot_size * trade['Quantity']
        roi_pct = (pnl_points / trade['Entry_Price'] * 100) if trade['Entry_Price'] > 0 else 0

        # Calculate max profit/loss seen
        if direction == 'SELL':
            max_profit_seen = trade['Entry_Price'] - trade['Min_Price_Seen']
            max_loss_seen = trade['Max_Price_Seen'] - trade['Entry_Price']
        else:
            max_profit_seen = trade['Max_Price_Seen'] - trade['Entry_Price']
            max_loss_seen = trade['Entry_Price'] - trade['Min_Price_Seen']

        exit_direction = 'BUY' if direction == 'SELL' else 'SELL'

        # Get current signal distribution for logging
        distribution = self.exit_aggregator.get_signal_distribution()

        log_data = {
            'Trade_ID': trade['Trade_ID'],
            'Status': 'EXIT',
            'Symbol': trade['Symbol'],
            'Contract': trade['Contract'],
            'Strike': trade['Strike'],
            'Type': trade['Type'],
            'Direction': exit_direction,
            'Strategy': trade['Strategy'],
            'Regime': signal_data.get('regime', 'UNKNOWN'),
            'Entry_Price': trade['Entry_Price'],
            'Exit_Price': exit_price,
            'Quantity': trade['Quantity'],
            'Lot_Size': lot_size,
            'Target': trade['Target'],
            'StopLoss': trade['StopLoss'],
            'Underlying_LTP': signal_data.get('trade_setup', {}).get('underlying_ltp', 0),
            'PnL_Points': round(pnl_points, 2),
            'PnL_Amount': round(pnl_amount, 2),
            'ROI_Pct': round(roi_pct, 2),
            'Score': signal_data.get('score', 0),
            'Confidence': signal_data.get('regime_info', {}).get('confidence', 0),
            'Reason': exit_reason,
            'Signal_Distribution': str(distribution),
            'Entry_Confidence': trade.get('Entry_Confidence', 0),
            'Snapshot_PCR': metrics.get('pcr', 0) if metrics else 0,
            'Snapshot_IV': metrics.get('iv', 0) if metrics else 0,
            'Entry_Delta': trade.get('Entry_Delta', 0),
            'Entry_Gamma': trade.get('Entry_Gamma', 0),
            'Entry_Theta': trade.get('Entry_Theta', 0),
            'Time_Held_Sec': int(hold_duration),
            'Max_Profit_Seen': round(max_profit_seen, 2),
            'Max_Loss_Seen': round(max_loss_seen, 2)
        }

        self.log_trade(log_data)

        # CONSOLE LOG - Prominent Exit Notification
        if self.verbose_logging:
            self._log_exit_event(trade, exit_reason, exit_price, pnl_amount, roi_pct, hold_duration)

        # Reset state
        self.active_trade = None
        self.trade_cooldown_until = time.time() + self.cooldown_after_exit
        self.exit_aggregator.clear()

        logger.info(f"[TRADE LOGGER] EXIT: {exit_reason} | PnL: {pnl_amount:.2f} | "
                   f"ROI: {roi_pct:.2f}% | Held: {hold_duration/60:.1f}min")

    def _parse_action(self, action):
        """Parse action string to get trade type and direction"""
        action = action.upper()

        if 'STRADDLE' in action:
            direction = 'SELL' if 'SELL' in action else 'BUY'
            return 'STRADDLE', direction
        elif 'STRANGLE' in action:
            direction = 'SELL' if 'SELL' in action else 'BUY'
            return 'STRANGLE', direction
        elif 'CALL' in action:
            direction = 'SELL' if 'SELL' in action else 'BUY'
            return 'CE', direction
        elif 'PUT' in action:
            direction = 'SELL' if 'SELL' in action else 'BUY'
            return 'PE', direction

        return None, None

    def _get_entry_info(self, trade_type, direction, option_data, underlying_symbol):
        """Get entry information from option data"""

        if not option_data:
            return None

        # Find ATM strike
        strikes = sorted(option_data.keys())
        if not strikes:
            return None

        atm_strike = None
        for strike in strikes:
            if option_data[strike].get('tag') == 'ATM':
                atm_strike = strike
                break

        if not atm_strike and strikes:
            atm_strike = strikes[len(strikes) // 2]

        if not atm_strike:
            return None

        if trade_type == 'STRADDLE':
            ce_data = option_data[atm_strike].get('ce_data', {})
            pe_data = option_data[atm_strike].get('pe_data', {})
            combined_ltp = ce_data.get('ltp', 0) + pe_data.get('ltp', 0)

            return {
                'strike': atm_strike,
                'contract': f"STRADDLE {atm_strike}",
                'ltp': combined_ltp,
                'legs': [
                    {'name': option_data[atm_strike].get('ce_symbol'), 'ltp': ce_data.get('ltp', 0), 'type': 'CE'},
                    {'name': option_data[atm_strike].get('pe_symbol'), 'ltp': pe_data.get('ltp', 0), 'type': 'PE'}
                ]
            }

        elif trade_type == 'STRANGLE':
            # OTM strikes for strangle
            strike_step = strikes[1] - strikes[0] if len(strikes) > 1 else 50
            ce_strike = atm_strike + strike_step
            pe_strike = atm_strike - strike_step

            ce_data = option_data.get(ce_strike, {}).get('ce_data', {})
            pe_data = option_data.get(pe_strike, {}).get('pe_data', {})
            combined_ltp = ce_data.get('ltp', 0) + pe_data.get('ltp', 0)

            return {
                'strike': atm_strike,  # Reference strike
                'contract': f"STRANGLE {pe_strike}/{ce_strike}",
                'ltp': combined_ltp,
                'legs': [
                    {'name': option_data.get(ce_strike, {}).get('ce_symbol'), 'ltp': ce_data.get('ltp', 0), 'type': 'CE'},
                    {'name': option_data.get(pe_strike, {}).get('pe_symbol'), 'ltp': pe_data.get('ltp', 0), 'type': 'PE'}
                ]
            }

        else:
            # Single leg (CE or PE)
            tag = 'ce_data' if trade_type == 'CE' else 'pe_data'
            symbol_key = 'ce_symbol' if trade_type == 'CE' else 'pe_symbol'

            data = option_data[atm_strike].get(tag, {})

            return {
                'strike': atm_strike,
                'contract': option_data[atm_strike].get(symbol_key, ''),
                'ltp': data.get('ltp', 0)
            }

    def _get_current_ltp(self, trade, option_data):
        """Get current LTP for active trade"""

        strike = trade['Strike']
        trade_type = trade['Type']

        if strike not in option_data:
            return 0

        if trade_type in ['STRADDLE', 'STRANGLE']:
            # Combined CE + PE
            ce_ltp = option_data[strike]['ce_data'].get('ltp', 0)
            pe_ltp = option_data[strike]['pe_data'].get('ltp', 0)

            # For strangle, we need OTM strikes
            if trade_type == 'STRANGLE' and trade.get('Legs'):
                # Sum up the legs from actual positions
                total_ltp = 0
                for leg in trade['Legs']:
                    leg_strike = self._extract_strike_from_contract(leg.get('name', ''), option_data)
                    leg_type = leg.get('type', 'CE')
                    if leg_strike in option_data:
                        tag = 'ce_data' if leg_type == 'CE' else 'pe_data'
                        total_ltp += option_data[leg_strike][tag].get('ltp', 0)
                return total_ltp

            return ce_ltp + pe_ltp
        else:
            tag = 'ce_data' if trade_type == 'CE' else 'pe_data'
            return option_data[strike][tag].get('ltp', 0)

    def _calculate_levels(self, entry_price, direction, regime_info):
        """Calculate target and stoploss based on direction and regime"""

        if entry_price <= 0:
            return 0, 0

        # Get thresholds from regime if available
        thresholds = regime_info.get('thresholds', {})

        # Defaults
        if direction == 'SELL':
            target_pct = 0.50  # 50% profit (price drops to 50% of entry)
            sl_pct = 0.40     # 40% loss (price rises to 140% of entry)
        else:
            target_pct = 0.30  # 30% profit
            sl_pct = 0.15     # 15% loss

        # Parse from thresholds if available
        import re

        if thresholds.get('target'):
            match = re.search(r'(\d+)', str(thresholds['target']))
            if match:
                target_pct = float(match.group(1)) / 100.0

        if thresholds.get('sl'):
            match = re.search(r'(\d+)', str(thresholds['sl']))
            if match:
                sl_pct = float(match.group(1)) / 100.0

        # Calculate levels based on direction
        if direction == 'SELL':
            target = round(entry_price * (1 - target_pct), 1)
            stoploss = round(entry_price * (1 + sl_pct), 1)
            target = max(0.1, target)
        else:
            target = round(entry_price * (1 + target_pct), 1)
            stoploss = round(entry_price * (1 - sl_pct), 1)

        return target, stoploss

    def _get_lot_size(self, underlying_symbol):
        """Get lot size for underlying"""
        lot_sizes = {
            'NIFTY': 50,
            'BANKNIFTY': 15,
            'SENSEX': 10,
            'FINNIFTY': 40
        }
        return lot_sizes.get(underlying_symbol, 50)

    def _get_greeks(self, strike, trade_type, option_data):
        """Get Greeks for the position"""

        if strike not in option_data:
            return 0, 0, 0

        if trade_type in ['STRADDLE', 'STRANGLE']:
            ce_data = option_data[strike].get('ce_data', {})
            pe_data = option_data[strike].get('pe_data', {})

            delta = ce_data.get('delta', 0) + pe_data.get('delta', 0)
            gamma = ce_data.get('gamma', 0) + pe_data.get('gamma', 0)
            theta = ce_data.get('theta', 0) + pe_data.get('theta', 0)
        else:
            tag = 'ce_data' if trade_type == 'CE' else 'pe_data'
            data = option_data[strike].get(tag, {})

            delta = data.get('delta', 0)
            gamma = data.get('gamma', 0)
            theta = data.get('theta', 0)

        return delta, gamma, theta

    def _extract_strike_from_contract(self, contract_name, option_data):
        """Extract strike from contract name or find matching strike"""

        if not contract_name or not option_data:
            return None

        # Try to find matching symbol
        for strike, data in option_data.items():
            if data.get('ce_symbol') == contract_name or data.get('pe_symbol') == contract_name:
                return strike

        # Try to extract from string
        import re
        match = re.search(r'(\d{5,6})', contract_name)
        if match:
            potential_strike = int(match.group(1))
            if potential_strike in option_data:
                return potential_strike

        return None

    def get_active_trade(self):
        """Return current active trade for UI display"""
        return self.active_trade

    def get_signal_stats(self):
        """Get current signal statistics for debugging"""
        return {
            'entry_distribution': self.entry_aggregator.get_signal_distribution(),
            'exit_distribution': self.exit_aggregator.get_signal_distribution(),
            'has_active_trade': self.active_trade is not None,
            'cooldown_remaining': max(0, self.trade_cooldown_until - time.time())
        }

    def get_recent_trades(self, limit=10):
        """Read back recent trades for UI display"""
        trades = []
        try:
            if not os.path.exists(self.filename):
                return []

            with open(self.filename, 'r') as f:
                reader = csv.DictReader(f)
                all_rows = list(reader)
                trades = all_rows[-limit:]
                trades.reverse()
        except Exception as e:
            logger.error(f"Error reading trade log: {e}")
        return trades

    def get_performance_summary(self):
        """Calculate and return performance metrics"""
        trades = self.get_recent_trades(limit=1000)
        if not trades:
            return {'total_trades': 0, 'message': 'No trades logged yet'}

        completed_trades = [t for t in trades if t.get('Status') == 'EXIT']
        if not completed_trades:
            return {
                'total_trades': 0,
                'active_trade': self.active_trade,
                'message': 'Waiting for first trade to complete'
            }

        total_pnl = sum(float(t.get('PnL_Amount', 0) or 0) for t in completed_trades)
        winning_trades = [t for t in completed_trades if float(t.get('PnL_Amount', 0) or 0) > 0]
        losing_trades = [t for t in completed_trades if float(t.get('PnL_Amount', 0) or 0) < 0]

        win_rate = (len(winning_trades) / len(completed_trades) * 100) if completed_trades else 0

        avg_win = sum(float(t.get('PnL_Amount', 0)) for t in winning_trades) / len(winning_trades) if winning_trades else 0
        avg_loss = sum(float(t.get('PnL_Amount', 0)) for t in losing_trades) / len(losing_trades) if losing_trades else 0

        avg_hold_time = sum(int(t.get('Time_Held_Sec', 0) or 0) for t in completed_trades) / len(completed_trades) if completed_trades else 0

        # Calculate by trade type
        by_type = {}
        for t in completed_trades:
            trade_type = t.get('Type', 'UNKNOWN')
            if trade_type not in by_type:
                by_type[trade_type] = {'count': 0, 'pnl': 0, 'wins': 0}
            by_type[trade_type]['count'] += 1
            by_type[trade_type]['pnl'] += float(t.get('PnL_Amount', 0) or 0)
            if float(t.get('PnL_Amount', 0) or 0) > 0:
                by_type[trade_type]['wins'] += 1

        return {
            'total_trades': len(completed_trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': round(win_rate, 2),
            'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'avg_hold_time_min': round(avg_hold_time / 60, 2),
            'by_type': by_type,
            'active_trade': self.active_trade,
            'recent_trades': completed_trades[-5:]
        }
