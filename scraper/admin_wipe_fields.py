import os
import sys
import json
import datetime
import logging
from dotenv import load_dotenv

# Setup project root in path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Load environment variables
load_dotenv(os.path.join(project_root, '.env'))

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from dashboard.drive_client import download_json, upload_json
from scraper.drive_uploader import bulk_update_database, get_db_file_id

def main():
    logger.info("Initializing WIPE operation on 'genre' and 'language' fields...")
    
    db_file_id, parent_id = get_db_file_id()
    if not db_file_id:
        logger.error("Could not locate database.json on Google Drive.")
        sys.exit(1)
        
    logger.info(f"Downloading live database.json (ID: {db_file_id})...")
    db_data = download_json(db_file_id)
    if not db_data:
        logger.error("Failed to download or parse database.json.")
        sys.exit(1)
        
    # Create an immediate backup
    now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"database_backup_pre_wipe_{now}.json"
    
    logger.info(f"Creating local backup on Google Drive as '{backup_filename}'...")
    upload_json(None, db_data, backup_filename, parent_id=parent_id)
    logger.info("Backup successfully written to Drive.")
    
    # Extract tracks
    is_dict = isinstance(db_data, dict) and 'tracks' in db_data
    tracks = db_data['tracks'] if is_dict else (db_data if isinstance(db_data, list) else [])
    
    if not tracks:
        logger.warning("No tracks found in the database. Exiting.")
        sys.exit(0)
        
    logger.info(f"Loaded {len(tracks)} tracks. Beginning field overwrite...")
    
    wiped_count = 0
    wiped_tracks = []
    
    for track in tracks:
        track['genre'] = "Unknown"
        track['language'] = "unknown"
        wiped_tracks.append(track)
        wiped_count += 1
        
    logger.info(f"Successfully wiped fields for {wiped_count} tracks in memory.")
    
    # Empty the Drive database first to prevent bulk_update_database from duplicating tracks
    logger.info("Clearing live database to prepare for bulk update insertion...")
    empty_db = {'tracks': []} if is_dict else []
    upload_json(db_file_id, empty_db, 'database.json', parent_id=parent_id)
    
    logger.info("Uploading wiped tracks back to Drive via bulk_update_database...")
    try:
        bulk_update_database(wiped_tracks)
        logger.info(f"WIPE COMPLETE. {wiped_count} tracks were uploaded and are now ready for Gemini backfill.")
    except Exception as e:
        logger.error(f"Failed to upload wiped database: {e}")
        logger.error(f"Please restore from backup: {backup_filename}")
        sys.exit(1)

if __name__ == "__main__":
    main()
