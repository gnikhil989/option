from utils.trade_logger_v10 import TradeLoggerv10
import logging
import time
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

# Market timing constants (IST)
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30
FORCE_EXIT_HOUR, FORCE_EXIT_MIN = 15, 20  # Force close all positions 10 min before close
LAST_ENTRY_HOUR, LAST_ENTRY_MIN = 14, 45  # No new entries after this time (too little time to play out)


def _ist_now():
    return datetime.now(IST)


def _is_market_hours():
    now = _ist_now()
    open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0)
    close_time = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
    return open_time <= now <= close_time


def _is_force_exit_time():
    """Returns True after 3:20 PM IST - all positions must be closed."""
    now = _ist_now()
    return now.hour > FORCE_EXIT_HOUR or (now.hour == FORCE_EXIT_HOUR and now.minute >= FORCE_EXIT_MIN)


class BaseStrategy:
    """Base Class for v10 Modular Strategies.

    Subclasses MUST override `should_enter()` to define their edge.

    Base provides:
    - Signal aggregation with majority voting
    - Trailing SL (50%/75%/100% profit levels)
    - Trailing target extension for trend riding (single lot)
    - DTE-aware SL/Target scaling
    - Force exit at 3:20 PM IST
    - Loss streak circuit breaker
    - Daily trade limits
    """
    def __init__(self, name, entry_score=15, agg_window=90, min_conf=60,
                 min_hold=60, exit_style='conservative', max_trades=999999):
        self.name = name
        self.entry_score_threshold = entry_score
        self.agg_window = agg_window
        self.min_confidence = min_conf
        self.min_hold_seconds = min_hold
        self.exit_style = exit_style

        self.logger = TradeLoggerv10(
            filename=f"logs/v10_{name.lower()}_trades.csv",
            entry_window=agg_window,
            exit_window=agg_window // 2,
            min_samples=max(5, agg_window // 3)
        )

        self.active_trade = None
        self.trades_today = 0
        self.max_trades_per_day = max_trades
        self.exit_cooldown_until = 0
        self.daily_pnl = 0
        self.consecutive_losses = 0

    # ------------------------------------------------------------------
    # MAIN LOOP
    # ------------------------------------------------------------------
    def process_signals(self, manager, metrics):
        current_time = time.time()
        raw_score = metrics['raw_score']

        # FORCE EXIT: Close any open trade after 3:20 PM
        if self.active_trade and _is_force_exit_time():
            self._force_exit_active_trade(manager, metrics, "EOD FORCE EXIT (3:20 PM)")
            return

        # Don't process outside market hours
        if not _is_market_hours():
            return

        # Determine instantaneous action
        instant_action = 'WAIT'
        if raw_score > self.entry_score_threshold: instant_action = 'BUY CALL'
        elif raw_score < -self.entry_score_threshold: instant_action = 'BUY PUT'

        # Update Aggregator
        current_strike = manager.atm_strike
        current_contract = None
        if current_strike != 0 and instant_action != 'WAIT':
            opt_type = 'CE' if 'CALL' in instant_action else 'PE'
            current_contract = manager.construct_option_symbol(current_strike, opt_type)

        self.logger.entry_aggregator.add_signal(
            action=instant_action,
            score=raw_score,
            regime=manager.guardian.current_regime,
            strike=current_strike,
            contract=current_contract
        )

        # Heartbeat (every 60s)
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            maj_act, conf, samples, avg_score, _ = self.logger.entry_aggregator.get_majority_signal()
            print(f"[{self.name}] Score:{raw_score:+d} | Majority:{maj_act}({conf:.0f}%) | Trades:{self.trades_today}/{self.max_trades_per_day} | PnL:{self.daily_pnl:+.1f}")

        # Manage existing trade or check for new entry
        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_active_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            # Loss streak circuit breaker
            if self.consecutive_losses >= 9999:
                extra_cooldown = 300 * self.consecutive_losses
                if current_time < self.exit_cooldown_until + extra_cooldown:
                    return
            # Don't enter new trades after 2:45 PM (not enough time to play out)
            now = _ist_now()
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN):
                return
            self._check_entry(manager, metrics)

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        """Override in subclass to define strategy-specific entry logic."""
        return True

    # ------------------------------------------------------------------
    # ENTRY LOGIC
    # ------------------------------------------------------------------
    def _check_entry(self, manager, metrics):
        majority_action, confidence, sample_count, avg_score, majority_strike = \
            self.logger.entry_aggregator.get_majority_signal()

        if majority_action == 'WAIT': return
        if sample_count < self.logger.entry_min_samples: return
        if confidence < self.min_confidence: return

        # MARKET REGIME GATE: Throttle entries on SIDEWAYS/CHOPPY days
        # April 1 analysis: 47 trades on choppy day = -₹32K. Only EOD holds made money.
        market_type = metrics.get('market_regime_type', 'DETECTING')

        if market_type == 'CHOPPY':
            # CHOPPY: Only allow BUY entries with very high confidence (80%+)
            # SELL strategies (ThetaSell) can still enter at normal confidence
            if 'BUY' in majority_action and confidence < 80:
                return
            # Also require 3+ strong signals instead of 2
            quant = metrics.get('quant_signal', {})
            breakdown = quant.get('scores_breakdown', {})
            strong = sum(1 for v in breakdown.values() if abs(v) >= 10)
            if strong < 3:
                return

        elif market_type == 'SIDEWAYS':
            # SIDEWAYS: Block BUY strategies entirely (theta kills them)
            # Only SELL strategies should trade in sideways markets
            if 'BUY' in majority_action:
                return

        # Regime check
        guardian_regime = manager.guardian.update(
            'BULLISH' if avg_score > 0 else 'BEARISH',
            confidence, score=avg_score
        )
        if 'CALL' in majority_action and guardian_regime == 'BEARISH': return
        if 'PUT' in majority_action and guardian_regime == 'BULLISH': return

        # Signal confluence
        quant = metrics.get('quant_signal', {})
        if not self._has_signal_confluence(quant):
            return

        # Strategy-specific check
        if not self.should_enter(manager, metrics, majority_action, avg_score, confidence):
            return

        self._execute_entry(manager, majority_action, avg_score, metrics)

    def _has_signal_confluence(self, quant_signal):
        """Require 2+ strong individual signals agreeing on direction.
        Uses scores_breakdown dict (not string parsing) for reliability.
        """
        breakdown = quant_signal.get('scores_breakdown', {})
        if not breakdown:
            return False

        score = quant_signal.get('score', 0)
        strong_bull = sum(1 for v in breakdown.values() if v >= 10)
        strong_bear = sum(1 for v in breakdown.values() if v <= -10)

        if score > 0:
            return strong_bull >= 2
        elif score < 0:
            return strong_bear >= 2
        return False

    def _execute_entry(self, manager, action, score, metrics):
        bias = 'BULLISH' if 'CALL' in action else 'BEARISH'
        contract_info = manager._select_best_strike(bias)
        if not contract_info: return
        ltp = contract_info['ltp']
        if ltp <= 0: return

        # DTE-aware SL/Target scaling
        dte = metrics.get('days_to_expiry', 5)
        sl_points, target_points = self._calc_sl_target(ltp, contract_info, manager, dte)

        sl = ltp - sl_points
        target = ltp + target_points

        # Capture entry context for CSV logging
        majority_action, confidence, sample_count, avg_score, _ = \
            self.logger.entry_aggregator.get_majority_signal()
        distribution = self.logger.entry_aggregator.get_signal_distribution()

        trade_id = f"{self.name}_{int(time.time())}"
        self.active_trade = {
            'Trade_ID': trade_id,
            'Symbol': manager.underlying,
            'Contract': contract_info['name'],
            'strike': contract_info['strike'],
            'type': contract_info['type'],
            'direction': 'BUY',
            'entry_price': ltp,
            'sl': sl,
            'initial_sl': sl,
            'target': target,
            'initial_target': target,
            'target_extensions': 0,
            'entry_time': datetime.now().isoformat(),
            'Max_Price_Seen': ltp,
            'Min_Price_Seen': ltp,  # Track lowest price for Max_Loss_Seen
            'Strategy': self.name,
            'Lot_Size': self.logger._get_lot_size(manager.underlying),
            # Entry context (stored for EXIT log row)
            '_entry_confidence': round(confidence, 1),
            '_entry_distribution': str(distribution),
            '_entry_pcr': round(metrics.get('pcr', 0), 4),
            '_entry_iv': round(contract_info.get('iv', 0), 2),
            '_entry_delta': round(contract_info.get('delta', 0), 4),
            '_entry_gamma': round(manager.option_data.get(contract_info['strike'], {})
                                  .get('ce_data' if contract_info['type'] == 'CE' else 'pe_data', {})
                                  .get('gamma', 0), 6),
            '_entry_theta': round(manager.option_data.get(contract_info['strike'], {})
                                  .get('ce_data' if contract_info['type'] == 'CE' else 'pe_data', {})
                                  .get('theta', 0), 4),
            '_entry_vwap': round(metrics.get('vwap', 0), 2),
            '_entry_oi_wall_ce': metrics.get('oi_wall_ce', 0),
            '_entry_oi_wall_pe': metrics.get('oi_wall_pe', 0),
            '_entry_oi_pattern': metrics.get('oi_pattern', ''),
            '_entry_score': score,
        }

        logger.info(f"[{self.name}] ENTRY {action} @ {ltp} | SL:{sl:.2f} | Tgt:{target:.2f} | DTE:{dte:.1f}")

        log_entry = self._build_log_entry(manager, self.active_trade, 'ENTRY', ltp, score,
                                          f"Majority: {action}", metrics)
        self.logger.log_trade(log_entry)
        self.logger.entry_aggregator.clear()

    def _calc_sl_target(self, ltp, contract_info, manager, dte):
        """DTE-aware SL/Target calculation.

        March 20 analysis showed targets at +40-60% were NEVER hit.
        Root cause: delta conversion doesn't account for theta decay during hold.
        Fix: Reduced base targets by 40%. Added theta-decay discount for expiry week.
        """
        atm_iv = contract_info.get('iv', 0)
        if atm_iv <= 0: atm_iv = 15.0
        vol_multiplier = max(0.7, min(2.5, atm_iv / 15.0))

        delta = abs(contract_info.get('delta', 0.5))
        if delta < 0.1: delta = 0.5

        # DTE scaling: aggressive reduction near expiry
        if dte <= 0.5:  # Expiry day
            dte_scale = 0.35  # 35% of normal (was 50% - still too wide)
        elif dte <= 1:
            dte_scale = 0.50  # (was 70%)
        elif dte <= 2:
            dte_scale = 0.65  # (was 85%)
        elif dte <= 3:
            dte_scale = 0.80
        else:
            dte_scale = 1.0

        # Base targets reduced by 40% from original values
        if self.exit_style == 'aggressive':
            underlying_sl_pct = 0.002 * vol_multiplier * dte_scale  # was 0.003
            underlying_tgt_pct = 0.004 * vol_multiplier * dte_scale  # was 0.006
            underlying_tgt_pct = 0.006 * vol_multiplier * dte_scale
        elif self.exit_style == 'scalp':
            underlying_sl_pct = 0.002 * vol_multiplier * dte_scale
            underlying_tgt_pct = 0.002 * vol_multiplier * dte_scale  # was 0.003
        else:
            underlying_sl_pct = 0.003 * vol_multiplier * dte_scale  # was 0.004
            underlying_tgt_pct = 0.005 * vol_multiplier * dte_scale  # was 0.008

        underlying_price = manager.underlying_ltp if manager.underlying_ltp > 0 else 1
        sl_points = underlying_price * underlying_sl_pct * delta
        target_points = underlying_price * underlying_tgt_pct * delta

        min_sl = max(ltp * 0.08, 2.0)
        min_target = max(ltp * 0.10, 3.0)
        return max(sl_points, min_sl), max(target_points, min_target)

    # ------------------------------------------------------------------
    # TRADE MANAGEMENT (Trailing SL + Trailing Target for trend riding)
    # ------------------------------------------------------------------
    def _get_volatility_scale(self, manager):
        """Compute volatility scale factor from day's range.

        On high-range days (e.g., 2%+ range), trailing SL should be looser
        to avoid getting stopped out on normal pullbacks.
        Returns a factor 1.0 (normal) to 0.5 (very volatile = lock less profit).

        April 2 analysis: 22600CE had +27 pts profit but trailing SL was too tight
        for a 600-pt NIFTY range day. Both Momentum and OIWall hit SL at -45 pts
        when market pulled back before resuming the rally.
        """
        day_high = getattr(manager, 'underlying_high', 0)
        day_low = getattr(manager, 'underlying_low', 0)
        ltp = getattr(manager, 'underlying_ltp', 0)

        if day_high <= 0 or day_low <= 0 or ltp <= 0:
            return 1.0

        day_range_pct = (day_high - day_low) / ltp * 100

        # Normal day: range < 1% → scale = 1.0 (no change)
        # Moderate: 1-2% → scale = 0.85 (lock 15% less profit)
        # Volatile: 2%+ → scale = 0.70 (lock 30% less profit, give room for pullbacks)
        if day_range_pct >= 2.0:
            return 0.70
        elif day_range_pct >= 1.5:
            return 0.80
        elif day_range_pct >= 1.0:
            return 0.85
        return 1.0

    def _manage_active_trade(self, manager, metrics):
        trade = self.active_trade
        if not trade: return
        if trade.get('_closed', False):
            self.active_trade = None
            return

        current_ltp = self._get_trade_ltp(manager, trade)
        if current_ltp <= 0: return

        # MINIMUM HOLD ENFORCEMENT: No signal-based exit before min_hold_seconds
        # Only hard SL/Target can exit early (those are checked below)
        # This prevents 16s/27s/32s churning exits seen on March 23
        hold_seconds = 0
        try:
            entry_dt = datetime.fromisoformat(trade['entry_time'])
            hold_seconds = (datetime.now() - entry_dt).total_seconds()
        except Exception:
            pass
        trade['_hold_seconds'] = hold_seconds  # Store for use in exit checks

        # Update high/low water marks
        if current_ltp > trade.get('Max_Price_Seen', 0):
            trade['Max_Price_Seen'] = current_ltp
        if current_ltp < trade.get('Min_Price_Seen', float('inf')):
            trade['Min_Price_Seen'] = current_ltp

        entry = trade['entry_price']
        target = trade['target']
        initial_sl = trade.get('initial_sl', trade['sl'])
        initial_target = trade.get('initial_target', target)
        target_distance = initial_target - entry

        if target_distance > 0:
            profit = current_ltp - entry
            profit_pct = profit / target_distance  # 0=entry, 1.0=initial target

            # TRAILING SL: Progressive tightening with THETA BUFFER
            #
            # March 30 analysis: 15% breakeven trigger caused 12 trades to:
            #   - Go +15-20 pts profit → trailing SL to breakeven (entry+0.2%)
            #   - Theta ate 5-10 pts → hit breakeven → exit at -0.3 pts
            # Fix: Breakeven level must account for theta. Use entry + max(5pts, 3% of entry)
            # This gives ~5-10 pts of room for normal theta/IV fluctuation.
            #
            # Levels: 30% → breakeven+buffer, 50% → lock 25%, 75% → lock 50%, 100% → extend+lock 60%
            #
            # April 2 fix: On volatile days (2%+ range), scale down lock percentages
            # so trailing SL gives more room for pullbacks before locking profit.

            theta_buffer = max(5.0, entry * 0.03)  # ~5-10 pts room for theta/IV
            vol_scale = self._get_volatility_scale(manager)

            if profit_pct >= 1.0:
                new_sl = entry + (profit * 0.60 * vol_scale)
                trade['sl'] = max(trade['sl'], new_sl)

                max_extensions = 3
                if current_ltp >= trade['target'] and trade.get('target_extensions', 0) < max_extensions:
                    extension = target_distance * 0.5
                    trade['target'] = current_ltp + extension
                    trade['target_extensions'] = trade.get('target_extensions', 0) + 1
                    logger.info(f"[{self.name}] TARGET EXTENDED #{trade['target_extensions']}: {trade['target']:.2f} | Locked SL: {trade['sl']:.2f}")

            elif profit_pct >= 0.75:
                new_sl = entry + (profit * 0.50 * vol_scale)
                trade['sl'] = max(trade['sl'], new_sl)

            elif profit_pct >= 0.50:
                new_sl = entry + (profit * 0.25 * vol_scale)
                trade['sl'] = max(trade['sl'], new_sl)

            elif profit_pct >= 0.30:
                # Breakeven with theta buffer: entry + buffer instead of entry + 0.2%
                # On a 100 pt option, buffer = max(5, 3) = 5 pts
                # Trade must go +30 pts before this triggers (was 15% = 15 pts)
                new_sl = entry + theta_buffer
                trade['sl'] = max(trade['sl'], new_sl)

        exit_reason = None

        # SL/Target check
        if current_ltp <= trade['sl']:
            if trade['sl'] > initial_sl:
                exit_reason = "TRAILING SL HIT"
            else:
                exit_reason = "SL HIT"
        elif current_ltp >= trade['target'] and trade.get('target_extensions', 0) >= 3:
            exit_reason = "MAX TARGET HIT"  # All extensions used up

        # Signal-based exit (ONLY after minimum hold time - prevents 16s/27s churning exits)
        if not exit_reason and hold_seconds >= self.min_hold_seconds:
            raw_score = metrics['raw_score']
            profit = current_ltp - entry

            # Catastrophic flip: score must flip HARD against trade (2x entry threshold)
            # AND trade must be losing or barely profitable
            profit_is_significant = (profit > target_distance * 0.20) if target_distance > 0 else False
            flip_threshold = self.entry_score_threshold * 2

            if not profit_is_significant:
                if trade['type'] == 'CE' and raw_score < -flip_threshold:
                    exit_reason = f"CATASTROPHIC FLIP ({raw_score})"
                elif trade['type'] == 'PE' and raw_score > flip_threshold:
                    exit_reason = f"CATASTROPHIC FLIP ({raw_score})"

            # Signal fade: ONLY exits LOSING trades after min hold
            # Score must flip to opposite side of entry threshold (not just weaken to 0)
            if not exit_reason and profit <= 0:
                is_flipped = (
                    (trade['type'] == 'CE' and raw_score < -self.entry_score_threshold) or
                    (trade['type'] == 'PE' and raw_score > self.entry_score_threshold)
                )
                if is_flipped:
                    self.logger.exit_aggregator.add_signal('SIGNAL_WEAK', raw_score)
                    _, ext_conf, ext_samples, _, _ = self.logger.exit_aggregator.get_majority_signal()
                    if ext_samples >= self.logger.exit_min_samples and ext_conf >= 60:
                        exit_reason = f"SIGNAL FADE ({raw_score})"

        if exit_reason:
            self._close_trade(manager, current_ltp, exit_reason, metrics)

    def _get_trade_ltp(self, manager, trade):
        """Safely get current LTP for a trade's option contract."""
        try:
            strike = trade['strike']
            opt_type = trade['type']
            if strike not in manager.option_data:
                return 0
            key = 'ce_data' if opt_type == 'CE' else 'pe_data'
            return manager.option_data[strike][key].get('ltp', 0)
        except Exception as e:
            logger.error(f"[{self.name}] Error getting LTP: {e}")
            return 0

    def _force_exit_active_trade(self, manager, metrics, reason):
        """Force exit for EOD or emergency."""
        if not self.active_trade or self.active_trade.get('_closed', False):
            self.active_trade = None
            return
        current_ltp = self._get_trade_ltp(manager, self.active_trade)
        if current_ltp > 0:
            self._close_trade(manager, current_ltp, reason, metrics)
        else:
            self._close_trade(manager, self.active_trade.get('Max_Price_Seen', self.active_trade['entry_price']), reason, metrics)

    # ------------------------------------------------------------------
    # EXIT
    # ------------------------------------------------------------------
    def _close_trade(self, manager, exit_price, reason, metrics=None):
        trade = self.active_trade
        if not trade: return
        if trade.get('_closed', False): return

        trade['_closed'] = True
        self.active_trade = None

        try:
            pnl = exit_price - trade['entry_price']
            self.daily_pnl += pnl
            self.trades_today += 1

            if pnl < 0:
                self.consecutive_losses += 1
                # Longer cooldown after losses: 5 min base + 2 min per consecutive loss
                base_cooldown = 300 + (self.consecutive_losses * 120)
            else:
                self.consecutive_losses = 0
                base_cooldown = 180 if self.exit_style == 'conservative' else 120

            self.exit_cooldown_until = time.time() + base_cooldown

            manager.trades_today += 1
            manager.daily_pnl_points += pnl

            # Calculate hold duration
            hold_seconds = 0
            try:
                entry_dt = datetime.fromisoformat(trade['entry_time'])
                hold_seconds = int((datetime.now() - entry_dt).total_seconds())
            except Exception:
                pass

            # Calculate max profit/loss seen during trade
            max_profit_seen = trade.get('Max_Price_Seen', trade['entry_price']) - trade['entry_price']
            max_loss_seen = trade['entry_price'] - trade.get('Min_Price_Seen', trade['entry_price'])

            exit_score = metrics.get('raw_score', 0) if metrics else 0

            extensions = trade.get('target_extensions', 0)
            logger.info(f"[{self.name}] EXIT @ {exit_price:.2f} | {reason} | PnL:{pnl:+.2f} | Hold:{hold_seconds}s | MaxP:{max_profit_seen:+.1f} | Extensions:{extensions}")

            log_entry = self._build_log_entry(manager, trade, 'EXIT', exit_price, exit_score,
                                              reason, metrics)
            log_entry.update({
                'Exit_Price': exit_price,
                'PnL_Points': round(pnl, 2),
                'PnL_Amount': round(pnl * trade['Lot_Size'], 2),
                'ROI_Pct': round((pnl / trade['entry_price'] * 100), 2) if trade['entry_price'] > 0 else 0,
                'Time_Held_Sec': hold_seconds,
                'Max_Profit_Seen': round(max_profit_seen, 2),
                'Max_Loss_Seen': round(max_loss_seen, 2),
            })
            self.logger.log_trade(log_entry)
            self.logger.entry_aggregator.clear()
            self.logger.exit_aggregator.clear()

        except Exception as e:
            logger.error(f"[{self.name}] Error in _close_trade: {e}")

    def _build_log_entry(self, manager, trade, status, price, score, reason, metrics=None):
        """Build a complete log entry matching all CSV headers including market context snapshot."""
        entry = {
            'Trade_ID': trade['Trade_ID'],
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'Status': status,
            'Symbol': manager.underlying,
            'Contract': trade['Contract'],
            'Strike': trade['strike'],
            'Type': trade['type'],
            'Direction': trade.get('direction', 'BUY'),
            'Strategy': self.name,
            'Regime': manager.guardian.current_regime,
            'Entry_Price': trade['entry_price'] if status == 'EXIT' else price,
            'Quantity': 1,
            'Lot_Size': trade['Lot_Size'],
            'Target': round(trade['target'], 2),
            'StopLoss': round(trade['sl'], 2),
            'Underlying_LTP': round(manager.underlying_ltp, 2),
            'Score': score,
            'Reason': reason,
            'Confidence': trade.get('_entry_confidence', ''),
            'Signal_Distribution': f"{trade.get('_entry_distribution', '')} | OI:{trade.get('_entry_oi_pattern', '')}",
            'Entry_Confidence': trade.get('_entry_confidence', ''),
            'Snapshot_PCR': trade.get('_entry_pcr', ''),
            'Snapshot_IV': trade.get('_entry_iv', ''),
            'Entry_Delta': trade.get('_entry_delta', ''),
            'Entry_Gamma': trade.get('_entry_gamma', ''),
            'Entry_Theta': trade.get('_entry_theta', ''),
        }

        # MARKET CONTEXT SNAPSHOT: Individual signal scores + market state
        # On ENTRY: capture live metrics. On EXIT: capture live metrics at exit moment.
        if metrics:
            quant = metrics.get('quant_signal', {})
            breakdown = quant.get('scores_breakdown', {})

            entry.update({
                'Snapshot_PCR': round(metrics.get('pcr', 0), 4),
                # 12 individual signal scores
                'Sig_Delta_OI': breakdown.get('delta_oi', ''),
                'Sig_IV_Skew': breakdown.get('iv_skew', ''),
                'Sig_OI_Unwind': breakdown.get('oi_unwind', ''),
                'Sig_Max_Pain': breakdown.get('max_pain', ''),
                'Sig_GEX': breakdown.get('gamma', ''),
                'Sig_Vanna': breakdown.get('vanna', ''),
                'Sig_Inst_Flow': breakdown.get('inst_flow', ''),
                'Sig_Momentum': breakdown.get('momentum', ''),
                'Sig_PCR_ROC': breakdown.get('pcr_roc', ''),
                'Sig_VWAP': breakdown.get('vwap', ''),
                'Sig_OI_Wall': breakdown.get('oi_wall', ''),
                'Sig_PCR_Abs': breakdown.get('pcr_absolute', ''),
                # Market context
                'VWAP_Price': round(metrics.get('vwap', 0), 2),
                'OI_Wall_CE': metrics.get('oi_wall_ce', ''),
                'OI_Wall_PE': metrics.get('oi_wall_pe', ''),
                'OI_Pattern': metrics.get('oi_pattern', ''),
                'Market_Regime': metrics.get('market_regime_type', ''),
                'Underlying_Open': round(getattr(manager, 'underlying_open', 0), 2),
                'Underlying_High': round(getattr(manager, 'underlying_high', 0), 2),
                'Underlying_Low': round(getattr(manager, 'underlying_low', 0), 2),
            })

        return entry


