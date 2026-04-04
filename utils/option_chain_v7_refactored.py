"""
Option Chain Manager Module v7 - REFACTORED
Signal Generation Improvements:
- Fixed Max Pain logic (attraction vs escape)
- Context-aware IV Skew interpretation
- Dynamic thresholds for scaling across underlyings
- Improved institutional flow detection
- Signal weighting by reliability
- Correlation filtering
- Better divergence detection
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


class OptionChainManagerV7Refactored:
    """
    REFACTORED Manager class with improved signal generation logic
    """

    def __init__(self, underlying, expiry, websocket_manager=None):
        """
        Initialize OptionChainManagerV7Refactored
        """
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
        self.manager_id = f"{underlying}_{expiry}_V7_REFACTORED"
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
        self.strategy_mode = 'SNIPER'

        # NEW: Signal reliability weights
        self.signal_weights = {
            'delta_oi': 1.5,      # High weight - direct positioning
            'oi_unwind': 1.3,     # High weight - trend confirmation
            'pcr_roc': 1.2,       # Medium-high - momentum
            'inst_flow': 1.2,     # Medium-high - smart money
            'gamma': 1.0,         # Medium - structural
            'momentum': 1.0,      # Medium - price action
            'max_pain': 0.8,      # Lower - only matters near expiry
            'iv_skew': 0.7,       # Lower - can be noisy
            'vanna': 0.6,         # Lower - second-order effect
        }

        logger.info(f"Initialized REFACTORED OptionChainManagerV7 for {underlying}")

    def set_strategy_mode(self, mode):
        """Update strategy mode dynamically"""
        if mode in ['SNIPER', 'SCALPER']:
            self.strategy_mode = mode
            logger.info(f"Strategy Mode updated to {mode} for {self.manager_id}")
            return True
        return False

    def _capture_initial_state(self):
        """Capture initial OI state for delta change calculations"""
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
        """
        Calculate recent price volatility for dynamic thresholds
        Returns: volatility as percentage
        """
        if len(self.price_history) < 60:
            return 0.1  # Default 0.1%

        recent_prices = list(self.price_history)[-60:]
        avg_price = sum(recent_prices) / len(recent_prices)
        price_range = max(recent_prices) - min(recent_prices)
        volatility = (price_range / avg_price) * 100

        return max(0.05, volatility)  # Minimum 0.05%

    # ============================================================================
    # REFACTORED SIGNAL METHODS
    # ============================================================================

    def _check_divergence(self):
        """
        REFACTORED: Detect Spot vs Option Divergence (Traps)

        IMPROVEMENTS:
        - Looser thresholds for better detection
        - Better tolerance levels
        """
        if len(self.price_history) < 10 or len(self.atm_ce_history) < 10:
            return None, ""

        current_spot = self.price_history[-1]
        current_ce = self.atm_ce_history[-1]
        current_pe = self.atm_pe_history[-1]

        spot_high = max(self.price_history)
        spot_low = min(self.price_history)
        ce_high = max(self.atm_ce_history)
        pe_high = max(self.atm_pe_history)

        # FIXED: Looser thresholds (0.998 vs 0.9998, 0.98 vs 0.995)
        # Bull Trap: Spot making new high, but Call lagging
        if current_spot >= spot_high * 0.998:  # Within 0.2% of high
            if current_ce < ce_high * 0.98:    # Call is 2% below its high
                return "BEARISH", f"🚨 BULL TRAP: Spot at {current_spot:.1f} (High) but Call Weak ({current_ce:.1f} vs Peak {ce_high:.1f})"

        # Bear Trap: Spot making new low, but Put lagging
        if current_spot <= spot_low * 1.002:   # Within 0.2% of low
            if current_pe < pe_high * 0.98:    # Put is 2% below its high
                return "BULLISH", f"🚨 BEAR TRAP: Spot at {current_spot:.1f} (Low) but Put Weak ({current_pe:.1f} vs Peak {pe_high:.1f})"

        return None, ""

    def _score_momentum_velocity(self):
        """
        REFACTORED: Signal 8 - Momentum Velocity

        IMPROVEMENTS:
        - Dynamic thresholds based on recent volatility
        - Better noise filtering
        """
        if len(self.price_history) < 10:
            return 0, "No Velocity Data"

        p_now = self.price_history[-1]
        p_prev = self.price_history[-10]

        velocity_pct = ((p_now - p_prev) / p_prev) * 100

        # FIXED: Dynamic threshold = 2x recent volatility (or minimum 0.1%)
        volatility = self._get_recent_volatility()
        threshold = max(0.1, volatility * 2)

        if velocity_pct > threshold:
            if velocity_pct > threshold * 3:  # 3x volatility = extreme
                return 25, f"🚀 MOMENTUM BURST (+{velocity_pct:.2f}%)"
            return 10, f"Positive Momentum (+{velocity_pct:.2f}%)"

        elif velocity_pct < -threshold:
            if velocity_pct < -threshold * 3:
                return -25, f"🔻 MOMENTUM CRASH ({velocity_pct:.2f}%)"
            return -10, f"Negative Momentum ({velocity_pct:.2f}%)"

        return 0, f"Stable Velocity ({velocity_pct:+.2f}%)"

    def _get_expiry_multiplier(self):
        """
        Signal 9: Time-Decay Awareness (Expiry Filter)
        Reduces signal confidence near expiry when Gamma risk is highest.
        """
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
        """
        REFACTORED: Signal 10 - PCR Rate-of-Change

        IMPROVEMENTS:
        - Longer window (60 ticks vs 20) for stability
        - Adjusted thresholds for longer window
        """
        # FIXED: Increased window from 20 to 60 for stability
        if len(self.pcr_history) < 60:
            return 0, f"Building PCR History ({len(self.pcr_history)}/60)"

        pcr_now = self.pcr_history[-1]
        pcr_prev = self.pcr_history[-60]  # ~60-120 seconds ago

        if pcr_prev <= 0:
            return 0, "Invalid PCR Data"

        roc = ((pcr_now - pcr_prev) / pcr_prev) * 100

        # FIXED: Adjusted thresholds for longer window
        if roc > 3:  # Reduced from 5 since window is longer
            return 20, f"🟢 PCR Surge +{roc:.2f}% (Strong Support Building)"
        elif roc > 1.5:
            return 10, f"PCR Rising +{roc:.2f}% (Bullish Support)"

        elif roc < -3:
            return -20, f"🔴 PCR Crash {roc:.2f}% (Strong Resistance)"
        elif roc < -1.5:
            return -10, f"PCR Falling {roc:.2f}% (Bearish Pressure)"

        return 0, f"Stable PCR (ROC: {roc:+.2f}%)"

    def _verify_volume_liquidity(self):
        """
        Signal 11: Volume Confirmation Filter
        """
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return False, "No ATM Data"

        atm_data = self.option_data[self.atm_strike]
        ce_vol = atm_data['ce_data'].get('volume', 0)
        pe_vol = atm_data['pe_data'].get('volume', 0)
        total_vol = ce_vol + pe_vol

        # Dynamic threshold based on underlying
        if self.underlying in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            min_volume = 10000
        else:
            min_volume = 2000

        if total_vol < min_volume:
            return False, f"⚠️ Low Volume ({total_vol:,})"
        elif total_vol < min_volume * 2:
            return True, f"Moderate Volume ({total_vol:,})"

        return True, f"High Volume ({total_vol:,})"

    def _check_advanced_divergence(self):
        """
        Divergence Engine 2.0: Advanced Pattern Detection
        """
        if len(self.price_history) < 50:
            return None, ""

        alerts = []

        # 1. PCR Divergence
        if len(self.pcr_history) >= 50:
            p_start, p_end = self.price_history[0], self.price_history[-1]
            pcr_start, pcr_end = self.pcr_history[0], self.pcr_history[-1]

            if p_end > p_start * 1.002 and pcr_end < pcr_start * 0.95:
                alerts.append("PCR DECAY: Price Up but PCR Dropping (Weak Longs)")
            elif p_end < p_start * 0.998 and pcr_end > pcr_start * 1.05:
                alerts.append("PCR SURGE: Price Down but PCR Rising (Accumulation)")

        # 2. Volume-OI Flux
        if len(self.vol_history) >= 20 and len(self.oi_history) >= 20:
            vol_delta = self.vol_history[-1] - self.vol_history[-20]
            oi_delta = self.oi_history[-1] - self.oi_history[-20]

            if vol_delta > 10000 and abs(oi_delta) < 1000:
                alerts.append("INSTITUTIONAL CHURN: High Volume, Zero OI Growth")

        # 3. IV Exhaustion
        if len(self.iv_history) >= 20:
            iv_start, iv_end = self.iv_history[0], self.iv_history[-1]
            p_start, p_end = self.price_history[-20], self.price_history[-1]

            if p_end < p_start * 0.999 and iv_end < iv_start * 0.97:
                alerts.append("IV EXHAUSTION: Price Down but Volatility Cooling (Bottom?)")
            elif p_end > p_start * 1.001 and iv_end > iv_start * 1.03:
                alerts.append("IV SPIKE: Price Up but Fear Rising (Potential Top)")

        return alerts[0] if alerts else None, " | ".join(alerts)

    def _score_gamma_exposure(self):
        """
        REFACTORED: Signal 5 - Gamma Exposure (GEX)

        IMPROVEMENTS:
        - Better interpretation of GEX sign vs price position
        - Gamma pinning detection
        - Trend amplification vs mean reversion
        """
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
            return 0, "No Gamma Data"

        top_wall_strike = gamma_walls[0][0]
        self.option_data[top_wall_strike]['is_gamma_wall'] = True

        score = 0
        reason = "Neutral Gamma"

        # FIXED: Better GEX interpretation
        # Positive GEX = Dealers SHORT gamma = Dampening (mean reversion)
        # Negative GEX = Dealers LONG gamma = Amplifying (trending)

        if total_gex > 0:  # Positive GEX = Pinning/Dampening
            distance_pct = abs(self.underlying_ltp - top_wall_strike) / top_wall_strike * 100

            if distance_pct < 0.5:  # Very close to wall
                score = 0
                reason = f"🧲 Gamma Pin at {top_wall_strike} (Mean Reversion)"
            else:
                # Far from wall = will be pulled back
                if self.underlying_ltp > top_wall_strike:
                    score = -5  # Above wall = bearish pull
                    reason = f"Gamma Pull Down toward {top_wall_strike}"
                else:
                    score = 5  # Below wall = bullish pull
                    reason = f"Gamma Pull Up toward {top_wall_strike}"

        elif total_gex < 0:  # Negative GEX = Trend Amplification
            # Check trend direction
            if len(self.price_history) > 5:
                is_uptrend = self.price_history[-1] > self.price_history[-5]
                score = 10 if is_uptrend else -10
                reason = f"⚡ Negative GEX: Trend Amplification ({'Up' if is_uptrend else 'Down'})"
            else:
                score = 0
                reason = "Negative GEX (Volatile Regime)"

        return score, reason

    def _score_vanna_charm(self):
        """
        Signal 6: Vanna/Charm Bias (Second Order Effects)
        """
        if len(self.iv_history) < 20 or len(self.price_history) < 20:
            return 0, "Wait for IV History"

        current_iv = self.iv_history[-1]
        avg_iv = sum(self.iv_history) / len(self.iv_history)

        current_price = self.price_history[-1]
        prev_price = self.price_history[-20]

        score = 0
        reason = "Vanna/Charm Neutral"

        # Squeeze: Price up + IV down
        if current_price > prev_price * 1.001 and current_iv < avg_iv * 0.98:
            score = 15
            reason = "Bullish Vanna Squeeze (Dealers Covering)"
        # Crush: Price down + IV up
        elif current_price < prev_price * 0.999 and current_iv > avg_iv * 1.02:
            score = -15
            reason = "Bearish Vanna Crush (Hedging Spike)"

        return score, reason

    def _detect_institutional_flow(self):
        """
        REFACTORED: Signal 7 - Institutional Flow

        IMPROVEMENTS:
        - Stricter threshold (1.5x OI vs 0.8x)
        - Check for OI increase (fresh positions)
        - Better filtering of false positives
        """
        alerts = []
        score = 0
        reason = "Normal Flow"

        for strike, data in self.option_data.items():
            ce_vol = data['ce_data'].get('volume', 0)
            ce_oi = data['ce_data'].get('oi', 0)
            pe_vol = data['pe_data'].get('volume', 0)
            pe_oi = data['pe_data'].get('oi', 0)

            # Get OI change if available
            ce_oi_change = 0
            pe_oi_change = 0
            if strike in self.initial_state:
                ce_oi_change = ce_oi - self.initial_state[strike]['ce_oi']
                pe_oi_change = pe_oi - self.initial_state[strike]['pe_oi']

            # FIXED: Stricter threshold - Volume > 1.5x OI + OI increasing
            if ce_oi > 0 and ce_vol > ce_oi * 1.5:
                # Better: Also check if OI is increasing (fresh positions)
                if ce_oi_change > 500:  # Fresh positions being opened
                    alerts.append(f"Fresh Institutional CE at {strike}")
                    data['ce_data']['institutional_flow'] = True
                    if strike > self.underlying_ltp:
                        score += 5
                else:
                    # High volume but no OI increase = churn
                    data['ce_data']['institutional_flow'] = False
            else:
                data['ce_data']['institutional_flow'] = False

            if pe_oi > 0 and pe_vol > pe_oi * 1.5:
                if pe_oi_change > 500:
                    alerts.append(f"Fresh Institutional PE at {strike}")
                    data['pe_data']['institutional_flow'] = True
                    if strike < self.underlying_ltp:
                        score -= 5
                else:
                    data['pe_data']['institutional_flow'] = False
            else:
                data['pe_data']['institutional_flow'] = False

        if alerts:
            reason = ", ".join(alerts[:3])  # Limit to 3

        score = max(-20, min(20, score))
        return score, reason

    def _scan_iv_surface(self):
        """
        Identify IV Anomalies (Cheap/Expensive strikes)
        """
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
        """
        Calculate OI Change Intensity (Heatmap) for UI
        """
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
        """
        REFACTORED: Signal 1 - Delta-Weighted Open Interest Change

        IMPROVEMENTS:
        - Dynamic thresholds based on total OI (percentage vs fixed)
        - Better scaling across underlyings
        """
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

        # FIXED: Dynamic threshold - use percentage of total change
        directional_pct = (abs(net_delta_oi) / current_total_oi_change) * 100 if current_total_oi_change > 0 else 0

        # If 30%+ of OI change is directional, it's significant
        if net_delta_oi < 0 and directional_pct > 30:  # Put Writing
            score = 30
            reason = f"Strong Put Writing ({directional_pct:.1f}% directional - Bullish)"
        elif net_delta_oi > 0 and directional_pct > 30:  # Call Writing
            score = -30
            reason = f"Strong Call Writing ({directional_pct:.1f}% directional - Bearish)"
        elif directional_pct > 15:  # Moderate directional flow
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
        """
        REFACTORED: Signal 2 - IV Skew

        IMPROVEMENTS:
        - Context-aware interpretation (trending vs stable)
        - Contrarian signal detection
        """
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return 0, "No ATM Data"

        atm_data = self.option_data[self.atm_strike]
        ce_iv = atm_data['ce_data'].get('iv', 0)
        pe_iv = atm_data['pe_data'].get('iv', 0)

        if ce_iv == 0 or pe_iv == 0:
            return 0, "IV Missing"

        diff = pe_iv - ce_iv

        # FIXED: Add context - check if price is falling or stable
        is_falling = False
        if len(self.price_history) > 20:
            price_change_pct = ((self.price_history[-1] - self.price_history[-20]) / self.price_history[-20]) * 100
            is_falling = price_change_pct < -0.1  # 0.1% down

        if diff > 2:  # Put IV significantly higher
            if is_falling:
                # Falling price + High Put IV = Justified fear (Bearish)
                return -15, f"Put IV Spike - Justified Fear (Bearish: PE {pe_iv:.1f} vs CE {ce_iv:.1f})"
            else:
                # Stable/Rising price + High Put IV = Excess fear (Contrarian Bullish)
                return 10, f"Put IV Spike - Excess Fear (Contrarian Bullish: PE {pe_iv:.1f} vs CE {ce_iv:.1f})"

        elif diff < -2:  # Call IV higher
            # High Call IV = Greed/FOMO (usually Bearish reversal)
            return -10, f"Call IV Spike - FOMO/Greed (Bearish: CE {ce_iv:.1f} vs PE {pe_iv:.1f})"

        return 0, f"Balanced IV Skew (PE {pe_iv:.1f} vs CE {ce_iv:.1f})"

    def _score_oi_unwind(self):
        """
        Signal 3: OI Build-up vs Unwinding
        """
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
        """
        REFACTORED: Signal 4 - Max Pain Drift

        IMPROVEMENTS:
        - FIXED: Price is ATTRACTED to Max Pain, not repelled
        - Price above Max Pain = Bearish pull expected
        - Price below Max Pain = Bullish pull expected
        """
        mp = self.calculate_max_pain()
        if mp == 0 or self.underlying_ltp == 0:
            return 0, "No Max Pain"

        diff_pct = (self.underlying_ltp - mp) / mp * 100

        # FIXED: Max Pain acts as MAGNET (attraction, not repulsion)

        # Price significantly ABOVE Max Pain
        if diff_pct > 0.5:
            # Check if still rising (resisting the pull)
            if len(self.price_history) > 5 and self.price_history[-1] > self.price_history[-5]:
                # Still rising despite being above MP = temporary, but pull will come
                return -5, f"Above Max Pain {mp} (Bearish Pull Expected)"
            else:
                # Already pulling down
                return -10, f"Pulling Down to Max Pain {mp}"

        # Price significantly BELOW Max Pain
        elif diff_pct < -0.5:
            # Check if still falling
            if len(self.price_history) > 5 and self.price_history[-1] < self.price_history[-5]:
                # Still falling despite being below MP
                return 5, f"Below Max Pain {mp} (Bullish Pull Expected)"
            else:
                # Already pulling up
                return 10, f"Pulling Up to Max Pain {mp}"

        return 0, f"Aligned with Max Pain {mp}"

    def _check_signal_correlation(self, scores_dict):
        """
        NEW: Correlation Filter

        Detects if multiple signals are measuring the same phenomenon
        Returns penalty multiplier (0.8-1.0)
        """
        correlation_penalty = 1.0

        # Extract scores
        score_iv = scores_dict.get('iv_skew', 0)
        score_vc = scores_dict.get('vanna', 0)
        score_delta = scores_dict.get('delta_oi', 0)
        score_flow = scores_dict.get('inst_flow', 0)

        # Check IV-related correlation (IV Skew + Vanna both use IV)
        if abs(score_iv) > 10 and abs(score_vc) > 10:
            if (score_iv > 0 and score_vc > 0) or (score_iv < 0 and score_vc < 0):
                correlation_penalty *= 0.85  # 15% penalty for redundancy
                logger.debug("Correlation detected: IV Skew + Vanna (15% penalty)")

        # Check OI-related correlation (Delta OI + Inst Flow both use OI)
        if abs(score_delta) > 20 and abs(score_flow) > 15:
            if (score_delta > 0 and score_flow > 0) or (score_delta < 0 and score_flow < 0):
                correlation_penalty *= 0.90  # 10% penalty
                logger.debug("Correlation detected: Delta OI + Inst Flow (10% penalty)")

        return correlation_penalty

    def calculate_max_pain(self):
        """
        Calculate Max Pain theory
        """
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

    def generate_signals(self, pcr, max_pain):
        """
        REFACTORED: Quant Decision Engine with ALL improvements

        MAJOR IMPROVEMENTS:
        1. Fixed Max Pain logic (attraction vs escape)
        2. Context-aware IV Skew
        3. Dynamic thresholds (Delta OI, Momentum)
        4. Better institutional flow detection
        5. Longer PCR window
        6. Signal weighting by reliability
        7. Correlation filtering
        8. Better divergence detection
        """
        # 0. Capture state if needed
        self._capture_initial_state()

        # Update History
        if self.underlying_ltp > 0:
            self.price_history.append(self.underlying_ltp)

        # Update ATM option prices for divergence detection
        if self.atm_strike in self.option_data:
            atm_data = self.option_data[self.atm_strike]
            self.atm_ce_history.append(atm_data['ce_data'].get('ltp', 0))
            self.atm_pe_history.append(atm_data['pe_data'].get('ltp', 0))

            atm_iv = (atm_data['ce_data'].get('iv', 0) + atm_data['pe_data'].get('iv', 0)) / 2
            if atm_iv > 0:
                self.iv_history.append(atm_iv)

        # Calculate Total OI for history
        total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        total_oi = total_ce_oi + total_pe_oi

        self.oi_history.append(total_oi)

        # Update PCR history
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
                'mode': self.strategy_mode
            }

        # 1. Calculate ALL Component Scores (REFACTORED versions)
        score_delta, s_d_reason = self._score_delta_oi()
        score_iv, s_iv_reason = self._score_iv_skew()
        score_unwind, s_u_reason = self._score_oi_unwind()
        score_mp, s_mp_reason = self._score_max_pain()  # FIXED
        score_gex, s_gex_reason = self._score_gamma_exposure()  # IMPROVED

        # Update GEX history after calculation
        net_gex = sum(opt.get('net_gex', 0) for opt in self.option_data.values())
        self.gex_history.append(net_gex)

        score_vc, s_vc_reason = self._score_vanna_charm()
        score_flow, s_flow_reason = self._detect_institutional_flow()  # FIXED
        score_mom, s_mom_reason = self._score_momentum_velocity()  # IMPROVED
        score_pcr_roc, s_pcr_reason = self._score_pcr_roc()  # FIXED

        # NEW: Volume Liquidity Check
        is_liquid, liquidity_reason = self._verify_volume_liquidity()

        # NEW: Expiry Time Decay Awareness
        expiry_mult, expiry_reason = self._get_expiry_multiplier()

        # Store scores in dict for correlation check
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

        # NEW: Apply signal weighting
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

        # Normalize to -100..100 range
        max_possible = sum(self.signal_weights.values()) * 30  # Max individual ~30
        base_score = (weighted_score / max_possible) * 100

        # NEW: Apply correlation penalty
        correlation_penalty = self._check_signal_correlation(scores_dict)
        base_score *= correlation_penalty

        # Apply expiry multiplier
        total_score = int(base_score * expiry_mult)

        # Clamp Score
        total_score = max(min(total_score, 100), -100)

        scores_list = [score_delta, score_iv, score_unwind, score_mp, score_gex, score_vc, score_flow, score_mom, score_pcr_roc]

        # Calculate OI Change for UI
        if self.initial_state:
            for strike, data in self.option_data.items():
                if strike in self.initial_state:
                    data['ce_data']['oi_change'] = data['ce_data'].get('oi', 0) - self.initial_state[strike]['ce_oi']
                    data['pe_data']['oi_change'] = data['pe_data'].get('oi', 0) - self.initial_state[strike]['pe_oi']

        # Pattern Discovery Scan
        self._scan_iv_surface()
        self._calculate_heatmap_intensity()

        # DIVERGENCE (TRAP) CHECK - IMPROVED thresholds
        trap_type, trap_msg = self._check_divergence()
        if trap_type:
            if trap_type == "BEARISH":
                total_score = -50
                s_mp_reason = f"⚠️ {trap_msg}"
            elif trap_type == "BULLISH":
                total_score = 50
                s_mp_reason = f"⚠️ {trap_msg}"

        # Determine Bias
        bias = "NEUTRAL"
        action = "WAIT"

        if total_score > 65:
            bias = "BULLISH"
            action = "BUY CALL"
        elif total_score < -65:
            bias = "BEARISH"
            action = "BUY PUT"

        # HYBRID SELL LOGIC
        is_pinning = net_gex > 0
        is_range_bound = -30 <= total_score <= 30

        if is_pinning and is_range_bound:
            bias = "NEUTRAL"
            action = "SELL STRADDLE"
        elif is_pinning and 30 < total_score <= 60:
            bias = "BULLISH"
            action = "SELL PUT"
        elif is_pinning and -60 <= total_score < -30:
            bias = "BEARISH"
            action = "SELL CALL"

        # SIGNAL SMOOTHING (HYSTERESIS)
        self.action_history.append(action)
        if len(self.action_history) >= 5:
            counts = Counter(self.action_history)
            action = counts.most_common(1)[0][0]
            # Sync bias
            if action == "SELL STRADDLE":
                bias = "NEUTRAL"
            elif "CALL" in action:
                bias = "BULLISH" if "BUY" in action else "BEARISH"
            elif "PUT" in action:
                bias = "BEARISH" if "BUY" in action else "BULLISH"

        reasons = [
            f"Score: {total_score} ({bias})",
            f"Delta OI: {score_delta} ({s_d_reason})",
            f"IV Skew: {score_iv} ({s_iv_reason})",
            f"OI Flow: {score_unwind} ({s_u_reason})",
            f"Max Pain: {score_mp} ({s_mp_reason})",
            f"Gamma: {score_gex} ({s_gex_reason})",
            f"Inst. Flow: {score_flow} ({s_flow_reason})",
            f"Momentum: {score_mom} ({s_mom_reason})",
            f"PCR ROC: {score_pcr_roc} ({s_pcr_reason})",
        ]

        # Add warnings
        if expiry_mult < 1.0:
            reasons.insert(0, f"⏰ {expiry_reason} (Score x{expiry_mult:.1f})")

        if not is_liquid:
            reasons.insert(0, f"💧 {liquidity_reason}")

        if correlation_penalty < 1.0:
            reasons.insert(0, f"🔗 Signal Correlation Detected (Penalty: {correlation_penalty:.0%})")

        if trap_type:
            reasons.insert(0, f"🚨 {trap_msg}")

        # Advanced Divergence 2.0
        adv_div_type, adv_div_msg = self._check_advanced_divergence()
        if adv_div_type:
            reasons.insert(0, f"🔍 {adv_div_msg}")

        # Trade Setup
        trade_setup = {}
        warning = None

        if bias != "NEUTRAL" or action == "SELL STRADDLE":
            best_contract = self._select_best_strike(bias, action)
            if best_contract:
                levels = self._calculate_trade_levels(best_contract['ltp'], action)
                trade_setup = {
                    'contract': best_contract['name'],
                    'entry': best_contract['ltp'],
                    'sl': levels['sl'],
                    'target': levels['target'],
                    'rr': '1:2',
                    'legs': best_contract.get('contracts', [])
                }

        # Warnings
        if bias == "BULLISH" and score_mp < 0:
            warning = "⚠️ Bullish Signal but Max Pain Pull Down. Tighten SL."
        elif bias == "BEARISH" and score_mp > 0:
            warning = "⚠️ Bearish Signal but Max Pain Pull Up. Tighten SL."

        if trap_type:
            warning = f"⚠️ {trap_msg}"

        # Calculate PCR Bias
        pcr_bias = "NEUTRAL"
        if score_pcr_roc > 0:
            pcr_bias = "BULLISH"
        elif score_pcr_roc < 0:
            pcr_bias = "BEARISH"

        return {
            'action': action,
            'confidence': f"{abs(total_score)}%",
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
            'flow_alerts': s_flow_reason.split(", ") if "Institutional" in s_flow_reason else []
        }

    # ============================================================================
    # HELPER METHODS (Same as original, added for completeness)
    # ============================================================================

    def _select_best_strike(self, bias, action="BUY"):
        """
        Select Best Strike based on Action Type
        """
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
                'contracts': [
                    {'name': self.option_data[strike].get('ce_symbol'), 'ltp': ce_data.get('ltp', 0), 'type': 'CE'},
                    {'name': self.option_data[strike].get('pe_symbol'), 'ltp': pe_data.get('ltp', 0), 'type': 'PE'}
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

    def _calculate_trade_levels(self, entry_price, action_type="BUY"):
        """
        Calculate Stoploss and Target
        """
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
        """Calculate PCR and other metrics"""
        total_ce_volume = sum(opt['ce_data'].get('volume', 0) for opt in self.option_data.values())
        total_pe_volume = sum(opt['pe_data'].get('volume', 0) for opt in self.option_data.values())
        total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())

        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0

        max_pain = self.calculate_max_pain()
        signals = self.generate_signals(pcr, max_pain)

        inst_score, inst_reason = self._detect_institutional_flow()
        flow_alerts = inst_reason.split(", ") if inst_reason != "Normal Flow" else []

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
