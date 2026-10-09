import logging
from .config import load_config
from .source_values import decode_response
import requests


class TallyClient:
    def __init__(self):
        config = load_config()
        self.port = config["tally_port"]
        self.url = config["tally_url"]

    def send_request(self, xml_request, timeout=300):  # ← timeout parameter added
        response = requests.post(
            self.url,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "text/xml;charset=utf-8", "Connection": "close"},
            timeout=timeout,
        )
        response.raise_for_status()
        return decode_response(response)

    def test_connection(self):
        try:
            response = requests.post(
                self.url,
                data="<ENVELOPE></ENVELOPE>".encode("utf-8"),
                headers={"Content-Type": "text/xml;charset=utf-8", "Connection": "close"},
                timeout=5,
            )
            return response.status_code == 200
        except Exception:
            return False


if __name__ == "__main__":
    client = TallyClient()
    if client.test_connection():
        logging.info("Connected to Tally")
    else:
        logging.info("Could not connect to Tally")