# =============================================================================
# STRATEGY 1: TREND FOLLOWER
# =============================================================================
class V10TrendStrategy(BaseStrategy):
    """Trades ONLY in confirmed trending markets.
    Requires price trend + OI buildup + regime all confirming.
    Target: 3-5 trades/day. Rides trends via target extension.
    """
    def __init__(self):
        super().__init__(
            "V10_Trend", entry_score=18, agg_window=90, min_conf=65,
            min_hold=120, exit_style='conservative', max_trades=999999
        )

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        # No trades in first 30 min (9:15-9:45 is opening noise for trend strategy)
        now = _ist_now()
        if now.hour == 9 and now.minute < 45:
            return False

        # Stop after 3 consecutive losses
        if self.consecutive_losses >= 9999:
            return False

        # Price must confirm direction (2-min trend - 60s was too short, caught fake bounces)
        if len(manager.price_history) < 120:
            return False
        prices = list(manager.price_history)
        trend = (prices[-1] - prices[-120]) / prices[-120] * 100
        if 'CALL' in majority_action and trend < -0.01:
            return False
        if 'PUT' in majority_action and trend > 0.01:
            return False

        # OI buildup must confirm - check CE and PE OI separately
        # Bullish: PE OI buildup (put writing = bullish) OR CE OI decline (call unwinding)
        # Bearish: CE OI buildup (call writing = bearish) OR PE OI decline (put unwinding)
        with manager.data_lock:
            total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in manager.option_data.values())
            total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in manager.option_data.values())
            init_ce_oi = sum(v.get('ce_oi', 0) for v in manager.initial_state.values())
            init_pe_oi = sum(v.get('pe_oi', 0) for v in manager.initial_state.values())

        ce_oi_change = total_ce_oi - init_ce_oi
        pe_oi_change = total_pe_oi - init_pe_oi

        if 'CALL' in majority_action:
            # Bullish: need put writing (PE OI up) or call unwinding (CE OI down)
            if pe_oi_change <= 0 and ce_oi_change >= 0:
                return False  # No supportive OI activity
        if 'PUT' in majority_action:
            # Bearish: need call writing (CE OI up) or put unwinding (PE OI down)
            if ce_oi_change <= 0 and pe_oi_change >= 0:
                return False  # No supportive OI activity

        return True


