import json
import os
import threading
import logging
import time

logger = logging.getLogger(__name__)

class ConfigManager:
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(ConfigManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, config_path="config/v10_config.json"):
        if self._initialized:
            return
            
        self.config_path = config_path
        self.config = {}
        self.last_loaded = 0
        self.load_config()
        self._initialized = True
        
    def load_config(self):
        """Load configuration from JSON file"""
        try:
            if not os.path.exists(self.config_path):
                logger.error(f"Config file not found: {self.config_path}")
                # Load default fallback if needed
                return
                
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)
                self.last_loaded = time.time()
                logger.info(f"Loaded V10 Configuration from {self.config_path}")
                
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            import traceback
            traceback.print_exc()

    def get(self, key_path, default=None):
        """
        Get config value using dot notation for nested keys.
        e.g. config.get('risk.sl_pct', 15)
        """
        try:
            keys = key_path.split('.')
            value = self.config
            
            for k in keys:
                if isinstance(value, dict) and k in value:
                    value = value[k]
                else:
                    return default
            
            return value
        except Exception:
            return default
            
    def reload(self):
        """Force reload of configuration"""
        self.load_config()

# Global Instance
config_manager = ConfigManager()
