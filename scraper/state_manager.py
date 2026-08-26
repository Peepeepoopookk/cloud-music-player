import os
import sys
import datetime
import logging
import difflib

# Configure logging
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Resolve project root path and append to sys.path if not present
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.append(project_root)

from dashboard.drive_client import download_json, upload_json, list_files
from scraper.drive_uploader import get_db_file_id
from scraper.operation_lock import library_write_lock

# Default Configuration
DEFAULT_CONFIG = {
    "allowed_genres": [
        "pop", "hip-hop", "r&b", "electronic", "rock", "latin", "k-pop", "classical",
        "jazz", "blues", "country", "metal", "indie", "alternative", "reggae", "soul",
        "funk", "disco", "house", "techno", "ambient", "folk", "punk", "gospel",
        "afrobeats", "dancehall", "trap", "drill", "phonk", "synthwave", "lo-fi",
        "bollywood", "indian-classical", "carnatic", "devotional", "anime", "j-pop", "c-pop"
    ],
    "allowed_languages": [
        "english", "malayalam", "tamil", "hindi", "indian"
    ],
    "songs_per_run": 5,
    "auto_refresh_days": 7,
    "filter_mode": "filtered"
}

# Default State
DEFAULT_STATE = {
    "pool": [],
    "cursor": 0,
    "pool_date": None,
    "downloaded_ids": [],
    "downloaded_titles": []
}

def _get_file_id(filename, folder_id):
    """
    Helper function to locate a file by name inside a specific folder ID on Google Drive.
    """
    if not folder_id:
        raise ValueError(f"Cannot locate '{filename}' without a Drive folder ID.")

    logger.info(f"Searching for file '{filename}' in folder ID: {folder_id}")
    try:
        files = list_files(folder_id)
        for f in files:
            if f.get('name') == filename:
                logger.info(f"Found '{filename}' with ID: {f.get('id')}")
                return f.get('id')
    except Exception as e:
        logger.error(f"Failed to search for file '{filename}' in folder {folder_id}: {e}", exc_info=True)
        raise
    logger.info(f"File '{filename}' not found in folder {folder_id}")
    return None

def load_config():
    """
    Downloads scraper_config.json from Drive database folder, returns DEFAULT_CONFIG if not found.
    """
    logger.info("load_config: Request to load scraper configuration.")
    try:
        _, db_folder_id = get_db_file_id()
        if not db_folder_id:
            raise ValueError("Database folder could not be resolved.")

        file_id = _get_file_id("scraper_config.json", db_folder_id)
        if file_id:
            logger.info(f"load_config: Downloading scraper_config.json from file ID: {file_id}")
            config = download_json(file_id)
            # Ensure loaded config contains all default fields
            full_config = DEFAULT_CONFIG.copy()
            if isinstance(config, dict):
                full_config.update(config)
            logger.info("load_config: Successfully loaded configuration.")
            return full_config
        else:
            logger.info("load_config: scraper_config.json not found on Drive. Returning DEFAULT_CONFIG.")
            return DEFAULT_CONFIG.copy()
    except Exception as e:
        logger.error(f"load_config: Error reading config from Drive: {e}", exc_info=True)
        raise

def save_config(config):
    """
    Uploads scraper_config.json to Drive database folder.
    """
    logger.info("save_config: Request to save scraper configuration.")
    try:
        _, db_folder_id = get_db_file_id()
        if not db_folder_id:
            raise ValueError("Database folder could not be resolved.")

        with library_write_lock("config"):
            file_id = _get_file_id("scraper_config.json", db_folder_id)
            result = upload_json(file_id, config, "scraper_config.json", parent_id=db_folder_id)
        logger.info(f"save_config: Successfully saved config. File ID: {result.get('id')}")
        return result
    except Exception as e:
        logger.error(f"save_config: Error writing config to Drive: {e}", exc_info=True)
        raise

def load_state():
    """
    Downloads scraper_state.json from Drive database folder, returns DEFAULT_STATE if not found.
    """
    logger.info("load_state: Request to load scraper state.")
    try:
        _, db_folder_id = get_db_file_id()
        if not db_folder_id:
            raise ValueError("Database folder could not be resolved.")

        file_id = _get_file_id("scraper_state.json", db_folder_id)
        if file_id:
            logger.info(f"load_state: Downloading scraper_state.json from file ID: {file_id}")
            state = download_json(file_id)
            # Ensure loaded state contains all default fields
            full_state = DEFAULT_STATE.copy()
            if isinstance(state, dict):
                full_state.update(state)
            logger.info("load_state: Successfully loaded state.")
            return full_state
        else:
            logger.info("load_state: scraper_state.json not found on Drive. Returning DEFAULT_STATE.")
            return DEFAULT_STATE.copy()
    except Exception as e:
        logger.error(f"load_state: Error reading state from Drive: {e}", exc_info=True)
        raise

