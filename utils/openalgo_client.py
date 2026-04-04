"""
Extended OpenAlgo API client with additional methods
"""
from openalgo import api

class ExtendedOpenAlgoAPI(api):
    """Extended OpenAlgo API client with ping method"""
    
    def ping(self):
        """
        Test connectivity and validate API key authentication
        """
        payload = {"apikey": self.api_key}
    
    def multioptiongreeks(self, symbols_list):
        """
        Fetch option greeks for multiple symbols in one batch call
        symbols_list: list of dicts [{'symbol': '...', 'exchange': '...'}]
        """
        payload = {
            "apikey": self.api_key,
            "symbols": symbols_list
        }
        print(f"DEBUG MULTIOPTIONGREEKS PAYLOAD: {payload}")
        return self._make_request("multioptiongreeks", payload)