# =============================================================================
# STRATEGY 2: MOMENTUM SCALPER
# =============================================================================
class V10MomentumStrategy(BaseStrategy):
    """Catches quick directional bursts. Requires velocity + acceleration.
    Tight stop, fast exit. Skips illiquid or expensive options.
    """
    def __init__(self):
        super().__init__(
            "V10_Momentum", entry_score=12, agg_window=45, min_conf=60,
            min_hold=120, exit_style='aggressive', max_trades=999999
        )

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        if len(manager.price_history) < 60:
            return False

        # DEAD ZONE FILTER: 12:30-1:30 PM has thin liquidity, random noise
        now = _ist_now()
        if now.hour == 12 and now.minute >= 30:
            return False
        if now.hour == 13 and now.minute < 30:
            return False

        prices = list(manager.price_history)
        # 60-second velocity window (was 30s - too noisy, one spike triggered entry)
        velocity = (prices[-1] - prices[-60]) / prices[-60] * 100

        # INSTRUMENT-ADAPTIVE threshold
        vel_threshold = 0.03  # Default for NIFTY
        if manager.underlying == 'BANKNIFTY':
            vel_threshold = 0.06
        elif manager.underlying in ('RELIANCE', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'INFY', 'BHARTIARTL'):
            vel_threshold = 0.05

        if 'CALL' in majority_action and velocity < vel_threshold:
            return False
        if 'PUT' in majority_action and velocity > -vel_threshold:
            return False

        # Acceleration check (30s windows - was 15s which was pure noise)
        if len(prices) >= 90:
            vel_recent = (prices[-1] - prices[-30]) / prices[-30] * 100
            vel_old = (prices[-30] - prices[-60]) / prices[-60] * 100
            accel = vel_recent - vel_old
            if 'CALL' in majority_action and accel < 0: return False
            if 'PUT' in majority_action and accel > 0: return False

        # Spread check
        if manager.atm_strike and manager.atm_strike in manager.option_data:
            opt_key = 'ce_data' if 'CALL' in majority_action else 'pe_data'
            opt = manager.option_data[manager.atm_strike][opt_key]
            spread, ltp = opt.get('spread', 0), opt.get('ltp', 1)
            if ltp > 0 and spread / ltp > 0.02:
                return False

        # IV check: skip expensive options UNLESS IV is expanding WITH momentum
        # (during genuine breakouts, IV expansion IS the confirmation)
        if len(manager.iv_history) >= 30:
            avg_iv = sum(manager.iv_history) / len(manager.iv_history)
            current_iv = manager.iv_history[-1]
            if current_iv > avg_iv * 1.2:
                # IV is elevated - only allow if it's RISING over 60s (expansion = breakout)
                if len(manager.iv_history) >= 90:
                    iv_60ago = manager.iv_history[-60]
                    if current_iv <= iv_60ago:
                        return False  # IV elevated but not rising over 1 min = expensive, skip

        return True


# =============================================================================
# STRATEGY 3: MEAN REVERSION
# =============================================================================
class V10MeanReversionStrategy(BaseStrategy):
    """Trades reversals at extremes. Waits for overextension, enters on fade.

    Uses adaptive extreme threshold based on rolling score volatility
    instead of hardcoded value.
    """
    def __init__(self):
        super().__init__(
            "V10_MeanRev", entry_score=8, agg_window=60, min_conf=65,
            min_hold=30, exit_style='scalp', max_trades=999999
        )
        self._extreme_detected_at = 0
        self._extreme_direction = None
        self._score_history = []  # For adaptive threshold
        self._score_history_max = 300

    def _get_extreme_threshold(self):
        """Adaptive extreme threshold = mean + 2 * std dev of recent scores.
        In high-vol market where score swings to 60, threshold adapts higher.
        In low-vol market where score stays in -15..+15, threshold drops.
        """
        if len(self._score_history) < 60:
            return 25  # Default until enough data (35 was too high, never triggered)

        import statistics
        recent = self._score_history[-120:]
        abs_scores = [abs(s) for s in recent]
        mean_abs = statistics.mean(abs_scores)
        std_abs = statistics.stdev(abs_scores) if len(abs_scores) > 1 else 5

        threshold = mean_abs + (1.5 * std_abs)
        return max(20, min(60, threshold))  # Clamp to 20-60 range

    def process_signals(self, manager, metrics):
        raw_score = metrics['raw_score']
        current_time = time.time()

        # Track score history for adaptive threshold
        self._score_history.append(raw_score)
        if len(self._score_history) > self._score_history_max:
            self._score_history = self._score_history[-self._score_history_max:]

        # Force exit check
        if self.active_trade and _is_force_exit_time():
            self._force_exit_active_trade(manager, metrics, "EOD FORCE EXIT")
            return
        if not _is_market_hours():
            return

        # Phase 1: Detect extreme using adaptive threshold
        extreme_threshold = self._get_extreme_threshold()
        if abs(raw_score) > extreme_threshold:
            self._extreme_detected_at = current_time
            self._extreme_direction = 'BULLISH' if raw_score > 0 else 'BEARISH'

        # Phase 2: Look for reversal within 120s of extreme
        if (self._extreme_direction and
                current_time - self._extreme_detected_at < 120):
            fade_threshold = extreme_threshold * 0.3  # Score must fade to 30% of threshold
            if self._extreme_direction == 'BULLISH' and raw_score < fade_threshold:
                instant_action = 'BUY PUT'
            elif self._extreme_direction == 'BEARISH' and raw_score > -fade_threshold:
                instant_action = 'BUY CALL'
            else:
                instant_action = 'WAIT'
        else:
            instant_action = 'WAIT'
            self._extreme_direction = None

        # Feed aggregator
        current_strike = manager.atm_strike
        current_contract = None
        if current_strike != 0 and instant_action != 'WAIT':
            opt_type = 'CE' if 'CALL' in instant_action else 'PE'
            current_contract = manager.construct_option_symbol(current_strike, opt_type)

        self.logger.entry_aggregator.add_signal(
            action=instant_action, score=raw_score,
            regime=manager.guardian.current_regime,
            strike=current_strike, contract=current_contract
        )

        # Heartbeat
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            maj_act, conf, samples, _, _ = self.logger.entry_aggregator.get_majority_signal()
            print(f"[{self.name}] Extreme:{self._extreme_direction or '-'} Thresh:{extreme_threshold:.0f} | Score:{raw_score:+d} | Maj:{maj_act}({conf:.0f}%)")

        # Standard management
        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_active_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return
            now = _ist_now()
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN): return
            self._check_entry(manager, metrics)

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        # Only in non-trending or stale regime
        regime = manager.guardian.current_regime
        if regime in ('BULLISH', 'BEARISH'):
            lock_age = time.time() - manager.guardian.last_change_time
            if lock_age < 180: return False

        # GEX positive = mean reverting conditions
        if manager.gex_history and len(manager.gex_history) > 5:
            avg_gex = sum(list(manager.gex_history)[-5:]) / 5
            if avg_gex < 0: return False

        if not self._extreme_direction: return False
        return True


# =============================================================================
# STRATEGY 4: HIGH CONVICTION SWING
# =============================================================================
class V10SwingStrategy(BaseStrategy):
    """Quality over quantity. 4+ strong signals + institutional flow + regime lock.
    Max 2 trades/day. Widest stops, longest holds, maximum trend riding.
    """
    def __init__(self):
        super().__init__(
            "V10_Swing", entry_score=20, agg_window=120, min_conf=65,
            min_hold=300, exit_style='conservative', max_trades=999999
        )

    def _has_signal_confluence(self, quant_signal):
        """Override: Require 3+ strong signals."""
        breakdown = quant_signal.get('scores_breakdown', {})
        if not breakdown: return False
        score = quant_signal.get('score', 0)
        strong_bull = sum(1 for v in breakdown.values() if v >= 10)
        strong_bear = sum(1 for v in breakdown.values() if v <= -10)
        if score > 0: return strong_bull >= 3
        elif score < 0: return strong_bear >= 3
        return False

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        # Regime must be locked and aged
        if not manager.guardian.locked: return False
        if time.time() - manager.guardian.last_change_time < 60: return False

        # DTE FILTER: Don't enter swing trades with < 2 days to expiry
        # Theta will kill the position even if direction is right
        dte = metrics.get('days_to_expiry', 5)
        if dte < 2:
            return False

        # Institutional flow must not be strongly against (directional, not magnitude)
        breakdown = metrics.get('quant_signal', {}).get('scores_breakdown', {})
        flow_score = breakdown.get('inst_flow', 0)
        if 'CALL' in majority_action and flow_score < -5: return False
        if 'PUT' in majority_action and flow_score > 5: return False

        # DYNAMIC PCR crowding check using rolling PCR range (not fixed 0.5/1.5)
        if manager.pcr_history and len(manager.pcr_history) > 180:
            recent_pcr = list(manager.pcr_history)[-180:]
            pcr_mean = sum(recent_pcr) / len(recent_pcr)
            import statistics
            pcr_std = statistics.stdev(recent_pcr) if len(recent_pcr) > 1 else 0.2
            pcr_now = manager.pcr_history[-1]

            # "Crowded" = more than 2 std devs from mean
            if pcr_now > pcr_mean + (2 * pcr_std) and 'PUT' in majority_action:
                return False  # Everyone already bearish
            if pcr_now < pcr_mean - (2 * pcr_std) and 'CALL' in majority_action:
                return False  # Everyone already bullish

        return True


