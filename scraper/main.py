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

from scraper.spotify_charts import build_song_pool
from scraper.metadata_enricher import enrich_track_metadata
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

def determine_language_from_source(source, fallback="unknown"):
    """Legacy helper for fallback"""
    if source == "unknown":
        return fallback
    return fallback


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
    Checks database for tracks missing duration backfill, and uses iTunes API to backfill them.
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
            
        updated_count = 0
        import requests
        for track in tracks:
            if track.get("durationSeconds") is None or track.get("duration") == "--:--":
                title = track.get("title")
                artist = track.get("artist")
                logger.info(f"backfill_durations: Backfilling duration for '{title}' by '{artist}'...")
                
                try:
                    search_term = f"{artist} {title}"
                    itunes_url = "https://itunes.apple.com/search"
                    params = {"term": search_term, "media": "music", "limit": 5}
                    r = requests.get(itunes_url, params=params, timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        results = data.get("results", [])
                        if results:
                            best_match = results[0]
                            track_time_millis = best_match.get("trackTimeMillis")
                            if track_time_millis:
                                duration_seconds = int(track_time_millis) // 1000
                                minutes = duration_seconds // 60
                                seconds = duration_seconds % 60
                                track["duration"] = f"{minutes:02d}:{seconds:02d}"
                                track["durationSeconds"] = duration_seconds
                                updated_count += 1
                                logger.info(f"backfill_durations: Updated duration to {track['duration']}")
                                continue
                except Exception as e:
                    logger.warning(f"backfill_durations: Failed iTunes search for '{title}': {e}")
                    
                track["duration"] = "--:--"
                
        if updated_count > 0:
            logger.info(f"backfill_durations: Uploading updated database with {updated_count} duration backfills...")
            if isinstance(db_data, dict) and 'tracks' in db_data:
                db_data['tracks'] = tracks
                upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
            else:
                upload_json(db_file_id, tracks, 'database.json', parent_id=parent_folder_id)
        else:
            logger.info("backfill_durations: All tracks have valid durations or no updates made.")
            
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
        import requests
        
        for track in tracks:
            lang = track.get("language", "unknown")
            if not lang:
                lang = "unknown"
            lang = lang.lower()
                
            if lang in ("unknown", "", "none"):
                new_lang = "unknown"
                source = track.get("source", "")
                source_lower = source.lower()
                
                # Priority 1: Check source field
                if "jiosaavn" in source_lower:
                    if "malayalam" in source_lower: new_lang = "malayalam"
                    elif "tamil" in source_lower: new_lang = "tamil"
                    elif "hindi" in source_lower: new_lang = "hindi"
                    elif "indian" in source_lower or "top-50" in source_lower: new_lang = "indian"
                elif "global charts" in source_lower or "regional chart" in source_lower or "genre chart" in source_lower:
                    new_lang = "english"
                elif "playlist_import" in source_lower or "spotify_link" in source_lower or "playlist" in source_lower or "spotify" in source_lower:
                    title = track.get("title", "")
                    artist = track.get("artist", "")
                    
                    lang_det, method = detect_track_language(title, artist)
                    if method == "itunes":
                        if lang_det == "hindi": # IND storefront maps to hindi in detect_track_language
                            new_lang = "indian"
                        elif lang_det == "english":
                            new_lang = "english"
                        else:
                            new_lang = "unknown"
                    else:
                        new_lang = "unknown"
                        
                if new_lang != "unknown" and new_lang != lang:
                    track["language"] = new_lang
                    updated_count += 1
                    logger.info(f"Set '{track.get('title')}' language to '{new_lang}' because source='{source}'")
                    
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
    try:
        # Clear the contents of scraper.log by opening it in write mode
        open(log_path, "w", encoding="utf-8").close()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write(f"NEW SCRAPER SESSION STARTED: {datetime.datetime.utcnow().isoformat()}\n")
            f.write("=" * 60 + "\n")
    except Exception as e:
        print(f"Failed to clear scraper.log: {e}")

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
            # Step A: Download audio via yt-dlp
            logger.info(f"Downloading audio stream for: '{title}'...")
            local_file_path = download_track(title, artist, temp_dir)
            
            # Step B: Enrich Metadata (Replaces separate album_art, duration, and language calls)
            logger.info(f"Enriching metadata for '{title}' by '{artist}'...")
            enriched = enrich_track_metadata(title, artist, local_file_path=local_file_path, source=source)
            
            # Step C: Upload file to Google Drive
            logger.info(f"Uploading file '{local_file_path}' to Google Drive...")
            drive_file_id = upload_track(local_file_path)
            
            metadata = {
                "title": title,
                "artist": artist,
                "album": enriched.get("album", "Single"),
                "genre": enriched.get("genre", genre),
                "duration": enriched.get("duration", "--:--"),
                "durationSeconds": enriched.get("durationSeconds"),
                "spotify_id": spotify_id,
                "album_art": enriched.get("album_art"),
                "language": enriched.get("language", "unknown"),
                "source": source,
                "lyrics": enriched.get("lyrics"),
                "syncedLyrics": enriched.get("syncedLyrics")
            }
            
            logger.info("Updating central database.json index on Google Drive...")
            update_database(drive_file_id, metadata)
            
            # Feed newly uploaded track back into local lookup cache
            existing_tracks.append({
                "id": drive_file_id,
                "driveFileId": drive_file_id,
                "title": title,
                "artist": artist,
                "album": enriched.get("album", "Single"),
                "genre": enriched.get("genre", genre),
                "duration": enriched.get("duration", "--:--"),
                "durationSeconds": enriched.get("durationSeconds"),
                "album_art": enriched.get("album_art"),
                "language": enriched.get("language", "unknown"),
                "source": source,
                "lyrics": enriched.get("lyrics"),
                "syncedLyrics": enriched.get("syncedLyrics"),
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

def run_full_enrichment_pass():
    """
    Backfill engine for full metadata enrichment.
    Iterates through the existing database, enriches missing fields, and updates.
    """
    logger.info("Starting run_full_enrichment_pass...")
    db_file_id, parent_id = get_db_file_id()
    if not db_file_id:
        logger.error("No database.json file ID resolved.")
        return {"status": "error", "message": "Database not found."}

    try:
        db_data = download_json(db_file_id)
        if not db_data:
            return {"status": "error", "message": "Database is empty."}

        tracks = db_data if isinstance(db_data, list) else db_data.get('tracks', [])
        
        # Create Backup
        now = datetime.datetime.now()
        backup_filename = f"database_backup_enrich_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json"
        upload_json(None, db_data, backup_filename, parent_id=parent_id)
        logger.info(f"Created backup: {backup_filename}")

        updated = False
        initial_counts = {
            "album_art_missing": 0, "duration_missing": 0, "language_missing": 0,
            "genre_missing": 0, "album_missing": 0, "lyrics_missing": 0
        }
        final_counts = {
            "album_art_missing": 0, "duration_missing": 0, "language_missing": 0,
            "genre_missing": 0, "album_missing": 0, "lyrics_missing": 0
        }

        for i, t in enumerate(tracks):
            title = t.get("title", "Unknown Title")
            artist = t.get("artist", "Unknown Artist")
            source = t.get("source", "unknown")
            
            # Identify missing fields
            needs_enrichment = False
            
            # Initial tally
            if not t.get("album_art"): initial_counts["album_art_missing"] += 1
            if not t.get("duration") or t.get("duration") == "--:--": initial_counts["duration_missing"] += 1
            if not t.get("language") or t.get("language") == "unknown": initial_counts["language_missing"] += 1
            if not t.get("genre") or t.get("genre") == "Unknown": initial_counts["genre_missing"] += 1
            if not t.get("album") or t.get("album") == "Unknown Album": initial_counts["album_missing"] += 1
            if not t.get("lyrics"): initial_counts["lyrics_missing"] += 1
            
            if not t.get("album_art") or \
               not t.get("duration") or t.get("duration") == "--:--" or \
               not t.get("language") or t.get("language") == "unknown" or \
               not t.get("genre") or t.get("genre") == "Unknown" or \
               not t.get("album") or t.get("album") == "Unknown Album" or \
               not t.get("lyrics"):
                needs_enrichment = True
                
            if needs_enrichment:
                logger.info(f"Enriching [{i+1}/{len(tracks)}]: '{title}' by '{artist}'...")
                enriched = enrich_track_metadata(title, artist, local_file_path=None, source=source)
                
                if not t.get("album_art") and enriched.get("album_art"):
                    t["album_art"] = enriched["album_art"]
                    t["albumArt"] = enriched["album_art"]
                    updated = True
                if (not t.get("duration") or t.get("duration") == "--:--") and enriched.get("duration") != "--:--":
                    t["duration"] = enriched["duration"]
                    t["durationSeconds"] = enriched["durationSeconds"]
                    updated = True
                if (not t.get("language") or t.get("language") == "unknown") and enriched.get("language") != "unknown":
                    t["language"] = enriched["language"]
                    updated = True
                if (not t.get("genre") or t.get("genre") == "Unknown") and enriched.get("genre") != "Unknown":
                    t["genre"] = enriched["genre"]
                    updated = True
                if (not t.get("album") or t.get("album") == "Unknown Album") and enriched.get("album") != "Unknown Album":
                    t["album"] = enriched["album"]
                    updated = True
                if not t.get("lyrics") and enriched.get("lyrics"):
                    t["lyrics"] = enriched["lyrics"]
                    t["syncedLyrics"] = enriched["syncedLyrics"]
                    updated = True

            # Final tally
            if not t.get("album_art"): final_counts["album_art_missing"] += 1
            if not t.get("duration") or t.get("duration") == "--:--": final_counts["duration_missing"] += 1
            if not t.get("language") or t.get("language") == "unknown": final_counts["language_missing"] += 1
            if not t.get("genre") or t.get("genre") == "Unknown": final_counts["genre_missing"] += 1
            if not t.get("album") or t.get("album") == "Unknown Album": final_counts["album_missing"] += 1
            if not t.get("lyrics"): final_counts["lyrics_missing"] += 1

        if updated:
            if isinstance(db_data, dict):
                db_data['tracks'] = tracks
            upload_json(db_file_id, db_data, 'database.json', parent_id=parent_id)
            logger.info("run_full_enrichment_pass: Database updated successfully.")
        else:
            logger.info("run_full_enrichment_pass: No new metadata found. Database unchanged.")

        result = {
            "status": "success",
            "total_tracks": len(tracks),
            "updated": updated,
            "initial_counts": initial_counts,
            "final_counts": final_counts
        }
        logger.info(f"run_full_enrichment_pass complete: {result}")
        return result

    except Exception as e:
        logger.error(f"Error in run_full_enrichment_pass: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

def run_complete_backfill():
    """
    Advanced backfill engine that processes all tracks and aggressively fills missing fields
    using lenient iTunes matching, LRCLIB fuzzy matching, and intelligent source inference.
    """
    import requests
    import difflib
    from dashboard.drive_client import upload_json
    from scraper.drive_uploader import audit_database_fields
    from scraper.metadata_enricher import detect_script_language_from_lyrics
    from scraper.spotify_charts import detect_track_language
    
    logger.info("Starting run_complete_backfill...")
    db_file_id, parent_id = get_db_file_id()
    if not db_file_id:
        logger.error("No database.json file ID resolved.")
        return {"status": "error", "message": "Database not found."}

    try:
        db_data = download_json(db_file_id)
        if not db_data:
            return {"status": "error", "message": "Database is empty."}

        tracks = db_data if isinstance(db_data, list) else db_data.get('tracks', [])
        
        # 1. Create Backup
        now = datetime.datetime.now()
        backup_filename = f"database_backup_complete_backfill_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json"
        upload_json(None, db_data, backup_filename, parent_id=parent_id)
        logger.info(f"Created backup before complete backfill: {backup_filename}")

        # 2. Audit Baseline
        audit_before = audit_database_fields()
        logger.info(f"Audit before backfill: {audit_before['missing_counts']}")

        processed_count = 0
        updated_since_last_save = False

        for i, t in enumerate(tracks):
            title = t.get("title", "Unknown Title")
            artist = t.get("artist", "Unknown Artist")
            track_updated = False
            
            logger.info(f"Complete Backfill [{i+1}/{len(tracks)}]: '{title}' by '{artist}'...")
            
            # Identify missing fields
            missing_art = not t.get("album_art") and not t.get("albumArt")
            missing_dur = not t.get("duration") or t.get("duration") == "--:--"
            missing_genre = not t.get("genre") or t.get("genre") == "Unknown"
            missing_album = not t.get("album") or t.get("album") == "Unknown Album"
            missing_lyrics = not t.get("lyrics")
            missing_synced = not t.get("syncedLyrics")
            missing_lang = not t.get("language") or t.get("language") == "unknown"
            missing_source = not t.get("source") or t.get("source") == "unknown"

            # ALBUM_ART, GENRE, ALBUM
            if missing_art or missing_genre or missing_album:
                search_term = f"{artist} {title}"
                itunes_url = "https://itunes.apple.com/search"
                try:
                    r = requests.get(itunes_url, params={"term": search_term, "media": "music", "limit": 10}, timeout=5)
                    if r.status_code == 200:
                        results = r.json().get("results", [])
                        best_match = None
                        best_score = -1.0
                        norm_title = title.lower()
                        norm_artist = artist.lower()
                        for item in results:
                            res_title = (item.get("trackName") or "").lower()
                            res_artist = (item.get("artistName") or "").lower()
                            score = difflib.SequenceMatcher(None, norm_title, res_title).ratio() + \
                                    difflib.SequenceMatcher(None, norm_artist, res_artist).ratio()
                            if score > best_score:
                                best_score = score
                                best_match = item
                        
                        # More lenient match (e.g. > 0.8 out of 2.0)
                        if best_match and best_score > 0.8:
                            if missing_art and best_match.get("artworkUrl100"):
                                art = best_match.get("artworkUrl100").replace("100x100", "600x600").replace("100x100bb", "600x600bb")
                                t["album_art"] = art
                                t["albumArt"] = art
                                track_updated = True
                                missing_art = False
                            if missing_album and best_match.get("collectionName"):
                                t["album"] = best_match.get("collectionName")
                                track_updated = True
                                missing_album = False
                            if missing_genre and best_match.get("primaryGenreName"):
                                t["genre"] = best_match.get("primaryGenreName")
                                track_updated = True
                                missing_genre = False
                except Exception as e:
                    logger.warning(f"Lenient iTunes lookup failed for {title}: {e}")

            # DURATION
            if missing_dur:
                try:
                    r = requests.get("https://itunes.apple.com/search", params={"term": artist, "media": "music", "limit": 25}, timeout=5)
                    if r.status_code == 200:
                        results = r.json().get("results", [])
                        best_match = None
                        best_score = -1.0
                        norm_title = title.lower()
                        for item in results:
                            res_title = (item.get("trackName") or "").lower()
                            score = difflib.SequenceMatcher(None, norm_title, res_title).ratio()
                            if score > best_score:
                                best_score = score
                                best_match = item
                        if best_match and best_score > 0.6 and best_match.get("trackTimeMillis"):
                            duration_seconds = int(best_match.get("trackTimeMillis")) // 1000
                            t["duration"] = f"{duration_seconds // 60:02d}:{duration_seconds % 60:02d}"
                            t["durationSeconds"] = duration_seconds
                            track_updated = True
                except Exception as e:
                    logger.warning(f"Artist-only duration lookup failed for {title}: {e}")

            # GENRE (Artist-only fallback)
            if missing_genre:
                try:
                    r = requests.get("https://itunes.apple.com/search", params={"term": artist, "media": "music", "limit": 25}, timeout=5)
                    if r.status_code == 200:
                        results = r.json().get("results", [])
                        genres = [item.get("primaryGenreName") for item in results if item.get("primaryGenreName")]
                        if genres:
                            from collections import Counter
                            t["genre"] = Counter(genres).most_common(1)[0][0]
                            track_updated = True
                except Exception as e:
                    pass

            # LYRICS
            if missing_lyrics or missing_synced:
                try:
                    headers = {"User-Agent": "CloudMusicPlayer/1.0.0"}
                    d_sec = t.get("durationSeconds") or 0
                    alb = t.get("album") if t.get("album") != "Unknown Album" else ""
                    
                    # 1. Exact
                    found_lyrics = False
                    r_exact = requests.get("https://lrclib.net/api/get", params={"artist_name": artist, "track_name": title, "album_name": alb, "duration": d_sec}, headers=headers, timeout=5)
                    if r_exact.status_code == 200 and r_exact.json().get("plainLyrics"):
                        t["lyrics"] = r_exact.json().get("plainLyrics")
                        t["syncedLyrics"] = r_exact.json().get("syncedLyrics")
                        found_lyrics = True
                        track_updated = True
                    
                    # 2. Fuzzy title+artist
                    if not found_lyrics:
                        r_search = requests.get("https://lrclib.net/api/search", params={"track_name": title, "artist_name": artist}, headers=headers, timeout=5)
                        if r_search.status_code == 200 and isinstance(r_search.json(), list):
                            items = r_search.json()
                            best_match = None
                            best_score = -1.0
                            norm_title = title.lower()
                            norm_artist = artist.lower()
                            for item in items:
                                s = difflib.SequenceMatcher(None, norm_title, (item.get("trackName") or "").lower()).ratio() + \
                                    difflib.SequenceMatcher(None, norm_artist, (item.get("artistName") or "").lower()).ratio()
                                if s > best_score:
                                    best_score = s
                                    best_match = item
                            if best_match and best_score > 1.0 and best_match.get("plainLyrics"):
                                t["lyrics"] = best_match.get("plainLyrics")
                                t["syncedLyrics"] = best_match.get("syncedLyrics")
                                found_lyrics = True
                                track_updated = True
                    
                    # 3. Fuzzy title only
                    if not found_lyrics:
                        r_search_title = requests.get("https://lrclib.net/api/search", params={"q": title}, headers=headers, timeout=5)
                        if r_search_title.status_code == 200 and isinstance(r_search_title.json(), list):
                            items = r_search_title.json()
                            best_match = None
                            best_score = -1.0
                            norm_title = title.lower()
                            for item in items:
                                s = difflib.SequenceMatcher(None, norm_title, (item.get("trackName") or "").lower()).ratio()
                                if s > best_score:
                                    best_score = s
                                    best_match = item
                            if best_match and best_score > 0.8 and best_match.get("plainLyrics"):
                                t["lyrics"] = best_match.get("plainLyrics")
                                t["syncedLyrics"] = best_match.get("syncedLyrics")
                                track_updated = True
                except Exception as e:
                    logger.warning(f"LRCLIB fallback lookup failed for {title}: {e}")

            # LANGUAGE
            if missing_lang:
                new_lang = "unknown"
                if t.get("lyrics"):
                    script_lang = detect_script_language_from_lyrics(t.get("lyrics"))
                    if script_lang: new_lang = script_lang
                
                if new_lang == "unknown":
                    new_lang = determine_language_from_source(t.get("source"), "unknown")
                    
                if new_lang == "unknown":
                    lang_det, _ = detect_track_language(title, artist)
                    if lang_det == "hindi": new_lang = "indian"
                    elif lang_det in ["english", "malayalam", "tamil"]: new_lang = lang_det
                    
                if new_lang != "unknown" and new_lang != t.get("language"):
                    t["language"] = new_lang
                    track_updated = True

            # SOURCE
            if missing_source:
                added_at = t.get("addedAt") or t.get("timestamp")
                if added_at:
                    if "2026-06-1" in added_at:
                        t["source"] = "scraper"
                        logger.info(f"Source inferred as 'scraper' based on timestamp {added_at} for '{title}'")
                    else:
                        t["source"] = "legacy"
                        logger.info(f"Source inferred as 'legacy' based on timestamp {added_at} for '{title}'")
                else:
                    t["source"] = "legacy"
                    logger.info(f"Source inferred as 'legacy' due to missing timestamp for '{title}'")
                track_updated = True

            if track_updated:
                updated_since_last_save = True

            processed_count += 1
            # Incremental save
            if processed_count % 15 == 0 and updated_since_last_save:
                if isinstance(db_data, dict):
                    db_data['tracks'] = tracks
                upload_json(db_file_id, db_data, 'database.json', parent_id=parent_id)
                logger.info(f"Incremental save completed after processing {processed_count} tracks.")
                updated_since_last_save = False

        # Final Save
        if updated_since_last_save:
            if isinstance(db_data, dict):
                db_data['tracks'] = tracks
            upload_json(db_file_id, db_data, 'database.json', parent_id=parent_id)
            logger.info("Final database save completed.")

        # Audit After
        audit_after = audit_database_fields()
        
        logger.info("=" * 60)
        logger.info("COMPLETE BACKFILL SUMMARY:")
        logger.info(f"Before: {audit_before['missing_counts']}")
        logger.info(f"After:  {audit_after['missing_counts']}")
        logger.info("=" * 60)

        result = {
            "status": "success",
            "total_tracks": len(tracks),
            "audit_before": audit_before,
            "audit_after": audit_after
        }
        return result

    except Exception as e:
        logger.error(f"Error in run_complete_backfill: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    run_scraper()