def save_state(state):
    """
    Uploads scraper_state.json to Drive database folder.
    """
    logger.info("save_state: Request to save scraper state.")
    try:
        _, db_folder_id = get_db_file_id()
        if not db_folder_id:
            raise ValueError("Database folder could not be resolved.")

        with library_write_lock("state"):
            file_id = _get_file_id("scraper_state.json", db_folder_id)
            result = upload_json(file_id, state, "scraper_state.json", parent_id=db_folder_id)
        logger.info(f"save_state: Successfully saved state. File ID: {result.get('id')}")
        return result
    except Exception as e:
        logger.error(f"save_state: Error writing state to Drive: {e}", exc_info=True)
        raise

def is_pool_expired(state):
    """
    returns True if pool_date is null or older than auto_refresh_days days.
    """
    logger.info("is_pool_expired: Checking if pool is expired.")
    pool_date_str = state.get("pool_date")
    if not pool_date_str:
        logger.info("is_pool_expired: pool_date is null or missing. Pool is expired.")
        return True

    try:
        # Load configuration to get allowed limits
        config = load_config()
        auto_refresh_days = config.get("auto_refresh_days", 7)

        # Parse ISO pool_date string, e.g. "2026-06-09T10:24:55.123456Z"
        date_str = pool_date_str
        if date_str.endswith('Z'):
            date_str = date_str[:-1]

        pool_datetime = datetime.datetime.fromisoformat(date_str)
        now_datetime = datetime.datetime.utcnow()

        delta = now_datetime - pool_datetime
        expired = delta.days >= int(auto_refresh_days)
        logger.info(f"is_pool_expired: Pool date is {pool_datetime}. Now is {now_datetime}. Age: {delta.days} days. Refresh days: {auto_refresh_days}. Expired: {expired}")
        return expired
    except Exception as e:
        logger.error(f"is_pool_expired: Error checking pool expiration: {e}. Treating as expired.", exc_info=True)
        return True

def find_duplicate_track(track, state, database_tracks):
    """
    checks all three layers and returns the matching track dict from database_tracks, or None:
       - Spotify ID match against state downloaded_ids AND database_tracks
       - Exact title + artist match against database_tracks
       - Fuzzy match using difflib against database_tracks title+artist, threshold 0.85
    """
    title = (track.get("title") or "").strip()
    artist = (track.get("artist") or "").strip()
    logger.info(f"find_duplicate_track: Checking duplicates for track '{title}' by '{artist}'")

    spotify_id = track.get("spotify_id")
    downloaded_ids = state.get("downloaded_ids", []) if isinstance(state, dict) else []
    database_tracks = database_tracks or []
    
    # Layer 1: Spotify ID match against state downloaded_ids AND database_tracks
    if spotify_id:
        for db_track in database_tracks:
            if db_track.get("spotify_id") == spotify_id:
                logger.info(f"find_duplicate_track: Duplicate detected in Layer 1 (DB Spotify ID Match): {spotify_id}")
                return db_track
        # If in downloaded_ids but not in database_tracks, fall through to next layers

    # Normalize track parameters for string comparisons
    norm_title = title.lower()
    norm_artist = artist.lower()
    norm_track_str = f"{norm_title} {norm_artist}"

    for db_track in database_tracks:
        db_title = (db_track.get("title") or "").strip().lower()
        db_artist = (db_track.get("artist") or "").strip().lower()
        db_track_str = f"{db_title} {db_artist}"
        
        # Layer 2: Exact title + artist match against database_tracks
        if norm_title == db_title and norm_artist == db_artist:
            logger.info(f"find_duplicate_track: Duplicate detected in Layer 2 (Exact Title + Artist Match): '{title}' by '{artist}'")
            return db_track

        # Layer 3: Fuzzy match using difflib against database_tracks title+artist, threshold 0.85
        matcher = difflib.SequenceMatcher(None, norm_track_str, db_track_str)
        ratio = matcher.ratio()
        if ratio >= 0.85:
            logger.info(f"find_duplicate_track: Duplicate detected in Layer 3 (Fuzzy Match ratio={ratio:.3f} >= 0.85 with '{db_track.get('title')} by {db_track.get('artist')}')")
            return db_track

    logger.info(f"find_duplicate_track: No duplicate found for '{title}' by '{artist}'")
    return None

def is_duplicate(track, state, database_tracks):
    """
    checks all three layers:
       - Spotify ID match against state downloaded_ids AND database_tracks
       - Exact title + artist match against database_tracks
       - Fuzzy match using difflib against database_tracks title+artist, threshold 0.85
       - Returns True if any layer matches
    """
    return find_duplicate_track(track, state, database_tracks) is not None

def get_effective_pool(state, database_tracks, songs_per_run):
    """
    Returns a fresh pool of tracks by filtering out any duplicates.
    If the remaining tracks are fewer than songs_per_run, returns an empty list to force a refresh.
    """
    pool = state.get("pool", [])
    effective_pool = []
    logger.info(f"get_effective_pool: Filtering {len(pool)} total tracks in current pool.")
    for track in pool:
        if not is_duplicate(track, state, database_tracks):
            effective_pool.append(track)
            
    if len(effective_pool) < songs_per_run:
        logger.warning(f"get_effective_pool: Only {len(effective_pool)} fresh tracks remain. Needed {songs_per_run}. Forcing pool refresh.")
        return []
        
    logger.info(f"get_effective_pool: Returned {len(effective_pool)} fresh tracks ready for processing.")
    return effective_pool