# =============================================================================
# STRATEGY 5: OPENING RANGE BREAKOUT (ORB)
# Captures 9:15-9:30 range, enters on confirmed breakout.
# =============================================================================
class V10ORBStrategy(BaseStrategy):
    """Opening Range Breakout - Trades the first 15-minute range break.

    False breakout protection:
    - Price must HOLD above/below range for 2 minutes (not just wick)
    - OI must build in breakout direction (real breakouts attract fresh OI)
    - VWAP must confirm (breakout above range AND above VWAP = strong)
    - Won't trade if breakout is right at an OI wall (likely to bounce back)
    - Only active 9:30 - 10:30 AM (breakout window)

    Late server start: Uses underlying_high/low or open±0.3% as fallback range.
    """
    def __init__(self):
        super().__init__(
            "V10_ORB", entry_score=5, agg_window=30, min_conf=60,
            min_hold=60, exit_style='aggressive', max_trades=999999
        )
        self._breakout_side = None     # 'UP' or 'DOWN'
        self._breakout_time = 0        # When breakout first detected
        self._breakout_confirmed = False

    def process_signals(self, manager, metrics):
        """Override: Custom ORB logic instead of score-based entry."""
        current_time = time.time()

        # Force exit check
        if self.active_trade and _is_force_exit_time():
            self._force_exit_active_trade(manager, metrics, "EOD FORCE EXIT")
            return
        if not _is_market_hours():
            return

        now = _ist_now()
        ltp = metrics.get('ltp', 0)
        orb_high = metrics.get('opening_range_high', 0)
        orb_low = metrics.get('opening_range_low', 0)
        orb_locked = metrics.get('opening_range_locked', False)

        # Only active 9:30 - 10:30
        if not orb_locked or orb_high <= 0 or orb_low <= 0:
            if self.active_trade:
                self._manage_active_trade(manager, metrics)
            return
        if now.hour > 10 or (now.hour == 10 and now.minute > 30):
            if self.active_trade:
                self._manage_active_trade(manager, metrics)
            # Reset for next day
            self._breakout_side = None
            self._breakout_confirmed = False
            return

        orb_range = orb_high - orb_low
        if orb_range <= 0:
            return

        # Manage existing trade
        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_active_trade(manager, metrics)
            return

        if self.trades_today >= self.max_trades_per_day:
            return
        if current_time < self.exit_cooldown_until:
            return

        # PHASE 1: DETECT breakout
        if ltp > orb_high and self._breakout_side != 'UP':
            self._breakout_side = 'UP'
            self._breakout_time = current_time
            self._breakout_confirmed = False
        elif ltp < orb_low and self._breakout_side != 'DOWN':
            self._breakout_side = 'DOWN'
            self._breakout_time = current_time
            self._breakout_confirmed = False
        elif orb_low <= ltp <= orb_high:
            # Price back inside range = false breakout, reset
            if self._breakout_side and not self._breakout_confirmed:
                self._breakout_side = None

        # PHASE 2: CONFIRM breakout (must hold for 120 seconds)
        if self._breakout_side and not self._breakout_confirmed:
            hold_time = current_time - self._breakout_time
            if hold_time >= 120:  # 2 minutes hold
                # Validate the breakout
                if self._validate_breakout(manager, metrics):
                    self._breakout_confirmed = True
                    action = 'BUY CALL' if self._breakout_side == 'UP' else 'BUY PUT'
                    logger.info(f"[{self.name}] ORB CONFIRMED: {self._breakout_side} breakout | Range: {orb_low:.2f}-{orb_high:.2f}")
                    self._execute_entry(manager, action, metrics['raw_score'], metrics)
                else:
                    # Validation failed, reset
                    self._breakout_side = None

        # Heartbeat
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            holding = f"Holding {self._breakout_side} {current_time - self._breakout_time:.0f}s" if self._breakout_side else "Scanning"
            print(f"[{self.name}] Range:{orb_low:.0f}-{orb_high:.0f} | LTP:{ltp:.2f} | {holding}")

    def _validate_breakout(self, manager, metrics):
        """Multi-factor breakout validation to filter false breakouts."""
        ltp = metrics.get('ltp', 0)
        vwap = metrics.get('vwap', 0)

        # 1. VWAP CONFIRMATION: Breakout direction must agree with VWAP
        if vwap > 0:
            if self._breakout_side == 'UP' and ltp < vwap:
                return False  # Breaking up but below VWAP = weak
            if self._breakout_side == 'DOWN' and ltp > vwap:
                return False  # Breaking down but above VWAP = weak

        # 2. OI WALL CHECK: Don't buy into an OI wall
        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)
        if ce_wall > 0 and self._breakout_side == 'UP':
            dist_to_wall = ((ce_wall - ltp) / ltp) * 100
            if dist_to_wall < 0.15:  # Within 0.15% of CE wall = resistance
                return False
        if pe_wall > 0 and self._breakout_side == 'DOWN':
            dist_to_wall = ((ltp - pe_wall) / ltp) * 100
            if dist_to_wall < 0.15:  # Within 0.15% of PE wall = support
                return False

        # 3. OI BUILDUP: Fresh OI in breakout direction (3-min window)
        if len(manager.oi_history) >= 180:
            oi_trend = manager.oi_history[-1] - manager.oi_history[-180]
            if oi_trend < 0:
                return False  # OI declining over last 3 min = no conviction behind breakout
        elif len(manager.oi_history) >= 60:
            oi_trend = manager.oi_history[-1] - manager.oi_history[-60]
            if oi_trend < 0:
                return False

        return True


# =============================================================================
# STRATEGY 6: OI WALL BOUNCE / BREAKOUT
# Trades bounces off OI walls and breakouts through them.
# =============================================================================
class V10OIWallStrategy(BaseStrategy):
    """OI Wall Bounce/Breakout Strategy.

    Highest CE OI strike = resistance (call writers defend it).
    Highest PE OI strike = support (put writers defend it).

    BOUNCE: Price approaches wall and reverses -> trade the bounce.
    BREAKOUT: Price blasts through wall with OI unwinding -> trade the break.

    Edge: These are real levels because option writers hedge positions there,
    creating actual buying/selling pressure at these strikes.
    """
    def __init__(self):
        super().__init__(
            "V10_OIWall", entry_score=10, agg_window=60, min_conf=65,
            min_hold=120, exit_style='aggressive', max_trades=999999
        )
        self._wall_signal = None  # 'BOUNCE_SUPPORT', 'BOUNCE_RESIST', 'BREAK_UP', 'BREAK_DOWN'

    def process_signals(self, manager, metrics):
        """Override: Generate signals based on wall proximity."""
        ltp = metrics.get('ltp', 0)
        current_time = time.time()

        # Force exit / market hours check
        if self.active_trade and _is_force_exit_time():
            self._force_exit_active_trade(manager, metrics, "EOD FORCE EXIT")
            return
        if not _is_market_hours():
            return

        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)

        if ltp <= 0 or ce_wall <= 0 or pe_wall <= 0:
            if self.active_trade and not self.active_trade.get('_closed', False):
                self._manage_active_trade(manager, metrics)
            return

        # Calculate distances
        dist_to_resistance_pct = ((ce_wall - ltp) / ltp) * 100 if ltp > 0 else 999
        dist_to_support_pct = ((ltp - pe_wall) / ltp) * 100 if ltp > 0 else 999

        # Determine wall-based action
        instant_action = 'WAIT'
        raw_score = metrics['raw_score']

        # BREAKOUT: Price above CE wall = resistance broken = strong bullish
        if ltp > ce_wall and raw_score > 5:
            instant_action = 'BUY CALL'
            self._wall_signal = 'BREAK_UP'
        # BREAKDOWN: Price below PE wall = support broken = strong bearish
        elif ltp < pe_wall and raw_score < -5:
            instant_action = 'BUY PUT'
            self._wall_signal = 'BREAK_DOWN'
        # BOUNCE OFF SUPPORT: Price near PE wall + score turning positive
        elif dist_to_support_pct < 0.3 and raw_score > 0:
            instant_action = 'BUY CALL'
            self._wall_signal = 'BOUNCE_SUPPORT'
        # BOUNCE OFF RESISTANCE: Price near CE wall + score turning negative
        elif dist_to_resistance_pct < 0.3 and raw_score < 0:
            instant_action = 'BUY PUT'
            self._wall_signal = 'BOUNCE_RESIST'

        # Feed aggregator
        current_strike = manager.atm_strike
        current_contract = None
        if current_strike != 0 and instant_action != 'WAIT':
            opt_type = 'CE' if 'CALL' in instant_action else 'PE'
            current_contract = manager.construct_option_symbol(current_strike, opt_type)

        self.logger.entry_aggregator.add_signal(
            action=instant_action, score=raw_score,
            regime=manager.guardian.current_regime,
            strike=current_strike, contract=current_contract
        )

        # Heartbeat
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            print(f"[{self.name}] Walls:{pe_wall}-{ce_wall} | LTP:{ltp:.2f} | Signal:{self._wall_signal or '-'} | Trades:{self.trades_today}/{self.max_trades_per_day}")

        # Standard trade management
        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_active_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return
            now = _ist_now()
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN): return
            self._check_entry(manager, metrics)

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        # OI walls aren't reliable before 10:00 AM (early OI = overnight orders + opening adjustments)
        now = _ist_now()
        if now.hour < 10:
            return False

        # Must have a wall signal active
        if not self._wall_signal:
            return False

        # For BREAKOUT trades, OI at wall must be unwinding over 3 min (writers covering)
        if self._wall_signal in ('BREAK_UP', 'BREAK_DOWN'):
            if len(manager.oi_history) >= 180:
                oi_trend = manager.oi_history[-1] - manager.oi_history[-180]
                if oi_trend > 0:
                    return False  # OI still building over 3 min = wall holding, not breaking
            elif len(manager.oi_history) >= 60:
                oi_trend = manager.oi_history[-1] - manager.oi_history[-60]
                if oi_trend > 0:
                    return False

        # For BOUNCE trades, GEX should be positive (mean reverting conditions)
        if self._wall_signal in ('BOUNCE_SUPPORT', 'BOUNCE_RESIST'):
            if manager.gex_history and len(manager.gex_history) > 3:
                avg_gex = sum(list(manager.gex_history)[-3:]) / 3
                if avg_gex < 0:
                    return False  # Negative GEX = trending through wall, not bouncing

        return True


# =============================================================================
# STRATEGY 7: EXPIRY DAY MAX PAIN MAGNET
# Thursday-only (weekly) strategy. Trades toward max pain after 1 PM.
# =============================================================================
class V10ExpiryMagnetStrategy(BaseStrategy):
    """Expiry Day Max Pain Magnet - Thursday specialist.

    Edge: NIFTY closes within 50 points of max pain ~60% of expiry days.
    After 1 PM, the magnet effect strengthens as market makers delta-hedge
    toward max pain.

    Rules:
    - Only active on expiry day (DTE < 1)
    - Only after 1:00 PM IST
    - Spot must be > 0.3% away from max pain (enough room for profit)
    - GEX positive = mean reverting toward max pain (confirmation)
    - Very tight targets (just needs to move toward max pain, not reach it)
    """
    def __init__(self):
        super().__init__(
            "V10_Expiry", entry_score=5, agg_window=45, min_conf=65,
            min_hold=30, exit_style='scalp', max_trades=999999
        )

    def process_signals(self, manager, metrics):
        """Override: Only runs on expiry day after 1 PM."""
        current_time = time.time()

        # Force exit check
        if self.active_trade and _is_force_exit_time():
            self._force_exit_active_trade(manager, metrics, "EOD FORCE EXIT")
            return
        if not _is_market_hours():
            return

        now = _ist_now()
        dte = metrics.get('days_to_expiry', 99)
        ltp = metrics.get('ltp', 0)

        # GATE: Only on expiry day (DTE < 1) and after 1 PM
        if dte >= 1 or now.hour < 13:
            if self.active_trade and not self.active_trade.get('_closed', False):
                self._manage_active_trade(manager, metrics)
            return

        # Don't enter after 3:00 PM (too close to close, theta acceleration extreme)
        if now.hour >= 15:
            if self.active_trade and not self.active_trade.get('_closed', False):
                self._manage_active_trade(manager, metrics)
            return

        # Get max pain
        quant = metrics.get('quant_signal', {})
        max_pain = quant.get('max_pain', 0)
        if max_pain <= 0 or ltp <= 0:
            return

        # Distance from max pain
        dist_pct = ((ltp - max_pain) / max_pain) * 100

        # Generate signal: trade TOWARD max pain
        instant_action = 'WAIT'
        if dist_pct > 0.3:
            instant_action = 'BUY PUT'  # Above max pain, expect pull down
        elif dist_pct < -0.3:
            instant_action = 'BUY CALL'  # Below max pain, expect pull up

        # Feed aggregator
        raw_score = metrics['raw_score']
        current_strike = manager.atm_strike
        current_contract = None
        if current_strike != 0 and instant_action != 'WAIT':
            opt_type = 'CE' if 'CALL' in instant_action else 'PE'
            current_contract = manager.construct_option_symbol(current_strike, opt_type)

        self.logger.entry_aggregator.add_signal(
            action=instant_action, score=raw_score,
            regime=manager.guardian.current_regime,
            strike=current_strike, contract=current_contract
        )

        # Heartbeat
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            print(f"[{self.name}] DTE:{dte:.2f} | MaxPain:{max_pain} | Dist:{dist_pct:+.2f}% | Trades:{self.trades_today}/{self.max_trades_per_day}")

        # Trade management
        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_active_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return  # Tighter for expiry day
            self._check_entry(manager, metrics)

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        # GEX should be positive (mean reverting = magnet active)
        if manager.gex_history and len(manager.gex_history) > 3:
            avg_gex = sum(list(manager.gex_history)[-3:]) / 3
            if avg_gex < 0:
                return False  # Negative GEX = trending, magnet won't work

        # Max pain must be between OI walls (otherwise max pain is unreliable)
        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)
        quant = metrics.get('quant_signal', {})
        max_pain = quant.get('max_pain', 0)

        if ce_wall > 0 and pe_wall > 0 and max_pain > 0:
            if max_pain > ce_wall or max_pain < pe_wall:
                return False  # Max pain outside walls = unreliable

        return True


