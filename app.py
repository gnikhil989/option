from flask import Flask, render_template, request, jsonify, Response, redirect, url_for
from config import Config
from utils.option_chain import OptionChainManager
from utils.option_chain_v2 import OptionChainManagerV2
from utils.option_chain_v3 import OptionChainManagerV3
from utils.option_chain_v4 import OptionChainManagerV4
from utils.option_chain_v5 import OptionChainManagerV5
from utils.option_chain_v6 import OptionChainManagerV6
from utils.option_chain_v7 import OptionChainManagerV7
from utils.option_chain_v7_adaptive import OptionChainManagerV7Adaptive
from utils.option_chain_v8_antiwhipsaw import OptionChainManagerV8AntiWhipsaw
from utils.option_chain_v9_safe import OptionChainManagerV9Safe
from utils.option_chain_v10_safe import OptionChainManagerV10Safe
from utils.openalgo_client import ExtendedOpenAlgoAPI
from utils.websocket_manager import ProfessionalWebSocketManager
import json
import time
import threading
import logging
import os
import signal
import atexit

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Reduce verbosity of third-party loggers
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

app = Flask(__name__)
app.config.from_object(Config)

# Log config to verify loading
logger.info(f"Config loaded: HOST={app.config.get('OPENALGO_HOST')}, WS={app.config.get('OPENALGO_WS_URL')}")
logger.info(f"API Key present: {bool(app.config.get('OPENALGO_API_KEY'))}")

# Global instances
active_managers = {}
websocket_managers = {}
shared_websocket_manager = None
manager_lock = threading.Lock()


_shutdown_done = False  # Prevent double shutdown (signal + atexit)

def graceful_shutdown(signum=None, frame=None):
    """Close all open trades and stop all managers before shutting down.
    Called on Ctrl+C (SIGINT) or program exit.
    """
    global _shutdown_done
    if _shutdown_done:
        # Second Ctrl+C - force exit immediately
        print("\n[APP] Force exit.")
        os._exit(1)

    _shutdown_done = True
    logger.warning("[APP] Graceful shutdown initiated...")
    print(f"\n{'='*60}\n[APP] Shutting down - closing all open trades...\n{'='*60}")

    with manager_lock:
        for key, manager in active_managers.items():
            if hasattr(manager, 'close_all_open_trades'):
                try:
                    manager.close_all_open_trades(reason="PROGRAM SHUTDOWN (Ctrl+C)")
                except Exception as e:
                    logger.error(f"[APP] Error closing trades for {key}: {e}")

            if hasattr(manager, 'stop_monitoring'):
                try:
                    manager.stop_monitoring()
                except Exception:
                    pass

    logger.warning("[APP] All trades closed. Exiting.")
    print(f"{'='*60}\n[APP] Shutdown complete.\n{'='*60}\n")

    # Actually exit the process
    if signum is not None:
        # Called from signal handler - exit
        import sys
        sys.exit(0)


# Register shutdown handlers
signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)
atexit.register(graceful_shutdown)


def get_manager_key(underlying, expiry, version='v5'):
    """Generate a normalized key for manager lookup"""
    u = str(underlying).upper().strip()
    e = str(expiry).upper().strip() if expiry else "NONE"
    v = str(version).lower().strip()
    return f"{u}_{e}_{v}"

def get_manager_key_v6(underlying, expiry):
    """Generate a normalized key for manager lookup"""
    u = str(underlying).upper().strip()
    e = str(expiry).upper().strip() if expiry else "NONE"
    return f"{u}_{e}_v6"

def get_manager_key_v7(underlying, expiry):
    """Generate a normalized key for manager lookup"""
    u = str(underlying).upper().strip()
    e = str(expiry).upper().strip() if expiry else "NONE"
    return f"{u}_{e}_v7_adaptive"
def get_manager_key_v8(underlying, expiry):
    """Generate a normalized key for manager lookup"""
    u = str(underlying).upper().strip()
    e = str(expiry).upper().strip() if expiry else "NONE"
    return f"{u}_{e}_v8_antiwhipsaw"
    
def get_manager_key_v9(underlying, expiry):
    """Generate a normalized key for manager lookup"""
    u = str(underlying).upper().strip()
    e = str(expiry).upper().strip() if expiry else "NONE"
    return f"{u}_{e}_v9_safe"
def get_manager_key_v10(underlying, expiry):
    """Generate a normalized key for manager lookup"""
    u = str(underlying).upper().strip()
    e = str(expiry).upper().strip() if expiry else "NONE"
    return f"{u}_{e}_v10_safe"

