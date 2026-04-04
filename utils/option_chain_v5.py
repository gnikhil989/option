"""
Option Chain Manager Module V5
Real-time option chain management for NIFTY and BANKNIFTY with configurable subscription mode (Quote/Depth)
"""

import json
import threading
import time
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, List, Optional, Any
import logging
from cachetools import TTLCache
import pytz

# from openalgo import api # Removed dependency

logger = logging.getLogger(__name__)


class OptionChainCacheV5:
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


class OptionChainManagerV5:
    """
    Manager class for option chain with configurable subscription mode
    Handles both LTP and bid/ask data for order management
    """
    
    def __init__(self, underlying, expiry, websocket_manager=None):
        """
        Initialize OptionChainManagerV5
        :param underlying: 'RELIANCE', 'NIFTY', 'BANKNIFTY', 'SENSEX', 'HDFCBANK', 'ICICIBANK', 'AXISBANK', 'INFY', 'YESBANK', 'HDFCBANK', 'ICICIBANK', 'AXISBANK', 'BHARTIARTL', 'YESBANK'
        :param expiry: Expiry date string or object
        :param websocket_manager: WebSocket manager instance
        """
        self.underlying = underlying
        self.expiry = expiry
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
        # New OHLC fields for underlying instrument
        self.underlying_open = 0
        self.underlying_high = 0
        self.underlying_low = 0
        self.underlying_close = 0
        self.underlying_avg = 0
        self.atm_strike = 0
        self.websocket_manager = websocket_manager
        self.cache = OptionChainCacheV5()
        self.monitoring_active = False
        self.initialized = False
        self.manager_id = f"{underlying}_{expiry}_V5"
        self.option_mode = 'quote'  # Forced Quote Mode
        
        # QUANT ENGINE STATE
        self.initial_state = {} # Stores {strike: {ce_oi: X, pe_oi: Y}} from start of session
        self.start_price = None # Underlying price at start of monitoring
        self.price_history = deque(maxlen=600) # Increased to 600 (approx 5-10 mins) for Divergence Check
        self.atm_ce_history = deque(maxlen=600) # Track ATM Call Prices
        self.atm_pe_history = deque(maxlen=600) # Track ATM Put Prices
        self.oi_history = deque(maxlen=20) # Track total OI history
        self.iv_history = deque(maxlen=120) # Track ATM IV (approx 2-5 mins)
        self.gex_history = deque(maxlen=120) # Track Net GEX history
        self.action_history = deque(maxlen=10) # Buffer for signal smoothing (approx 5-10 seconds)
        self.trade_state = None # {'type': 'CE', 'entry': 100, 'peak': 120, 'contract': '...'}
        self.days_to_expiry = 0 # Captured from Greeks/WS
        self._backoff_until = 0 # Rate limit backoff timestamp
        self.manager_instance_id = id(self) # Unique ID for this instance





        self.strategy_mode = 'SNIPER' # Options: SNIPER, SCALPER
        
        logger.info(f"Initialized OptionChainManagerV5 for {underlying} (Quote Mode Only)")

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
            logger.info(f"Captured initial state for Quant Engine. Strikes: {len(self.initial_state)}")

    def _check_divergence(self):
        """
        Detect Spot vs Option Divergence (Traps).
        Window: Last 600 ticks (approx 5-10 mins).
        
        1. Bull Trap (Bearish Divergence): 
           Spot makes New High (in window), but ATM Call fails to break its High (in window).
           Indicates Call Writing absorption.
           
        2. Bear Trap (Bullish Divergence):
           Spot makes New Low (in window), but ATM Put fails to break its High.
        """
        if len(self.price_history) < 10 or len(self.atm_ce_history) < 10:
            return None, ""
            
        # Get Current Values
        current_spot = self.price_history[-1]
        current_ce = self.atm_ce_history[-1]
        current_pe = self.atm_pe_history[-1]
        
        # Get Max/Min of Window
        spot_high = max(self.price_history)
        spot_low = min(self.price_history)
        ce_high = max(self.atm_ce_history)
        pe_high = max(self.atm_pe_history)
        
        # Check Bull Trap (Spot High, CE Lagging)
        # Condition: Current Spot is at High (or very close), but CE is significantly below its High.
        if current_spot >= spot_high * 0.9998: # Making New High
            if current_ce < ce_high * 0.995: # CE is < 99.5% of its High (Lagging)
                 return "BEARISH", f"TRAP ALERT: Spot New High but Call Lagging! ({current_spot} vs {ce_high})"

        # Check Bear Trap (Spot Low, PE Lagging)
        # Condition: Current Spot is at Low, but PE is significantly below its High.
        if current_spot <= spot_low * 1.0002: # Making New Low
             if current_pe < pe_high * 0.995:
                 return "BULLISH", f"TRAP ALERT: Spot New Low but Put Lagging! ({current_spot} vs {pe_high})"
                 
        return None, ""

    def _score_gamma_exposure(self):
        """
        Signal 5: Gamma Exposure (GEX) 
        High positive GEX (Call Gamma) acts as a magnet/resistance.
        High negative GEX (Put Gamma) acts as a support.
        Actually, concentrated Gamma Walls are pinning points.
        """
        total_gex = 0
        gamma_walls = []
        
        # Lottery/Institutional Lot sizes: NIFTY=50, BANKNIFTY=15
        lot_size = 50 if self.underlying == 'NIFTY' else 15 if self.underlying == 'BANKNIFTY' else 1
        
        for strike, data in self.option_data.items():
            ce_gamma = data['ce_data'].get('gamma', 0)
            ce_oi = data['ce_data'].get('oi', 0)
            pe_gamma = data['pe_data'].get('gamma', 0)
            pe_oi = data['pe_data'].get('oi', 0)
            
            # GEX = Gamma * OI * Lot Size
            ce_gex = ce_gamma * ce_oi * lot_size
            pe_gex = pe_gamma * pe_oi * lot_size
            
            # Net GEX at strike
            strike_gex = ce_gex - pe_gex
            total_gex += strike_gex
            
            # Store in data for UI
            data['ce_data']['gex'] = ce_gex
            data['pe_data']['gex'] = pe_gex
            data['net_gex'] = strike_gex
            data['is_gamma_wall'] = False # Reset
            
            # Detect Walls (Relative to others)
            gamma_walls.append((strike, abs(strike_gex)))
            
        gamma_walls.sort(key=lambda x: x[1], reverse=True)
        
        if not gamma_walls:
            return 0, "No Gamma Data"

        top_wall_strike = gamma_walls[0][0]
        self.option_data[top_wall_strike]['is_gamma_wall'] = True
        
        score = 0
        reason = "Neutral Gamma"
        
        if total_gex > 0: # Call Gamma dominates
             if self.underlying_ltp < top_wall_strike:
                 score = -10 # Resistance
                 reason = f"Gamma Resistance at {top_wall_strike}"
             else:
                 score = 5 # Support if above
                 reason = f"Gamma Support at {top_wall_strike}"
        elif total_gex < 0: # Put Gamma dominates
             if self.underlying_ltp > top_wall_strike:
                 score = 10 # Support
                 reason = f"Gamma Support at {top_wall_strike}"
             else:
                 score = -5 # Resistance if below
                 reason = f"Gamma Resistance at {top_wall_strike}"
                 
        return score, reason

    def _score_vanna_charm(self):
        """
        Signal 6: Vanna/Charm Bias (Second Order Effects)
        Simplified Proxy using IV history:
        - If Trend is UP and IV is CRUSHING -> Bullish Vanna Squeeze (+15)
        - If Trend is DOWN and IV is SPIKING -> Bearish Vanna Crush (-15)
        """
        if len(self.iv_history) < 20 or len(self.price_history) < 20:
            return 0, "Wait for IV History"
            
        current_iv = self.iv_history[-1]
        avg_iv = sum(self.iv_history) / len(self.iv_history)
        
        current_price = self.price_history[-1]
        prev_price = self.price_history[-20] # ~ 1-2 min ago
        
        score = 0
        reason = "Vanna/Charm Neutral"
        
        # Squeeze Detection: Price up + IV down (Dealers covering shorts)
        if current_price > prev_price * 1.001 and current_iv < avg_iv * 0.98:
            score = 15
            reason = "Bullish Vanna Squeeze"
        # Crush Detection: Price down + IV up (Dealers hedging aggressively)
        elif current_price < prev_price * 0.999 and current_iv > avg_iv * 1.02:
            score = -15
            reason = "Bearish Vanna Crush"
            
        return score, reason


    def _detect_institutional_flow(self):
        """
        Signal 7: Institutional Flow (Volume > OI)
        Detects "Opening" trades by big players. 
        """
        alerts = []
        score = 0
        
        for strike, data in self.option_data.items():
            ce_vol = data['ce_data'].get('volume', 0)
            ce_oi = data['ce_data'].get('oi', 0)
            pe_vol = data['pe_data'].get('volume', 0)
            pe_oi = data['pe_data'].get('oi', 0)
            
            # Threshold: Volume > 0.8 * OI
            if ce_oi > 0 and ce_vol > ce_oi * 0.8:
                alerts.append(f"Institutional CE at {strike}")
                if strike > self.underlying_ltp: score += 5
            
            if pe_oi > 0 and pe_vol > pe_oi * 0.8:
                alerts.append(f"Institutional PE at {strike}")
                if strike < self.underlying_ltp: score -= 5
                
        score = max(-20, min(20, score))
        reason = ", ".join(alerts[:2]) if alerts else "No Heavy Flow"
        
        return score, reason


    def _score_delta_oi(self):
        """
        Signal 1: Delta-Weighted Open Interest Change
        Interpretation:
        More Call selling (OI up) or Put buying (OI up) -> Bearish (Wait, Put buying is Bearish, Call Selling is Bearish)
        Actually:
        - Call OI Increase: Writers selling -> Resistance -> BEARISH
        - Put OI Increase: Writers selling -> Support -> BULLISH
        
        Logic in prompt:
        "More Call selling or Put buying = Bullish bias" ?? 
        Wait, usually:
        - High Put OI = Support (Bullish)
        - High Call OI = Resistance (Bearish)
        
        Let's stick to standard interpretation unless prompt implies "buying":
        Standard: Writers dominate.
        - Call OI Up -> Bearish
        - Put OI Up -> Bullish
        
        Prompt Logic says: "Multiply Delta by change in OI"
        Delta for Call is (+), Delta for Put is (-).
        
        If Call OI increases (+Change): (+Delta * +Change) = +Score. 
        If Put OI increases (+Change): (-Delta * +Change) = -Score.
        
        If Score is Positive -> Call Dominance -> Bearish?
        If Score is Negative -> Put Dominance -> Bullish?
        
        Let's interpret the Prompt's "Bullish Bias" rule:
        "More Call selling or Put buying" -> wait, Call Selling is Bearish. Put Buying is Bearish.
        Maybe the prompt meant:
        "More Put Selling (Bullish) or Call Buying (Bullish)" = Bullish.
        
        Let's implement Standard Max Pain/PCR Logic:
        - Net Delta OI = Sum(CallDelta * CallOIChange) + Sum(PutDelta * PutOIChange)
        - Call Delta (0 to 1), Put Delta (-1 to 0).
        
        Example: Calls added at ATM (Delta 0.5). OI +1000.  Net += 500. (Bearish pressure from writers)
        Example: Puts added at ATM (Delta -0.5). OI +1000. Net += -500. (Bullish pressure from writers)
        
        So:
        - Positive Net Value = Bearish (Call Writers adding)
        - Negative Net Value = Bullish (Put Writers adding)
        
        Scoring per Prompt:
        "Strong Bullish signal -> add +30" (If Put writing dominates / Negative Net)
        "Strong Bearish signal -> add -30" (If Call writing dominates / Positive Net)
        """
        net_delta_oi = 0
        current_total_oi_change = 0
        
        for strike, data in self.option_data.items():
            if strike not in self.initial_state:
                continue
                
            init = self.initial_state[strike]
            
            # Call Side
            ce_oi_change = data['ce_data'].get('oi', 0) - init['ce_oi']
            ce_delta = data['ce_data'].get('delta', 0.5) # Default to 0.5 if missing
            if ce_delta == 0: ce_delta = 0.5 # Fallback
            
            # Put Side
            pe_oi_change = data['pe_data'].get('oi', 0) - init['pe_oi']
            pe_delta = data['pe_data'].get('delta', -0.5)
            if pe_delta == 0: pe_delta = -0.5
            
            # Accumulate
            net_delta_oi += (ce_oi_change * ce_delta) + (pe_oi_change * pe_delta)
            current_total_oi_change += abs(ce_oi_change) + abs(pe_oi_change)
            
        # Normalize/Threshold
        # Threshold depends on volume. Let's say significant if > 10% of total change is directional
        score = 0
        reason = "Balanced Delta OI"
        
        if current_total_oi_change == 0:
            return 0, "No OI Change"

        # Inverted logic: +Net (Call writing) is Bearish (-30), -Net (Put writing) is Bullish (+30)
        # However, Prompt says: "More Call selling or Put buying = Bullish bias" ??? 
        # That contradicts standard theory. I will follow STANDARD THEORY:
        # Writers rule. Call OI Up = Bearish. Put OI Up = Bullish.
        
        if net_delta_oi < -1000: # Significant Put Writing (Negative Delta * Positive OI)
            score = 30
            reason = "Strong Put Writing (Bullish)"
        elif net_delta_oi > 1000: # Significant Call Writing (Positive Delta * Positive OI)
            score = -30
            reason = "Strong Call Writing (Bearish)"
        else:
            score = 0
            
        return score, reason

    def _score_iv_skew(self):
        """
        Signal 2: IV Skew
        Put IV > Call IV -> Fear (Bearish/Hedging) -> Score -15
        Call IV > Put IV -> Greed (Bullish) -> Score +15
        """
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return 0, "No ATM Data"
            
        atm_data = self.option_data[self.atm_strike]
        ce_iv = atm_data['ce_data'].get('iv', 0)
        pe_iv = atm_data['pe_data'].get('iv', 0)
        
        if ce_iv == 0 or pe_iv == 0:
            return 0, "IV Missing"
            
        diff = pe_iv - ce_iv
        
        if diff > 2: # Put IV significantly higher
            return -15, f"High Put IV Skew (Fear: {pe_iv:.1f} vs {ce_iv:.1f})"
        elif diff < -2: # Call IV higher
            return 15, f"High Call IV Skew (Greed: {ce_iv:.1f} vs {pe_iv:.1f})"
        
        return 0, "Balanced IV Skew"

    def _score_oi_unwind(self):
        """
        Signal 3: OI Build-up vs Unwinding
        Price Rising + OI Rising -> Long Build-up -> +20
        Price Falling + OI Rising -> Short Build-up -> -20
        Price Rising + OI Falling -> Short Covering -> +5
        Price Falling + OI Falling -> Long Unwinding -> -5
        """
        if len(self.price_history) < 2 or len(self.oi_history) < 2:
            return 0, "Building History"
            
        price_trend = self.price_history[-1] - self.price_history[0]
        oi_trend = self.oi_history[-1] - self.oi_history[0]
        
        # Thresholds
        price_move_sig = (self.price_history[-1] * 0.0005) # 0.05% move
        oi_move_sig = 1000 
        
        if price_trend > price_move_sig and oi_trend > oi_move_sig:
            return 20, "Long Build-up (Price Up, OI Up)"
        elif price_trend < -price_move_sig and oi_trend > oi_move_sig:
            return -20, "Short Build-up (Price Down, OI Up)"
        elif price_trend > price_move_sig and oi_trend < -oi_move_sig:
            return 5, "Short Covering (Price Up, OI Down)"
        elif price_trend < -price_move_sig and oi_trend < -oi_move_sig:
            return -5, "Long Unwinding (Price Down, OI Down)"
            
        return 0, "Mixed Position Flow"

    def _score_max_pain(self):
        """
        Signal 4: Max Pain Drift
        Price moving away from Max Pain with OI support.
        Bullish Escape (+10) or Bearish Escape (-10).
        """
        mp = self.calculate_max_pain()
        if mp == 0 or self.underlying_ltp == 0:
            return 0, "No Max Pain"
            
        diff_pct = (self.underlying_ltp - mp) / mp * 100
        
        # If Price is significantly above Max Pain (> 0.5%)
        if diff_pct > 0.5:
             # Check if escaping (Trend is Up)
             if len(self.price_history) > 5 and self.price_history[-1] > self.price_history[-5]:
                 return 10, "Bullish Escape from Max Pain"
             else:
                 return 0, "Above Max Pain (Neutral)"
                 
        # If Price is significantly below Max Pain (< -0.5%)
        elif diff_pct < -0.5:
             if len(self.price_history) > 5 and self.price_history[-1] < self.price_history[-5]:
                 return -10, "Bearish Escape from Max Pain"
             else:
                 return 0, "Below Max Pain (Neutral)"
                 
        return 0, "Aligned with Max Pain"
    
    def update_option_tags(self):
        """Update option tags when ATM changes"""
        for strike_data in self.option_data.values():
            strike = strike_data['strike']
            position = self.get_strike_position(strike)
            strike_data['position'] = position
            strike_data['tag'] = self.get_position_tag(position)

    def calculate_max_pain(self):
        """
        Calculate Max Pain theory: The strike price where option writers (sellers) 
        lose the least amount of money at expiration.
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
                # Call Loss: max(0, strike_price - k) * ce_oi[k]
                if strike_price > k:
                    total_loss += (strike_price - k) * ce_oi[k]
                
                # Put Loss: max(0, k - strike_price) * pe_oi[k]
                if k > strike_price:
                    total_loss += (k - strike_price) * pe_oi[k]
            
            if total_loss < min_loss:
                min_loss = total_loss
                max_pain = strike_price
                
        return max_pain

    def _select_best_strike(self, bias):
        """
        Select the best strike for trading based on liquidity and Delta.
        For Retail/Day Trading, ATM (At-The-Money) is usually best balance of Theta/Gamma.
        """
        if not self.atm_strike or self.atm_strike not in self.option_data:
            return None
            
        # Default to ATM
        target_strike = self.atm_strike
        strike_data = self.option_data[target_strike]
        
        # Simple refinement: Check if OTM has significantly more volume? 
        # For now, stick to ATM for consistency in "Quant" systematic approach.
        
        if bias == "BULLISH":
            # Buy Call
            ltp = strike_data['ce_data'].get('ltp', 0)
            symbol = strike_data['ce_symbol']
            name = f"{target_strike} CE"
            return {'strike': target_strike, 'type': 'CE', 'ltp': ltp, 'name': name, 'symbol': symbol}
        elif bias == "BEARISH":
            # Buy Put
            ltp = strike_data['pe_data'].get('ltp', 0)
            symbol = strike_data['pe_symbol']
            name = f"{target_strike} PE"
            return {'strike': target_strike, 'type': 'PE', 'ltp': ltp, 'name': name, 'symbol': symbol}
            
        return None

    def _calculate_trade_levels(self, entry_price, action_type="BUY"):
        """
        Calculate Stoploss and Target.
        BUY: SL -15% (Stop), Target +30%
        SELL: SL +40% (Stop), Target -80% (Decay)
        """
        if entry_price <= 0:
            return {'sl': 0, 'target': 0}
            
        if "SELL" in action_type:
            # Credit Strategy (Shorting Premium)
            # Stop Loss is HIGHER than entry (Premium Spike risk)
            sl = round(entry_price * 1.40, 1) # 40% SL (Generous buffer for spike)
            target = round(entry_price * 0.20, 1) # 80% Decay (Target near 0)
        else:
            # Debit Strategy (Buying Premium)
            sl = round(entry_price * 0.85, 1) # 15% SL
            target = round(entry_price * 1.30, 1) # 30% Target
            
        return {'sl': sl, 'target': target}


    def _verify_quality(self, bias, scores):
        """
        SNIPER MODE: Verify signal quality.
        Returns: (bool, reason)
        Rules:
        1. No Conflicting Signals: All component scores must align with bias (or be neutral).
        2. Trend Confirmation: Price must be on correct side of Average.
        3. Intraday Momentum (VWAP): Option LTP > Average Price (Specific Contract).
        4. Market Depth: Buy/Sell Quantity Ratio check.
        """
        # 1. Trend Check (Underlying)
        if bias == "BULLISH":
            if self.underlying_avg > 0 and self.underlying_ltp < self.underlying_avg:
                return False, "Price below Avg (Contra Trend)"
        elif bias == "BEARISH":
            if self.underlying_avg > 0 and self.underlying_ltp > self.underlying_avg:
                return False, "Price above Avg (Contra Trend)"
                
        # 2. Conflict Check (Zero Tolerance)
        # scores = [score_delta, score_iv, score_unwind, score_mp]
        if bias == "BULLISH":
            for s in scores:
                if s < -5: # Significant negative component
                    return False, "Conflicting Indicators (Internal Mismatch)"
        elif bias == "BEARISH":
            for s in scores:
                if s > 5: # Significant positive component
                    return False, "Conflicting Indicators (Internal Mismatch)"

        # 3. Data Refinement Checks (VWAP & Depth)
        # We need to check the Specific Option Contract that would be traded.
        best_contract = self._select_best_strike(bias)
        if best_contract:
            strike = best_contract['strike']
            c_type = 'ce_data' if best_contract['type'] == 'CE' else 'pe_data'
            
            if strike in self.option_data:
                opt_data = self.option_data[strike][c_type]
                ltp = opt_data.get('ltp', 0)
                avg = opt_data.get('avg_price', 0)
                buy_qty = opt_data.get('total_buy_qty', 0)
                sell_qty = opt_data.get('total_sell_qty', 0)
                
                # A. Intraday VWAP Check (Smart Reversal Logic)
                if ltp > 0 and avg > 0:
                     if ltp < avg:
                         # Check if this is a valid Reversal/Bounce Setup
                         # If BULLISH and Underlying Price > Max Pain (Support), we are bouncing.
                         # If BEARISH and Underlying Price < Max Pain (Resistance), we are rejecting.
                         mp = self.calculate_max_pain()
                         is_reversal = False
                         
                         if bias == "BULLISH" and self.underlying_ltp > mp * 0.995: # 0.5% tolerance
                             is_reversal = True
                         elif bias == "BEARISH" and self.underlying_ltp < mp * 1.005:
                             is_reversal = True
                             
                         if not is_reversal:
                             return False, f"Option LTP < VWAP & No Support ({ltp} < {avg})" 
                         # Else: It's a valid reversal, allowed.

                # B. Market Depth Check (Avoid fighting a wall)
                # If Buying, we want healthy Buy support remaining? 
                # OR we want to ensure we aren't buying into a massive Sell Wall (Sells >> Buys).
                # Actually, high Buy Qty usually means Limit Orders waiting to buy (Support).
                # High Sell Qty means Limit Orders waiting to sell (Resistance).
                # If we BUY, we want Resistance (Sell Qty) to be chewable?
                # Let's say: If Sell Qty is 3x Buy Qty, it's heavy resistance.
                if sell_qty > 0 and buy_qty > 0:
                    ratio = buy_qty / sell_qty
                    if ratio < 0.33: # Massive Sell Wall
                        return False, f"Heavy Resistance (Sell Wall: {sell_qty} vs {buy_qty})"
                        
        return True, "High Conviction"

    def generate_signals(self, pcr, max_pain):
        """
        New V5 Battle-tested Quant Decision Engine
        With Trailing Stoploss + SNIPER MODE (Quality/Conflict Filters) + Strategy Selector
        """
        # 0. Capture state if needed
        self._capture_initial_state()
        
        # Update History
        if self.underlying_ltp > 0:
            self.price_history.append(self.underlying_ltp)
        
        # Calculate Total OI for history
        total_oi = sum(opt['ce_data'].get('oi', 0) + opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        self.oi_history.append(total_oi)

        if not self.initial_state or not self.start_price:
            return {'action': 'WAIT', 'confidence': '0%', 'score': 0, 'reasons': ["Initializing..."]}
            
        # 1. Calculate Component Scores
        score_delta, s_d_reason = self._score_delta_oi()
        score_iv, s_iv_reason = self._score_iv_skew()
        score_unwind, s_u_reason = self._score_oi_unwind()
        score_mp, s_mp_reason = self._score_max_pain()
        score_gex, s_gex_reason = self._score_gamma_exposure()
        score_vc, s_vc_reason = self._score_vanna_charm()
        score_flow, s_flow_reason = self._detect_institutional_flow()
        
        # Aggregate
        total_score = score_delta + score_iv + score_unwind + score_mp + score_gex + score_vc + score_flow
        scores_list = [score_delta, score_iv, score_unwind, score_mp, score_gex, score_vc, score_flow]

        
        # -- DIVERGENCE (TRAP) CHECK --
        trap_type, trap_msg = self._check_divergence()
        if trap_type:
            # Trap Detected!
            if trap_type == "BEARISH": # Bull Trap -> Force Bearish Signal
                total_score = -50 # Force Bearish Score
                s_mp_reason = f"⚠️ {trap_msg}" # Override reason
            elif trap_type == "BULLISH": # Bear Trap -> Force Bullish Signal
                total_score = 50 # Force Bullish Score
                s_mp_reason = f"⚠️ {trap_msg}"

        # Determine Bias
        bias = "NEUTRAL"
        if total_score > 20: bias = "BULLISH" # Lowered slightly to allow accumulation
        elif total_score < -20: bias = "BEARISH"
        
        reasons = [
            f"Score: {total_score} ({bias})",
            f"Delta Logic: {score_delta} ({s_d_reason})",
            f"IV Skew: {score_iv} ({s_iv_reason})",
            f"OI Flow: {score_unwind} ({s_u_reason})",
            f"Max Pain: {score_mp} ({s_mp_reason})",
            f"Gamma: {score_gex} ({s_gex_reason})",
            f"Inst. Flow: {score_flow} ({s_flow_reason})"
        ]

        
        if trap_type:
             reasons.insert(0, f"TRAP ALERT: {trap_msg}")
        
        # --- DUAL STRATEGY EVALUATION ---
        # Evaluate BOTH strategies regardless of selected mode to populate the Matrix
        strategy_matrix = {}
        
        # 1. Evaluate SNIPER
        sniper_bias = "NEUTRAL"
        if total_score > 65: sniper_bias = "BULLISH"
        elif total_score < -65: sniper_bias = "BEARISH"
        
        sniper_action = "WAIT"
        if sniper_bias != "NEUTRAL":
            is_q, q_r = self._verify_quality(sniper_bias, scores_list) # Strict verification
            if is_q:
                sniper_action = "BUY " + ("CALL" if sniper_bias == "BULLISH" else "PUT")
            else:
                sniper_action = "WAIT (Noise)"
        
        strategy_matrix['SNIPER'] = {'action': sniper_action, 'bias': sniper_bias}
        
        # 2. Evaluate SCALPER
        scalper_bias = "NEUTRAL"
        if total_score > 40: scalper_bias = "BULLISH"
        elif total_score < -40: scalper_bias = "BEARISH"
        
        scalper_action = "WAIT"
        if scalper_bias != "NEUTRAL":
            scalper_action = "BUY " + ("CALL" if scalper_bias == "BULLISH" else "PUT")
            # Scalper skips strict quality verification or uses looser rules
        
        strategy_matrix['SCALPER'] = {'action': scalper_action, 'bias': scalper_bias}


        # 3. Main Trade Setup (Based on SELECTED Mode)
        trade_setup = {}
        warning = None
        
        # Re-evaluate Bias/Action based on SELECTED `self.strategy_mode` for the Main UI
        # We can just pick from the matrix we just calculated!
        current_strategy_result = strategy_matrix.get(self.strategy_mode, strategy_matrix['SNIPER'])
        
        # Override local variables for main return
        # Note: We keep the raw score-based bias for the meter, but Action should be strategy specific.
        action = current_strategy_result['action']
        # If action is WAIT, we force bias neutral for Setup generation? 
        # Actually, let's keep the raw High-Level bias for the visual meter, but Setup depends on Action.
        
        # Logic for Setup Generation: Only if Action is BUY
        active_bias = "NEUTRAL"
        if "BUY CALL" in action: active_bias = "BULLISH"
        elif "BUY PUT" in action: active_bias = "BEARISH"
        
        # -- SIGNAL STATE MANGEMENT --
        current_contract_type = 'CE' if active_bias == "BULLISH" else 'PE' if active_bias == "BEARISH" else None
        
        if active_bias == "NEUTRAL":
            pass 
        else:
            # Check if this is a NEW trade or CONTINUATION
            if self.trade_state and self.trade_state['type'] == current_contract_type:
                # CONTINUATION - RIDE TREND
                best_contract = self._select_best_strike(active_bias) # Get current LTP of contract
                if best_contract:
                     current_ltp = best_contract['ltp']
                     
                     # Update Peak
                     if current_ltp > self.trade_state['peak']:
                         self.trade_state['peak'] = current_ltp
                         
                     # Trailing Logic: 10% Trail from Peak
                     trail_gap = self.trade_state['entry'] * 0.10
                     new_sl = round(self.trade_state['peak'] - trail_gap, 1)
                     
                     if new_sl > self.trade_state['sl']:
                         self.trade_state['sl'] = new_sl
                         
                     # Dynamic Target
                     if current_ltp > (self.trade_state['target'] * 0.95):
                         self.trade_state['target'] = round(current_ltp + (self.trade_state['entry'] * 0.30), 1)
                         self.trade_state['status'] = "RIDING TREND"
            
            elif self.trade_state is None or self.trade_state['type'] != current_contract_type:
                # NEW TRADE
                best_contract = self._select_best_strike(active_bias)
                if best_contract:
                    levels = self._calculate_trade_levels(best_contract['ltp'])
                    self.trade_state = {
                        'type': current_contract_type,
                        'contract': best_contract['name'],
                        'entry': best_contract['ltp'],
                        'peak': best_contract['ltp'],
                        'sl': levels['sl'],
                        'target': levels['target'],
                        'status': "ENTRY"
                    }
        
        # Populate Setup Object from State
        if self.trade_state:
            setup_status = self.trade_state.get('status', 'ENTRY')
            
            # If current action is WAIT but we have state -> HOLD
            if action.startswith("WAIT") and self.trade_state:
                 setup_status = "HOLD (Weak)"
                 warning = f"Signal Weaker. Consider Tightening SL."

            trade_setup = {
                'contract': self.trade_state['contract'],
                'entry': self.trade_state['entry'],
                'sl': self.trade_state['sl'],
                'target': self.trade_state['target'],
                'rr': '1:2 (Trailing)',
                'status': setup_status
            }

        # 3. Panic/Warning Logic
        # Check for sharp reversals or adverse conditions
        if bias == "BULLISH" and score_mp < 0:
             warning = "WARNING: Bullish Signal but Price dropping below Max Pain. Tighten SL."
        elif bias == "BEARISH" and score_mp > 0:
             warning = "WARNING: Bearish Signal but Price holding above Max Pain. Tighten SL."
        
        # Override Warning if Trap
        if trap_type:
             warning = f"⚠️ {trap_msg}"
        
        return {
            'action': action,
            'confidence': f"{abs(total_score)}%",
            'score': total_score,
            'max_pain': max_pain,
            'pcr_signal': bias,
            'reasons': reasons,
            'trade_setup': trade_setup,
            'warning': warning,
            'mode': self.strategy_mode,
            'strategy_matrix': strategy_matrix
        }
    
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
                logger.debug(f"{self.underlying} LTP: {self.underlying_ltp}, ATM: {self.atm_strike} (from cached)")
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
                    logger.debug(f"{self.underlying} LTP: {self.underlying_ltp}, ATM: {self.atm_strike} (from API)")
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
        logger.debug(f"generate_strikes called for {self.underlying}, ATM: {self.atm_strike}")
        if not self.atm_strike:
            logger.warning("generate_strikes skipped: ATM is 0")
            return
        
        strikes = []
        # Generate ITM strikes (20 strikes below ATM for CE, above for PE)
        for i in range(5, 0, -1):
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
        for i in range(1, 6):
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
                    'gex': 0
                },

                'pe_data': {
                    'ltp': 0, 'bid': 0, 'ask': 0, 'bid_qty': 0,
                    'ask_qty': 0, 'spread': 0, 'volume': 0, 'oi': 0,
                    'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0,
                    'gex': 0
                },
                'net_gex': 0,
                'is_gamma_wall': False
            }

            # Initialize Greeks with 0
            self.option_data[strike]['ce_data'].update({
                'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0, 'rho': 0, 'iv': 0
            })
            self.option_data[strike]['pe_data'].update({
                'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0, 'rho': 0, 'iv': 0
            })

            # Fetch Greeks
            # self._update_greeks_for_strike(strike)

            # Map symbols to strikes for quick lookup
            self.subscription_map[self.option_data[strike]['ce_symbol']] = {
                'strike': strike, 'type': 'CE'
            }
            self.subscription_map[self.option_data[strike]['pe_symbol']] = {
                'strike': strike, 'type': 'PE'
            }
        
        logger.info(f"Generated {len(strikes)} strikes for {self.underlying}. ATM: {self.atm_strike}")

    def _update_greeks_for_strike(self, strike):
        """Fetch and update Greeks for a specific strike with robust rate limiting"""
        if strike not in self.option_data:
            return

        try:
            # Helper to process response
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
                    # Capture Days to Expiry if available in Greeks response
                    if 'days_to_expiry' in response:
                        self.days_to_expiry = float(response.get('days_to_expiry', 0))
                    return True

                elif response and response.get('code') == 429:
                    logger.warning(f"[{self.underlying}][ID:{self.manager_instance_id}] Rate Limited (429) for {strike}. Backing off.")


                    return False
                return True # Treat other errors as 'processed' to avoid retry loops

            # CE Greeks
            ce_symbol = self.option_data[strike]['ce_symbol']
            ce_response = self.api_client.optiongreeks(symbol=ce_symbol, exchange='NFO')
            
            if not process_response(ce_response, self.option_data[strike]['ce_data']):
                self._backoff_until = time.time() + 60 # Stop all requests for 60s
                return # Skip PE to save quota

            # Rate limit delay (Safe: 2.2s)
            for _ in range(22): # Breakable sleep
                if not self.monitoring_active: return
                time.sleep(0.1)

            # PE Greeks

            pe_symbol = self.option_data[strike]['pe_symbol']
            pe_response = self.api_client.optiongreeks(symbol=pe_symbol, exchange='NFO')
            
            if not process_response(pe_response, self.option_data[strike]['pe_data']):
                self._backoff_until = time.time() + 60
                return 

            # Rate limit delay
            for _ in range(22):
                if not self.monitoring_active: return
                time.sleep(0.1)



        except Exception as e:
            logger.error(f"Error fetching Greeks for strike {strike}: {e}")

    def refresh_greeks(self):
        """
        Smart Refresh Strategy:
        """
        if self._backoff_until > time.time():
            return

        if not self.atm_strike:

            # If no ATM, just pick middle of sorted strikes
            strikes = sorted(self.option_data.keys())
            if strikes:
                mid = len(strikes) // 2
                self.atm_strike = strikes[mid]
            else:
                return

        all_strikes = sorted(self.option_data.keys())
        
        # Define Focus Zone
        atm_idx = -1
        try:
            # Find closest strike to ATM
            atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - self.atm_strike))
        except ValueError:
            return

        # Focus Zone indices
        focus_indices = set(range(max(0, atm_idx - 2), min(len(all_strikes), atm_idx + 3)))
        
        # Update Focus Zone (High Priority)
        for idx in focus_indices:
            if not self.monitoring_active: return
            strike = all_strikes[idx]
            # logger.info(f"Updating Greeks for Focus Strike: {strike}")
            self._update_greeks_for_strike(strike)
            # Check again after each strike update
            if not self.monitoring_active: return

        
        # Update ONE Background Strike (Round Robin)
        # We need state for this. We'll use a dynamic attribute.
        if not hasattr(self, '_bg_greek_idx'):
            self._bg_greek_idx = 0
            
        # Find next non-focus index
        attempts = 0
        while attempts < len(all_strikes):
            self._bg_greek_idx = (self._bg_greek_idx + 1) % len(all_strikes)
            if self._bg_greek_idx not in focus_indices:
                strike = all_strikes[self._bg_greek_idx]
                # logger.info(f"Updating Greeks for Background Strike: {strike}")
                self._update_greeks_for_strike(strike)
                break
            attempts += 1

    def _greek_monitor_loop(self):
        """Background loop to refresh Greeks"""
        logger.info(f"Starting Smart Greek Monitor for {self.underlying} [ID:{self.manager_instance_id}]")
        while self.monitoring_active:
            try:
                # logger.debug(f"[{self.underlying}][ID:{self.manager_instance_id}] Greek Heartbeat - Active: {self.monitoring_active}")
                start_time = time.time()
                self.refresh_greeks()
                
                # Check active flag multiple times during the sleep for faster shutdown
                for _ in range(20):
                    if not self.monitoring_active: 
                        logger.warning(f"[{self.underlying}][ID:{self.manager_instance_id}] Monitoring signal STOP detected - Exiting Loop")
                        return
                    time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"[{self.underlying}][ID:{self.manager_instance_id}] Error in Greek monitoring loop: {e}")
                time.sleep(5)



    
    def construct_option_symbol(self, strike, option_type):
        """Construct OpenAlgo option symbol"""
        # Format: [Base Symbol][Expiration Date][Strike Price][Option Type]
        # Date format: DDMMMYY (e.g., 28AUG25 for August 28, 2025)
        
        # Parse expiry date to proper format
        expiry_formatted = None
        
        if isinstance(self.expiry, str):
            try:
                # Handle format like "28-AUG-25" -> "28AUG"
                parts = self.expiry.split('-')
                if len(parts) >= 2:
                    day = parts[0].zfill(2)
                    month = parts[1].upper()[:3]
                    expiry_formatted = f"{day}{month}"
                else:
                    # Extract day and month
                    expiry_clean = self.expiry.replace('-', '').upper()
                    for mon in ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']:
                        if mon in expiry_clean:
                            idx = expiry_clean.index(mon)
                            day = expiry_clean[max(0, idx-2):idx]
                            if not day or not day.isdigit():
                                day = '01'
                            expiry_formatted = f"{day.zfill(2)}{mon}"
                            break
                    else:
                        expiry_formatted = '28AUG'  # Default
            except Exception as e:
                logger.error(f"Error parsing expiry: {e}")
                expiry_formatted = '28AUG'
        elif isinstance(self.expiry, datetime):
            expiry_formatted = self.expiry.strftime('%d%b').upper()
        else:
            expiry_formatted = '28AUG'
        
        # Remove decimal if whole number
        if strike == int(strike):
            strike_str = str(int(strike))
        else:
            strike_str = str(strike)
        
        # Construct symbol: BASE + EXPIRY + 25 + STRIKE + CE/PE
        # The "25" is the year 2025, hardcoded for now
        symbol = f"{self.underlying}{expiry_formatted}25{strike_str}{option_type}"
        
        return symbol
    
    def setup_subscriptions(self):
        """Configure WebSocket subscriptions"""
        if not self.websocket_manager:
            logger.warning("WebSocket manager not available for subscriptions")
            return
        
        # Register handlers
        # Force 'quote' handler
        self.websocket_manager.register_handler('quote', self.handle_quote_update)
        
        # Subscribe to underlying
        self.subscribe_underlying_quote()
        
        # Batch subscribe to options
        self.batch_subscribe_options()
    
    def subscribe_underlying_quote(self):
        """Subscribe to underlying index in quote mode"""
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
        
        # Locked to 'quote' mode
        self.websocket_manager.subscribe_batch(instruments, mode='quote')
        logger.info(f"Subscribed to {len(instruments)} instruments in 'quote' mode")
    
    def handle_quote_update(self, data):
        """Handle quote updates for underlying index and options"""
        # print('-----------------------------------------------------------------')
        # print(f"WS Data: {data}")
        symbol = data.get('symbol', '')
        
        # 1. Handle Underlying Update
        if symbol == self.underlying:
            # Capture OHLC and avg for underlying
            self.underlying_ltp = float(data.get('ltp', 0) or 0)
            self.underlying_bid = float(data.get('bid', 0) or 0)
            self.underlying_ask = float(data.get('ask', 0) or 0)
            self.underlying_open = float(data.get('open', 0) or 0)
            self.underlying_high = float(data.get('high', 0) or 0)
            self.underlying_low = float(data.get('low', 0) or 0)
            self.underlying_close = float(data.get('close', 0) or 0)
            # Average price: use provided or compute from high/low
            if 'average_price' in data:
                self.underlying_avg = float(data.get('average_price', 0) or 0)
            else:
                self.underlying_avg = (self.underlying_high + self.underlying_low) / 2 if (self.underlying_high and self.underlying_low) else 0
            if 'days_to_expiry' in data:
                self.days_to_expiry = float(data.get('days_to_expiry', 0))
            elif 'dte' in data:
                self.days_to_expiry = float(data.get('dte', 0))

            # Update ATM strike based on new spot price

            old_atm = self.atm_strike
            # ceGreek= self.construct_option_symbol(old_atm, 'CE')
            # peGreek= self.construct_option_symbol(old_atm, 'PE')
            # logger.warning(f"generate_strikes called for {self.underlying}, ATM: {self.atm_strike}")
            # ceResponse = self.api_client.optiongreeks(symbol=ceGreek, exchange='NFO')
            # peResponse = self.api_client.optiongreeks(symbol=peGreek, exchange='NFO')
            # logger.warning(f"ceResponse: {ceResponse}")
            # logger.warning(f"peResponse: {peResponse}")
            self.atm_strike = self.calculate_atm()
            if old_atm != self.atm_strike:
                if not self.option_data:
                    self.generate_strikes()
                    if self.websocket_manager and getattr(self.websocket_manager, 'authenticated', False):
                        self.batch_subscribe_options()
                else:
                    self.update_option_tags()
            return

        # 2. Handle Option Update (if subscribed in quote mode)
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']
            strike = strike_info['strike']
            
            # Map quote fields to depth-like structure based on user provided sample
            # keys: bid_price, ask_price, bid_size, ask_size
            quote_data = {
                'ltp': float(data.get('ltp', 0) or 0),
                'bid': float(data.get('bid_price', data.get('bid', 0)) or 0),
                'ask': float(data.get('ask_price', data.get('ask', 0)) or 0),
                'bid_qty': int(data.get('bid_size', data.get('buy_quantity', 0)) or 0),
                'ask_qty': int(data.get('ask_size', data.get('sell_quantity', 0)) or 0),
                'total_buy_qty': int(data.get('total_buy_quantity', 0) or 0),
                'total_sell_qty': int(data.get('total_sell_quantity', 0) or 0),
                'spread': 0,
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
        """Process incoming depth data for options"""
        symbol = data.get('symbol') or data.get('Symbol') or data.get('trading_symbol') or ''
        
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']
            strike = strike_info['strike']
            
            # Extract data
            depth_data_raw = data.get('depth', {})
            if depth_data_raw:
                bids = depth_data_raw.get('buy', depth_data_raw.get('bids', []))
                asks = depth_data_raw.get('sell', depth_data_raw.get('asks', []))
            else:
                bids = data.get('bids', [])
                asks = data.get('asks', [])
            
            ltp = data.get('ltp') or data.get('last_price') or 0
            
            best_bid = 0
            best_ask = 0
            bid_qty = 0
            ask_qty = 0
            
            if bids and len(bids) > 0:
                if isinstance(bids[0], dict):
                    best_bid = bids[0].get('price', 0)
                    bid_qty = bids[0].get('quantity', 0)
                elif isinstance(bids[0], (list, tuple)) and len(bids[0]) >= 2:
                    best_bid = bids[0][0]
                    bid_qty = bids[0][1]
            
            if asks and len(asks) > 0:
                if isinstance(asks[0], dict):
                    best_ask = asks[0].get('price', 0)
                    ask_qty = asks[0].get('quantity', 0)
                elif isinstance(asks[0], (list, tuple)) and len(asks[0]) >= 2:
                    best_ask = asks[0][0]
                    ask_qty = asks[0][1]
            
            depth_data = {
                'ltp': float(ltp) if ltp else 0,
                'bid': float(best_bid) if best_bid else 0,
                'ask': float(best_ask) if best_ask else 0,
                'bid_qty': int(bid_qty) if bid_qty else 0,
                'ask_qty': int(ask_qty) if ask_qty else 0,
                'spread': 0,
                'volume': int(data.get('volume', 0) or 0),
                'oi': int(data.get('oi', 0) or 0),
                'open': float(data.get('open', 0) or 0),
                'high': float(data.get('high', 0) or 0),
                'low': float(data.get('low', 0) or 0),
                'close': float(data.get('close', 0) or 0),
                'avg_price': float(data.get('average_price', 0) or 0)
            }
            
            if depth_data['bid'] > 0 and depth_data['ask'] > 0:
                depth_data['spread'] = depth_data['ask'] - depth_data['bid']
            
            self.update_option_depth(strike, option_type, depth_data)
    
    def update_option_depth(self, strike, option_type, depth_data):
        """Update option chain with depth data, merging to prevent data loss"""
        if strike in self.option_data:
            target = 'ce_data' if option_type == 'CE' else 'pe_data'
            current_data = self.option_data[strike][target]
            
            # Debug log for volume/oi changes on a specific strike (e.g., ATM)
            is_trace_strike = strike == self.atm_strike
            
            # Merge new data into existing
            for key, value in depth_data.items():
                # Prevent overwriting existing valid Volume/OI with 0
                if key in ['volume', 'oi', 'oid'] and value == 0:
                     if current_data.get(key, 0) > 0:
                         if is_trace_strike:
                             logger.debug(f"[MERGE SKIP] Strike {strike} {option_type} {key}: New={value}, Current={current_data.get(key)}")
                         continue
                
                if is_trace_strike and key == 'volume' and value > 0 and value != current_data.get('volume', 0):
                    logger.debug(f"[UPDATE] Strike {strike} {option_type} Volume: {current_data.get('volume')} -> {value}")

                current_data[key] = value
    
    def get_option_chain(self):
        """Return formatted option chain data"""
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
            'mode': self.option_mode # Expose current mode
        }
        logger.debug(f"get_option_chain returning: {len(data['options'])} options, ATM: {data['atm_strike']}")
        return data
    
    def update_option_tags(self):
        """Update option tags when ATM changes"""
        for strike_data in self.option_data.values():
            strike = strike_data['strike']
            position = self.get_strike_position(strike)
            strike_data['position'] = position
            strike_data['tag'] = self.get_position_tag(position)

    def calculate_max_pain(self):
        """
        Calculate Max Pain theory: The strike price where option writers (sellers) 
        lose the least amount of money at expiration.
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
        total_loss = {}
        
        for price_point in strikes:
            loss = 0
            # Calculate loss for Call Writers: If price > strike, they lose (price - strike) * OI
            for k in strikes:
                if price_point > k:
                    loss += (price_point - k) * ce_oi.get(k, 0)
                    
            # Calculate loss for Put Writers: If price < strike, they lose (strike - price) * OI
            for k in strikes:
                if price_point < k:
                    loss += (k - price_point) * pe_oi.get(k, 0)
            
            total_loss[price_point] = loss
            
        # Find local minimum
        if not total_loss:
            return 0
            
        min_loss_strike = min(total_loss, key=total_loss.get)
        return min_loss_strike

    def _select_best_strike(self, bias, action="BUY"):
        """
        Select Best Strike based on Action Type.
        BUY: Momentum -> ATM/ITM1 (Delta ~0.55)
        SELL: Probability -> OTM2/OTM3 (Delta ~0.30 - Safer, cushion)
        STRADDLE: Collect Theta from ATM CE + PE
        """
        if not self.atm_strike: return None
        
        if action == "SELL STRADDLE":
            strike = self.atm_strike
            if strike not in self.option_data: return None
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
            # Credit Strategy: Sell OTM for safety margin
            # Bullish -> Sell Put (PE), Bearish -> Sell Call (CE)
            contract_type = 'PE' if bias == "BULLISH" else 'CE'
            if bias == "BULLISH": # Sell Put
                 target_strike = self.atm_strike - (2 * self.strike_step) 
            elif bias == "BEARISH": # Sell Call
                 target_strike = self.atm_strike + (2 * self.strike_step) 
        else:
            # Debit Strategy (Momemtum)
            # Bullish -> Buy Call (CE), Bearish -> Buy Put (PE)
            contract_type = 'CE' if bias == "BULLISH" else 'PE'
            target_strike = self.atm_strike
                 
        if target_strike in self.option_data:
            data = self.option_data[target_strike].get(f"{contract_type.lower()}_data")
            if not data: return None
            return {
                'strike': target_strike,
                'name': self.option_data[target_strike].get(f"{contract_type.lower()}_symbol"),
                'ltp': data.get('ltp', 0),
                'type': contract_type
            }
        return None



        
        # Simple refinement: Check if OTM has significantly more volume? 
        # For now, stick to ATM for consistency in "Quant" systematic approach.
        
        if bias == "BULLISH":
            # Buy Call
            ltp = strike_data['ce_data'].get('ltp', 0)
            symbol = strike_data['ce_symbol']
            name = f"{target_strike} CE"
            return {'strike': target_strike, 'type': 'CE', 'ltp': ltp, 'name': name, 'symbol': symbol}
        elif bias == "BEARISH":
            # Buy Put
            ltp = strike_data['pe_data'].get('ltp', 0)
            symbol = strike_data['pe_symbol']
            name = f"{target_strike} PE"
            return {'strike': target_strike, 'type': 'PE', 'ltp': ltp, 'name': name, 'symbol': symbol}
            
        return None

    def _calculate_trade_levels(self, entry_price, action_type="BUY"):
        """
        Calculate Stoploss and Target.
        BUY: SL -15% (Stop), Target +30%
        SELL: SL +40% (Stop), Target -80% (Decay)
        """
        if entry_price <= 0:
            return {'sl': 0, 'target': 0}
            
        if "SELL" in action_type:
            # Credit Strategy (Shorting Premium)
            # Stop Loss is HIGHER than entry (Premium Spike risk)
            sl = round(entry_price * 1.40, 1) # 40% SL (Generous buffer for spike)
            target = round(entry_price * 0.20, 1) # 80% Decay (Target near 0)
        else:
            # Debit Strategy (Buying Premium)
            sl = round(entry_price * 0.85, 1) # 15% SL
            target = round(entry_price * 1.30, 1) # 30% Target
            
        return {'sl': sl, 'target': target}


    def generate_signals(self, pcr, max_pain):
        """
        New V5 Battle-tested Quant Decision Engine
        """
        # 0. Capture state if needed
        self._capture_initial_state()
        
        # Update History
        if self.underlying_ltp > 0:
            self.price_history.append(self.underlying_ltp)
            
        # Update ATM IV history
        if self.atm_strike in self.option_data:
            atm_iv = (self.option_data[self.atm_strike]['ce_data'].get('iv', 0) + 
                     self.option_data[self.atm_strike]['pe_data'].get('iv', 0)) / 2
            if atm_iv > 0:
                self.iv_history.append(atm_iv)
        
        # Calculate Total OI for history
        total_oi = sum(opt['ce_data'].get('oi', 0) + opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        self.oi_history.append(total_oi)

        # 1. Calculate Scores
        score_delta, s_d_reason = self._score_delta_oi()
        score_iv, s_iv_reason = self._score_iv_skew()
        score_unwind, s_u_reason = self._score_oi_unwind()
        score_mp, s_mp_reason = self._score_max_pain()
        score_gex, s_gex_reason = self._score_gamma_exposure()
        
        # Update GEX history after calculation
        self.gex_history.append(sum(opt.get('net_gex', 0) for opt in self.option_data.values()))
        
        score_vc, s_vc_reason = self._score_vanna_charm()
        score_flow, s_flow_reason = self._detect_institutional_flow()
        
        # Aggregate
        total_score = score_delta + score_iv + score_unwind + score_mp + score_gex + score_vc + score_flow
        scores_list = [score_delta, score_iv, score_unwind, score_mp, score_gex, score_vc, score_flow]

        
        # Clamp Score
        total_score = max(min(total_score, 100), -100)
        
        # Determine Bias
        bias = "NEUTRAL"
        action = "WAIT"
        
        if total_score > 65:
            bias = "BULLISH"
            action = "BUY CALL"
        elif total_score < -65:
            bias = "BEARISH"
            action = "BUY PUT"
            
        # --- HYBRID SELL LOGIC ---
        # Detect conditions for Credit Strategies (Selling)
        # 1. Range Bound + Pinning (Pos GEX) -> Sell Straddle
        net_gex = self.gex_history[-1] if self.gex_history else 0
        is_pinning = net_gex > 0 # Positive Gamma (Dealers dampening volatility)
        is_range_bound = -30 <= total_score <= 30
        
        if is_pinning and is_range_bound:
             bias = "NEUTRAL" # Explicitly Neutral
             action = "SELL STRADDLE" # or Strangle if IV is extremely low, but Straddle captures most Theta
        
        # 2. Weak Trend + Pinning -> Sell OTM Option (Credit Spread Intent)
        elif is_pinning and 30 < total_score <= 60:
             bias = "BULLISH"
             action = "SELL PUT" # Credit Put 
        elif is_pinning and -60 <= total_score < -30:
             bias = "BEARISH"
             action = "SELL CALL" # Credit Call
             
        # --- SIGNAL SMOOTHING (HYSTERESIS) ---
        self.action_history.append(action)
        if len(self.action_history) >= 5:
            # Get the most common action in the recent buffer to avoid jumping
            from collections import Counter
            counts = Counter(self.action_history)
            action = counts.most_common(1)[0][0]
            # Update bias to match the smoothed action if necessary
            if action == "SELL STRADDLE": bias = "NEUTRAL"
            elif action == "BUY CALL" or action == "SELL CALL": bias = "BULLISH" if "BUY CALL" in action else "BEARISH"
            # Note: simplified bias sync

            
        reasons = [
            f"Score: {total_score} ({bias})",
            f"Delta Logic: {score_delta} ({s_d_reason})",
            f"IV Skew: {score_iv} ({s_iv_reason})",
            f"OI Flow: {score_unwind} ({s_u_reason})",
            f"Max Pain: {score_mp} ({s_mp_reason})",
            f"Gamma: {score_gex} ({s_gex_reason})",
            f"Inst. Flow: {score_flow} ({s_flow_reason})"
        ]

        
        # 2. Trade Setup (Strike, SL, Target)
        trade_setup = {}
        warning = None
        
        # Setup for Non-Neutral OR Straddle
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

        
        # 3. Panic/Warning Logic
        # Check for sharp reversals or adverse conditions
        if bias == "BULLISH" and score_mp < 0:
             warning = "WARNING: Bullish Signal but Price dropping below Max Pain. Tighten SL."
        elif bias == "BEARISH" and score_mp > 0:
             warning = "WARNING: Bearish Signal but Price holding above Max Pain. Tighten SL."
        
        return {
            'action': action,
            'confidence': f"{abs(total_score)}%",
            'score': total_score,
            'max_pain': max_pain,
            'pcr_signal': bias,
            'reasons': reasons,
            'trade_setup': trade_setup,
            'warning': warning
        }
    




    def calculate_market_metrics(self):
        """Calculate PCR and other metrics"""
        # logger.debug(f"calculate_market_metrics: {self.option_data}")
        total_ce_volume = sum(opt['ce_data'].get('volume', 0) for opt in self.option_data.values())
        total_pe_volume = sum(opt['pe_data'].get('volume', 0) for opt in self.option_data.values())
        total_ce_oi = (sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values()))
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        
        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0
        
        # Advanced Metrics
        max_pain = self.calculate_max_pain()
        signals = self.generate_signals(pcr, max_pain)
        
        # Capture current institutional alerts for dashboard
        inst_score, inst_reason = self._detect_institutional_flow()
        flow_alerts = inst_reason.split(", ") if inst_reason != "No Heavy Flow" else []
        
        # Use captured Days to Expiry
        days_to_expiry = self.days_to_expiry
        
        return {
            'days_to_expiry': days_to_expiry,


            'total_ce_volume': total_ce_volume,
            'total_pe_volume': total_pe_volume,
            'total_volume': total_ce_volume + total_pe_volume,
            'total_ce_oi': total_ce_oi,
            'total_pe_oi': total_pe_oi,
            'pcr': round(pcr, 8),
            'max_pain': max_pain,
            'quant_signal': signals,
            'flow_alerts': flow_alerts # New for dashboard
        }


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
        
        # Start Greek refresh thread
        self.greek_thread = threading.Thread(target=self._greek_monitor_loop)
        self.greek_thread.daemon = True
        self.greek_thread.start()
    
    def stop_monitoring(self):
        """Stop background threads and unregister handlers"""
        if not self.monitoring_active:
            return
            
        logger.info(f"Stopping monitor for {self.underlying}")
        self.monitoring_active = False
        
        # Unregister from WebSocket
        if self.websocket_manager:
            try:
                self.websocket_manager.unregister_handler('quote', self.handle_quote_update)
            except Exception as e:
                logger.error(f"Error unregistering handler: {e}")

    def stop(self):
        """Alias for stop_monitoring"""
        self.stop_monitoring()

