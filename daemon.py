import time
import logging
import sys
from main import run_sync_cycle

logging.basicConfig(
    level=logging.INFO, 
    format='[%(asctime)s] %(message)s', 
    datefmt='%b %d %I:%M %p', 
    handlers=[
        logging.FileHandler("daemon.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

SLEEP_INTERVAL_SECONDS = 300  # 5 minutes

if __name__ == "__main__":
    logging.info("Starting Tally Sync Daemon Service...")
    
    while True:
        try:
            logging.info("Starting sync cycle...")
            run_sync_cycle()
            logging.info(f"Sync cycle finished. Sleeping for {SLEEP_INTERVAL_SECONDS / 60} minutes...")
        except Exception as e:
            logging.error(f"Daemon encountered an error: {e}")
            logging.info("Will retry in next cycle.")
            
        time.sleep(SLEEP_INTERVAL_SECONDS)