def cleanup_inactive_managers(active_key):
    """
    Shut down and remove managers of the SAME VERSION that aren't the active one.
    This prevents background threads for old symbols from consuming API quota (429 fix).

    Version-aware: V10 cleanup only stops other V10 managers, not V9 (and vice versa).
    This prevents cross-version interference where starting a V10 session kills V9.
    """
    # Extract version suffix from key (e.g. "NIFTY_28-MAR-26_v10_safe" -> "v10_safe")
    active_version = ''
    if '_v10_' in active_key:
        active_version = 'v10'
    elif '_v9_' in active_key:
        active_version = 'v9'
    elif '_v7_' in active_key:
        active_version = 'v7'

    keys_to_stop = []
    for k in active_managers.keys():
        if k == active_key:
            continue
        # Only cleanup managers of the SAME version
        if active_version and f'_{active_version}_' in k:
            keys_to_stop.append(k)
        elif not active_version:
            # Fallback: if version can't be determined, stop all others (legacy behavior)
            keys_to_stop.append(k)

    if keys_to_stop:
        logger.warning(f"[CLEANUP] Stopping same-version ({active_version}) managers: {keys_to_stop}")

    for k in keys_to_stop:
        try:
            mgr = active_managers.pop(k, None)
            if mgr:
                if hasattr(mgr, 'stop_monitoring'):
                    mgr.stop_monitoring()
                elif hasattr(mgr, 'stop'):
                    mgr.stop()
                logger.warning(f"DECOMMISSIONED manager for {k}")
        except Exception as e:
            logger.error(f"Error shutting down manager {k}: {e}")




def get_api_client():
    """Create OpenAlgo API client from config"""
    return ExtendedOpenAlgoAPI(
        api_key=app.config['OPENALGO_API_KEY'],
        host=app.config['OPENALGO_HOST']
    )

def get_or_create_websocket_manager(underlying):
    """Get or create a WebSocket manager for the underlying"""
    global shared_websocket_manager
    
    # Use shared manager if available and active
    if shared_websocket_manager and shared_websocket_manager.active:
        return shared_websocket_manager
        
    # Create new manager
    logger.warning("WebSocket manager inactive or missing. Creating NEW instance.")
    ws_manager = ProfessionalWebSocketManager()
    ws_manager.connect(
        ws_url=app.config['OPENALGO_WS_URL'],
        api_key=app.config['OPENALGO_API_KEY']
    )
    
    # Wait for connection
    time.sleep(1)
    
    if ws_manager.active:
        shared_websocket_manager = ws_manager
        # Notify all active managers about the new WebSocket manager
        logger.info(f"Broadcasting new WebSocket manager to {len(active_managers)} active managers")
        for manager in active_managers.values():
            if hasattr(manager, 'update_websocket_manager'):
                try:
                    manager.update_websocket_manager(ws_manager)
                except Exception as e:
                    logger.error(f"Error updating manager with new WS: {e}")
            else:
                # Fallback for managers without the update method
                manager.websocket_manager = ws_manager
                if hasattr(manager, 'setup_subscriptions'):
                    manager.setup_subscriptions()
                    
        return ws_manager
    return None

@app.route('/')
def index():
    return redirect('/trading/option-chain')

