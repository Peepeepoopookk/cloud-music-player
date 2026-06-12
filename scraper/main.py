import os
import sys
import time
import logging
import datetime
import subprocess
import json

# Set project root and add to sys.path first to ensure absolute imports resolve correctly
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Configure logger with multiple handlers (Console and File)
log_path = os.path.join(project_root, 'scraper.log')
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clear existing handlers to prevent duplicate messages
if logger.handlers:
    logger.handlers.clear()

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Reconfigure stdout for UTF-8 on Windows
sys.stdout.reconfigure(encoding='utf-8')

# Console Handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
console_handler.encoding = 'utf-8'
logger.addHandler(console_handler)

# File Handler
file_handler = logging.FileHandler(log_path, encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

from dotenv import load_dotenv
# Load env variables
load_dotenv(dotenv_path=os.path.join(project_root, '.env'))

# Import functions from other modules
from scraper.spotify_charts import build_song_pool, fetch_album_art, detect_track_language
from scraper.downloader import download_track
from scraper.drive_uploader import upload_track, update_database, get_db_file_id
from dashboard.drive_client import download_json, upload_json
from scraper.state_manager import (
    load_config,
    save_config,
    load_state,
    save_state,
    is_pool_expired,
    is_duplicate,
    get_effective_pool
)

def extract_duration(file_path):
    """
    Extracts accurate duration using ffprobe.
    Returns (duration_string, duration_seconds) e.g., ("03:35", 215).
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration_float = float(data['streams'][0]['duration'])
            duration_seconds = int(round(duration_float))
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            return f"{minutes:02d}:{seconds:02d}", duration_seconds
    except Exception as e:
        logger.warning(f"Could not read audio duration using ffprobe: {e}")
    return "--:--", None

def backfill_album_art():
    """
    Loads all tracks from database.json on Drive, and backfills missing or null album_art.
    """
    logger.info("Starting album art backfill task...")
    db_file_id, parent_folder_id = get_db_file_id()
    if not db_file_id:
        logger.info("backfill_album_art: No database.json file ID resolved. Skipping backfill.")
        return
        
    try:
        db_data = download_json(db_file_id)
        if not db_data:
            logger.info("backfill_album_art: Database is empty. Skipping backfill.")
            return
            
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
        else:
            logger.warning("backfill_album_art: Invalid database format. Skipping backfill.")
            return
            
        backfilled_count = 0
        for track in tracks:
            if not track.get("album_art"):
                title = track.get("title")
                artist = track.get("artist")
                if title and artist:
                    logger.info(f"backfill_album_art: Backfilling album art for '{title}' by '{artist}'...")
                    art = fetch_album_art(title, artist)
                    if art:
                        track["album_art"] = art
                        track["albumArt"] = art
                        backfilled_count += 1
                        
        if backfilled_count > 0:
            logger.info(f"backfill_album_art: Uploading updated database with {backfilled_count} backfilled album arts...")
            if isinstance(db_data, dict) and 'tracks' in db_data:
                db_data['tracks'] = tracks
                upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
            else:
                upload_json(db_file_id, tracks, 'database.json', parent_id=parent_folder_id)
                
        logger.info(f"backfill_album_art: Finished. {backfilled_count} tracks were backfilled.")
    except Exception as e:
        logger.error(f"backfill_album_art: Error during backfilling: {e}", exc_info=True)

def backfill_durations():
    """
    Checks database for tracks missing duration backfill and logs the count.
    """
    logger.info("Starting duration backfill check task...")
    db_file_id, parent_folder_id = get_db_file_id()
    if not db_file_id:
        return
        
    try:
        db_data = download_json(db_file_id)
        if not db_data:
            return
            
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
        else:
            return
            
        needs_backfill = 0
        for track in tracks:
            if track.get("durationSeconds") is None or track.get("duration") == "--:--":
                needs_backfill += 1
                
        if needs_backfill > 0:
            logger.info(f"backfill_durations: {needs_backfill} tracks still need duration backfill, but local files are unavailable. Skipping.")
        else:
            logger.info("backfill_durations: All tracks have valid durations.")
            
    except Exception as e:
        logger.error(f"backfill_durations: Error during backfilling check: {e}", exc_info=True)

def determine_language_from_source(source, current_language="unknown"):
    source_lower = (source or "").lower()
    if "jiosaavn" in source_lower:
        if "malayalam" in source_lower: return "malayalam"
        if "tamil" in source_lower: return "tamil"
        if "hindi" in source_lower: return "hindi"
        if "indian" in source_lower or "india" in source_lower: return "indian"
    if "itunes india" in source_lower: return "indian"
    if "global" in source_lower or "genre" in source_lower: return "english"
    if "regional" in source_lower and ("(us)" in source_lower or "(gb)" in source_lower):
        return "english"
    return current_language if current_language else "unknown"

def backfill_languages():
    """
    Checks database for tracks missing language or labeled 'unknown', and backfills them.
    """
    logger.info("Starting language backfill check task...")
    db_file_id, parent_folder_id = get_db_file_id()
    if not db_file_id:
        return
        
    try:
        db_data = download_json(db_file_id)
        if not db_data:
            return
            
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
        else:
            return
            
        updated_count = 0
        for track in tracks:
            lang = track.get("language", "unknown").lower()
            if lang == "unknown" or lang == "":
                new_lang = determine_language_from_source(track.get("source"), lang)
                if new_lang == "unknown" and (not track.get("source") or track.get("source").lower() == "unknown"):
                    detected_lang, _ = detect_track_language(track.get("title"), track.get("artist"))
                    new_lang = detected_lang
                    
                if new_lang != lang and new_lang != "unknown":
                    track["language"] = new_lang
                    updated_count += 1
                    logger.info(f"backfill_languages: Updated '{track.get('title')}' language to {new_lang}")
                    
        if updated_count > 0:
            logger.info(f"backfill_languages: Uploading updated database with {updated_count} language backfills...")
            if isinstance(db_data, dict) and 'tracks' in db_data:
                db_data['tracks'] = tracks
                upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
            else:
                upload_json(db_file_id, tracks, 'database.json', parent_id=parent_folder_id)
        else:
            logger.info("backfill_languages: All tracks have valid languages or no updates needed.")
            
    except Exception as e:
        logger.error(f"backfill_languages: Error during language backfilling: {e}", exc_info=True)

def run_scraper():
    """
    Orchestrates the scraping job using a pool-based structure:
    Loads config, loads state, retrieves database tracks, updates pool if expired,
    processes tracks from cursor position, downloads allowed genres and languages,
    deduplicates tracks, and logs execution.
    """
    logger.info("=" * 60)
    logger.info("Starting pool-based Spotify Charts crawler and uploader job...")
    logger.info("=" * 60)
    
    # Startup validation
    gdrive_folder_id = os.environ.get('GDRIVE_FOLDER_ID')
    gdrive_media_folder_id = os.environ.get('GDRIVE_MEDIA_FOLDER_ID')
    gdrive_db_file_id = os.environ.get('GDRIVE_DB_FILE_ID')
    
    if not gdrive_folder_id:
        logger.error("GDRIVE_FOLDER_ID is not set in environment variables.")
        sys.exit(1)
    if not gdrive_media_folder_id:
        logger.error("GDRIVE_MEDIA_FOLDER_ID is not set in environment variables.")
        sys.exit(1)
    if not gdrive_db_file_id:
        logger.error("GDRIVE_DB_FILE_ID is not set in environment variables.")
        sys.exit(1)
        
    try:
        from dashboard.drive_client import get_file_metadata
        get_file_metadata(gdrive_db_file_id)
    except Exception as e:
        if "404" in str(e):
            logger.error("GDRIVE_DB_FILE_ID is invalid or account does not have access to this file")
        else:
            logger.error(f"Drive API test call failed: {e}")
        sys.exit(1)
        
    # Run backfill first
    backfill_album_art()
    backfill_durations()
    backfill_languages()
    
    # 1. Load config and state
    config = load_config()
    state = load_state()
    
    # 2. Get local temp downloads directory
    temp_dir = os.environ.get('TEMP_DIR')
    if not temp_dir:
        temp_dir = os.path.join(project_root, 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    logger.info(f"Using temporary downloads directory: {temp_dir}")
    
    # 3. Retrieve existing tracks index from database.json to check for duplicates
    existing_tracks = []
    db_file_id, _ = get_db_file_id()
    if db_file_id:
        try:
            logger.info(f"Downloading current database file ID: {db_file_id} to check for duplicate tracks...")
            existing_data = download_json(db_file_id)
            if isinstance(existing_data, list):
                existing_tracks = existing_data
            elif isinstance(existing_data, dict) and 'tracks' in existing_data:
                existing_tracks = existing_data['tracks']
            logger.info(f"Loaded {len(existing_tracks)} existing tracks from index database.")
        except Exception as e:
            logger.warning(f"Could not retrieve existing tracks index: {e}. Proceeding assuming empty library.")
    else:
        logger.info("No database.json file found on Google Drive. Will initialize a new database index.")

    # Allow overriding songs_per_run via environment variable for manual workflow dispatch
    env_songs = os.environ.get("SONGS_PER_RUN")
    if env_songs and env_songs.isdigit():
        songs_per_run = int(env_songs)
    else:
        songs_per_run = config.get("songs_per_run", 5)

    # 4. Check if pool is expired
    if is_pool_expired(state):
        logger.info("Pool is expired. Generating a fresh pool...")
        fresh_pool = build_song_pool(config)
        state["pool"] = fresh_pool
        state["cursor"] = 0
        state["pool_date"] = datetime.datetime.utcnow().isoformat() + 'Z'
        logger.info(f"Pool refreshed with {len(fresh_pool)} songs.")
        # Save state immediately to reflect pool update
        save_state(state)
        
    # Get remaining fresh tracks
    effective_pool = get_effective_pool(state, existing_tracks, songs_per_run)
    
    if not effective_pool:
        logger.info("Effective pool is empty or has too few tracks. Forcing a refresh...")
        fresh_pool = build_song_pool(config)
        state["pool"] = fresh_pool
        state["cursor"] = 0
        state["pool_date"] = datetime.datetime.utcnow().isoformat() + 'Z'
        save_state(state)
        
        effective_pool = get_effective_pool(state, existing_tracks, songs_per_run)
        if not effective_pool:
            logger.warning("Still not enough fresh tracks after forcing a pool refresh. Exiting gracefully.")
            return

    cursor = state.get("cursor", 0)
    # Ensure cursor isn't somehow out of bounds of effective pool
    if cursor >= len(effective_pool):
        cursor = 0
        state["cursor"] = 0
        save_state(state)

    downloaded_songs = []
    skipped_songs = []
    failed_songs = []
    
    logger.info(f"Current effective pool size: {len(effective_pool)}. Resume index: {cursor}. Quota target: {songs_per_run} songs.")
    
    # 5. Process tracks
    while cursor < len(effective_pool) and len(downloaded_songs) < songs_per_run:
        track = effective_pool[cursor]
        title = track.get("title")
        artist = track.get("artist")
        spotify_id = track.get("spotify_id")
        genre = track.get("genre", "Unknown")
        source = track.get("source", "Unknown")
        language = determine_language_from_source(source, track.get("language", "unknown"))
        
        # Triple check duplicate
        if is_duplicate(track, state, existing_tracks):
            logger.info(f"Skipping duplicate track (triple check): '{title}' by '{artist}' (Spotify ID: {spotify_id}).")
            skipped_songs.append(f"{title} by {artist}")
            cursor += 1
            state["cursor"] = cursor
            save_state(state)
            continue
            
        logger.info("-" * 50)
        logger.info(f"Processing candidate track [{cursor}]: '{title}' by '{artist}' (Genre: {genre})")
        logger.info("-" * 50)
        
        local_file_path = None
        try:
            # Fetch album art before downloading
            album_art = fetch_album_art(title, artist)
            
            # Step A: Download audio via yt-dlp
            logger.info(f"Downloading audio stream for: '{title}'...")
            local_file_path = download_track(title, artist, temp_dir)
            
            # Step B: Upload file to Google Drive
            logger.info(f"Uploading file '{local_file_path}' to Google Drive...")
            drive_file_id = upload_track(local_file_path)
            
            # Step C: Extract duration and update index database on Drive
            duration, duration_seconds = extract_duration(local_file_path)
            logger.info(f"Extracted audio duration: {duration} ({duration_seconds}s)")
            
            metadata = {
                "title": title,
                "artist": artist,
                "album": "Single",
                "genre": genre,
                "duration": duration,
                "durationSeconds": duration_seconds,
                "spotify_id": spotify_id,
                "album_art": album_art,
                "language": language,
                "source": source
            }
            
            logger.info("Updating central database.json index on Google Drive...")
            update_database(drive_file_id, metadata)
            
            # Feed newly uploaded track back into local lookup cache
            existing_tracks.append({
                "id": drive_file_id,
                "driveFileId": drive_file_id,
                "title": title,
                "artist": artist,
                "album": "Single",
                "genre": genre,
                "duration": duration,
                "durationSeconds": duration_seconds,
                "album_art": album_art,
                "language": language,
                "source": source,
                "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
                "spotify_id": spotify_id
            })
            
            # Update state variables immediately
            if spotify_id and spotify_id != "UnknownID":
                state["downloaded_ids"].append(spotify_id)
            state["downloaded_titles"].append(title)
            
            cursor += 1
            state["cursor"] = cursor
            downloaded_songs.append(f"{title} by {artist}")
            logger.info(f"Successfully processed track: '{title}' by '{artist}'")
            
            # Save updated state after every successful download
            save_state(state)
            
        except Exception as track_err:
            logger.error(f"Failed to process track '{title}' by '{artist}': {track_err}", exc_info=True)
            failed_songs.append(f"{title} by {artist}")
            cursor += 1
            state["cursor"] = cursor
            # Save state to maintain the cursor position even on failure
            save_state(state)
            
        finally:
            if local_file_path and os.path.exists(local_file_path):
                try:
                    os.remove(local_file_path)
                    logger.info(f"Cleaned up local temp file: {local_file_path}")
                except Exception as cleanup_err:
                    logger.warning(f"Could not remove local temp file {local_file_path}: {cleanup_err}")
                    
    # Check if pool was exhausted before meeting quota
    if cursor >= len(effective_pool) and len(downloaded_songs) < songs_per_run:
        logger.warning(f"Effective pool exhausted (processed {len(downloaded_songs)}/{songs_per_run} songs). Will refresh on next run.")
        state["pool_date"] = None
        state["cursor"] = 0
        save_state(state)
        
    # final logs summary
    logger.info("=" * 60)
    logger.info("Spotify Charts crawler and uploader job finished.")
    logger.info(f"Summary: Downloaded: {len(downloaded_songs)} | Skipped: {len(skipped_songs)} | Failed: {len(failed_songs)}")
    if downloaded_songs:
        logger.info("Downloaded songs:")
        for s in downloaded_songs:
            logger.info(f" - {s}")
    if skipped_songs:
        logger.info("Skipped duplicate songs:")
        for s in skipped_songs:
            logger.info(f" - {s}")
    if failed_songs:
        logger.info("Failed songs:")
        for s in failed_songs:
            logger.info(f" - {s}")
    logger.info("=" * 60)

if __name__ == "__main__":
    run_scraper()