# =============================================================================
# STRATEGY 8: SPOT / FUTURES PAPER TRADE ENGINE
#
# WHY FUTURES/SPOT IS DIFFERENT FROM OPTIONS:
# - No theta decay → can hold for hours without time killing you
# - Linear P&L → 1 point NIFTY move = 1 point profit/loss (no delta/gamma)
# - SL/Target in direct points (no delta conversion needed)
# - Can go LONG and SHORT equally (no BUY-only limitation)
# - Lower transaction cost (no STT on sell side for futures)
#
# STRATEGY: VWAP + OI Wall + Trend hybrid
# - Above VWAP + bullish regime + above PE wall support → LONG
# - Below VWAP + bearish regime + below CE wall resistance → SHORT
# - OI walls define the expected range → target = opposite wall
# - VWAP acts as the dynamic support/resistance
# - Trailing SL in absolute points (not % based)
# =============================================================================
class V10SpotFuturesStrategy(BaseStrategy):
    """Spot/Futures Paper Trade Engine - VWAP + OI Wall + Trend.

    Advantages over options:
    - No theta → hold positions for hours
    - Linear P&L → cleaner risk management
    - Can go both LONG and SHORT

    Entry logic:
    - LONG: Price above VWAP + above PE wall (support) + regime BULLISH/NEUTRAL + score > 0
    - SHORT: Price below VWAP + below CE wall (resistance) + regime BEARISH/NEUTRAL + score < 0

    Exit logic:
    - Trailing SL in absolute NIFTY points (not option points)
    - Target = OI wall on opposite side (CE wall for LONG, PE wall for SHORT)
    - VWAP cross exit: if price crosses VWAP against trade direction
    - Force exit at 3:20 PM IST

    SL/Target:
    - SL: 15-25 points (NIFTY), 30-50 points (BANKNIFTY), adaptive to volatility
    - Target: Distance to opposite OI wall, minimum 20 points
    - Trailing: At +15 pts → move SL to breakeven. At +30 pts → lock 50%.

    Max trades: 4/day (futures have higher margin, trade less)
    """
    def __init__(self):
        super().__init__(
            "V10_Spot",
            entry_score=12,
            agg_window=90,
            min_conf=60,
            min_hold=120,   # Hold at least 2 minutes
            exit_style='conservative',
            max_trades=999999
        )
        self._trade_direction = None  # 'LONG' or 'SHORT'

    def process_signals(self, manager, metrics):
        """Override: Trade the underlying price directly, not options."""
        current_time = time.time()

        # Force exit / market hours
        if self.active_trade and _is_force_exit_time():
            self._force_exit_spot(manager, metrics, "EOD FORCE EXIT")
            return
        if not _is_market_hours():
            return

        # No trades in first 30 min (let VWAP and OI walls establish)
        now = _ist_now()
        if now.hour == 9 and now.minute < 45:
            return

        ltp = metrics.get('ltp', 0)
        vwap = metrics.get('vwap', 0)
        raw_score = metrics['raw_score']
        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)

        if ltp <= 0 or vwap <= 0:
            return

        # Determine spot-level action using VWAP + OI Walls + OI Pattern
        instant_action = 'WAIT'
        oi_pattern = metrics.get('oi_pattern', '')

        # LONG: Price above VWAP + above support wall + score positive
        # OI pattern must NOT be bearish (Short Buildup, Long Unwinding, Call Unwinding)
        if ltp > vwap and raw_score > 0 and pe_wall > 0 and ltp > pe_wall:
            if 'Short Buildup' not in oi_pattern and 'Long Unwinding' not in oi_pattern:
                instant_action = 'BUY CALL'
                self._trade_direction = 'LONG'

        # SHORT: Price below VWAP + below resistance wall + score negative
        # OI pattern must NOT be bullish (Long Buildup, Short Covering, Put Unwinding)
        elif ltp < vwap and raw_score < 0 and ce_wall > 0 and ltp < ce_wall:
            if 'Long Buildup' not in oi_pattern and 'Short Covering' not in oi_pattern:
                instant_action = 'BUY PUT'
                self._trade_direction = 'SHORT'

        # Feed aggregator
        current_strike = manager.atm_strike
        current_contract = None
        if current_strike and instant_action != 'WAIT':
            opt_type = 'CE' if 'CALL' in instant_action else 'PE'
            current_contract = f"SPOT_{manager.underlying}"

        self.logger.entry_aggregator.add_signal(
            action=instant_action, score=raw_score,
            regime=manager.guardian.current_regime,
            strike=current_strike, contract=current_contract
        )

        # Heartbeat
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            vwap_dist = ((ltp - vwap) / vwap * 100) if vwap > 0 else 0
            wall_info = f"Walls:{pe_wall}-{ce_wall}" if pe_wall and ce_wall else "Walls:N/A"
            oi_info = metrics.get('oi_pattern', 'N/A')
            print(f"[{self.name}] LTP:{ltp:.2f} | VWAP:{vwap:.2f} ({vwap_dist:+.2f}%) | {wall_info} | OI:{oi_info} | Score:{raw_score:+d} | Trades:{self.trades_today}/{self.max_trades_per_day}")

        # Manage active trade (spot-specific management)
        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_spot_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return
            # Don't enter new trades after 2:45 PM
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN): return
            self._check_spot_entry(manager, metrics)

    def _check_spot_entry(self, manager, metrics):
        """Check entry using aggregator, then execute spot trade."""
        majority_action, confidence, sample_count, avg_score, _ = \
            self.logger.entry_aggregator.get_majority_signal()

        if majority_action == 'WAIT': return
        if sample_count < self.logger.entry_min_samples: return
        if confidence < self.min_confidence: return

        # MARKET REGIME GATE for Spot
        market_type = metrics.get('market_regime_type', 'DETECTING')
        if market_type == 'CHOPPY':
            # Choppy: require 80%+ confidence for Spot
            if confidence < 80: return
        elif market_type == 'SIDEWAYS':
            # Sideways: require 85%+ confidence (Spot is directional, needs strong conviction)
            if confidence < 85: return

        # Regime check
        guardian_regime = manager.guardian.update(
            'BULLISH' if avg_score > 0 else 'BEARISH',
            confidence, score=avg_score
        )
        if 'CALL' in majority_action and guardian_regime == 'BEARISH': return
        if 'PUT' in majority_action and guardian_regime == 'BULLISH': return

        # Confluence check
        quant = metrics.get('quant_signal', {})
        if not self._has_signal_confluence(quant): return

        # VWAP must confirm at entry moment
        ltp = metrics.get('ltp', 0)
        vwap = metrics.get('vwap', 0)
        if 'CALL' in majority_action and ltp < vwap: return
        if 'PUT' in majority_action and ltp > vwap: return

        # OI PATTERN must confirm direction
        # LONG needs bullish OI (Long Buildup, Short Covering, Put Unwinding)
        # SHORT needs bearish OI (Short Buildup, Long Unwinding, Call Unwinding)
        oi_pattern = metrics.get('oi_pattern', '')
        oi_strength = metrics.get('oi_pattern_strength', 0)

        if oi_strength > 5:  # Only filter if OI pattern is meaningful
            if 'CALL' in majority_action:
                # Going LONG - block if OI pattern is bearish
                if any(p in oi_pattern for p in ['Short Buildup', 'Long Unwinding', 'Call Unwinding']):
                    return
            if 'PUT' in majority_action:
                # Going SHORT - block if OI pattern is bullish
                if any(p in oi_pattern for p in ['Long Buildup', 'Short Covering', 'Put Unwinding']):
                    return

        self._execute_spot_entry(manager, majority_action, avg_score, metrics)

    def _execute_spot_entry(self, manager, action, score, metrics):
        """Execute a spot/futures paper trade.

        SL/Target based on OI Walls:
        - Target = opposite OI wall (real level where writers defend)
        - SL = 40% of target distance (gives minimum 1:2.5 R:R)
        - If walls too close (<30 pts NIFTY): skip trade
        - If walls too far (>200 pts): cap SL at 50 pts
        - GEX negative = breakout likely, allow wider SL
        """
        ltp = manager.underlying_ltp
        if ltp <= 0: return

        direction = 'LONG' if 'CALL' in action else 'SHORT'
        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)
        vwap = metrics.get('vwap', 0)

        # TARGET = 80% of distance to opposite OI wall (price rarely touches exact wall)
        # SL = 50% of distance to YOUR OI wall (support for LONG, resistance for SHORT)
        # This gives real breathing room based on actual market structure
        if direction == 'LONG' and ce_wall > ltp and pe_wall > 0:
            target_distance = (ce_wall - ltp) * 0.80
            sl_distance = (ltp - pe_wall) * 0.50   # Half-way to support wall
        elif direction == 'SHORT' and pe_wall > 0 and pe_wall < ltp and ce_wall > 0:
            target_distance = (ltp - pe_wall) * 0.80
            sl_distance = (ce_wall - ltp) * 0.50    # Half-way to resistance wall
        else:
            target_distance = 0
            sl_distance = 0

        # Minimum total range check (PE wall to CE wall)
        total_range = (ce_wall - pe_wall) if ce_wall > 0 and pe_wall > 0 else 0
        if manager.underlying == 'BANKNIFTY':
            min_range = 100     # At least 100 pts between walls
            max_sl = 120
            min_sl = 40
        elif manager.underlying == 'NIFTY':
            min_range = 50      # At least 50 pts between walls
            max_sl = 70
            min_sl = 25
        else:
            min_range = ltp * 0.005
            max_sl = ltp * 0.007
            min_sl = ltp * 0.003

        if total_range < min_range or target_distance < 20:
            logger.debug(f"[{self.name}] Skipping: range {total_range:.0f} < {min_range} or target {target_distance:.0f} too small")
            return

        # Clamp SL within bounds
        sl_points = max(min_sl, min(sl_distance, max_sl))

        # GEX adjustment: negative GEX = volatile, give more room
        if manager.gex_history and len(manager.gex_history) > 3:
            avg_gex = sum(list(manager.gex_history)[-3:]) / 3
            if avg_gex < 0:
                sl_points = min(sl_points * 1.25, max_sl)

        target_points = target_distance

        rr = target_points / sl_points if sl_points > 0 else 0
        if rr < 1.2:
            logger.debug(f"[{self.name}] Skipping: R:R {rr:.1f} < 1.2 (target:{target_points:.0f} sl:{sl_points:.0f})")
            return

        if direction == 'LONG':
            sl = ltp - sl_points
            target = ltp + target_points
        else:
            sl = ltp + sl_points
            target = ltp - target_points

        logger.info(f"[{self.name}] {direction} | Walls:{pe_wall}-{ce_wall} | Target dist:{target_distance:.0f} | SL:{sl_points:.0f} | R:R 1:{target_distance/sl_points:.1f}")

        # Capture entry context
        majority_action, confidence, _, _, _ = self.logger.entry_aggregator.get_majority_signal()
        distribution = self.logger.entry_aggregator.get_signal_distribution()

        lot_sizes = {
            'NIFTY': 75, 'BANKNIFTY': 15, 'SENSEX': 10,
            'RELIANCE': 250, 'HDFCBANK': 550, 'ICICIBANK': 700,
            'SBIN': 1500, 'INFY': 300, 'BHARTIARTL': 458
        }
        lot_size = lot_sizes.get(manager.underlying, 50)

        trade_id = f"{self.name}_{int(time.time())}"
        self.active_trade = {
            'Trade_ID': trade_id,
            'Symbol': manager.underlying,
            'Contract': f"SPOT_{manager.underlying}",
            'strike': 0,  # No strike for spot
            'type': 'FUTURES' if direction == 'LONG' else 'FUTURES',
            'direction': direction,
            'entry_price': ltp,
            'sl': sl,
            'initial_sl': sl,
            'target': target,
            'initial_target': target,
            'target_extensions': 0,
            'entry_time': datetime.now().isoformat(),
            'Max_Price_Seen': ltp,
            'Min_Price_Seen': ltp,
            'Strategy': self.name,
            'Lot_Size': lot_size,
            '_entry_confidence': round(confidence, 1),
            '_entry_distribution': str(distribution),
            '_entry_pcr': round(metrics.get('pcr', 0), 4),
            '_entry_iv': 0,
            '_entry_delta': 1.0 if direction == 'LONG' else -1.0,
            '_entry_gamma': 0,
            '_entry_theta': 0,
            '_entry_vwap': round(vwap, 2),
            '_entry_oi_wall_ce': ce_wall,
            '_entry_oi_wall_pe': pe_wall,
            '_entry_score': score,
        }

        logger.info(f"[{self.name}] {direction} ENTRY @ {ltp:.2f} | SL:{sl:.2f} | Tgt:{target:.2f} | Walls:{pe_wall}-{ce_wall}")

        log_entry = self._build_log_entry(manager, self.active_trade, 'ENTRY', ltp, score,
                                          f"Majority: {direction}", metrics)
        self.logger.log_trade(log_entry)
        self.logger.entry_aggregator.clear()

    def _manage_spot_trade(self, manager, metrics):
        """Manage an active spot/futures trade with point-based trailing SL."""
        trade = self.active_trade
        if not trade: return

        ltp = manager.underlying_ltp
        if ltp <= 0: return

        direction = trade['direction']
        entry = trade['entry_price']

        # Track hold time
        hold_seconds = 0
        try:
            entry_dt = datetime.fromisoformat(trade['entry_time'])
            hold_seconds = (datetime.now() - entry_dt).total_seconds()
        except Exception:
            pass

        # Update high/low water marks
        if ltp > trade.get('Max_Price_Seen', 0):
            trade['Max_Price_Seen'] = ltp
        if ltp < trade.get('Min_Price_Seen', float('inf')):
            trade['Min_Price_Seen'] = ltp

        # Calculate P&L in points
        if direction == 'LONG':
            pnl_points = ltp - entry
            max_favorable = trade['Max_Price_Seen'] - entry
        else:
            pnl_points = entry - ltp
            max_favorable = entry - trade['Min_Price_Seen']

        # TRAILING TARGET: Update target if OI walls shift during trade
        # Skip if walls are 0 or invalid (happens during chain regeneration for 50-120s)
        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)
        entry_ce_wall = trade.get('_entry_oi_wall_ce', 0)
        entry_pe_wall = trade.get('_entry_oi_wall_pe', 0)

        # Only update if walls are valid (non-zero AND not wildly different from entry walls)
        walls_valid = (ce_wall > 0 and pe_wall > 0 and ce_wall > pe_wall)
        if walls_valid and entry_ce_wall > 0:
            # Sanity check: wall shouldn't have shifted more than 500 pts from entry
            # (indicates data reset, not real wall movement)
            wall_shift = abs(ce_wall - entry_ce_wall) + abs(pe_wall - entry_pe_wall)
            walls_valid = wall_shift < 500

        if walls_valid:
            if direction == 'LONG' and ce_wall > entry:
                new_target = entry + (ce_wall - entry) * 0.80
                if new_target > trade['target']:
                    trade['target'] = new_target
            elif direction == 'SHORT' and pe_wall > 0 and pe_wall < entry:
                new_target = entry - (entry - pe_wall) * 0.80
                if new_target < trade['target']:
                    trade['target'] = new_target

        # TRAILING SL - Loose trailing to give room for pullbacks
        # Start trailing only at 40% of target (not 25% - too tight)
        # Lock small percentages to avoid frequent trailing SL hits
        #
        # 40% toward target → breakeven (just protect capital)
        # 60% → lock 30% of profit (still gives room for pullback)
        # 80% → lock 50% of profit
        # 100% (target) → lock 60% + target already trailing from live walls
        #
        # April 2 fix: Scale lock percentages with day's volatility
        initial_target = trade.get('initial_target', trade['target'])
        if direction == 'LONG':
            target_distance = initial_target - entry
        else:
            target_distance = entry - initial_target

        if target_distance > 0:
            profit_pct = max_favorable / target_distance
            vol_scale = self._get_volatility_scale(manager)

            if profit_pct >= 1.0:
                lock_pct = 0.60 * vol_scale
            elif profit_pct >= 0.80:
                lock_pct = 0.50 * vol_scale
            elif profit_pct >= 0.60:
                lock_pct = 0.30 * vol_scale
            elif profit_pct >= 0.40:
                lock_pct = 0.02  # Breakeven with tiny buffer (not scaled - capital protection)
            else:
                lock_pct = None

            if lock_pct is not None:
                if direction == 'LONG':
                    new_sl = entry + (max_favorable * lock_pct)
                    trade['sl'] = max(trade['sl'], new_sl)
                else:
                    new_sl = entry - (max_favorable * lock_pct)
                    trade['sl'] = min(trade['sl'], new_sl)

        exit_reason = None

        # SL/Target check
        if direction == 'LONG':
            if ltp <= trade['sl']:
                exit_reason = "SL HIT" if trade['sl'] == trade['initial_sl'] else "TRAILING SL"
            elif ltp >= trade['target']:
                # Extend target if trend continues
                if trade.get('target_extensions', 0) < 3:
                    initial_dist = trade['initial_target'] - entry
                    trade['target'] = ltp + (initial_dist * 0.5)
                    trade['target_extensions'] = trade.get('target_extensions', 0) + 1
                    trade['sl'] = max(trade['sl'], entry + (max_favorable * 0.60))
                    logger.info(f"[{self.name}] TARGET EXTENDED #{trade['target_extensions']} to {trade['target']:.2f}")
                else:
                    exit_reason = "MAX TARGET HIT"
        else:  # SHORT
            if ltp >= trade['sl']:
                exit_reason = "SL HIT" if trade['sl'] == trade['initial_sl'] else "TRAILING SL"
            elif ltp <= trade['target']:
                if trade.get('target_extensions', 0) < 3:
                    initial_dist = entry - trade['initial_target']
                    trade['target'] = ltp - (initial_dist * 0.5)
                    trade['target_extensions'] = trade.get('target_extensions', 0) + 1
                    trade['sl'] = min(trade['sl'], entry - (max_favorable * 0.60))
                    logger.info(f"[{self.name}] TARGET EXTENDED #{trade['target_extensions']} to {trade['target']:.2f}")
                else:
                    exit_reason = "MAX TARGET HIT"

        # OI PATTERN EXIT: If OI pattern flips against trade on a losing position
        # LONG + Short Buildup/Long Unwinding = institutions betting against you → exit
        # SHORT + Long Buildup/Short Covering = institutions betting against you → exit
        # Only on losing trades - profitable trades use trailing SL
        oi_pattern = metrics.get('oi_pattern', '')
        oi_strength = metrics.get('oi_pattern_strength', 0)

        # OI PATTERN EXIT disabled - OI patterns flip every 2-3 min on sideways days
        # causing false exits. OI pattern still used for ENTRY filtering.
        # Exits handled by: Hard SL + Trailing SL + VWAP Cross

        # VWAP CROSS EXIT: Only after min hold AND price must be significantly past VWAP
        # March 30: VWAP cross triggered 5 times on minor crosses (-12 to -47 pts loss)
        # Fix: Price must cross VWAP by at least 0.1% (not just touch it)
        #
        # April 2 fix: In first 60 min (before 10:45), VWAP is still establishing.
        # Widen margin to 0.2% and require longer hold (180s) to avoid morning whipsaw.
        # Morning trades on Apr 2 lost -93 pts from premature VWAP cross exits.
        vwap = metrics.get('vwap', 0)
        now = _ist_now()
        is_early_session = (now.hour == 9) or (now.hour == 10 and now.minute < 45)

        # Early session: wider margin (0.2%) and longer hold (180s)
        # Normal session: standard margin (0.1%) and normal hold
        if is_early_session:
            vwap_margin_pct = 0.002   # 0.2% (~44 pts for NIFTY at 22000)
            vwap_min_hold = max(self.min_hold_seconds, 180)
        else:
            vwap_margin_pct = 0.001   # 0.1% (~22 pts for NIFTY at 22000)
            vwap_min_hold = self.min_hold_seconds

        if not exit_reason and vwap > 0 and pnl_points <= 0 and hold_seconds >= vwap_min_hold:
            vwap_cross_margin = vwap * vwap_margin_pct

            if direction == 'LONG' and ltp < (vwap - vwap_cross_margin):
                self.logger.exit_aggregator.add_signal('VWAP_CROSS', metrics['raw_score'])
                _, conf, samples, _, _ = self.logger.exit_aggregator.get_majority_signal()
                if samples >= self.logger.exit_min_samples and conf >= 60:
                    exit_reason = f"VWAP CROSS DOWN ({ltp:.2f} < {vwap:.2f})"
            elif direction == 'SHORT' and ltp > (vwap + vwap_cross_margin):
                self.logger.exit_aggregator.add_signal('VWAP_CROSS', metrics['raw_score'])
                _, conf, samples, _, _ = self.logger.exit_aggregator.get_majority_signal()
                if samples >= self.logger.exit_min_samples and conf >= 60:
                    exit_reason = f"VWAP CROSS UP ({ltp:.2f} > {vwap:.2f})"

        if exit_reason:
            self._close_spot_trade(manager, ltp, exit_reason, metrics)

    def _close_spot_trade(self, manager, exit_price, reason, metrics=None):
        """Close spot trade and log."""
        trade = self.active_trade
        if not trade or trade.get('_closed', False): return

        trade['_closed'] = True
        self.active_trade = None
        self.exit_cooldown_until = time.time() + 180

        try:
            direction = trade['direction']
            if direction == 'LONG':
                pnl = exit_price - trade['entry_price']
                max_profit_seen = trade.get('Max_Price_Seen', trade['entry_price']) - trade['entry_price']
                max_loss_seen = trade['entry_price'] - trade.get('Min_Price_Seen', trade['entry_price'])
            else:
                pnl = trade['entry_price'] - exit_price
                max_profit_seen = trade['entry_price'] - trade.get('Min_Price_Seen', trade['entry_price'])
                max_loss_seen = trade.get('Max_Price_Seen', trade['entry_price']) - trade['entry_price']

            self.daily_pnl += pnl
            self.trades_today += 1
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

            manager.trades_today += 1
            manager.daily_pnl_points += pnl

            hold_seconds = 0
            try:
                entry_dt = datetime.fromisoformat(trade['entry_time'])
                hold_seconds = int((datetime.now() - entry_dt).total_seconds())
            except Exception:
                pass

            exit_score = metrics.get('raw_score', 0) if metrics else 0

            logger.info(f"[{self.name}] {direction} EXIT @ {exit_price:.2f} | {reason} | PnL:{pnl:+.2f} pts | Hold:{hold_seconds}s")

            log_entry = self._build_log_entry(manager, trade, 'EXIT', exit_price, exit_score, reason, metrics)
            log_entry.update({
                'Exit_Price': exit_price,
                'PnL_Points': round(pnl, 2),
                'PnL_Amount': round(pnl * trade['Lot_Size'], 2),
                'ROI_Pct': round((pnl / trade['entry_price'] * 100), 2) if trade['entry_price'] > 0 else 0,
                'Time_Held_Sec': hold_seconds,
                'Max_Profit_Seen': round(max_profit_seen, 2),
                'Max_Loss_Seen': round(max_loss_seen, 2),
            })
            self.logger.log_trade(log_entry)
            self.logger.entry_aggregator.clear()
            self.logger.exit_aggregator.clear()

        except Exception as e:
            logger.error(f"[{self.name}] Error closing spot trade: {e}")

    def _force_exit_spot(self, manager, metrics, reason):
        """Force exit for EOD."""
        if not self.active_trade or self.active_trade.get('_closed', False):
            self.active_trade = None
            return
        ltp = manager.underlying_ltp
        if ltp > 0:
            self._close_spot_trade(manager, ltp, reason, metrics)
        else:
            self._close_spot_trade(manager, self.active_trade['entry_price'], reason, metrics)