@app.route('/trading/option-chain')
def option_chain():
    underlying = request.args.get('underlying', 'NIFTY')
    expiry = request.args.get('expiry')
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager
        manager_key = get_manager_key(underlying, expiry, 'v1')
        
        with manager_lock:
            if manager_key in active_managers:
                logger.debug(f"Reusing active manager for {manager_key}")
                manager = active_managers[manager_key]
            else:
                logger.info(f"Creating new manager for {manager_key}")
                manager = OptionChainManager(underlying, expiry)
                manager.initialize(client)
                
            # Cleanup old sessions
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager



        # Get option chain data
        chain_data = manager.get_option_chain()
        # print("chain_data", chain_data)
        logger.debug(f"Initial chain data type: {type(chain_data)}")
        logger.debug(f"Initial chain data bool: {bool(chain_data)}")
        logger.debug(f"Initial chain data keys: {chain_data.keys() if isinstance(chain_data, dict) else 'Not a dict'}")
        logger.debug(f"Initial chain data options count: {len(chain_data.get('options', [])) if isinstance(chain_data, dict) else 'N/A'}")
        return render_template('option_chain.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain: {e}")
        return render_template('option_chain.html',
                             error=f"Error loading option chain: {str(e)}",
                             underlying=underlying)

# @app.route('/trading/option-chain-new')
# def option_chain_new():
#     underlying = request.args.get('underlying', 'NIFTY')
#     expiry = request.args.get('expiry')
    
#     try:
#         client = get_api_client()
        
#         # Get expiry if not provided
#         if not expiry:
#             exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
#             expiry_response = client.expiry(
#                 symbol=underlying,
#                 exchange=exchange,
#                 instrumenttype='options'
#             )
            
#             if expiry_response.get('status') == 'success':
#                 expiries = expiry_response.get('data', [])
#                 if expiries:
#                     expiry = expiries[0]
        
#         # Initialize option chain manager
#         manager_key = f"{underlying}_{expiry}"
        
#         if manager_key in active_managers:
#             logger.debug(f"Reusing active manager for {manager_key}")
#             manager = active_managers[manager_key]
#         else:
#             logger.info(f"Creating new manager for {manager_key}")
#             manager = OptionChainManager(underlying, expiry)
#             manager.initialize(client)

#         # Get option chain data
#         chain_data = manager.get_option_chain()
        
#         return render_template('option_chain_new.html',
#                              chain_data=chain_data,
#                              underlying=underlying,
#                              expiry=expiry,
#                              available_expiries=expiries if 'expiries' in locals() else [])
                             
#     except Exception as e:
#         logger.error(f"Error loading option chain: {e}")
#         return render_template('option_chain_new.html',
#                              error=f"Error loading option chain: {str(e)}",
#                              underlying=underlying)

@app.route('/trading/option-chain-v2')
def option_chain_v2():
    underlying = request.args.get('underlying', 'NIFTY')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V2
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V2
        manager_key = f"{underlying}_{expiry}_v2"
        
        if manager_key in active_managers:
            logger.debug(f"Reusing active V2 manager for {manager_key}")
            manager = active_managers[manager_key]
        else:
            logger.info(f"Creating new V2 manager for {manager_key} (Quote Mode)")
            ws_manager = get_or_create_websocket_manager(underlying)
            manager = OptionChainManagerV2(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)

        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v2.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V2: {e}")
        return render_template('option_chain_v2.html',
                             error=f"Error loading option chain v2 : {str(e)}",
                             underlying=underlying)

@app.route('/trading/option-chain-v3')
def option_chain_v3():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V3
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V3
        manager_key = f"{underlying}_{expiry}_v3"
        
        if manager_key in active_managers:
            logger.debug(f"Reusing active V3 manager for {manager_key}")
            manager = active_managers[manager_key]
        else:
            logger.info(f"Creating new V3 manager for {manager_key} (Quote Mode)")
            ws_manager = get_or_create_websocket_manager(underlying)
            manager = OptionChainManagerV3(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)

        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v3.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V3: {e}")
        return render_template('option_chain_v3.html',
                             error=f"Error loading option chain v3 : {str(e)}",
                             underlying=underlying)

@app.route('/trading/option-chain-v4')
def option_chain_v4():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V4
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V4
        manager_key = f"{underlying}_{expiry}_v4"
        
        if manager_key in active_managers:
            logger.debug(f"Reusing active V4 manager for {manager_key}")
            manager = active_managers[manager_key]
        else:
            logger.info(f"Creating new V4 manager for {manager_key} (Quote Mode)")
            ws_manager = get_or_create_websocket_manager(underlying)
            manager = OptionChainManagerV4(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            manager.start_monitoring()
            
            # Cleanup old sessions
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager


        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v4.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V4: {e}")
        return render_template('option_chain_v4.html',
                             error=f"Error loading option chain v4 : {str(e)}",
                             underlying=underlying)


@app.route('/trading/option-chain-v5')
def option_chain_v5():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V5
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V5
        manager_key = get_manager_key(underlying, expiry, 'v5')
        
        with manager_lock:
            if manager_key in active_managers:
                logger.debug(f"Reusing active V5 manager for {manager_key}")
                manager = active_managers[manager_key]
            else:
                logger.info(f"Creating new V5 manager for {manager_key} (Quote Mode)")
                ws_manager = get_or_create_websocket_manager(underlying)
                manager = OptionChainManagerV5(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Aggressive Lifecycle Management: Ensure monitored and others stopped
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager





        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v5.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V5: {e}")
        return render_template('option_chain_v5.html',
                             error=f"Error loading option chain v5 : {str(e)}",
                             underlying=underlying)

@app.route('/trading/option-chain-v6')
def option_chain_v6():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V6
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V6
        manager_key = get_manager_key_v6(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                logger.debug(f"Reusing active V6 manager for {manager_key}")
                manager = active_managers[manager_key]
            else:
                logger.info(f"Creating new V6 manager for {manager_key} (Quote Mode)")
                ws_manager = get_or_create_websocket_manager(underlying)
                manager = OptionChainManagerV6(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Aggressive Lifecycle Management: Ensure monitored and others stopped
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager





        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v6.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V6: {e}")
        return render_template('option_chain_v6.html',
                             error=f"Error loading option chain v6 : {str(e)}",
                             underlying=underlying)



@app.route('/trading/option-chain-v7')
def option_chain_v7():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V7
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V7
        manager_key = get_manager_key_v7(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                logger.debug(f"Reusing active V7 manager for {manager_key}")
                manager = active_managers[manager_key]
            else:
                logger.info(f"Creating new V7 ADAPTIVE manager for {manager_key}")
                ws_manager = get_or_create_websocket_manager(underlying)
                manager = OptionChainManagerV7Adaptive(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Aggressive Lifecycle Management: Ensure monitored and others stopped
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager





        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v7.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V7: {e}")
        return render_template('option_chain_v7.html',
                             error=f"Error loading option chain v7 : {str(e)}",
                             underlying=underlying)

@app.route('/trading/option-chain-v8')
def option_chain_v8():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V8
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V8
        manager_key = get_manager_key_v8(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                logger.debug(f"Reusing active V8 manager for {manager_key}")
                manager = active_managers[manager_key]
            else:
                logger.info(f"Creating new V8 manager for {manager_key}")
                ws_manager = get_or_create_websocket_manager(underlying)
                manager = OptionChainManagerV8AntiWhipsaw(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Aggressive Lifecycle Management: Ensure monitored and others stopped
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager





        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v8.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V8: {e}")
        return render_template('option_chain_v8.html',
                             error=f"Error loading option chain v8 : {str(e)}",
                             underlying=underlying)

@app.route('/trading/option-chain-v9')
def option_chain_v9():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
    # Mode is always 'quote' for V9
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V9 (SAFE GUARDIAN)
        manager_key = get_manager_key_v9(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                logger.debug(f"Reusing active V9 SAFE manager for {manager_key}")
                manager = active_managers[manager_key]
            else:
                logger.info(f"Creating new V9 SAFE GUARDIAN for {manager_key}")
                ws_manager = get_or_create_websocket_manager(underlying)
                manager = OptionChainManagerV9Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Start Background Guardian Engine
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager

        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v9.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V9: {e}")
        return render_template('option_chain_v9.html',
                             error=f"Error loading option chain v9 : {str(e)}",
                             underlying=underlying)


@app.route('/trading/option-chain-v10')
def option_chain_v10():
    underlying = request.args.get('underlying', 'RELIANCE')
    expiry = request.args.get('expiry')
     # Mode is always 'quote' for V10
    
    try:
        client = get_api_client()
        
        # Get expiry if not provided
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Initialize option chain manager V10 (SAFE GUARDIAN)
        manager_key = get_manager_key_v10(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                logger.debug(f"Reusing active V10 SAFE manager for {manager_key}")
                manager = active_managers[manager_key]
            else:
                logger.info(f"Creating new V10 SAFE GUARDIAN for {manager_key}")
                ws_manager = get_or_create_websocket_manager(underlying)
                manager = OptionChainManagerV10Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Start Background Guardian Engine
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager

        # Get option chain data
        chain_data = manager.get_option_chain()        
        return render_template('option_chain_v10.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [])
                             
    except Exception as e:
        logger.error(f"Error loading option chain V10: {e}")
        return render_template('option_chain_v10.html',
                             error=f"Error loading option chain v10 : {str(e)}",
                             underlying=underlying)






@app.route('/trading/api/option-chain/expiry/<underlying>')
def get_expiry_dates(underlying):
    try:
        client = get_api_client()
        exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
        
        logger.debug(f"Fetching expiry for {underlying} ({exchange})")
        expiry_response = client.expiry(
            symbol=underlying,
            exchange=exchange,
            instrumenttype='options'
        )
        logger.debug(f"Expiry response for {underlying}: {expiry_response}")
        
        return jsonify(expiry_response)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/trading/api/option-chain/stream/<underlying>')
def option_chain_stream(underlying):
    expiry = request.args.get('expiry')
    
    def generate():
        manager_key = f"{underlying}_{expiry}"
        
        # Get or create manager
        if manager_key in active_managers:
            manager = active_managers[manager_key]
        else:
            client = get_api_client()
            ws_manager = get_or_create_websocket_manager(underlying)
            
            manager = OptionChainManager(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            manager.start_monitoring()
            active_managers[manager_key] = manager
        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(4)
            except Exception as e:
                logger.error(f"Stream error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')


def option_chain_stream_v2(underlying):
    expiry = request.args.get('expiry')
    # Mode is hardcoded in V2 now
    
    def generate():
        manager_key = f"{underlying}_{expiry}_v2"
        
        logger.info(f"Stream V2 requested for {manager_key}")

        # Get or create manager
        if manager_key in active_managers:
            manager = active_managers[manager_key]
        else:
            client = get_api_client()
            ws_manager = get_or_create_websocket_manager(underlying)
            
            logger.info(f"Creating new V2 manager for {manager_key} (Quote Mode)")
            manager = OptionChainManagerV2(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            manager.start_monitoring()
            active_managers[manager_key] = manager
        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(4)
            except Exception as e:
                logger.error(f"Stream V2 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')

@app.route('/trading/api/option-chain/stream-v3/<underlying>')
def option_chain_stream_v3(underlying):
    expiry = request.args.get('expiry')
    # Mode is hardcoded in V3 now
    
    def generate():
        manager_key = f"{underlying}_{expiry}_v3"
        
        logger.info(f"Stream V3 requested for {manager_key}")

        # Get or create manager
        if manager_key in active_managers:
            manager = active_managers[manager_key]
        else:
            client = get_api_client()
            ws_manager = get_or_create_websocket_manager(underlying)
            
            logger.info(f"Creating new V3 manager for {manager_key} (Quote Mode)")
            manager = OptionChainManagerV3(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            manager.start_monitoring()
            
            # Clean up other active managers to save quota
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager

        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(4)
            except Exception as e:
                logger.error(f"Stream V3 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')

@app.route('/trading/api/option-chain/stream-v4/<underlying>')
def option_chain_stream_v4(underlying):
    expiry = request.args.get('expiry')
    # Mode is hardcoded in V4 now
    
    def generate():
        manager_key = f"{underlying}_{expiry}_v4"
        
        logger.info(f"Stream V4 requested for {manager_key}")

        # Get or create manager
        if manager_key in active_managers:
            manager = active_managers[manager_key]
        else:
            client = get_api_client()
            ws_manager = get_or_create_websocket_manager(underlying)
            
            logger.info(f"Creating new V4 manager for {manager_key} (Quote Mode)")
            manager = OptionChainManagerV4(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            manager.start_monitoring()
            
            # Clean up other active managers to save quota
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager

        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(1)
            except Exception as e:
                logger.error(f"Stream V4 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')


@app.route('/trading/api/option-chain/stream-v5/<underlying>')
def option_chain_stream_v5(underlying):
    expiry = request.args.get('expiry')
    
    def generate():
        nonlocal expiry
        client = get_api_client()
        
        # Ensure expiry is fetched if missing (CRITICAL for key consistency)
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(symbol=underlying, exchange=exchange, instrumenttype='options')
            if expiry_response.get('status') == 'success' and expiry_response.get('data'):
                expiry = expiry_response.get('data')[0]

        manager_key = get_manager_key(underlying, expiry, 'v5')
        logger.info(f"Stream V5 requested for {manager_key}")

        # Get or create manager
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                ws_manager = get_or_create_websocket_manager(underlying)
                logger.info(f"Creating new V5 manager for {manager_key} (Quote Mode)")
                manager = OptionChainManagerV5(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Ensure only this one active
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager



        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(1)
            except Exception as e:
                logger.error(f"Stream V5 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')



@app.route('/trading/api/option-chain/stream-v6/<underlying>')
def option_chain_stream_v6(underlying):
    expiry = request.args.get('expiry')
    
    def generate():
        nonlocal expiry
        client = get_api_client()
        
        # Ensure expiry is fetched if missing (CRITICAL for key consistency)
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(symbol=underlying, exchange=exchange, instrumenttype='options')
            if expiry_response.get('status') == 'success' and expiry_response.get('data'):
                expiry = expiry_response.get('data')[0]

        manager_key = get_manager_key(underlying, expiry, 'v6')
        logger.info(f"Stream V6 requested for {manager_key}")

        # Get or create manager
        if manager_key in active_managers:
            manager = active_managers[manager_key]
        else:
            logger.info(f"Creating new V7 ADAPTIVE manager for {manager_key}")
            ws_manager = get_or_create_websocket_manager(underlying)
            manager = OptionChainManagerV7Adaptive(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
        
        # Aggressive Lifecycle Management
        manager.start_monitoring()
        cleanup_inactive_managers(manager_key)
        active_managers[manager_key] = manager



        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(1)
            except Exception as e:
                logger.error(f"Stream V6 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')


@app.route('/trading/api/option-chain/stream-v7/<underlying>')
def option_chain_stream_v7(underlying):
    expiry = request.args.get('expiry')
    
    def generate():
        nonlocal expiry
        client = get_api_client()
        
        # Ensure expiry is fetched if missing (CRITICAL for key consistency)
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(symbol=underlying, exchange=exchange, instrumenttype='options')
            if expiry_response.get('status') == 'success' and expiry_response.get('data'):
                expiry = expiry_response.get('data')[0]

        manager_key = get_manager_key_v7(underlying, expiry)
        logger.info(f"Stream V7 requested for {manager_key}")

        # Get or create manager
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                ws_manager = get_or_create_websocket_manager(underlying)
                logger.info(f"Creating new V7 ADAPTIVE manager for {manager_key}")
                manager = OptionChainManagerV7Adaptive(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Ensure only this one active
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager



        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(1)
            except Exception as e:
                logger.error(f"Stream V7 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')


@app.route('/trading/api/option-chain/stream-v8/<underlying>')
def option_chain_stream_v8(underlying):
    expiry = request.args.get('expiry')
    
    def generate():
        nonlocal expiry
        client = get_api_client()
        
        # Ensure expiry is fetched if missing (CRITICAL for key consistency)
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(symbol=underlying, exchange=exchange, instrumenttype='options')
            if expiry_response.get('status') == 'success' and expiry_response.get('data'):
                expiry = expiry_response.get('data')[0]

        manager_key = get_manager_key_v8(underlying, expiry)
        logger.info(f"Stream V8 requested for {manager_key}")

        # Get or create manager
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                ws_manager = get_or_create_websocket_manager(underlying)
                logger.info(f"Creating new V8 AntiWhipsaw manager for {manager_key}")
                manager = OptionChainManagerV8AntiWhipsaw(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Ensure only this one active
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager



        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(1)
            except Exception as e:
                logger.error(f"Stream V8 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')


@app.route('/trading/api/option-chain/stream-v9/<underlying>')
def option_chain_stream_v9(underlying):
    expiry = request.args.get('expiry')
    
    def generate():
        nonlocal expiry
        client = get_api_client()
        
        # Ensure expiry is fetched if missing
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(symbol=underlying, exchange=exchange, instrumenttype='options')
            if expiry_response.get('status') == 'success' and expiry_response.get('data'):
                expiry = expiry_response.get('data')[0]

        manager_key = get_manager_key_v9(underlying, expiry)
        logger.info(f"Stream V9 SAFE requested for {manager_key}")

        # Get or create manager
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                ws_manager = get_or_create_websocket_manager(underlying)
                logger.info(f"Creating new V9 SAFE manager for {manager_key}")
                manager = OptionChainManagerV9Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Start Background Guardian Engine
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager

        
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                time.sleep(1) # Visual update rate
            except Exception as e:
                logger.error(f"Stream V9 error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
                
    return Response(generate(), mimetype='text/event-stream')

@app.route('/trading/api/option-chain/stream-v10/<underlying>')
def option_chain_stream_v10(underlying):
    expiry = request.args.get('expiry')

    def generate():
        nonlocal expiry
        client = get_api_client()

        # Ensure expiry is fetched if missing
        if not expiry:
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'
            expiry_response = client.expiry(symbol=underlying, exchange=exchange, instrumenttype='options')
            if expiry_response.get('status') == 'success' and expiry_response.get('data'):
                expiry = expiry_response.get('data')[0]

        manager_key = get_manager_key_v10(underlying, expiry)
        logger.info(f"Stream V10 SAFE requested for {manager_key}")

        # Get or create manager
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                ws_manager = get_or_create_websocket_manager(underlying)
                logger.info(f"Creating new V10 SAFE manager for {manager_key}")
                manager = OptionChainManagerV10Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)

            # Start Background Guardian Engine
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager

        consecutive_errors = 0
        while True:
            try:
                chain_data = manager.get_option_chain()
                yield f"data: {json.dumps(chain_data)}\n\n"
                consecutive_errors = 0
                time.sleep(1) # Visual update rate
            except GeneratorExit:
                # Client disconnected - clean exit
                logger.info(f"Stream V10 client disconnected for {manager_key}")
                return
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Stream V10 error: {e}")
                if consecutive_errors >= 3:
                    logger.error(f"Stream V10 too many errors, closing for {manager_key}")
                    break
                try:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                except GeneratorExit:
                    return
                time.sleep(2)

    return Response(generate(), mimetype='text/event-stream')


@app.route('/trading/api/option-chain-session/create-v9', methods=['POST'])
def create_session_v9():
    """Dedicated V9 Session Creator"""
    try:
        data = request.json
        underlying = data.get('underlying')
        expiry = data.get('expiry')
        # Mode is ignored, defaulting to SAFE GUARDIAN

        print("Creating V9 Session for:", underlying, expiry)

        manager_key = get_manager_key_v9(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                # Reuse existing manager
                manager = active_managers[manager_key]
                logger.info(f"Reusing existing V9 manager: {manager_key}")
            else:
                # Create NEW V9 Manager
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V9 SAFE manager for {manager_key}")
                manager = OptionChainManagerV9Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
        
        # Ensure Active & Clean others
        manager.start_monitoring()
        cleanup_inactive_managers(manager_key)
        active_managers[manager_key] = manager

        return jsonify({
            'status': 'success',
            'session_id': manager_key,
            'subscribed_symbols': len(manager.option_data) if manager.option_data else 0,
            'message': 'V9 Session Activated'
        })

    except Exception as e:
        logger.error(f"Error creating V9 session: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/trading/api/option-chain-session/create-v10', methods=['POST'])
def create_session_v10():
    """Dedicated V10 Session Creator"""
    try:
        data = request.json
        underlying = data.get('underlying')
        expiry = data.get('expiry')
        # Mode is ignored, defaulting to SAFE GUARDIAN

        print("Creating V10 Session for:", underlying, expiry)

        manager_key = get_manager_key_v10(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                # Reuse existing manager
                manager = active_managers[manager_key]
                # Reset regeneration state — fresh page load means chain is rebuilt in UI
                manager._regeneration_needed = False
                manager._chain_atm = manager.atm_strike
                manager._atm_mismatch_since = 0
                logger.info(f"Reusing existing V10 manager: {manager_key} (reset chain_atm to {manager.atm_strike})")
            else:
                # Create NEW V10 Manager
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V10 SAFE manager for {manager_key}")
                manager = OptionChainManagerV10Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
        
        # Ensure Active & Clean others
        manager.start_monitoring()
        cleanup_inactive_managers(manager_key)
        active_managers[manager_key] = manager

        return jsonify({
            'status': 'success',
            'session_id': manager_key,
            'subscribed_symbols': len(manager.option_data) if manager.option_data else 0,
            'message': 'V10 Session Activated'
        })

    except Exception as e:
        logger.error(f"Error creating V10 session: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Session management routes (mocked/simplified)
@app.route('/trading/api/option-chain-session/create', methods=['POST'])
def create_session():
    data = request.json
    underlying = data.get('underlying')
    expiry = data.get('expiry')
    
    version = data.get('version')
    mode = data.get('mode')
    print("Mode: ", mode)
    print("Version: ", version)
    print("Underlying: ", underlying)
    print("Expiry: ", expiry)
    
    if version == 'V9':
        # V9 SAFE GUARDIAN Session
        manager_key = get_manager_key_v9(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V9 SAFE manager for {manager_key}")
                manager = OptionChainManagerV9Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
        
        # Ensure only this one is active
        manager.start_monitoring()
        cleanup_inactive_managers(manager_key)
        active_managers[manager_key] = manager
    elif version == 'V10':
        # V10 SAFE GUARDIAN Session
        manager_key = get_manager_key_v10(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V10 SAFE manager for {manager_key}")
                manager = OptionChainManagerV10Safe(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
        
        # Ensure only this one is active
        manager.start_monitoring()
        cleanup_inactive_managers(manager_key)
        active_managers[manager_key] = manager

    elif version == 'V7':
        # print("Creating V7 session")
        # V7 Session
        manager_key = get_manager_key_v7(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V7 ADAPTIVE manager for {manager_key}")
                manager = OptionChainManagerV7Adaptive(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
        
        # Ensure only this one is active
        manager.start_monitoring()
        cleanup_inactive_managers(manager_key)
        active_managers[manager_key] = manager

    
    elif version == 'V6':
        # print("Creating V6 session")
        # V6 Session
        manager_key = get_manager_key_v6(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V6 manager for {manager_key} (Quote Mode)")
                manager = OptionChainManagerV6(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Ensure only this one is active
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager

    elif version == 'V8':
        # print("Creating V6 session")
        # V6 Session
        manager_key = get_manager_key_v8(underlying, expiry)
        
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V8 manager for {manager_key} (Quote Mode)")
                manager = OptionChainManagerV8AntiWhipsaw(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
            
            # Ensure only this one is active
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager
            
    elif version == 'v5':
        # V5 Session
        manager_key = get_manager_key(underlying, expiry, 'v5')
        
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V5 manager for {manager_key} (Quote Mode)")
                manager = OptionChainManagerV5(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
                
            # Ensure only this one is active
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager





            
    elif version == 'v4':
        # V4 Session
        manager_key = get_manager_key(underlying, expiry, 'v4')
        
        with manager_lock:
            if manager_key in active_managers:
                manager = active_managers[manager_key]
            else:
                client = get_api_client()
                ws_manager = get_or_create_websocket_manager(underlying)
                
                logger.info(f"Creating new V4 manager for {manager_key} (Quote Mode)")
                manager = OptionChainManagerV4(underlying, expiry, websocket_manager=ws_manager)
                manager.initialize(client)
                
            # Ensure only this one is active
            manager.start_monitoring()
            cleanup_inactive_managers(manager_key)
            active_managers[manager_key] = manager



            
    elif version == 'v3':
        # V3 Session
        manager_key = f"{underlying}_{expiry}_v3"
        if manager_key in active_managers:
            manager = active_managers[manager_key]
        else:
            client = get_api_client()
            ws_manager = get_or_create_websocket_manager(underlying)
            
            logger.info(f"Creating new V3 manager for {manager_key} (Quote Mode)")
            manager = OptionChainManagerV3(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            
        # Ensure only this one is active
        manager.start_monitoring()
        cleanup_inactive_managers(manager_key)
        active_managers[manager_key] = manager


    elif version == 'v2' or mode:
        # V2 Session (Quote Only) - Default for legacy 'mode' param
        manager_key = f"{underlying}_{expiry}_v2"
        if manager_key not in active_managers:
            client = get_api_client()
            ws_manager = get_or_create_websocket_manager(underlying)
            
            logger.info(f"Creating new V2 manager for {manager_key} (Quote Mode)")
            # Initialize without mode param (uses hardcoded 'quote')
            manager = OptionChainManagerV2(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            manager.start_monitoring()
            active_managers[manager_key] = manager
    else:
        # V1 Session
        manager_key = f"{underlying}_{expiry}"
        if manager_key not in active_managers:
            client = get_api_client()
            ws_manager = get_or_create_websocket_manager(underlying)
            
            manager = OptionChainManager(underlying, expiry, websocket_manager=ws_manager)
            manager.initialize(client)
            manager.start_monitoring()
            active_managers[manager_key] = manager
    
    return jsonify({'status': 'success', 'session_id': manager_key, 'subscribed_symbols': 0})

@app.route('/trading/api/option-chain-session/heartbeat', methods=['POST'])
def session_heartbeat():
    return jsonify({'status': 'success'})

@app.route('/trading/api/option-chain-v5/set-mode', methods=['POST'])
def set_strategy_mode():
    try:
        data = request.json
        session_id = data.get('session_id')
        mode = data.get('mode')
        
        if not session_id or not mode:
            return jsonify({'status': 'error', 'message': 'Missing session_id or mode'}), 400
            
        manager = active_managers.get(session_id)
        if manager and hasattr(manager, 'set_strategy_mode'):
            success = manager.set_strategy_mode(mode)
            if success:
                return jsonify({'status': 'success', 'mode': mode})
            else:
                return jsonify({'status': 'error', 'message': 'Invalid mode'}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Manager not found or invalid type'}), 404
            
    except Exception as e:
        logger.error(f"Error setting strategy mode: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/trading/api/option-chain-v7/set-mode', methods=['POST'])
def set_strategy_mode_v7():
    """Set trading mode for V7 (ADAPTIVE/MOMENTUM_RIDER/THETA_HUNTER/etc)"""
    try:
        data = request.json
        session_id = data.get('session_id')
        mode = data.get('mode', 'ADAPTIVE')

        if not session_id or not mode:
            return jsonify({'status': 'error', 'message': 'Missing session_id or mode'}), 400

        manager = active_managers.get(session_id)
        if manager and hasattr(manager, 'set_strategy_mode'):
            success = manager.set_strategy_mode(mode)
            if success:
                return jsonify({
                    'status': 'success',
                    'mode': mode,
                    'current_regime': getattr(manager, 'current_regime', 'DETECTING'),
                    'message': f'Mode set to {mode}'
                })
            else:
                return jsonify({'status': 'error', 'message': 'Invalid mode'}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Manager not found or invalid type'}), 404

    except Exception as e:
        logger.error(f"Error setting strategy mode v7: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/trading/api/option-chain-v8/set-mode', methods=['POST'])
def set_strategy_mode_v8():
    """Set trading mode for V8 (ADAPTIVE/MOMENTUM_RIDER/THETA_HUNTER/etc)"""
    try:
        data = request.json
        session_id = data.get('session_id')
        mode = data.get('mode', 'ADAPTIVE')

        if not session_id or not mode:
            return jsonify({'status': 'error', 'message': 'Missing session_id or mode'}), 400

        manager = active_managers.get(session_id)
        if manager and hasattr(manager, 'set_strategy_mode'):
            success = manager.set_strategy_mode(mode)
            if success:
                return jsonify({
                    'status': 'success',
                    'mode': mode,
                    'current_regime': getattr(manager, 'current_regime', 'DETECTING'),
                    'message': f'Mode set to {mode}'
                })
            else:
                return jsonify({'status': 'error', 'message': 'Invalid mode'}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Manager not found or invalid type'}), 404

    except Exception as e:
        logger.error(f"Error setting strategy mode v8: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/trading/api/option-chain-v9/set-mode', methods=['POST'])
def set_strategy_mode_v9():
    """Set trading mode for V9 SAFE (e.g. Force Unlock?)"""
    try:
        data = request.json
        session_id = data.get('session_id')
        mode = data.get('mode', 'ADAPTIVE')

        if not session_id or not mode:
            return jsonify({'status': 'error', 'message': 'Missing session_id or mode'}), 400

        manager = active_managers.get(session_id)
        if manager and hasattr(manager, 'set_strategy_mode'):
            success = manager.set_strategy_mode(mode)
            if success:
                return jsonify({
                    'status': 'success',
                    'mode': mode,
                    'current_regime': getattr(manager, 'current_regime', 'DETECTING'),
                    'message': f'Mode set to {mode}'
                })
            else:
                return jsonify({'status': 'error', 'message': 'Invalid mode'}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Manager not found or invalid type'}), 404

    except Exception as e:
        logger.error(f"Error setting strategy mode v9: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/trading/api/option-chain-v10/set-mode', methods=['POST'])
def set_strategy_mode_v10():
    """Set trading mode for V10 SAFE (e.g. Force Unlock?)"""
    try:
        data = request.json
        session_id = data.get('session_id')
        mode = data.get('mode', 'ADAPTIVE')

        if not session_id or not mode:
            return jsonify({'status': 'error', 'message': 'Missing session_id or mode'}), 400

        manager = active_managers.get(session_id)
        if manager and hasattr(manager, 'set_strategy_mode'):
            success = manager.set_strategy_mode(mode)
            if success:
                return jsonify({
                    'status': 'success',
                    'mode': mode,
                    'current_regime': getattr(manager, 'current_regime', 'DETECTING'),
                    'message': f'Mode set to {mode}'
                })
            else:
                return jsonify({'status': 'error', 'message': 'Invalid mode'}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Manager not found or invalid type'}), 404

    except Exception as e:
        logger.error(f"Error setting strategy mode v10: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/trading/api/option-chain-session/destroy', methods=['POST'])
def destroy_session():
    data = request.json
    session_id = data.get('session_id')
    logger.info(f"Destroy session called for: {session_id}")
    
    if session_id in active_managers:
        manager = active_managers[session_id]
        
        # Stop background threads
        if hasattr(manager, 'stop_monitoring'):
            logger.info(f"Stopping monitoring for {session_id}")
            manager.stop_monitoring()
            
        # Close WebSocket connection if it was specific to this manager
        # (Note: In current implementation, WS might be shared or created per manager. 
        # Ideally we check usage count, but for now we trust garbage collection or explicit close)
        if hasattr(manager, 'websocket_manager') and manager.websocket_manager:
            # If the WS manager has a specific close/disconnect method, call it
            # manager.websocket_manager.disconnect() 
            pass

        # Remove from active managers
        del active_managers[session_id]
        logger.info(f"Successfully destroyed session {session_id}")
        return jsonify({'status': 'success', 'message': 'Session destroyed'})
    
    return jsonify({'status': 'error', 'message': 'Session not found'})

if __name__ == '__main__':
    app.run(debug=False, port=5800)
