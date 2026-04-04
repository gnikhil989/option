"""
Option Chain Manager Module V4
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


class OptionChainCacheV4:
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


class OptionChainManagerV4:
    """
    Manager class for option chain with configurable subscription mode
    Handles both LTP and bid/ask data for order management
    """
    
    def __init__(self, underlying, expiry, websocket_manager=None):
        """
        Initialize OptionChainManagerV4
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
        self.cache = OptionChainCacheV4()
        self.monitoring_active = False
        self.initialized = False
        self.manager_id = f"{underlying}_{expiry}_v4"
        self.option_mode = 'quote'  # Forced Quote Mode
        logger.info(f"Initialized OptionChainManagerV4 for {underlying} (Quote Mode Only)")
    
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
                    'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0
                },
                'pe_data': {
                    'ltp': 0, 'bid': 0, 'ask': 0, 'bid_qty': 0,
                    'ask_qty': 0, 'spread': 0, 'volume': 0, 'oi': 0,
                    'open': 0, 'high': 0, 'low': 0, 'close': 0, 'avg_price': 0
                }
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
        """Fetch and update Greeks for a specific strike with rate limiting"""
        if strike not in self.option_data:
            return

        try:
            # CE Greeks
            ce_symbol = self.option_data[strike]['ce_symbol']
            logger.info(f"Fetching Greeks for {ce_symbol}")
            ce_response = self.api_client.optiongreeks(symbol=ce_symbol, exchange='NFO')
            
            if ce_response and ce_response.get('status') == 'success':
                greeks = ce_response.get('greeks', {})
                self.option_data[strike]['ce_data'].update({
                    'delta': float(greeks.get('delta', 0) or 0),
                    'gamma': float(greeks.get('gamma', 0) or 0),
                    'theta': float(greeks.get('theta', 0) or 0),
                    'vega': float(greeks.get('vega', 0) or 0),
                    'rho': float(greeks.get('rho', 0) or 0),
                    'iv': float(ce_response.get('implied_volatility', 0) or 0)
                })
            
            # Rate limit delay (30 requests/min = 1 req / 2 sec)
            for _ in range(21):
                if not self.monitoring_active: return
                time.sleep(0.1)


            # PE Greeks
            pe_symbol = self.option_data[strike]['pe_symbol']
            logger.info(f"Fetching Greeks for {pe_symbol}")
            pe_response = self.api_client.optiongreeks(symbol=pe_symbol, exchange='NFO')
            
            if pe_response and pe_response.get('status') == 'success':
                greeks = pe_response.get('greeks', {})
                self.option_data[strike]['pe_data'].update({
                    'delta': float(greeks.get('delta', 0) or 0),
                    'gamma': float(greeks.get('gamma', 0) or 0),
                    'theta': float(greeks.get('theta', 0) or 0),
                    'vega': float(greeks.get('vega', 0) or 0),
                    'rho': float(greeks.get('rho', 0) or 0),
                    'iv': float(pe_response.get('implied_volatility', 0) or 0)
                })

            # Rate limit delay
            for _ in range(21):
                if not self.monitoring_active: return
                time.sleep(0.1)


        except Exception as e:
            logger.error(f"Error fetching Greeks for strike {strike}: {e}")

    def refresh_greeks(self):
        """Refresh Greeks for all strikes"""
        # Create a copy of keys to avoid modification during iteration issues (though strikes shouldn't change often)
        strikes = list(self.option_data.keys())
        
        # Sort strikes to update ATM/Near-ATM first (better UX)
        if self.atm_strike:
            strikes.sort(key=lambda x: abs(x - self.atm_strike))

        for strike in strikes:
            if not self.monitoring_active: # Break early if stopped
                break
            self._update_greeks_for_strike(strike)
            # Small sleep to be nice to the API? 
            # With 40 strikes (80 calls), we don't want to flood too hard.
            # But we want it fast. Let's rely on sequential execution speed.

    def _greek_monitor_loop(self):
        """Background loop to refresh Greeks periodically"""
        logger.info(f"Starting Greek monitoring loop for {self.underlying}")
        while self.monitoring_active:
            try:
                start_time = time.time()
                self.refresh_greeks()
                elapsed = time.time() - start_time
                logger.debug(f"Greeks refreshed in {elapsed:.2f}s")
                
                # Wait for 10 seconds before next update, but check active status
                sleep_time = 10
                for _ in range(sleep_time):
                    if not self.monitoring_active:
                        break
                    time.sleep(1)
            except Exception as e:
                logger.error(f"Error in Greek monitoring loop: {e}")
                time.sleep(5) # Backoff on error
    
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
    
    def calculate_market_metrics(self):
        """Calculate PCR and other metrics"""
        # logger.debug(f"calculate_market_metrics: {self.option_data}")
        total_ce_volume = sum(opt['ce_data'].get('volume', 0) for opt in self.option_data.values())
        total_pe_volume = sum(opt['pe_data'].get('volume', 0) for opt in self.option_data.values())
        total_ce_oi = (sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values()))
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        
        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0
        
        return {
            'total_ce_volume': total_ce_volume,
            'total_pe_volume': total_pe_volume,
            'total_volume': total_ce_volume + total_pe_volume,
            'total_ce_oi': total_ce_oi,
            'total_pe_oi': total_pe_oi,
            'pcr': round(pcr, 8)
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
        self.monitoring_active = False