# =============================================================================
# SELL BASE CLASS
# Overrides entry/management/exit for option SELLING (writing).
# PnL = entry_price - exit_price (reversed from BUY)
# SL = ABOVE entry (premium rising = losing)
# Target = BELOW entry (premium dropping = winning)
# Trailing SL moves DOWN as premium decays in our favor
# =============================================================================
class SellBaseStrategy(BaseStrategy):
    """Base for option SELLING strategies. Overrides trade lifecycle for SELL direction."""

    def _execute_sell_entry(self, manager, action, score, metrics):
        """Execute a SELL option entry."""
        # For SELL CE: we want bearish market (sell call premium)
        # For SELL PE: we want bullish market (sell put premium)
        if 'CALL' in action:
            opt_type = 'CE'
        else:
            opt_type = 'PE'

        if not manager.atm_strike or manager.atm_strike not in manager.option_data:
            return

        data_key = 'ce_data' if opt_type == 'CE' else 'pe_data'
        opt_data = manager.option_data[manager.atm_strike][data_key]
        ltp = opt_data.get('ltp', 0)
        if ltp <= 0: return

        contract_info = {
            'strike': manager.atm_strike,
            'name': manager.option_data[manager.atm_strike][f'{opt_type.lower()}_symbol'],
            'ltp': ltp,
            'type': opt_type,
            'iv': opt_data.get('iv', 0),
            'delta': opt_data.get('delta', 0),
        }

        dte = metrics.get('days_to_expiry', 5)
        sl_points, target_points = self._calc_sl_target(ltp, contract_info, manager, dte)

        # SELL: SL is ABOVE entry (premium rising = loss), Target is BELOW (premium falling = profit)
        sl = ltp + sl_points
        target = ltp - target_points

        majority_action, confidence, _, _, _ = self.logger.entry_aggregator.get_majority_signal()
        distribution = self.logger.entry_aggregator.get_signal_distribution()

        trade_id = f"{self.name}_{int(time.time())}"
        self.active_trade = {
            'Trade_ID': trade_id,
            'Symbol': manager.underlying,
            'Contract': contract_info['name'],
            'strike': contract_info['strike'],
            'type': contract_info['type'],
            'direction': 'SELL',
            'entry_price': ltp,
            'sl': sl,
            'initial_sl': sl,
            'target': target,
            'initial_target': target,
            'target_extensions': 0,
            'entry_time': datetime.now().isoformat(),
            'Max_Price_Seen': ltp,
            'Min_Price_Seen': ltp,
            'Strategy': self.name,
            'Lot_Size': self.logger._get_lot_size(manager.underlying),
            '_entry_confidence': round(confidence, 1),
            '_entry_distribution': str(distribution),
            '_entry_pcr': round(metrics.get('pcr', 0), 4),
            '_entry_iv': round(contract_info.get('iv', 0), 2),
            '_entry_delta': round(contract_info.get('delta', 0), 4),
            '_entry_gamma': round(opt_data.get('gamma', 0), 6),
            '_entry_theta': round(opt_data.get('theta', 0), 4),
            '_entry_vwap': round(metrics.get('vwap', 0), 2),
            '_entry_oi_wall_ce': metrics.get('oi_wall_ce', 0),
            '_entry_oi_wall_pe': metrics.get('oi_wall_pe', 0),
            '_entry_oi_pattern': metrics.get('oi_pattern', ''),
            '_entry_score': score,
        }

        logger.info(f"[{self.name}] SELL {opt_type} @ {ltp} | SL:{sl:.2f} | Tgt:{target:.2f} | DTE:{dte:.1f}")

        log_entry = self._build_log_entry(manager, self.active_trade, 'ENTRY', ltp, score,
                                          f"Majority: {action}", metrics)
        self.logger.log_trade(log_entry)
        self.logger.entry_aggregator.clear()

    def _manage_sell_trade(self, manager, metrics):
        """Manage SELL trade: profit when premium drops, loss when premium rises."""
        trade = self.active_trade
        if not trade or trade.get('_closed', False):
            self.active_trade = None
            return

        current_ltp = self._get_trade_ltp(manager, trade)
        if current_ltp <= 0: return

        if current_ltp > trade.get('Max_Price_Seen', 0):
            trade['Max_Price_Seen'] = current_ltp
        if current_ltp < trade.get('Min_Price_Seen', float('inf')):
            trade['Min_Price_Seen'] = current_ltp

        # Track hold time
        hold_seconds = 0
        try:
            entry_dt = datetime.fromisoformat(trade['entry_time'])
            hold_seconds = (datetime.now() - entry_dt).total_seconds()
        except Exception:
            pass

        entry = trade['entry_price']
        initial_sl = trade.get('initial_sl', trade['sl'])

        # SELL PnL: profit when premium DROPS
        pnl = entry - current_ltp

        # TRAILING SL for SELL (same 25/50/75/100 levels as BUY)
        target_distance = entry - trade.get('initial_target', trade['target'])
        if target_distance > 0:
            profit_pct = pnl / target_distance

            if profit_pct >= 0.75:
                new_sl = entry - (pnl * 0.60)
                trade['sl'] = min(trade['sl'], new_sl)
            elif profit_pct >= 0.50:
                new_sl = entry - (pnl * 0.40)
                trade['sl'] = min(trade['sl'], new_sl)
            elif profit_pct >= 0.25:
                new_sl = entry + (entry * 0.002)  # Just above breakeven
                trade['sl'] = min(trade['sl'], new_sl)

        exit_reason = None

        # SL: premium ROSE above our SL (we're losing)
        if current_ltp >= trade['sl']:
            exit_reason = "SL HIT" if trade['sl'] == initial_sl else "TRAILING SL HIT"
        # TARGET: premium DROPPED to our target (we're profiting)
        elif current_ltp <= trade['target']:
            exit_reason = "TARGET HIT"

        # Signal-based exit on losing SELL trades (ONLY after min hold - prevents 27s exits)
        if not exit_reason and pnl <= 0 and hold_seconds >= self.min_hold_seconds:
            raw_score = metrics['raw_score']
            if trade['type'] == 'CE' and raw_score > self.entry_score_threshold:
                exit_reason = f"SCORE AGAINST SELL CE ({raw_score})"
            elif trade['type'] == 'PE' and raw_score < -self.entry_score_threshold:
                exit_reason = f"SCORE AGAINST SELL PE ({raw_score})"

        if exit_reason:
            self._close_sell_trade(manager, current_ltp, exit_reason, metrics)

    def _close_sell_trade(self, manager, exit_price, reason, metrics=None):
        """Close SELL trade. PnL = entry - exit (reversed from BUY)."""
        trade = self.active_trade
        if not trade or trade.get('_closed', False): return

        trade['_closed'] = True
        self.active_trade = None
        self.exit_cooldown_until = time.time() + 180

        try:
            # SELL PnL: profit if premium dropped
            pnl = trade['entry_price'] - exit_price
            self.daily_pnl += pnl
            self.trades_today += 1

            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

            manager.trades_today += 1
            manager.daily_pnl_points += pnl

            hold_seconds = 0
            try:
                entry_dt = datetime.fromisoformat(trade['entry_time'])
                hold_seconds = int((datetime.now() - entry_dt).total_seconds())
            except Exception:
                pass

            # For SELL: max profit = entry - min_price, max loss = max_price - entry
            max_profit_seen = trade['entry_price'] - trade.get('Min_Price_Seen', trade['entry_price'])
            max_loss_seen = trade.get('Max_Price_Seen', trade['entry_price']) - trade['entry_price']

            exit_score = metrics.get('raw_score', 0) if metrics else 0

            logger.info(f"[{self.name}] SELL EXIT @ {exit_price:.2f} | {reason} | PnL:{pnl:+.2f} | Hold:{hold_seconds}s")

            log_entry = self._build_log_entry(manager, trade, 'EXIT', exit_price, exit_score, reason, metrics)
            log_entry.update({
                'Exit_Price': exit_price,
                'PnL_Points': round(pnl, 2),
                'PnL_Amount': round(pnl * trade['Lot_Size'], 2),
                'ROI_Pct': round((pnl / trade['entry_price'] * 100), 2) if trade['entry_price'] > 0 else 0,
                'Time_Held_Sec': hold_seconds,
                'Max_Profit_Seen': round(max_profit_seen, 2),
                'Max_Loss_Seen': round(max_loss_seen, 2),
            })
            self.logger.log_trade(log_entry)
            self.logger.entry_aggregator.clear()
            self.logger.exit_aggregator.clear()

        except Exception as e:
            logger.error(f"[{self.name}] Error closing sell trade: {e}")


