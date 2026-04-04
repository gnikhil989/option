"""
Option Chain Manager Module v7 - ADAPTIVE MODE
Revolutionary auto-switching strategy based on market regime detection

MODES:
- ADAPTIVE: Auto-switches between strategies (RECOMMENDED)
- MOMENTUM_RIDER: Trending markets with negative GEX
- THETA_HUNTER: Range-bound markets with positive GEX
- CONTRARIAN_REVERSAL: Divergence/trap detection
- INSTITUTIONAL_SHADOW: Follow smart money
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
from utils.trade_logger import TradeLogger
from utils.trade_logger_v3 import TradeLoggerV3
import uuid
import re

logger = logging.getLogger(__name__)


class OptionChainCacheV7:
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


class OptionChainManagerV7Adaptive:
    """
    ADAPTIVE Manager - Auto-switches strategy based on market regime
    """

    def __init__(self, underlying, expiry, websocket_manager=None):
        """Initialize ADAPTIVE OptionChainManager"""
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
        self.cache = OptionChainCacheV7()
        self.monitoring_active = False
        self.initialized = False
        self.manager_id = f"{underlying}_{expiry}_V7_ADAPTIVE"
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
        self.action_history = deque(maxlen=10)
        self.trade_state = None
        self.days_to_expiry = 0
        self._backoff_until = 0
        self.manager_instance_id = id(self)

        # ADAPTIVE MODE
        self.strategy_mode = 'ADAPTIVE'  # Default to ADAPTIVE
        self.current_regime = 'DETECTING'  # Current detected regime
        self.regime_confidence = 0  # How confident are we in this regime

        # PAPER TRADING STATE
        self.logger = TradeLogger("logs/v7_paper_trades.csv")
        self.logger_v3 = TradeLoggerV3("logs/v7_paper_trades_v3.csv")  # NEW: Enhanced logger with signal aggregation
        self.paper_trade_state = None  # Current active trade
        self.trade_cooldown = 0
        self.paper_trading_enabled = True  # Can be toggled
        self.use_v3_logger = True  # Toggle to use new V3 logger with majority voting
        self.last_signal_action = 'WAIT'  # Track last signal for debugging

        # V3 LOGGER CONFIGURATION - Console Logging & Signal Windows
        self.logger_v3.verbose_logging = True   # Set False to disable console output
        self.logger_v3.log_interval = 5         # Log every N seconds (reduce for more frequent updates)
        self.logger_v3.min_entry_confidence = 60  # Min % confidence to enter trade
        self.logger_v3.min_exit_confidence = 50   # Min % confidence to exit trade

        # Signal Window Configuration (how long to track signals for majority voting)
        self.logger_v3.entry_window_seconds = 180  # 3 minutes - signals older than this are ignored
        self.logger_v3.exit_window_seconds = 120   # 2 minutes - for exit decisions
        self.logger_v3.entry_min_samples = 15      # Min signals needed before entry decision
        self.logger_v3.exit_min_samples = 10       # Min signals needed for exit decision

        # Update aggregators with new settings
        self.logger_v3.entry_aggregator.window_seconds = self.logger_v3.entry_window_seconds
        self.logger_v3.entry_aggregator.min_samples = self.logger_v3.entry_min_samples
        self.logger_v3.exit_aggregator.window_seconds = self.logger_v3.exit_window_seconds
        self.logger_v3.exit_aggregator.min_samples = self.logger_v3.exit_min_samples

        # TRADE FILTER CONFIGURATION (Anti-Whipsaw)
        self.min_hold_time = 30          # Minimum seconds to hold a trade (ignore neutral signals)
        self.signal_confirm_time = 10    # Seconds signal must persist before entry
        self.min_score_threshold = 15    # Minimum absolute score to enter trade
        self.trade_cooldown_duration = 30  # Seconds to wait after exit before new entry

        # SIGNAL CONFIRMATION TRACKING
        self.pending_signal = None       # {'action': 'BUY CALL', 'first_seen': timestamp, 'score': X}
        self.signal_confirm_start = 0    # When current pending signal was first seen
        self.exit_signal_start = None    # Timer for exit confirmation

        # Available modes
        self.available_modes = [
            'ADAPTIVE',
            'MOMENTUM_RIDER',
            'THETA_HUNTER',
            'CONTRARIAN_REVERSAL',
            'INSTITUTIONAL_SHADOW'
        ]

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

        logger.info(f"Initialized ADAPTIVE OptionChainManagerV7 for {underlying}")

    def set_strategy_mode(self, mode):
        """Update strategy mode dynamically"""
        if mode in self.available_modes:
            old_mode = self.strategy_mode
            self.strategy_mode = mode
            logger.info(f"Strategy Mode: {old_mode} → {mode} for {self.manager_id}")
            return True
        return False

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

    # ============================================================================
    # REFACTORED SIGNAL METHODS (Same as refactored version)
    # ============================================================================

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

        # Bull Trap
        if current_spot >= spot_high * 0.999:  # Was 0.998 (0.2%) -> Now 0.999 (0.1%)
            if current_ce < ce_high * 0.97:    # Was 0.98 -> Now 0.97 (Stronger divergence)
                return "BEARISH", f"🚨 BULL TRAP: Spot at {current_spot:.1f} (High) but Call Weak ({current_ce:.1f} vs Peak {ce_high:.1f})"

        # Bear Trap
        if current_spot <= spot_low * 1.001:   # Was 1.002 (0.2%) -> Now 1.001 (0.1%)
            if current_pe < pe_high * 0.97:    # Was 0.98 -> Now 0.97
                return "BULLISH", f"🚨 BEAR TRAP: Spot at {current_spot:.1f} (Low) but Put Weak ({current_pe:.1f} vs Peak {pe_high:.1f})"

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
                return 25, f"🚀 MOMENTUM BURST (+{velocity_pct:.2f}%)"
            return 10, f"Positive Momentum (+{velocity_pct:.2f}%)"
        elif velocity_pct < -threshold:
            if velocity_pct < -threshold * 3:
                return -25, f"🔻 MOMENTUM CRASH ({velocity_pct:.2f}%)"
            return -10, f"Negative Momentum ({velocity_pct:.2f}%)"

        return 0, f"Stable Velocity ({velocity_pct:+.2f}%)"

    def _get_expiry_multiplier(self):
        """Time-Decay Awareness"""
        if self.days_to_expiry <= 0:
            return 1.0, "Expiry Unknown"

        if self.days_to_expiry <= 1:
            return 0.4, "⚠️ EXPIRY DAY: High Gamma Risk"
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
            return 20, f"🟢 PCR Surge +{roc:.2f}% (Strong Support Building)"
        elif roc > 1.5:
            return 10, f"PCR Rising +{roc:.2f}% (Bullish Support)"
        elif roc < -3:
            return -20, f"🔴 PCR Crash {roc:.2f}% (Strong Resistance)"
        elif roc < -1.5:
            return -10, f"PCR Falling {roc:.2f}% (Bearish Pressure)"

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
            return False, f"⚠️ Low Volume ({total_vol:,})"
        elif total_vol < min_volume * 2:
            return True, f"Moderate Volume ({total_vol:,})"

        return True, f"High Volume ({total_vol:,})"

    def _score_gamma_exposure(self):
        """REFACTORED: Gamma Exposure with pinning detection"""
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

        if total_gex > 0:  # Positive GEX = Pinning
            distance_pct = abs(self.underlying_ltp - top_wall_strike) / top_wall_strike * 100

            if distance_pct < 0.5:
                score = 0
                reason = f"🧲 Gamma Pin at {top_wall_strike} (Mean Reversion)"
            else:
                if self.underlying_ltp > top_wall_strike:
                    score = -5
                    reason = f"Gamma Pull Down toward {top_wall_strike}"
                else:
                    score = 5
                    reason = f"Gamma Pull Up toward {top_wall_strike}"

        elif total_gex < 0:  # Negative GEX = Trending
            if len(self.price_history) > 5:
                is_uptrend = self.price_history[-1] > self.price_history[-5]
                score = 10 if is_uptrend else -10
                reason = f"⚡ Negative GEX: Trend Amplification ({'Up' if is_uptrend else 'Down'})"
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
        """REFACTORED: Institutional Flow with stricter thresholds"""
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
        """REFACTORED: Delta OI with dynamic thresholds"""
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
        """REFACTORED: IV Skew with context"""
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
                return -15, f"Put IV Spike - Justified Fear (Bearish: PE {pe_iv:.1f} vs CE {ce_iv:.1f})"
            else:
                return 10, f"Put IV Spike - Excess Fear (Contrarian Bullish: PE {pe_iv:.1f} vs CE {ce_iv:.1f})"

        elif diff < -2:
            return -10, f"Call IV Spike - FOMO/Greed (Bearish: CE {ce_iv:.1f} vs PE {pe_iv:.1f})"

        return 0, f"Balanced IV Skew (PE {pe_iv:.1f} vs CE {ce_iv:.1f})"

    def _score_oi_unwind(self):
        """OI Build-up vs Unwinding"""
        if len(self.price_history) < 2 or len(self.oi_history) < 2:
            return 0, "Building History"

        price_trend = self.price_history[-1] - self.price_history[0]
        oi_trend = self.oi_history[-1] - self.oi_history[0]

        price_move_sig = (self.price_history[-1] * 0.0005)
        oi_move_sig = 1000

        if price_trend > price_move_sig and oi_trend > oi_move_sig:
            return 20, "Long Build-up (Price ↑, OI ↑)"
        elif price_trend < -price_move_sig and oi_trend > oi_move_sig:
            return -20, "Short Build-up (Price ↓, OI ↑)"
        elif price_trend > price_move_sig and oi_trend < -oi_move_sig:
            return 5, "Short Covering (Price ↑, OI ↓)"
        elif price_trend < -price_move_sig and oi_trend < -oi_move_sig:
            return -5, "Long Unwinding (Price ↓, OI ↓)"

        return 0, "Mixed Position Flow"

    def _score_max_pain(self):
        """REFACTORED: Max Pain with correct magnetism"""
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
                logger.debug("Correlation detected: IV Skew + Vanna")

        if abs(score_delta) > 20 and abs(score_flow) > 15:
            if (score_delta > 0 and score_flow > 0) or (score_delta < 0 and score_flow < 0):
                correlation_penalty *= 0.90
                logger.debug("Correlation detected: Delta OI + Inst Flow")

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
    # ADAPTIVE REGIME DETECTION - THE CORE INNOVATION
    # ============================================================================

    def detect_market_regime(self, total_score, net_gex, volatility, scores_dict):
        """
        🎯 AUTO-DETECT MARKET REGIME AND SELECT OPTIMAL STRATEGY

        Returns: {
            'regime': 'THETA_HUNTER',
            'confidence': 85,
            'action': 'SELL STRADDLE',
            'reason': 'Gamma pinning + Range-bound',
            'thresholds': {...}
        }
        """

        # Extract scores for decision-making
        inst_score = scores_dict.get('inst_flow', 0)
        pcr_roc_score = scores_dict.get('pcr_roc', 0)

        # Check for trap/divergence
        trap_type, trap_msg = self._check_divergence()

        # ========== REGIME 1: THETA_HUNTER (Range-Bound + Pinning) ==========
        # Conditions: Positive GEX, low score, near max pain
        if net_gex > 0 and -30 <= total_score <= 30:
            return {
                'regime': 'THETA_HUNTER',
                'confidence': 85,
                'action': 'SELL STRADDLE',
                'reason': '🧲 Gamma Pinning Detected + Range-Bound Market',
                'description': 'Market is pinned around max pain. Selling premium for theta decay.',
                'thresholds': {
                    'entry': 'ATM strikes',
                    'target': '50-80% profit',
                    'sl': 'Premium spike >40%'
                },
                'expected_hold': 'Until expiry or 50% profit',
                'win_rate': '70-75%'
            }

        # ========== REGIME 2: MOMENTUM_RIDER (Trending + Negative GEX) ==========
        # Conditions: Negative GEX, strong score, trending
        elif net_gex < 0 and abs(total_score) > 60:
            direction = 'BULLISH' if total_score > 0 else 'BEARISH'
            action = 'BUY CALL' if total_score > 0 else 'BUY PUT'

            return {
                'regime': 'MOMENTUM_RIDER',
                'confidence': 80,
                'action': action,
                'reason': f'⚡ Negative GEX + Strong {direction} Trend',
                'description': 'Dealers amplifying moves. Ride the momentum.',
                'thresholds': {
                    'entry': 'ATM or ITM1',
                    'target': '+30%',
                    'sl': '-15%'
                },
                'expected_hold': '1-3 days',
                'win_rate': '60-65%'
            }

        # ========== REGIME 3: CONTRARIAN_REVERSAL (Trap Detected) ==========
        # Conditions: Divergence/trap signal
        elif trap_type:
            action = 'BUY PUT' if trap_type == 'BEARISH' else 'BUY CALL'

            return {
                'regime': 'CONTRARIAN_REVERSAL',
                'confidence': 75,
                'action': action,
                'reason': f'🚨 {trap_msg}',
                'description': 'Divergence detected. Fading the fake move.',
                'thresholds': {
                    'entry': 'ATM strikes',
                    'target': '+20%',
                    'sl': '-10%'
                },
                'expected_hold': 'Intraday to 1 day',
                'win_rate': '55-60%'
            }

        # ========== REGIME 4: INSTITUTIONAL_SHADOW (Follow Smart Money) ==========
        # Conditions: Strong institutional flow detected
        elif abs(inst_score) > 15:
            action = 'BUY CALL' if inst_score > 0 else 'BUY PUT'

            return {
                'regime': 'INSTITUTIONAL_SHADOW',
                'confidence': 70,
                'action': action,
                'reason': f'🐋 Following Institutional Flow (Score: {inst_score:+d})',
                'description': 'Large fresh positions detected. Following smart money.',
                'thresholds': {
                    'entry': 'Same strikes as institutions',
                    'target': '+25%',
                    'sl': '-12%'
                },
                'expected_hold': '1-5 days',
                'win_rate': '65-70%'
            }

        # ========== REGIME 5: RISK_OFF (High Volatility / Uncertain) ==========
        # Conditions: High volatility, mixed signals
        elif volatility > 0.3 or abs(total_score) < 20:
            return {
                'regime': 'RISK_OFF',
                'confidence': 50,
                'action': 'WAIT',
                'reason': '⚠️ High Volatility or No Clear Setup',
                'description': 'Market conditions unclear. Waiting for better opportunity.',
                'thresholds': {},
                'expected_hold': 'Cash',
                'win_rate': 'N/A'
            }

        # ========== REGIME 6: CONSERVATIVE (Moderate Signal) ==========
        # Conditions: Moderate score, no special conditions
        elif abs(total_score) > 40:
            direction = 'BULLISH' if total_score > 0 else 'BEARISH'
            action = 'BUY CALL' if total_score > 0 else 'BUY PUT'

            return {
                'regime': 'CONSERVATIVE',
                'confidence': 60,
                'action': action,
                'reason': f'Moderate {direction} Signal (Score: {total_score:+d})',
                'description': 'Moderate conviction trade.',
                'thresholds': {
                    'entry': 'ATM strikes',
                    'target': '+20%',
                    'sl': '-12%'
                },
                'expected_hold': '1-2 days',
                'win_rate': '55-60%'
            }

        # ========== DEFAULT: NEUTRAL ==========
        else:
            return {
                'regime': 'NEUTRAL',
                'confidence': 40,
                'action': 'WAIT',
                'reason': 'No Clear Opportunity',
                'description': 'Waiting for clearer market direction.',
                'thresholds': {},
                'expected_hold': 'Cash',
                'win_rate': 'N/A'
            }

    # ============================================================================
    # PAPER TRADING ENGINE
    # ============================================================================

    def _manage_paper_trade(self, strategy_matrix, total_score, regime_data, pcr_bias, metrics):
        """
        Manages the lifecycle of a paper trade:
        1. ENTRY: If no active trade and signal is strong.
        2. MONITOR: Check SL/Target.
        3. EXIT: If SL/Target hit or Signal Reverses.
        """
        if not self.paper_trading_enabled:
            return

        current_time = datetime.now()

        # Capture current action for Reversal Check
        current_action = regime_data.get('action', 'WAIT')

        # Debug logging for signal tracking
        if current_action != self.last_signal_action:
            logger.info(f"[PAPER TRADE] Signal changed: {self.last_signal_action} → {current_action} | Score: {total_score} | Regime: {regime_data.get('regime', 'UNKNOWN')}")
            self.last_signal_action = current_action

        # 1. MONITOR ACTIVE TRADE
        if self.paper_trade_state:
            trade = self.paper_trade_state

            # Find current LTP (handle multi-leg strategies)
            current_ltp = 0
            if trade['Strike'] in self.option_data:
                trade_type = trade.get('Type', 'CE')
                if trade_type in ['STRADDLE', 'STRANGLE']:
                    # For straddle/strangle, combine CE + PE LTPs
                    ce_ltp = self.option_data[trade['Strike']]['ce_data'].get('ltp', 0)
                    pe_ltp = self.option_data[trade['Strike']]['pe_data'].get('ltp', 0)
                    current_ltp = ce_ltp + pe_ltp
                else:
                    tag = 'ce_data' if trade_type == 'CE' else 'pe_data'
                    current_ltp = self.option_data[trade['Strike']][tag].get('ltp', 0)

            if current_ltp <= 0: return  # No data yet
            
            # Check Exit Conditions
            exit_reason = None
            pnl_points = 0
            
            # A. HARD STOPS
            direction = trade.get('Direction', 'BUY')
            
            if direction == 'SELL':
                # SELL: Loss if Price goes UP (>= SL), Profit if Price goes DOWN (<= Target)
                if current_ltp >= trade['StopLoss']:
                    exit_reason = "SL HIT"
                    pnl_points = trade['Price'] - trade['StopLoss'] # PnL = Entry - Exit
                    exit_price = trade['StopLoss']
                elif current_ltp <= trade['Target']:
                    exit_reason = "TARGET HIT"
                    pnl_points = trade['Price'] - trade['Target']   # PnL = Entry - Exit
                    exit_price = trade['Target']
            else:
                # BUY: Loss if Price goes DOWN (<= SL), Profit if Price goes UP (>= Target)
                if current_ltp <= trade['StopLoss']:
                    exit_reason = "SL HIT"
                    pnl_points = trade['StopLoss'] - trade['Price'] # PnL = Exit - Entry
                    exit_price = trade['StopLoss']
                elif current_ltp >= trade['Target']:
                    exit_reason = "TARGET HIT"
                    pnl_points = trade['Target'] - trade['Price']   # PnL = Exit - Entry
                    exit_price = trade['Target']
                
            # B. SIGNAL VALIDITY CHECK (With Hysteresis)
            # Handle both LONG (BUY) and SHORT (SELL) positions
            # If Action becomes 'WAIT' or flips -> EXIT (but respect min hold time for neutral)

            is_signal_valid = False
            # HYSTERESIS: If score is strong enough (>= 10), ignore 'WAIT' signals and hold
            # This prevents exiting just because score dropped from 15 to 14
            hysteresis_threshold = 10

            # Get trade direction and type
            trade_type = trade.get('Type', 'CE')
            trade_dir = trade.get('Direction', 'BUY')

            if trade_dir == 'SELL':
                # SHORT positions (SELL STRADDLE, SELL STRANGLE, etc.)
                if trade_type in ['STRADDLE', 'STRANGLE']:
                    # Short straddle/strangle valid when SELL STRADDLE action continues
                    # OR market remains range-bound (low score = gamma pinning regime)
                    if f'SELL {trade_type}' in current_action:
                        is_signal_valid = True
                    elif regime_data.get('regime') != 'CONTRARIAN_REVERSAL' and -30 <= total_score <= 30:
                        # Range-bound market - valid for theta decay strategies
                        is_signal_valid = True
                elif trade_type == 'CE':
                    # Short CE valid when SELL CALL continues or bearish signal
                    if 'SELL' in current_action and ('CALL' in current_action or 'STRADDLE' in current_action):
                        is_signal_valid = True
                    # Only use score if NOT in Contrarian mode
                    elif regime_data.get('regime') != 'CONTRARIAN_REVERSAL' and abs(total_score) >= hysteresis_threshold and total_score < 0:
                        is_signal_valid = True
                elif trade_type == 'PE':
                    # Short PE valid when SELL PUT continues or bullish signal
                    if 'SELL' in current_action and ('PUT' in current_action or 'STRADDLE' in current_action):
                        is_signal_valid = True
                    elif regime_data.get('regime') != 'CONTRARIAN_REVERSAL' and abs(total_score) >= hysteresis_threshold and total_score > 0:
                        is_signal_valid = True
            else:
                # LONG positions (BUY CALL, BUY PUT)
                if trade_type == 'CE':
                    if 'BUY CALL' in current_action:
                        is_signal_valid = True
                    elif regime_data.get('regime') != 'CONTRARIAN_REVERSAL' and abs(total_score) >= hysteresis_threshold and total_score > 0:
                        is_signal_valid = True  # Sticky hold

                elif trade_type == 'PE':
                    if 'BUY PUT' in current_action:
                        is_signal_valid = True
                    elif regime_data.get('regime') != 'CONTRARIAN_REVERSAL' and abs(total_score) >= hysteresis_threshold and total_score < 0:
                        is_signal_valid = True  # Sticky hold

            # Calculate how long we've held this trade
            try:
                entry_time_dt = datetime.fromisoformat(trade['EntryTime'])
                hold_duration = (current_time - entry_time_dt).total_seconds()
            except:
                hold_duration = 0

            if not is_signal_valid and not exit_reason:
                if 'WAIT' in current_action:
                    # NEUTRAL SIGNAL - IGNORE AND HOLD
                    # User requested to fix false exits on neutral signals.
                    # We simply reset the flip timer and existing hold logic applies (relying on SL/Target).
                    if self.exit_signal_start is not None:
                        self.exit_signal_start = None
                        logger.info(f"[PAPER TRADE] Signal Neutral (WAIT) - Resetting exit timer and HOLDING")
                    
                    # Log occasionally tailored to debug levels to avoid spam
                    # logger.debug(f"[PAPER TRADE] Holding through neutral signal...")
                else:
                    # SIGNAL FLIP (opposite direction) - Wait for 10s confirmation
                    exit_ts = time.time()
                    if self.exit_signal_start is None:
                        self.exit_signal_start = exit_ts
                        logger.info(f"[PAPER TRADE] Potential Exit Detected ({current_action}). Waiting 10s confirmation...")
                    
                    elif exit_ts - self.exit_signal_start >= 10:
                        # Confirmed Flip
                        exit_reason = f"SIGNAL FLIP ({current_action})"
                        exit_price = current_ltp
                        # PnL depends on direction: SELL profits when price falls, BUY profits when price rises
                        if trade_dir == 'SELL':
                            pnl_points = trade['Price'] - exit_price
                        else:
                            pnl_points = exit_price - trade['Price']
                    else:
                        # Waiting
                        logger.debug(f"[PAPER TRADE] Holding through signal flip: {exit_ts - self.exit_signal_start:.1f}s/10s")
            
            else:
                # Signal is valid (or other exit reason like SL/Target already set)
                # Reset exit timer
                if self.exit_signal_start is not None:
                     logger.info(f"[PAPER TRADE] Exit Signal Cancelled - Signal Valid Again")
                     self.exit_signal_start = None

            # EXECUTE EXIT
            if exit_reason:
                lot_size = trade.get('Lot_Size', 50 if self.underlying == 'NIFTY' else 15 if self.underlying == 'BANKNIFTY' else 250)
                pnl_amount = pnl_points * lot_size * trade['Quantity']

                # Parse EntryTime string to calculate duration
                entry_time_str = trade['EntryTime']
                try:
                    entry_time_dt = datetime.fromisoformat(entry_time_str)
                    duration = (current_time - entry_time_dt).total_seconds()
                except:
                    duration = 0

                # Calculate ROI percentage
                roi_pct = (pnl_points / trade['Price'] * 100) if trade['Price'] > 0 else 0

                # Calculate max profit/loss seen during trade (direction-aware)
                entry_dir = trade.get('Direction', 'BUY')
                if entry_dir == 'SELL':
                    # SHORT: profit when price drops, loss when price rises
                    max_profit_seen = trade['Price'] - trade.get('Min_Price_Seen', trade['Price'])
                    max_loss_seen = trade.get('Max_Price_Seen', trade['Price']) - trade['Price']
                else:
                    # LONG: profit when price rises, loss when price drops
                    max_profit_seen = trade.get('Max_Price_Seen', trade['Price']) - trade['Price']
                    max_loss_seen = trade['Price'] - trade.get('Min_Price_Seen', trade['Price'])

                # Exit direction is opposite of entry direction
                exit_direction = 'BUY' if entry_dir == 'SELL' else 'SELL'

                log_data = {
                    'Trade_ID': trade['Trade_ID'],
                    'Status': 'EXIT',
                    'Timestamp': current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    'Symbol': self.underlying,
                    'Contract': trade['Contract'],
                    'Strike': trade['Strike'],
                    'Type': trade['Type'],
                    'Direction': exit_direction,
                    'Strategy': trade['Strategy'],
                    'Regime': regime_data.get('regime', 'UNKNOWN'),
                    'Entry_Price': trade['Price'],
                    'Exit_Price': exit_price,
                    'Quantity': trade['Quantity'],
                    'Lot_Size': lot_size,
                    'Target': trade['Target'],
                    'StopLoss': trade['StopLoss'],
                    'Underlying_LTP': self.underlying_ltp,
                    'PnL_Points': round(pnl_points, 2),
                    'PnL_Amount': round(pnl_amount, 2),
                    'ROI_Pct': round(roi_pct, 2),
                    'Score': total_score,
                    'Confidence': sorted(regime_data.get('confidence', 0)) if isinstance(regime_data.get('confidence'), list) else regime_data.get('confidence', 0),
                    'Reason': exit_reason,
                    'Snapshot_PCR': metrics.get('pcr', 0),
                    'Snapshot_IV': metrics.get('iv', 0),
                    'Entry_Delta': trade.get('Entry_Delta', 0),
                    'Entry_Gamma': trade.get('Entry_Gamma', 0),
                    'Entry_Theta': trade.get('Entry_Theta', 0),
                    'Time_Held_Sec': int(duration),
                    'Max_Profit_Seen': round(max_profit_seen, 2),
                    'Max_Loss_Seen': round(max_loss_seen, 2)
                }

                self.logger.log_trade(log_data)

                # Reset State
                self.paper_trade_state = None

                # Handover Logic:
                # Signal Flip: Longer cooldown (30s) to let market settle
                # Signal Lost: Full cooldown to avoid re-entry on noise
                if "SIGNAL FLIP" in exit_reason:
                    self.trade_cooldown = time.time() + 30  # Increased from 10s -> 30s
                    self.pending_signal = None  # Reset - new signal needs confirmation
                    logger.info(f"PAPER TRADE FLIP: Exited {trade['Contract']}, 30s cooldown before new entry")
                else:
                    self.trade_cooldown = time.time() + self.trade_cooldown_duration
                    self.pending_signal = None  # Reset pending signal
                    logger.info(f"PAPER TRADE EXIT: {exit_reason} | PnL: {pnl_amount} | ROI: {roi_pct:.2f}% | Cooldown: {self.trade_cooldown_duration}s")

            else:
                # UPDATE MAX/MIN PRICE TRACKING
                if current_ltp > trade.get('Max_Price_Seen', 0):
                    trade['Max_Price_Seen'] = current_ltp
                if current_ltp < trade.get('Min_Price_Seen', float('inf')):
                    trade['Min_Price_Seen'] = current_ltp

                # TRAILING STOP LOGIC - Dynamic Break-Even
                # If we are up 10%, move SL to Entry Price (Break Even)
                direction = trade.get('Direction', 'BUY')
                
                if direction == 'SELL':
                     # SELL: Profit if price drops. 10% gain means price is 90% of entry
                     if current_ltp <= trade['Price'] * 0.90:
                         if trade['StopLoss'] > trade['Price']: # SL is above price for SELL
                             trade['StopLoss'] = trade['Price']
                             logger.info(f"[PAPER TRADE] 🛡️ Secured Break-Even: SL moved to {trade['Price']} (+10% ROI)")
                else:
                    # BUY: Profit if price rises. 10% gain means price is 110% of entry
                    if current_ltp >= trade['Price'] * 1.10:
                        if trade['StopLoss'] < trade['Price']:
                             trade['StopLoss'] = trade['Price']
                             logger.info(f"[PAPER TRADE] 🛡️ Secured Break-Even: SL moved to {trade['Price']} (+10% ROI)")

                        
        # 2. CHECK ENTRY CONDITIONS
        # No 'elif' here - allows Exit -> Entry in same tick if cooldown is 0
        if not self.paper_trade_state and time.time() > self.trade_cooldown:
            # Only enter if Regime has actionable Confidence and Action
            action = current_action
            if 'BUY' in action or 'SELL' in action:
                # A. CHECK ENTRY CONDITIONS (With Regulator for Specific Regimes)
                # Default: Require Minimum Score
                entry_allowed = False
                
                # Exception 1: Contrarian Reversal (Traps)
                # These often have low trend scores (counter-trend), so we trust the signals
                # BUT we now enforce a minimum score of 10 to avoid entering on total noise
                if regime_data.get('regime') == 'CONTRARIAN_REVERSAL':
                     if abs(total_score) >= 10:
                        entry_allowed = True
                     else:
                        logger.debug(f"[PAPER TRADE] Contrarian Entry Refused: Score {total_score} too weak (<10)")
                     
                # Exception 2: Institutional Shadow
                # Follow the big money regardless of technical indicators
                elif regime_data.get('regime') == 'INSTITUTIONAL_SHADOW':
                     entry_allowed = True

                # Exception 3: Theta Hunter (Range Bound)
                # These trades SPECIFICALLY target low scores (neutral markets)
                # So we must NOT enforce the >=15 score threshold
                elif regime_data.get('regime') == 'THETA_HUNTER':
                     entry_allowed = True
                     
                # Standard: Momentum/Trend trades need score confirmation
                elif abs(total_score) >= self.min_score_threshold:
                    entry_allowed = True
                
                if not entry_allowed:
                    logger.debug(f"[PAPER TRADE] Entry blocked: Score {total_score} below threshold ±{self.min_score_threshold}")
                    # Reset pending signal if score drops
                    self.pending_signal = None
                    return

                # B. SIGNAL CONFIRMATION CHECK
                # Signal must persist for signal_confirm_time seconds before entry
                current_timestamp = time.time()

                if self.pending_signal is None or self.pending_signal.get('action') != action:
                    # New signal or signal changed - start confirmation timer
                    self.pending_signal = {
                        'action': action,
                        'first_seen': current_timestamp,
                        'score': total_score
                    }
                    logger.info(f"[PAPER TRADE] Signal confirmation started: {action} | Score: {total_score} | Regime: {regime_data.get('regime', 'N/A')} | Need {self.signal_confirm_time}s")
                    return

                # Check if signal has been confirmed (persisted long enough)
                signal_duration = current_timestamp - self.pending_signal['first_seen']
                if signal_duration < self.signal_confirm_time:
                    logger.debug(f"[PAPER TRADE] Waiting for confirmation: {signal_duration:.1f}s / {self.signal_confirm_time}s")
                    return

                # Signal confirmed! Proceed with entry
                logger.info(f"[PAPER TRADE] Signal CONFIRMED after {signal_duration:.1f}s: {action} | Score: {total_score}")
                self.pending_signal = None  # Reset for next trade

                # Determine bias and contract_type based on action
                # Handle multi-leg strategies (STRADDLE, STRANGLE) separately
                if 'STRADDLE' in action:
                    bias = 'NEUTRAL'
                    contract_type = 'STRADDLE'
                elif 'STRANGLE' in action:
                    bias = 'NEUTRAL'
                    contract_type = 'STRANGLE'
                elif 'CALL' in action:
                    bias = 'BULLISH'
                    contract_type = 'CE'
                elif 'PUT' in action:
                    bias = 'BEARISH'
                    contract_type = 'PE'
                else:
                    # Default fallback
                    bias = 'NEUTRAL'
                    contract_type = 'CE'

                # Determine direction (BUY = long, SELL = short)
                trade_direction = 'SELL' if 'SELL' in action else 'BUY'

                # Select Strike
                best_contract = self._select_best_strike(bias)
                if not best_contract:
                    logger.warning(f"[PAPER TRADE] Entry blocked: No best_contract found for {bias} bias")
                    return
                if best_contract['ltp'] <= 0:
                    logger.warning(f"[PAPER TRADE] Entry blocked: LTP is 0 for {best_contract.get('name', 'Unknown')}")
                    return

                if best_contract and best_contract['ltp'] > 0:

                    # Calculate Levels
                    levels = self._calculate_trade_levels(best_contract['ltp'], action, regime_data.get('thresholds'))

                    trade_id = f"TRD_{uuid.uuid4().hex[:8]}"

                    # Determine lot size based on underlying
                    lot_size = 50 if self.underlying == 'NIFTY' else 15 if self.underlying == 'BANKNIFTY' else 250

                    # Get Greeks at entry
                    strike = best_contract['strike']
                    entry_delta = 0
                    entry_gamma = 0
                    entry_theta = 0

                    # Handle Greeks for multi-leg strategies
                    if contract_type in ['STRADDLE', 'STRANGLE']:
                        if strike in self.option_data:
                            ce_delta = self.option_data[strike]['ce_data'].get('delta', 0)
                            pe_delta = self.option_data[strike]['pe_data'].get('delta', 0)
                            entry_delta = ce_delta + pe_delta  # Combined delta
                            entry_gamma = (self.option_data[strike]['ce_data'].get('gamma', 0) +
                                          self.option_data[strike]['pe_data'].get('gamma', 0))
                            entry_theta = (self.option_data[strike]['ce_data'].get('theta', 0) +
                                          self.option_data[strike]['pe_data'].get('theta', 0))
                    else:
                        tag = 'ce_data' if contract_type == 'CE' else 'pe_data'
                        if strike in self.option_data:
                            entry_delta = self.option_data[strike][tag].get('delta', 0)
                            entry_gamma = self.option_data[strike][tag].get('gamma', 0)
                            entry_theta = self.option_data[strike][tag].get('theta', 0)

                    self.paper_trade_state = {
                        'Trade_ID': trade_id,
                        'EntryTime': current_time.isoformat(),
                        'Symbol': self.underlying,
                        'Contract': best_contract['name'],
                        'Strike': best_contract['strike'],
                        'Type': contract_type,
                        'Direction': trade_direction,
                        'Strategy': self.strategy_mode,
                        'Price': best_contract['ltp'],
                        'Quantity': 1,
                        'Lot_Size': lot_size,
                        'Target': levels['target'],
                        'StopLoss': levels['sl'],
                        'Underlying_LTP': self.underlying_ltp,
                        'Entry_Delta': entry_delta,
                        'Entry_Gamma': entry_gamma,
                        'Entry_Theta': entry_theta,
                        'Max_Price_Seen': best_contract['ltp'],
                        'Min_Price_Seen': best_contract['ltp']
                    }

                    log_data = {
                        'Trade_ID': trade_id,
                        'Status': 'ENTRY',
                        'Timestamp': current_time.strftime("%Y-%m-%d %H:%M:%S"),
                        'Symbol': self.underlying,
                        'Contract': best_contract['name'],
                        'Strike': best_contract['strike'],
                        'Type': contract_type,
                        'Direction': trade_direction,
                        'Strategy': self.strategy_mode,
                        'Regime': regime_data.get('regime', 'UNKNOWN'),
                        'Entry_Price': best_contract['ltp'],
                        'Exit_Price': '',
                        'Quantity': 1,
                        'Lot_Size': lot_size,
                        'Target': levels['target'],
                        'StopLoss': levels['sl'],
                        'Underlying_LTP': self.underlying_ltp,
                        'PnL_Points': '',
                        'PnL_Amount': '',
                        'ROI_Pct': '',
                        'Score': total_score,
                        'Confidence': regime_data.get('confidence', 0),
                        'Reason': regime_data.get('reason', 'Signal Generated'),
                        'Snapshot_PCR': metrics.get('pcr', 0),
                        'Snapshot_IV': metrics.get('iv', 0),
                        'Entry_Delta': round(entry_delta, 4),
                        'Entry_Gamma': round(entry_gamma, 6),
                        'Entry_Theta': round(entry_theta, 4),
                        'Time_Held_Sec': '',
                        'Max_Profit_Seen': '',
                        'Max_Loss_Seen': ''
                    }

                    self.logger.log_trade(log_data)
                    logger.info(f"PAPER TRADE ENTRY: {action} on {best_contract['name']} @ {best_contract['ltp']} | Lot: {lot_size}")
            else:
                # Signal is WAIT - no entry, reset pending signal
                if self.pending_signal:
                    logger.debug(f"[PAPER TRADE] Signal lost during confirmation (WAIT), resetting pending signal")
                    self.pending_signal = None
        else:
            # Either trade is active or cooldown is active
            if self.paper_trade_state:
                logger.debug(f"[PAPER TRADE] Trade active: {self.paper_trade_state['Contract']}")
            elif time.time() <= self.trade_cooldown:
                cooldown_remaining = self.trade_cooldown - time.time()
                logger.debug(f"[PAPER TRADE] Cooldown active: {cooldown_remaining:.1f}s remaining")

    def get_paper_trade_summary(self):
        """
        Returns a summary of paper trading performance
        """
        # Use V3 logger if enabled
        if self.use_v3_logger:
            summary = self.logger_v3.get_performance_summary()
            # Add signal stats for debugging
            summary['signal_stats'] = self.logger_v3.get_signal_stats()
            return summary

        # Original logic for backward compatibility
        trades = self.logger.get_recent_trades(limit=1000)  # Get all trades
        if not trades:
            return {
                'total_trades': 0,
                'message': 'No trades logged yet. System is monitoring signals.'
            }

        completed_trades = [t for t in trades if t.get('Status') == 'EXIT']
        active_trade = self.paper_trade_state

        if not completed_trades:
            status_msg = "System is running. Waiting for first signal to complete." if not active_trade else f"First trade active: {active_trade['Contract']}"
            return {
                'total_trades': 0,
                'active_trade': active_trade,
                'message': status_msg
            }

        # Calculate statistics
        total_pnl = sum(float(t.get('PnL_Amount', 0)) for t in completed_trades)
        winning_trades = [t for t in completed_trades if float(t.get('PnL_Amount', 0)) > 0]
        losing_trades = [t for t in completed_trades if float(t.get('PnL_Amount', 0)) < 0]

        win_rate = (len(winning_trades) / len(completed_trades) * 100) if completed_trades else 0

        avg_win = sum(float(t.get('PnL_Amount', 0)) for t in winning_trades) / len(winning_trades) if winning_trades else 0
        avg_loss = sum(float(t.get('PnL_Amount', 0)) for t in losing_trades) / len(losing_trades) if losing_trades else 0

        # Calculate average holding time
        avg_hold_time = sum(int(t.get('Time_Held_Sec', 0)) for t in completed_trades) / len(completed_trades) if completed_trades else 0

        return {
            'total_trades': len(completed_trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': round(win_rate, 2),
            'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'avg_hold_time_min': round(avg_hold_time / 60, 2),
            'active_trade': active_trade,
            'recent_trades': completed_trades[-5:],  # Last 5 trades
        }

    def generate_signals(self, pcr, max_pain):
        """
        🎯 ADAPTIVE SIGNAL GENERATION
        Auto-switches strategy based on market regime
        """
        # 0. Capture state
        self._capture_initial_state()

        # Update History
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

        # 1. Calculate ALL scores
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

        # Store scores
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

        # Apply weighting
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

        # Calculate OI changes
        if self.initial_state:
            for strike, data in self.option_data.items():
                if strike in self.initial_state:
                    data['ce_data']['oi_change'] = data['ce_data'].get('oi', 0) - self.initial_state[strike]['ce_oi']
                    data['pe_data']['oi_change'] = data['pe_data'].get('oi', 0) - self.initial_state[strike]['pe_oi']

        self._scan_iv_surface()
        self._calculate_heatmap_intensity()

        volatility = self._get_recent_volatility()

        # ========== ADAPTIVE REGIME DETECTION ==========
        if self.strategy_mode == 'ADAPTIVE':
            regime_info = self.detect_market_regime(total_score, net_gex, volatility, scores_dict)
            action = regime_info['action']
            self.current_regime = regime_info['regime']
            self.regime_confidence = regime_info['confidence']
        else:
            # Manual mode selected
            regime_info = {
                'regime': self.strategy_mode,
                'confidence': 0,
                'action': 'WAIT',
                'reason': f'Manual mode: {self.strategy_mode}',
                'description': 'Manual strategy selection active',
                'thresholds': {},
                'expected_hold': 'Variable',
                'win_rate': 'N/A'
            }
            # Use simple threshold logic
            if total_score > 65:
                action = 'BUY CALL'
            elif total_score < -65:
                action = 'BUY PUT'
            else:
                action = 'WAIT'

        # Build reasons list
        reasons = [
            f"📊 Regime: {regime_info['regime']} (Confidence: {regime_info['confidence']}%)",
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
            reasons.insert(0, f"⏰ {expiry_reason} (Score x{expiry_mult:.1f})")

        if not is_liquid:
            reasons.insert(0, f"💧 {liquidity_reason}")

        if correlation_penalty < 1.0:
            reasons.insert(0, f"🔗 Signal Correlation (Penalty: {correlation_penalty:.0%})")

        # Trade Setup
        trade_setup = {}
        warning = None

        if action != 'WAIT':
            best_contract = self._select_best_strike('BULLISH' if 'CALL' in action else 'BEARISH' if 'PUT' in action else 'NEUTRAL', action)
            if best_contract:
                levels = self._calculate_trade_levels(best_contract['ltp'], action, regime_info.get('thresholds'))
                trade_setup = {
                    'contract': best_contract['name'],
                    'strike': best_contract.get('strike', self.atm_strike),  # Added for V3 logger
                    'entry': best_contract['ltp'],
                    'sl': levels['sl'],
                    'target': levels['target'],
                    'rr': '1:2',
                    'legs': best_contract.get('contracts', []),
                    'regime_thresholds': regime_info.get('thresholds', {}),
                    'underlying_ltp': self.underlying_ltp,  # Added for V3 logger
                    'type': best_contract.get('type', 'CE')  # Added for V3 logger
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
            'pcr_momentum': {
                'score': score_pcr_roc,
                'reason': s_pcr_reason
            },
            'reasons': reasons,
            'trade_setup': trade_setup,
            'warning': warning,
            'mode': self.strategy_mode,
            'regime': regime_info['regime'],
            'regime_info': regime_info,
            'flow_alerts': s_flow_reason.split(", ") if "Institutional" in s_flow_reason else []
        }

    # ============================================================================
    # HELPER METHODS
    # ============================================================================

    def _select_best_strike(self, bias, action="BUY"):
        """Select Best Strike"""
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
                'type': 'STRADDLE',  # Added for V3 logger
                'contracts': [
                    {'name': self.option_data[strike].get('ce_symbol'), 'ltp': ce_data.get('ltp', 0), 'type': 'CE'},
                    {'name': self.option_data[strike].get('pe_symbol'), 'ltp': pe_data.get('ltp', 0), 'type': 'PE'}
                ]
            }

        if action == "SELL STRANGLE":
            # STRANGLE: OTM CE (ATM + step) + OTM PE (ATM - step)
            ce_strike = self.atm_strike + self.strike_step
            pe_strike = self.atm_strike - self.strike_step

            if ce_strike not in self.option_data or pe_strike not in self.option_data:
                return None

            ce_data = self.option_data[ce_strike].get('ce_data', {})
            pe_data = self.option_data[pe_strike].get('pe_data', {})
            combined_ltp = ce_data.get('ltp', 0) + pe_data.get('ltp', 0)

            return {
                'is_multi': True,
                'strike': self.atm_strike,  # Reference strike (ATM)
                'ce_strike': ce_strike,
                'pe_strike': pe_strike,
                'name': f"STRANGLE {pe_strike}/{ce_strike}",
                'ltp': combined_ltp,
                'type': 'STRANGLE',  # Added for V3 logger
                'contracts': [
                    {'name': self.option_data[ce_strike].get('ce_symbol'), 'ltp': ce_data.get('ltp', 0), 'type': 'CE', 'strike': ce_strike},
                    {'name': self.option_data[pe_strike].get('pe_symbol'), 'ltp': pe_data.get('ltp', 0), 'type': 'PE', 'strike': pe_strike}
                ]
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

    def _calculate_trade_levels(self, entry_price, action_type="BUY", regime_thresholds=None):
        """
        Calculate SL and Target based on Regime Thresholds
        Parses strings like '+30%', '50-80% profit', 'Premium spike >40%'
        """
        if entry_price <= 0:
            return {'sl': 0, 'target': 0}

        # Defaults
        sl_pct = 0.15      # 15% SL
        target_pct = 0.30  # 30% Target
        
        # Override defaults if specific thresholds provided
        if "SELL" in action_type:
            # Default for SELL strategies (often straddles) is wider
            sl_pct = 0.40      # 40% SL
            target_pct = 0.50  # 50% Target
        
        if regime_thresholds:
            # Parse Target Percentage
            t_str = str(regime_thresholds.get('target', ''))
            # Find first number in string (e.g. "30" in "+30%", "50" in "50-80%")
            t_match = re.search(r'(\d+)', t_str)
            if t_match:
                target_pct = float(t_match.group(1)) / 100.0

            # Parse StopLoss Percentage
            s_str = str(regime_thresholds.get('sl', ''))
            s_match = re.search(r'(\d+)', s_str)
            if s_match:
                sl_pct = float(s_match.group(1)) / 100.0

        if "SELL" in action_type:
            # SELL: Profit when price drops, Loss when price rises
            # Target 50% profit means exit at 50% of entry price
            # SL 40% means exit at 140% of entry price
            sl = round(entry_price * (1 + sl_pct), 1)
            target = round(entry_price * (1 - target_pct), 1)
            target = max(0.1, target) # Prevent negative target
        else:
            # BUY: Profit when price rises, Loss when price drops
            sl = round(entry_price * (1 - sl_pct), 1)
            target = round(entry_price * (1 + target_pct), 1)

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

        # Calculate ATM IV for metrics
        atm_iv = 0
        if self.atm_strike and self.atm_strike in self.option_data:
            atm_data = self.option_data[self.atm_strike]
            atm_iv = (atm_data['ce_data'].get('iv', 0) + atm_data['pe_data'].get('iv', 0)) / 2

        # PREPARE METRICS FOR TRADE ENGINE
        trade_metrics = {
            'pcr': round(pcr, 8),
            'iv': atm_iv
        }

        # EXECUTE PAPER TRADING ENGINE
        if self.use_v3_logger:
            # NEW V3 Logger with Signal Aggregation (Majority Voting)
            # This handles whipsaw by aggregating signals over time window
            self.logger_v3.process_signal(
                signal_data=signals,
                option_data=self.option_data,
                underlying_ltp=self.underlying_ltp,
                underlying_symbol=self.underlying,
                metrics=trade_metrics
            )
            # Update paper_trade_state from V3 logger for UI display
            self.paper_trade_state = self.logger_v3.get_active_trade()
        else:
            # Original logger (kept for backward compatibility)
            self._manage_paper_trade(
                strategy_matrix=None,
                total_score=signals['score'],
                regime_data=signals['regime_info'],
                pcr_bias=signals['pcr_signal'],
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
    # INFRASTRUCTURE METHODS (Copied from V7)
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
            # If we already have underlying_ltp from WebSocket, use it
            if self.underlying_ltp and self.underlying_ltp > 0:
                # Calculate ATM strike from existing LTP
                self.atm_strike = round(self.underlying_ltp / self.strike_step) * self.strike_step
                return self.atm_strike
            
            # Otherwise fetch underlying quote from API
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
                
                # Calculate ATM strike
                if self.underlying_ltp > 0:
                    self.atm_strike = round(self.underlying_ltp / self.strike_step) * self.strike_step
                    return self.atm_strike
                else:
                    logger.warning(f"Invalid LTP received for {self.underlying}: {self.underlying_ltp}")
                    return 0
            else:
                logger.warning(f"Failed to fetch quote for {self.underlying}: {response.get('message', 'Unknown error')}")
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
        # Generate ITM strikes (20 strikes below ATM for CE, above for PE)
        for i in range(6, 0, -1):
            strike = self.atm_strike - (i * self.strike_step)
            strikes.append({
                'strike': strike,
                'tag': f'ITM{i}',
                'position': -i
            })
        
        # Add ATM strike
        strikes.append({
            'strike': self.atm_strike,
            'tag': 'ATM',
            'position': 0
        })
        
        # Generate OTM strikes (20 strikes above ATM for CE, below for PE)
        for i in range(1, 5):
            strike = self.atm_strike + (i * self.strike_step)
            strikes.append({
                'strike': strike,
                'tag': f'OTM{i}',
                'position': i
            })
        
        # Initialize option data structure
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

            # Map symbols to strikes for quick lookup
            self.subscription_map[self.option_data[strike]['ce_symbol']] = {
                'strike': strike, 'type': 'CE'
            }
            self.subscription_map[self.option_data[strike]['pe_symbol']] = {
                'strike': strike, 'type': 'PE'
            }
        
        logger.info(f"Generated {len(strikes)} strikes for {self.underlying}. ATM: {self.atm_strike}")

    def _update_greeks_batch(self, strikes_list):
        """
        Fetch Greeks for multiple strikes in a single batch call.
        strikes_list: list of strike prices to fetch
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
            response = self.api_client.multioptiongreeks(symbols_list=symbols_to_fetch)

            # 3. Process Response
            if response and response.get('status') in ['success', 'partial']:
                data_list = response.get('data', [])
                if not data_list:
                     logger.warning(f"Batch greeks returned {response.get('status')} but no data")
                     return

                for item in data_list:
                    # Ignore failed items in partial response
                    if item.get('status') == 'error':
                        continue
                        
                    symbol = item.get('symbol')
                    if symbol not in symbol_map: continue

                    details = symbol_map[symbol]
                    strike = details['strike']
                    option_type = details['type'] # CE or PE
                    
                    # Target dictionary to update
                    target_dict = self.option_data[strike]['ce_data'] if option_type == 'CE' else self.option_data[strike]['pe_data']
                    
                    greeks = item.get('greeks', {})
                    if not greeks: continue # faster skip

                    target_dict.update({
                        'delta': float(greeks.get('delta', 0) or 0),
                        'gamma': float(greeks.get('gamma', 0) or 0),
                        'theta': float(greeks.get('theta', 0) or 0),
                        'vega': float(greeks.get('vega', 0) or 0),
                        'rho': float(greeks.get('rho', 0) or 0),
                        'iv': float(item.get('implied_volatility', 0) or 0)
                    })
                    
                    # Capture DTE if available (usually in item root or greeks)
                    if 'days_to_expiry' in item:
                        self.days_to_expiry = float(item.get('days_to_expiry', 0))

            elif response and response.get('code') == 429:
                logger.warning(f"[{self.underlying}] Batch Rate Limit (429). Backing off.")
                self._backoff_until = time.time() + 60
            else:
                logger.warning(f"Batch greeks failed. Full Response: {response}")

        except Exception as e:
            logger.error(f"Error in batch greeks update: {e}")

    def refresh_greeks(self):
        """
        Smart Refresh Strategy for Greeks with Strict Batching
        - Max 4 Symbols (2 Strikes) per batch
        - 4 Second Interval
        - Priority: ATM -> Neighbors -> Background (Weighted Rotation)
        """
        if self._backoff_until > time.time():
            return
        if not self.atm_strike:
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
        # Pattern: [Neighbor1, Neighbor2, Background, Neighbor1, Neighbor2, Background...]
        # This ensures immediate neighbors are refreshed every ~12s
        if len(sorted_strikes) > 1:
            if not hasattr(self, '_refresh_cycle'): self._refresh_cycle = 0
            if not hasattr(self, '_bg_greek_idx'): self._bg_greek_idx = 3 # Start background from index 3
            
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
                    # Fallback if specific neighbors don't exist
                    target_strike = sorted_strikes[1]

            if target_strike and target_strike not in final_batch:
                final_batch.append(target_strike)
            
            self._refresh_cycle += 1

        # 3. Execute Batch
        if final_batch:
            # logger.info(f"Fetching Greeks for: {final_batch}")
            self._update_greeks_batch(final_batch)
            
            # STRICT 4s Interval
            time.sleep(4.0)

    def _greek_monitor_loop(self):
        """Background loop to refresh Greeks"""
        logger.info(f"Starting Smart Greek Monitor for {self.underlying}")
        while self.monitoring_active:
            try:
                self.refresh_greeks()
                
                # Sleep with check
                for _ in range(20):
                    if not self.monitoring_active: return
                    time.sleep(0.5) 
            except Exception as e:
                logger.error(f"[{self.underlying}] Error in Greek monitoring loop: {e}")
                time.sleep(5)
    
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
        symbol = f"{self.underlying}{expiry_formatted}{expiry_year}{strike_str}{option_type}"
        return symbol
    
    def setup_subscriptions(self):
        """Configure WebSocket subscriptions"""
        if not self.websocket_manager:
            logger.warning("WebSocket manager not available for subscriptions")
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
            subscription = {
            'exchange': exchange,
            'symbol': self.underlying,
            'mode': 'quote'
            }
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
        
        # 1. Handle Underlying Update
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

            # Update ATM strike
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

        # 2. Handle Option Update
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']
            strike = strike_info['strike']
            
            # Map quote fields
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

    def handle_depth_update(self, data):
        """Process incoming depth data"""
        symbol = data.get('symbol') or data.get('Symbol') or ''
        
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']
            strike = strike_info['strike']
            
            depth_data = {
                'ltp': float(data.get('ltp', 0) or 0),
                'volume': int(data.get('volume', 0) or 0),
                'oi': int(data.get('oi', 0) or 0)
            }
            
            self.update_option_depth(strike, option_type, depth_data)
    
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

    def calculate_max_pain(self):
        """Calculate Max Pain theory"""
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
        total_loss = {}
        
        for price_point in strikes:
            loss = 0
            for k in strikes:
                if price_point > k:
                    loss += (price_point - k) * ce_oi.get(k, 0)
                if price_point < k:
                    loss += (k - price_point) * pe_oi.get(k, 0)
            total_loss[price_point] = loss
            
        if not total_loss:
            return 0
        return min(total_loss, key=total_loss.get)

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

