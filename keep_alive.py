import time
import logging
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

url = "https://wavify-proxy.onrender.com/ping"

logger.info(f"Keep-alive script started. Target: {url}")

while True:
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            logger.info(f"Ping successful. Response: {response.text.strip()}")
        else:
            logger.error(f"Ping failed. Status code: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Ping failed. Exception: {e}")
    
    time.sleep(600)