# =============================================================================
# STRATEGY 9: THETA COLLECTOR (SELL ONLY)
# Sells ATM options in sideways/range markets. Collects theta decay.
# =============================================================================
class V10ThetaSellStrategy(SellBaseStrategy):
    """Theta Collector - SELL options in range-bound markets.

    Edge: 70-80% of options expire worthless. In sideways markets with positive GEX
    and clear OI walls, selling premium is highest probability trade.

    Entry: SELL CE when near resistance wall, SELL PE when near support wall.
    Requires: GEX positive, OI pattern = range/neutral, price between walls.
    SL: 25 NIFTY points on underlying (not option premium) to cap risk.
    Max 3 trades/day.
    """
    def __init__(self):
        super().__init__(
            "V10_ThetaSell", entry_score=8, agg_window=120, min_conf=65,
            min_hold=120, exit_style='conservative', max_trades=999999
        )

    def process_signals(self, manager, metrics):
        current_time = time.time()

        if self.active_trade and _is_force_exit_time():
            if not self.active_trade.get('_closed', False):
                ltp = self._get_trade_ltp(manager, self.active_trade)
                if ltp > 0:
                    self._close_sell_trade(manager, ltp, "EOD FORCE EXIT", metrics)
            self.active_trade = None
            return
        if not _is_market_hours(): return

        now = _ist_now()
        if now.hour == 9 and now.minute < 45: return  # Wait for OI to establish

        raw_score = metrics['raw_score']
        ltp = metrics.get('ltp', 0)
        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)
        oi_pattern = metrics.get('oi_pattern', '')

        instant_action = 'WAIT'

        # SELL CE when price is in upper half of range (near resistance)
        if ce_wall > 0 and pe_wall > 0 and ltp > 0:
            range_mid = (ce_wall + pe_wall) / 2
            if ltp > range_mid and raw_score < 5:  # Upper half + not strongly bullish
                instant_action = 'SELL CALL'
            elif ltp < range_mid and raw_score > -5:  # Lower half + not strongly bearish
                instant_action = 'SELL PUT'

        current_strike = manager.atm_strike
        self.logger.entry_aggregator.add_signal(
            action=instant_action, score=raw_score,
            regime=manager.guardian.current_regime,
            strike=current_strike, contract=f"SELL_{manager.underlying}"
        )

        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            maj, conf, samp, _, _ = self.logger.entry_aggregator.get_majority_signal()
            print(f"[{self.name}] OI:{oi_pattern[:30]} | Score:{raw_score:+d} | Maj:{maj}({conf:.0f}%) | Trades:{self.trades_today}/{self.max_trades_per_day}")

        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_sell_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return  # Tighter for sell strategies
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN): return
            self._check_sell_entry(manager, metrics)

    def _check_sell_entry(self, manager, metrics):
        majority_action, confidence, sample_count, avg_score, _ = \
            self.logger.entry_aggregator.get_majority_signal()
        if 'SELL' not in majority_action: return
        if sample_count < self.logger.entry_min_samples: return
        if confidence < self.min_confidence: return

        if not self.should_enter(manager, metrics, majority_action, avg_score, confidence):
            return

        self._execute_sell_entry(manager, majority_action, avg_score, metrics)

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        # GEX must be positive (mean reverting = range bound = theta friendly)
        if manager.gex_history and len(manager.gex_history) > 5:
            avg_gex = sum(list(manager.gex_history)[-5:]) / 5
            if avg_gex < 0: return False

        # OI pattern must NOT be trending
        oi_pattern = metrics.get('oi_pattern', '')
        if any(p in oi_pattern for p in ['Long Buildup', 'Short Buildup']):
            return False  # Fresh money entering = trend forming, don't sell

        # Price must be between OI walls (inside the range)
        ltp = metrics.get('ltp', 0)
        ce_wall = metrics.get('oi_wall_ce', 0)
        pe_wall = metrics.get('oi_wall_pe', 0)
        if ltp <= 0 or ce_wall <= 0 or pe_wall <= 0: return False
        if ltp >= ce_wall or ltp <= pe_wall: return False  # Outside walls = breakout, don't sell

        # Regime must be NEUTRAL or stale (not actively trending)
        regime = manager.guardian.current_regime
        if regime in ('BULLISH', 'BEARISH'):
            lock_age = time.time() - manager.guardian.last_change_time
            if lock_age < 300: return False  # Active trend < 5 min old

        return True


# =============================================================================
# STRATEGY 10: IV CRUSH SELLER (SELL ONLY)
# Sells when IV is elevated. Profits from IV contraction.
# =============================================================================
class V10IVCrushSellStrategy(SellBaseStrategy):
    """IV Crush Seller - Sells options when IV is significantly above average.

    Edge: After events (RBI, earnings, budget), IV drops rapidly.
    Selling high-IV options captures the crush as premium shrinks.
    Also works during intraday IV spikes from sudden moves.

    Entry: IV must be 20%+ above its rolling average. SELL the ATM option
    on the side with higher IV (PE IV > CE IV → sell PE, else sell CE).
    Max 2 trades/day.
    """
    def __init__(self):
        super().__init__(
            "V10_IVCrush", entry_score=5, agg_window=120, min_conf=60,
            min_hold=180, exit_style='conservative', max_trades=999999
        )

    def process_signals(self, manager, metrics):
        current_time = time.time()

        if self.active_trade and _is_force_exit_time():
            if not self.active_trade.get('_closed', False):
                ltp = self._get_trade_ltp(manager, self.active_trade)
                if ltp > 0:
                    self._close_sell_trade(manager, ltp, "EOD FORCE EXIT", metrics)
            self.active_trade = None
            return
        if not _is_market_hours(): return

        now = _ist_now()
        if now.hour == 9 and now.minute < 45: return

        raw_score = metrics['raw_score']
        instant_action = 'WAIT'

        # Check if IV is elevated
        if len(manager.iv_history) >= 120:
            avg_iv = sum(manager.iv_history) / len(manager.iv_history)
            current_iv = manager.iv_history[-1]

            if current_iv > avg_iv * 1.20:  # IV 20%+ above average
                # Sell the side with higher IV
                if manager.atm_strike and manager.atm_strike in manager.option_data:
                    atm = manager.option_data[manager.atm_strike]
                    ce_iv = atm['ce_data'].get('iv', 0)
                    pe_iv = atm['pe_data'].get('iv', 0)
                    if pe_iv > ce_iv:
                        instant_action = 'SELL PUT'  # PE IV higher, sell it
                    else:
                        instant_action = 'SELL CALL'  # CE IV higher, sell it

        current_strike = manager.atm_strike
        self.logger.entry_aggregator.add_signal(
            action=instant_action, score=raw_score,
            regime=manager.guardian.current_regime,
            strike=current_strike, contract=f"SELL_{manager.underlying}"
        )

        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            iv_info = f"IV:{manager.iv_history[-1]:.1f}" if manager.iv_history else "IV:N/A"
            avg_info = f"Avg:{sum(manager.iv_history)/len(manager.iv_history):.1f}" if len(manager.iv_history) > 10 else ""
            print(f"[{self.name}] {iv_info} {avg_info} | Score:{raw_score:+d} | Trades:{self.trades_today}/{self.max_trades_per_day}")

        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            else:
                self._manage_sell_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN): return
            self._check_sell_entry(manager, metrics)

    def _check_sell_entry(self, manager, metrics):
        majority_action, confidence, sample_count, avg_score, _ = \
            self.logger.entry_aggregator.get_majority_signal()
        if 'SELL' not in majority_action: return
        if sample_count < self.logger.entry_min_samples: return
        if confidence < self.min_confidence: return
        self._execute_sell_entry(manager, majority_action, avg_score, metrics)

    def should_enter(self, manager, metrics, majority_action, avg_score, confidence):
        return True  # IV check already done in process_signals


# =============================================================================
# STRATEGY 11: ADAPTIVE (DECIDES BUY OR SELL)
# Uses market regime, GEX, OI pattern to decide side.
# =============================================================================
class V10AdaptiveStrategy(BaseStrategy):
    """Adaptive Strategy - Decides whether to BUY or SELL options.

    Decision logic:
    - TRENDING market (strong regime + momentum) → BUY options (ride the move)
    - SIDEWAYS market (neutral regime + positive GEX) → SELL options (collect theta)
    - HIGH IV → prefer SELL (premium is expensive = seller's edge)
    - LOW IV → prefer BUY (premium is cheap = buyer's edge)
    - EXPIRY DAY → prefer SELL (theta acceleration = seller's edge)

    This strategy gives data on WHEN buying vs selling is more profitable.
    Max 4 trades/day.
    """
    def __init__(self):
        super().__init__(
            "V10_Adaptive", entry_score=15, agg_window=90, min_conf=65,
            min_hold=180, exit_style='conservative', max_trades=999999
        )
        self._current_mode = 'UNDECIDED'  # 'BUY' or 'SELL'

    def process_signals(self, manager, metrics):
        current_time = time.time()

        if self.active_trade and _is_force_exit_time():
            if self._current_mode == 'SELL' and not self.active_trade.get('_closed', False):
                ltp = self._get_trade_ltp(manager, self.active_trade)
                if ltp > 0:
                    self._close_sell_trade(manager, ltp, "EOD FORCE EXIT", metrics)
                self.active_trade = None
            else:
                self._force_exit_active_trade(manager, metrics, "EOD FORCE EXIT")
            return
        if not _is_market_hours(): return

        now = _ist_now()
        if now.hour == 9 and now.minute < 45: return

        raw_score = metrics['raw_score']
        dte = metrics.get('days_to_expiry', 5)

        # DECIDE: BUY or SELL?
        self._current_mode = self._decide_mode(manager, metrics, dte)

        if self._current_mode == 'SELL':
            # Generate SELL signals
            instant_action = 'WAIT'
            if raw_score < 5 and raw_score > -30:  # Not strongly directional
                ce_wall = metrics.get('oi_wall_ce', 0)
                pe_wall = metrics.get('oi_wall_pe', 0)
                ltp = metrics.get('ltp', 0)
                if ce_wall > 0 and pe_wall > 0 and ltp > 0:
                    mid = (ce_wall + pe_wall) / 2
                    if ltp > mid:
                        instant_action = 'SELL CALL'
                    else:
                        instant_action = 'SELL PUT'

            self.logger.entry_aggregator.add_signal(
                action=instant_action, score=raw_score,
                regime=manager.guardian.current_regime,
                strike=manager.atm_strike, contract=f"ADAPTIVE_{manager.underlying}"
            )
        else:
            # Generate BUY signals (same as base)
            instant_action = 'WAIT'
            if raw_score > self.entry_score_threshold: instant_action = 'BUY CALL'
            elif raw_score < -self.entry_score_threshold: instant_action = 'BUY PUT'

            current_strike = manager.atm_strike
            current_contract = None
            if current_strike and instant_action != 'WAIT':
                opt_type = 'CE' if 'CALL' in instant_action else 'PE'
                current_contract = manager.construct_option_symbol(current_strike, opt_type)

            self.logger.entry_aggregator.add_signal(
                action=instant_action, score=raw_score,
                regime=manager.guardian.current_regime,
                strike=current_strike, contract=current_contract
            )

        # Heartbeat
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            print(f"[{self.name}] Mode:{self._current_mode} | Score:{raw_score:+d} | DTE:{dte:.1f} | Trades:{self.trades_today}/{self.max_trades_per_day}")

        # Manage or enter
        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            elif self.active_trade.get('direction') == 'SELL':
                self._manage_sell_trade(manager, metrics)
            else:
                self._manage_active_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN): return
            self._check_adaptive_entry(manager, metrics)

    def _decide_mode(self, manager, metrics, dte):
        """Decide BUY or SELL based on current conditions."""
        score = 0  # Positive = favor SELL, Negative = favor BUY

        # DTE: Near expiry favors SELL
        if dte <= 1: score += 3
        elif dte <= 2: score += 1

        # GEX: Positive = range-bound = SELL, Negative = volatile = BUY
        if manager.gex_history and len(manager.gex_history) > 5:
            avg_gex = sum(list(manager.gex_history)[-5:]) / 5
            if avg_gex > 0: score += 2
            else: score -= 2

        # IV: High IV = SELL (expensive premium), Low IV = BUY (cheap)
        if len(manager.iv_history) >= 60:
            avg_iv = sum(manager.iv_history) / len(manager.iv_history)
            current_iv = manager.iv_history[-1]
            if current_iv > avg_iv * 1.15: score += 2
            elif current_iv < avg_iv * 0.90: score -= 2

        # OI pattern: Range forming = SELL, Buildup = BUY
        oi_pattern = metrics.get('oi_pattern', '')
        if 'Range Forming' in oi_pattern: score += 2
        elif 'Buildup' in oi_pattern: score -= 2
        elif 'Breakout Coming' in oi_pattern: score -= 3

        # Regime: Strong trend = BUY, Neutral = SELL
        regime = manager.guardian.current_regime
        if regime == 'NEUTRAL': score += 1
        elif regime in ('BULLISH', 'BEARISH'):
            lock_age = time.time() - manager.guardian.last_change_time
            if lock_age < 300: score -= 2  # Fresh strong trend = BUY

        return 'SELL' if score >= 3 else 'BUY'

    def _check_adaptive_entry(self, manager, metrics):
        majority_action, confidence, sample_count, avg_score, _ = \
            self.logger.entry_aggregator.get_majority_signal()
        if majority_action == 'WAIT': return
        if sample_count < self.logger.entry_min_samples: return
        if confidence < self.min_confidence: return

        if self._current_mode == 'SELL' and 'SELL' in majority_action:
            self._execute_sell_entry(manager, majority_action, avg_score, metrics)
        elif self._current_mode == 'BUY' and 'BUY' in majority_action:
            # Use base BUY entry with all checks
            quant = metrics.get('quant_signal', {})
            if not self._has_signal_confluence(quant): return
            self._execute_entry(manager, majority_action, avg_score, metrics)

    # Inherit SellBaseStrategy methods for SELL trades
    _execute_sell_entry = SellBaseStrategy._execute_sell_entry
    _manage_sell_trade = SellBaseStrategy._manage_sell_trade
    _close_sell_trade = SellBaseStrategy._close_sell_trade


# =============================================================================
# STRATEGY 12: DTE ADAPTIVE (BUY far, SELL near expiry)
# =============================================================================
class V10DTEAdaptiveStrategy(BaseStrategy):
    """DTE Adaptive - BUY when far from expiry, SELL when near.

    Logic:
    - DTE > 3 days → BUY options (time to be right, theta is small)
    - DTE 1-3 days → Either (based on IV level)
    - DTE < 1 (expiry day) → SELL options (theta acceleration = seller's edge)

    This gives direct data on whether the same signal is more profitable
    as a BUY trade vs SELL trade depending on time to expiry.
    Max 3 trades/day.
    """
    def __init__(self):
        super().__init__(
            "V10_DTEAdapt", entry_score=15, agg_window=90, min_conf=65,
            min_hold=120, exit_style='conservative', max_trades=999999
        )
        self._dte_mode = 'BUY'

    def process_signals(self, manager, metrics):
        current_time = time.time()

        if self.active_trade and _is_force_exit_time():
            if self._dte_mode == 'SELL' and not self.active_trade.get('_closed', False):
                ltp = self._get_trade_ltp(manager, self.active_trade)
                if ltp > 0:
                    self._close_sell_trade(manager, ltp, "EOD FORCE EXIT", metrics)
                self.active_trade = None
            else:
                self._force_exit_active_trade(manager, metrics, "EOD FORCE EXIT")
            return
        if not _is_market_hours(): return

        now = _ist_now()
        if now.hour == 9 and now.minute < 45: return

        raw_score = metrics['raw_score']
        dte = metrics.get('days_to_expiry', 5)

        # Decide mode based on DTE
        if dte <= 1:
            self._dte_mode = 'SELL'
        elif dte > 3:
            self._dte_mode = 'BUY'
        else:
            # DTE 1-3: Check IV. High IV = SELL, Low IV = BUY
            if len(manager.iv_history) >= 60:
                avg_iv = sum(manager.iv_history) / len(manager.iv_history)
                self._dte_mode = 'SELL' if manager.iv_history[-1] > avg_iv * 1.10 else 'BUY'
            else:
                self._dte_mode = 'BUY'

        # Generate signals based on mode
        if self._dte_mode == 'SELL':
            instant_action = 'WAIT'
            if abs(raw_score) < 20:  # Not strongly directional = safe to sell
                if raw_score > 0:
                    instant_action = 'SELL PUT'  # Mildly bullish = sell put
                else:
                    instant_action = 'SELL CALL'  # Mildly bearish = sell call

            self.logger.entry_aggregator.add_signal(
                action=instant_action, score=raw_score,
                regime=manager.guardian.current_regime,
                strike=manager.atm_strike, contract=f"DTE_{manager.underlying}"
            )
        else:
            instant_action = 'WAIT'
            if raw_score > self.entry_score_threshold: instant_action = 'BUY CALL'
            elif raw_score < -self.entry_score_threshold: instant_action = 'BUY PUT'

            current_strike = manager.atm_strike
            current_contract = None
            if current_strike and instant_action != 'WAIT':
                opt_type = 'CE' if 'CALL' in instant_action else 'PE'
                current_contract = manager.construct_option_symbol(current_strike, opt_type)

            self.logger.entry_aggregator.add_signal(
                action=instant_action, score=raw_score,
                regime=manager.guardian.current_regime,
                strike=current_strike, contract=current_contract
            )

        # Heartbeat
        if not hasattr(self, '_loop_count'): self._loop_count = 0
        self._loop_count += 1
        if self._loop_count % 60 == 0:
            print(f"[{self.name}] DTE:{dte:.1f} → {self._dte_mode} | Score:{raw_score:+d} | Trades:{self.trades_today}/{self.max_trades_per_day}")

        if self.active_trade:
            if self.active_trade.get('_closed', False):
                self.active_trade = None
            elif self.active_trade.get('direction') == 'SELL':
                self._manage_sell_trade(manager, metrics)
            else:
                self._manage_active_trade(manager, metrics)
        elif self.trades_today < self.max_trades_per_day and current_time > self.exit_cooldown_until:
            if self.consecutive_losses >= 9999: return
            if now.hour > LAST_ENTRY_HOUR or (now.hour == LAST_ENTRY_HOUR and now.minute >= LAST_ENTRY_MIN): return
            self._check_dte_entry(manager, metrics)

    def _check_dte_entry(self, manager, metrics):
        majority_action, confidence, sample_count, avg_score, _ = \
            self.logger.entry_aggregator.get_majority_signal()
        if majority_action == 'WAIT': return
        if sample_count < self.logger.entry_min_samples: return
        if confidence < self.min_confidence: return

        if self._dte_mode == 'SELL' and 'SELL' in majority_action:
            self._execute_sell_entry(manager, majority_action, avg_score, metrics)
        elif self._dte_mode == 'BUY' and 'BUY' in majority_action:
            quant = metrics.get('quant_signal', {})
            if not self._has_signal_confluence(quant): return
            self._execute_entry(manager, majority_action, avg_score, metrics)

    _execute_sell_entry = SellBaseStrategy._execute_sell_entry
    _manage_sell_trade = SellBaseStrategy._manage_sell_trade
    _close_sell_trade = SellBaseStrategy._close_sell_trade


# =============================================================================
# BACKWARD-COMPATIBLE ALIASES
# =============================================================================
V10SafeStrategy = V10TrendStrategy
V10AggressiveStrategy = V10MomentumStrategy
V10SidewaysStrategy = V10MeanReversionStrategy
V10QuantMaxStrategy = V10SwingStrategy
