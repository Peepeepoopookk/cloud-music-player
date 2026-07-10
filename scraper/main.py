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

from scraper.spotify_charts import build_song_pool, detect_track_language
from scraper.metadata_enricher import enrich_track_metadata
from scraper.downloader import download_track
from scraper.drive_uploader import upload_track, bulk_update_database, get_db_file_id, fetch_album_art
from scraper.album_art_resolver import resolve_album_art_with_details
from scraper.duration_resolver import resolve_duration_with_details
from scraper.gemini_import_pipeline import GEMINI_IMPORT_BATCH_SIZE, apply_gemini_to_import_batch
from scraper.lyrics_resolver import resolve_lyrics_with_details
from scraper.metadata_enricher import detect_script_mixing
from dashboard.drive_client import download_json, upload_json
from scraper.operation_lock import library_write_lock
from scraper.state_manager import (
    load_config,
    save_config,
    load_state,
    save_state,
    is_pool_expired,
    is_duplicate,
    get_effective_pool
)

def upload_database_json_locked(db_file_id, db_data, parent_id):
    with library_write_lock("database"):
        return upload_json(db_file_id, db_data, 'database.json', parent_id=parent_id)

def determine_language_from_source(source, fallback="unknown"):
    """Legacy helper for fallback"""
    if source == "unknown":
        return fallback
    return fallback


def _append_backfill_task_log(task_state, level, message):
    if task_state is None:
        return
    task_state.setdefault("logs", []).append({
        "time": datetime.datetime.utcnow().isoformat() + "Z",
        "level": level,
        "message": message
    })
    task_state["logs"] = task_state["logs"][-200:]


def _album_art_missing(track):
    return not (track.get("album_art") or track.get("albumArt"))


def _duration_missing(track):
    return track.get("durationSeconds") is None or not track.get("duration") or track.get("duration") == "--:--"


def _lyrics_missing(track):
    return not (track.get("lyrics") or track.get("syncedLyrics"))


def backfill_album_art(task_state=None, cancel_event=None, save_every=10):
    """
    Loads all tracks from database.json on Drive, backfills missing album_art, and
    reports progress/source diagnostics when a dashboard task_state is provided.
    """
    logger.info("Starting album art backfill task...")
    _append_backfill_task_log(task_state, "info", "Album art backfill started.")
    db_file_id, parent_folder_id = get_db_file_id()
    if not db_file_id:
        logger.info("backfill_album_art: No database.json file ID resolved. Skipping backfill.")
        _append_backfill_task_log(task_state, "error", "database.json could not be resolved.")
        return {"status": "error", "message": "database.json file ID not found."}
        
    try:
        db_data = download_json(db_file_id)
        if not db_data:
            logger.info("backfill_album_art: Database is empty. Skipping backfill.")
            _append_backfill_task_log(task_state, "warning", "database.json is empty.")
            return {"status": "error", "message": "Database is empty."}
            
        if isinstance(db_data, list):
            tracks = db_data
            was_dict = False
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
            was_dict = True
        else:
            logger.warning("backfill_album_art: Invalid database format. Skipping backfill.")
            _append_backfill_task_log(task_state, "error", "database.json format is not supported.")
            return {"status": "error", "message": "Invalid database format."}

        missing_tracks = [track for track in tracks if _album_art_missing(track)]
        total_missing = len(missing_tracks)
        source_counts = {}
        backfilled_count = 0
        failed_count = 0
        pending_save = False

        if task_state is not None:
            task_state["processed"] = 0
            task_state["total_candidates"] = total_missing
            task_state["album_art_initial_missing"] = total_missing
            task_state["album_art_downloaded"] = 0
            task_state["album_art_remaining"] = total_missing
            task_state["album_art_failed"] = 0
            task_state["album_art_source_counts"] = {}

        logger.info(f"backfill_album_art: Found {total_missing} tracks missing album art.")
        _append_backfill_task_log(task_state, "info", f"Found {total_missing} tracks missing album art.")

        for index, track in enumerate(missing_tracks, start=1):
            if cancel_event and cancel_event.is_set():
                logger.info("backfill_album_art: Cancel requested. Saving progress before stopping.")
                _append_backfill_task_log(task_state, "warning", "Cancel requested; saving resolved artwork before stopping.")
                break

            title = track.get("title")
            artist = track.get("artist")
            album = track.get("album")
            if not title or not artist:
                failed_count += 1
                _append_backfill_task_log(task_state, "warning", f"Skipped track {index}/{total_missing}: missing title or artist.")
                continue

            if task_state is not None:
                task_state["current_track"] = f"{title} - {artist}"

            logger.info(f"backfill_album_art [{index}/{total_missing}]: Resolving '{title}' by '{artist}'...")
            try:
                result = resolve_album_art_with_details(title, artist, album=album)
                art = result.get("url")
                if art:
                    source = result.get("source") or "unknown"
                    track["album_art"] = art
                    track["albumArt"] = art
                    track["updatedAt"] = datetime.datetime.utcnow().isoformat() + "Z"
                    if result.get("metadata", {}).get("album") and (not album or album == "Unknown Album"):
                        track["album"] = result["metadata"]["album"]
                    if result.get("metadata", {}).get("genre") and (not track.get("genre") or track.get("genre") == "Unknown"):
                        track["genre"] = result["metadata"]["genre"]

                    backfilled_count += 1
                    source_counts[source] = source_counts.get(source, 0) + 1
                    pending_save = True
                    logger.info(f"backfill_album_art: Found artwork via {source} for '{title}' by '{artist}'.")
                    _append_backfill_task_log(task_state, "success", f"{index}/{total_missing}: {title} - {artist} -> {source}")
                else:
                    failed_count += 1
                    attempt_summary = ", ".join(f"{a.get('source')}:{a.get('status')}" for a in result.get("attempts", []))
                    logger.info(f"backfill_album_art: No artwork found for '{title}' by '{artist}'. Attempts: {attempt_summary}")
                    _append_backfill_task_log(task_state, "warning", f"{index}/{total_missing}: no art found for {title} - {artist}")
            except Exception as e:
                failed_count += 1
                logger.warning(f"backfill_album_art: Resolver failed for '{title}' by '{artist}': {e}")
                _append_backfill_task_log(task_state, "error", f"{index}/{total_missing}: resolver failed for {title} - {artist}: {e}")

            if task_state is not None:
                task_state["processed"] = index
                task_state["album_art_downloaded"] = backfilled_count
                task_state["album_art_failed"] = failed_count
                task_state["album_art_remaining"] = sum(1 for t in tracks if _album_art_missing(t))
                task_state["album_art_source_counts"] = dict(source_counts)

            if pending_save and (backfilled_count % save_every == 0):
                if was_dict:
                    db_data['tracks'] = tracks
                    upload_database_json_locked(db_file_id, db_data, parent_folder_id)
                else:
                    upload_database_json_locked(db_file_id, tracks, parent_folder_id)
                logger.info(f"backfill_album_art: Incremental save after {backfilled_count} artwork updates.")
                _append_backfill_task_log(task_state, "info", f"Saved {backfilled_count} artwork updates to database.json.")
                pending_save = False
            time.sleep(0.1)

        if pending_save:
            if was_dict:
                db_data['tracks'] = tracks
                upload_database_json_locked(db_file_id, db_data, parent_folder_id)
            else:
                upload_database_json_locked(db_file_id, tracks, parent_folder_id)
            logger.info("backfill_album_art: Final database save completed.")
            _append_backfill_task_log(task_state, "info", "Final artwork updates saved to database.json.")

        remaining_missing = sum(1 for track in tracks if _album_art_missing(track))
        if task_state is not None:
            task_state["album_art_downloaded"] = backfilled_count
            task_state["album_art_remaining"] = remaining_missing
            task_state["album_art_failed"] = failed_count
            task_state["album_art_source_counts"] = dict(source_counts)

        result = {
            "status": "cancelled" if cancel_event and cancel_event.is_set() else "success",
            "total_tracks": len(tracks),
            "initial_missing": total_missing,
            "downloaded": backfilled_count,
            "failed": failed_count,
            "remaining_missing": remaining_missing,
            "source_counts": source_counts,
        }
        logger.info(f"backfill_album_art: Finished. {result}")
        level = "warning" if result["status"] == "cancelled" else "success"
        _append_backfill_task_log(task_state, level, f"Album art backfill finished: {backfilled_count} added, {remaining_missing} still missing.")
        return result
    except Exception as e:
        logger.error(f"backfill_album_art: Error during backfilling: {e}", exc_info=True)
        _append_backfill_task_log(task_state, "error", f"Album art backfill failed: {e}")
        return {"status": "error", "message": str(e)}

def backfill_durations(task_state=None, cancel_event=None):
    """
    Checks database for tracks missing duration backfill, and uses iTunes API to backfill them.
    """
    logger.info("Starting duration backfill check task...")
    _append_backfill_task_log(task_state, "info", "Duration backfill started.")
    db_file_id, parent_folder_id = get_db_file_id()
    if not db_file_id:
        _append_backfill_task_log(task_state, "error", "database.json could not be resolved.")
        return {"status": "error", "message": "database.json file ID not found."}
        
    try:
        db_data = download_json(db_file_id)
        if not db_data:
            _append_backfill_task_log(task_state, "warning", "database.json is empty.")
            return {"status": "error", "message": "Database is empty."}
            
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
        else:
            _append_backfill_task_log(task_state, "error", "database.json format is not supported.")
            return {"status": "error", "message": "Invalid database format."}
            
        missing_tracks = [track for track in tracks if _duration_missing(track)]
        updated_count = 0
        failed_count = 0
        source_counts = {}

        if task_state is not None:
            task_state["processed"] = 0
            task_state["total_candidates"] = len(missing_tracks)
            task_state["duration_initial_missing"] = len(missing_tracks)
            task_state["duration_updated"] = 0
            task_state["duration_remaining"] = len(missing_tracks)
            task_state["duration_failed"] = 0
            task_state["duration_source_counts"] = {}

        for index, track in enumerate(missing_tracks, start=1):
            if cancel_event and cancel_event.is_set():
                _append_backfill_task_log(task_state, "warning", "Cancel requested; saving duration updates before stopping.")
                break

            title = track.get("title")
            artist = track.get("artist")
            if not title or not artist:
                failed_count += 1
                _append_backfill_task_log(task_state, "warning", f"Skipped track {index}/{len(missing_tracks)}: missing title or artist.")
                continue

            logger.info(f"backfill_durations: Backfilling duration for '{title}' by '{artist}'...")

            try:
                result = resolve_duration_with_details(title, artist)
                if result.get("durationSeconds"):
                    source = result.get("source") or "unknown"
                    track["duration"] = result["duration"]
                    track["durationSeconds"] = result["durationSeconds"]
                    track["updatedAt"] = datetime.datetime.utcnow().isoformat() + "Z"
                    updated_count += 1
                    source_counts[source] = source_counts.get(source, 0) + 1
                    logger.info(f"backfill_durations: Updated duration to {track['duration']} via {source}")
                    _append_backfill_task_log(task_state, "success", f"{index}/{len(missing_tracks)}: {title} - {artist} -> {track['duration']} ({source})")
                else:
                    failed_count += 1
                    attempt_summary = ", ".join(f"{a.get('source')}:{a.get('status')}" for a in result.get("attempts", []))
                    logger.info(f"backfill_durations: No duration found for '{title}' by '{artist}'. Attempts: {attempt_summary}")
                    _append_backfill_task_log(task_state, "warning", f"{index}/{len(missing_tracks)}: no duration found for {title} - {artist}")
            except Exception as e:
                failed_count += 1
                logger.warning(f"backfill_durations: Failed lookup for '{title}': {e}")
                _append_backfill_task_log(task_state, "warning", f"{index}/{len(missing_tracks)}: duration lookup failed for {title} - {artist}: {e}")

            if task_state is not None:
                task_state["processed"] = index
                task_state["duration_updated"] = updated_count
                task_state["duration_failed"] = failed_count
                task_state["duration_remaining"] = sum(1 for t in tracks if _duration_missing(t))
                task_state["duration_source_counts"] = dict(source_counts)
                
        if updated_count > 0:
            logger.info(f"backfill_durations: Uploading updated database with {updated_count} duration backfills...")
            if isinstance(db_data, dict) and 'tracks' in db_data:
                db_data['tracks'] = tracks
                upload_database_json_locked(db_file_id, db_data, parent_folder_id)
            else:
                upload_database_json_locked(db_file_id, tracks, parent_folder_id)
        else:
            logger.info("backfill_durations: All tracks have valid durations or no updates made.")

        remaining_missing = sum(1 for track in tracks if _duration_missing(track))
        if task_state is not None:
            task_state["duration_updated"] = updated_count
            task_state["duration_remaining"] = remaining_missing
            task_state["duration_failed"] = failed_count
            task_state["duration_source_counts"] = dict(source_counts)
        result = {
            "status": "cancelled" if cancel_event and cancel_event.is_set() else "success",
            "total_tracks": len(tracks),
            "initial_missing": len(missing_tracks),
            "updated": updated_count,
            "failed": failed_count,
            "remaining_missing": remaining_missing,
            "source_counts": source_counts,
        }
        level = "warning" if result["status"] == "cancelled" else "success"
        _append_backfill_task_log(task_state, level, f"Duration backfill finished: {updated_count} updated, {remaining_missing} still missing.")
        return result
    except Exception as e:
        logger.error(f"backfill_durations: Error during backfilling check: {e}", exc_info=True)
        _append_backfill_task_log(task_state, "error", f"Duration backfill failed: {e}")
        return {"status": "error", "message": str(e)}


def backfill_lyrics(task_state=None, cancel_event=None, save_every=10):
    """
    Backfills missing plain/synced lyrics through API fallbacks and saves progress
    incrementally so long runs do not lose successful matches.
    """
    logger.info("Starting lyrics backfill task...")
    _append_backfill_task_log(task_state, "info", "Lyrics backfill started.")
    db_file_id, parent_folder_id = get_db_file_id()
    if not db_file_id:
        _append_backfill_task_log(task_state, "error", "database.json could not be resolved.")
        return {"status": "error", "message": "database.json file ID not found."}

    try:
        db_data = download_json(db_file_id)
        if not db_data:
            _append_backfill_task_log(task_state, "warning", "database.json is empty.")
            return {"status": "error", "message": "Database is empty."}

        if isinstance(db_data, list):
            tracks = db_data
            was_dict = False
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
            was_dict = True
        else:
            _append_backfill_task_log(task_state, "error", "database.json format is not supported.")
            return {"status": "error", "message": "Invalid database format."}

        missing_tracks = [track for track in tracks if _lyrics_missing(track)]
        downloaded_count = 0
        failed_count = 0
        source_counts = {}
        pending_save = False

        if task_state is not None:
            task_state["processed"] = 0
            task_state["total_candidates"] = len(missing_tracks)
            task_state["lyrics_initial_missing"] = len(missing_tracks)
            task_state["lyrics_downloaded"] = 0
            task_state["lyrics_remaining"] = len(missing_tracks)
            task_state["lyrics_failed"] = 0
            task_state["lyrics_source_counts"] = {}

        logger.info(f"backfill_lyrics: Found {len(missing_tracks)} tracks missing lyrics.")
        _append_backfill_task_log(task_state, "info", f"Found {len(missing_tracks)} tracks missing lyrics.")

        for index, track in enumerate(missing_tracks, start=1):
            if cancel_event and cancel_event.is_set():
                logger.info("backfill_lyrics: Cancel requested. Saving progress before stopping.")
                _append_backfill_task_log(task_state, "warning", "Cancel requested; saving resolved lyrics before stopping.")
                break

            title = track.get("title")
            artist = track.get("artist")
            album = track.get("album")
            duration_seconds = track.get("durationSeconds")
            if not title or not artist:
                failed_count += 1
                _append_backfill_task_log(task_state, "warning", f"Skipped track {index}/{len(missing_tracks)}: missing title or artist.")
                continue

            if task_state is not None:
                task_state["current_track"] = f"{title} - {artist}"

            logger.info(f"backfill_lyrics [{index}/{len(missing_tracks)}]: Resolving '{title}' by '{artist}'...")
            try:
                result = resolve_lyrics_with_details(title, artist, album=album, duration_seconds=duration_seconds)
                lyrics = result.get("lyrics")
                synced_lyrics = result.get("syncedLyrics")
                if lyrics or synced_lyrics:
                    source = result.get("source") or "unknown"
                    if lyrics:
                        track["lyrics"] = lyrics
                    if synced_lyrics:
                        track["syncedLyrics"] = synced_lyrics
                    lyrics_for_status = lyrics or synced_lyrics or ""
                    track["lyricsStatus"] = "needs_review" if detect_script_mixing(lyrics_for_status) else "ok"
                    track["updatedAt"] = datetime.datetime.utcnow().isoformat() + "Z"
                    downloaded_count += 1
                    source_counts[source] = source_counts.get(source, 0) + 1
                    pending_save = True
                    logger.info(f"backfill_lyrics: Found lyrics via {source} for '{title}' by '{artist}'.")
                    _append_backfill_task_log(task_state, "success", f"{index}/{len(missing_tracks)}: {title} - {artist} -> {source}")
                else:
                    failed_count += 1
                    attempt_summary = ", ".join(f"{a.get('source')}:{a.get('status')}" for a in result.get("attempts", []))
                    logger.info(f"backfill_lyrics: No lyrics found for '{title}' by '{artist}'. Attempts: {attempt_summary}")
                    _append_backfill_task_log(task_state, "warning", f"{index}/{len(missing_tracks)}: no lyrics found for {title} - {artist}")
            except Exception as e:
                failed_count += 1
                logger.warning(f"backfill_lyrics: Resolver failed for '{title}' by '{artist}': {e}")
                _append_backfill_task_log(task_state, "error", f"{index}/{len(missing_tracks)}: lyrics lookup failed for {title} - {artist}: {e}")

            if task_state is not None:
                task_state["processed"] = index
                task_state["lyrics_downloaded"] = downloaded_count
                task_state["lyrics_failed"] = failed_count
                task_state["lyrics_remaining"] = sum(1 for t in tracks if _lyrics_missing(t))
                task_state["lyrics_source_counts"] = dict(source_counts)

            if pending_save and (downloaded_count % save_every == 0):
                if was_dict:
                    db_data['tracks'] = tracks
                    upload_database_json_locked(db_file_id, db_data, parent_folder_id)
                else:
                    upload_database_json_locked(db_file_id, tracks, parent_folder_id)
                logger.info(f"backfill_lyrics: Incremental save after {downloaded_count} lyrics updates.")
                _append_backfill_task_log(task_state, "info", f"Saved {downloaded_count} lyrics updates to database.json.")
                pending_save = False
            time.sleep(0.1)

        if pending_save:
            if was_dict:
                db_data['tracks'] = tracks
                upload_database_json_locked(db_file_id, db_data, parent_folder_id)
            else:
                upload_database_json_locked(db_file_id, tracks, parent_folder_id)
            logger.info("backfill_lyrics: Final database save completed.")
            _append_backfill_task_log(task_state, "info", "Final lyrics updates saved to database.json.")

        remaining_missing = sum(1 for track in tracks if _lyrics_missing(track))
        if task_state is not None:
            task_state["lyrics_downloaded"] = downloaded_count
            task_state["lyrics_remaining"] = remaining_missing
            task_state["lyrics_failed"] = failed_count
            task_state["lyrics_source_counts"] = dict(source_counts)

        result = {
            "status": "cancelled" if cancel_event and cancel_event.is_set() else "success",
            "total_tracks": len(tracks),
            "initial_missing": len(missing_tracks),
            "downloaded": downloaded_count,
            "failed": failed_count,
            "remaining_missing": remaining_missing,
            "source_counts": source_counts,
        }
        logger.info(f"backfill_lyrics: Finished. {result}")
        level = "warning" if result["status"] == "cancelled" else "success"
        _append_backfill_task_log(task_state, level, f"Lyrics backfill finished: {downloaded_count} added, {remaining_missing} still missing.")
        return result
    except Exception as e:
        logger.error(f"backfill_lyrics: Error during backfilling: {e}", exc_info=True)
        _append_backfill_task_log(task_state, "error", f"Lyrics backfill failed: {e}")
        return {"status": "error", "message": str(e)}


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

def backfill_languages(task_state=None, cancel_event=None):
    """
    Checks database for tracks missing language or labeled 'unknown', and backfills them.
    """
    logger.info("Starting language backfill check task...")
    _append_backfill_task_log(task_state, "info", "Language backfill started.")
    db_file_id, parent_folder_id = get_db_file_id()
    if not db_file_id:
        _append_backfill_task_log(task_state, "error", "database.json could not be resolved.")
        return {"status": "error", "message": "database.json file ID not found."}
        
    try:
        db_data = download_json(db_file_id)
        if not db_data:
            _append_backfill_task_log(task_state, "warning", "database.json is empty.")
            return {"status": "error", "message": "Database is empty."}
            
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
        else:
            _append_backfill_task_log(task_state, "error", "database.json format is not supported.")
            return {"status": "error", "message": "Invalid database format."}
            
        updated_count = 0
        candidate_tracks = []
        for track in tracks:
            lang = track.get("language", "unknown")
            if not lang:
                lang = "unknown"
            lang = lang.lower()
            if lang in ("unknown", "", "none"):
                candidate_tracks.append(track)

        if task_state is not None:
            task_state["processed"] = 0
            task_state["total_candidates"] = len(candidate_tracks)

        for index, track in enumerate(candidate_tracks, start=1):
            if cancel_event and cancel_event.is_set():
                _append_backfill_task_log(task_state, "warning", "Cancel requested; saving language updates before stopping.")
                break

            lang = (track.get("language") or "unknown").lower()
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
                    _append_backfill_task_log(task_state, "success", f"{index}/{len(candidate_tracks)}: {track.get('title')} -> {new_lang}")

            if task_state is not None:
                task_state["processed"] = index
                    
        if updated_count > 0:
            logger.info(f"backfill_languages: Uploading updated database with {updated_count} language backfills...")
            if isinstance(db_data, dict) and 'tracks' in db_data:
                db_data['tracks'] = tracks
                upload_database_json_locked(db_file_id, db_data, parent_folder_id)
            else:
                upload_database_json_locked(db_file_id, tracks, parent_folder_id)
        else:
            logger.info("backfill_languages: All tracks have valid languages or no updates needed.")

        remaining_missing = sum(
            1 for track in tracks
            if (track.get("language") is None or str(track.get("language")).lower() in ("unknown", "", "none"))
        )
        result = {
            "status": "cancelled" if cancel_event and cancel_event.is_set() else "success",
            "total_tracks": len(tracks),
            "initial_missing": len(candidate_tracks),
            "updated": updated_count,
            "remaining_missing": remaining_missing,
        }
        level = "warning" if result["status"] == "cancelled" else "success"
        _append_backfill_task_log(task_state, level, f"Language backfill finished: {updated_count} updated, {remaining_missing} still missing.")
        return result
    except Exception as e:
        logger.error(f"backfill_languages: Error during language backfilling: {e}", exc_info=True)
        _append_backfill_task_log(task_state, "error", f"Language backfill failed: {e}")
        return {"status": "error", "message": str(e)}

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
    logger.info("Skipping legacy source-based language backfill; Gemini handles language/genre for new imports.")
    
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
    pending_gemini_batch = []
    deferred_gemini_tracks = []

    def flush_scraper_import_batch(reason, batch=None, final_attempt=False):
        target_batch = batch if batch is not None else pending_gemini_batch
        if not target_batch:
            return

        logger.info(
            f"Running Gemini import metadata batch for {len(target_batch)} tracks "
            f"before database write ({reason})."
        )
        state["gemini_pending"] = len(target_batch)
        state["gemini_deferred"] = len(deferred_gemini_tracks)
        save_state(state)

        gemini_stats = apply_gemini_to_import_batch(target_batch, logger, force_fields=["language", "genre"])
        state["gemini_last_batch"] = {
            "submitted": gemini_stats.get("tracks_submitted", len(target_batch)),
            "tracksUpdated": gemini_stats.get("tracks_updated", 0),
            "fieldsUpdated": gemini_stats.get("fields_updated", 0),
            "languageUpdates": gemini_stats.get("language_updates", 0),
            "genreUpdates": gemini_stats.get("genre_updates", 0),
            "errors": gemini_stats.get("errors", []),
        }

        if gemini_stats.get("ai_failed") and not final_attempt:
            deferred_gemini_tracks.extend(target_batch)
            logger.warning(
                f"Gemini failed for {len(target_batch)} scraper import tracks. "
                "Deferring database write until all downloads finish, then retrying AI."
            )
            target_batch.clear()
            state["gemini_pending"] = len(pending_gemini_batch)
            state["gemini_deferred"] = len(deferred_gemini_tracks)
            save_state(state)
            return

        if gemini_stats.get("ai_failed") and final_attempt:
            logger.error(
                f"Final Gemini retry failed for {len(target_batch)} scraper import tracks. "
                "Writing fallback metadata so downloaded songs are not lost."
            )

        if not bulk_update_database(target_batch):
            raise RuntimeError("bulk_update_database returned False during scraper Gemini import flush")

        timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
        for metadata in target_batch:
            existing_tracks.append({
                "id": metadata.get("id") or metadata.get("driveFileId"),
                "driveFileId": metadata.get("driveFileId") or metadata.get("id"),
                "title": metadata.get("title"),
                "artist": metadata.get("artist"),
                "album": metadata.get("album", "Single"),
                "genre": metadata.get("genre", "Unknown"),
                "duration": metadata.get("duration", "--:--"),
                "durationSeconds": metadata.get("durationSeconds"),
                "album_art": metadata.get("album_art"),
                "language": metadata.get("language", "unknown"),
                "source": metadata.get("source"),
                "lyrics": metadata.get("lyrics"),
                "syncedLyrics": metadata.get("syncedLyrics"),
                "lyricsStatus": metadata.get("lyricsStatus", "ok"),
                "timestamp": metadata.get("timestamp") or timestamp,
                "spotify_id": metadata.get("spotify_id")
            })

            spotify_id = metadata.get("spotify_id")
            if spotify_id and spotify_id != "UnknownID":
                state.setdefault("downloaded_ids", []).append(spotify_id)
            state.setdefault("downloaded_titles", []).append(metadata.get("title"))
            downloaded_songs.append(f"{metadata.get('title')} by {metadata.get('artist')}")

        logger.info(
            f"Database bulk write complete for {len(target_batch)} AI-processed imports "
            f"({gemini_stats.get('fields_updated', 0)} AI fields updated)."
        )
        target_batch.clear()
        state["gemini_pending"] = len(pending_gemini_batch)
        state["gemini_deferred"] = len(deferred_gemini_tracks)
        save_state(state)
    
    logger.info(f"Current effective pool size: {len(effective_pool)}. Resume index: {cursor}. Quota target: {songs_per_run} songs.")
    
        # 5. Process tracks
    while cursor < len(effective_pool) and (len(downloaded_songs) + len(pending_gemini_batch) + len(deferred_gemini_tracks)) < songs_per_run:
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
        drive_file_id = None
        queued_for_database = False
        cursor_advanced = False
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
                "syncedLyrics": enriched.get("syncedLyrics"),
                "lyricsStatus": enriched.get("lyricsStatus", "ok")
            }
            metadata["id"] = drive_file_id
            metadata["driveFileId"] = drive_file_id
            
            pending_gemini_batch.append(metadata)
            queued_for_database = True
            state["gemini_pending"] = len(pending_gemini_batch)
            
            cursor += 1
            state["cursor"] = cursor
            cursor_advanced = True
            logger.info(
                f"Queued '{title}' by '{artist}' for Gemini import metadata batch "
                f"({len(pending_gemini_batch)}/{GEMINI_IMPORT_BATCH_SIZE})."
            )
            save_state(state)

            if len(pending_gemini_batch) >= GEMINI_IMPORT_BATCH_SIZE:
                flush_scraper_import_batch("batch reached 20 successful downloads")
            
        except Exception as track_err:
            logger.error(f"Failed to process track '{title}' by '{artist}': {track_err}", exc_info=True)
            failed_songs.append(f"{title} by {artist}")
            if drive_file_id and not queued_for_database:
                try:
                    from dashboard.drive_client import delete_file
                    delete_file(drive_file_id)
                    logger.info(f"Deleted uploaded media {drive_file_id} after scraper track failure.")
                except Exception as cleanup_err:
                    logger.warning(f"Could not delete uploaded media {drive_file_id} after scraper track failure: {cleanup_err}")
            if not cursor_advanced:
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

    if deferred_gemini_tracks:
        logger.info(f"Retrying {len(deferred_gemini_tracks)} deferred Gemini scraper import tracks after downloads finished.")
        while deferred_gemini_tracks:
            retry_chunk = deferred_gemini_tracks[:GEMINI_IMPORT_BATCH_SIZE]
            retry_count = len(retry_chunk)
            try:
                flush_scraper_import_batch("final retry for deferred AI batch", batch=retry_chunk, final_attempt=True)
                del deferred_gemini_tracks[:retry_count]
                state["gemini_deferred"] = len(deferred_gemini_tracks)
                save_state(state)
            except Exception as flush_err:
                logger.error(f"Failed to flush deferred Gemini import batch: {flush_err}", exc_info=True)
                failed_songs.extend(f"{m.get('title')} by {m.get('artist')}" for m in retry_chunk)
                break

    if pending_gemini_batch:
        try:
            flush_scraper_import_batch("end of scraper run", final_attempt=True)
        except Exception as flush_err:
            logger.error(f"Failed to flush final Gemini import batch: {flush_err}", exc_info=True)
            failed_songs.extend(f"{m.get('title')} by {m.get('artist')}" for m in pending_gemini_batch)
            pending_gemini_batch.clear()
            state["gemini_pending"] = 0
            state["gemini_deferred"] = len(deferred_gemini_tracks)
            save_state(state)

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
            from dashboard.app import backfill_cancel_event, background_tasks
            if backfill_cancel_event.is_set():
                logger.info("Backfill run cleanly interrupted by user request.")
                background_tasks["backfill"]["status"] = "idle"
                return {"status": "cancelled", "message": "Backfill run cleanly interrupted by user request."}

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
            upload_database_json_locked(db_file_id, db_data, parent_id)
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
            from dashboard.app import backfill_cancel_event, background_tasks
            if backfill_cancel_event.is_set():
                logger.info("Backfill run cleanly interrupted by user request.")
                background_tasks["backfill"]["status"] = "idle"
                return {"status": "cancelled", "message": "Backfill run cleanly interrupted by user request."}

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
                upload_database_json_locked(db_file_id, db_data, parent_id)
                logger.info(f"Incremental save completed after processing {processed_count} tracks.")
                updated_since_last_save = False

        # Final Save
        if updated_since_last_save:
            if isinstance(db_data, dict):
                db_data['tracks'] = tracks
            upload_database_json_locked(db_file_id, db_data, parent_id)
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

def run_gemini_backfill(mode="auto", task_state=None, cancel_event=None):
    """
    Advanced backfill engine leveraging Gemini LLM to intelligently guess missing metadata.
    Chunks tracks into batches of 20 to maintain high LLM output quality and avoid rate limits.
    """
    import time
    from dashboard.drive_client import upload_json
    from scraper.gemini_metadata_judge import GeminiJudge, build_gemini_candidates, normalize_genre_value, normalize_language_value

    if task_state is None or cancel_event is None:
        import sys
        import threading
        app_module = sys.modules.get("dashboard.app") or sys.modules.get("__main__")
        if task_state is None and app_module and hasattr(app_module, "background_tasks"):
            task_state = app_module.background_tasks.setdefault("backfill", {})
        if cancel_event is None and app_module and hasattr(app_module, "backfill_cancel_event"):
            cancel_event = app_module.backfill_cancel_event
        if task_state is None:
            task_state = {}
        if cancel_event is None:
            cancel_event = threading.Event()

    task_state.setdefault("changelog", [])
    task_state.setdefault("processed", 0)
    task_state.setdefault("total_candidates", 0)
    task_state.setdefault("api_call_count", 0)
    task_state.setdefault("logs", [])

    def append_task_log(level, message):
        task_state.setdefault("logs", []).append({
            "time": datetime.datetime.utcnow().isoformat() + "Z",
            "level": level,
            "message": message
        })
        task_state["logs"] = task_state["logs"][-200:]
    
    logger.info(f"Starting run_gemini_backfill in {mode} mode...")
    append_task_log("info", f"Worker started in {mode} mode.")
    db_file_id, parent_id = get_db_file_id()
    if not db_file_id:
        logger.error("No database.json file ID resolved.")
        append_task_log("error", "database.json could not be resolved from Drive configuration.")
        return {"status": "error", "message": "Database not found."}

    try:
        db_data = download_json(db_file_id)
        if not db_data:
            append_task_log("error", "database.json downloaded successfully but was empty.")
            return {"status": "error", "message": "Database is empty."}

        tracks = db_data if isinstance(db_data, list) else db_data.get('tracks', [])
        
        # 1. Create Backup
        now = datetime.datetime.now()
        backup_filename = f"database_backup_gemini_backfill_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json"
        upload_json(None, db_data, backup_filename, parent_id=parent_id)
        logger.info(f"Created backup before Gemini backfill: {backup_filename}")
        append_task_log("success", f"Created backup {backup_filename}.")

        # 2. Identify exactly which fields need AI and prioritize tracks with usable evidence.
        candidates = build_gemini_candidates(tracks)

        total_ai_candidates = len(candidates)
        if mode == "single":
            candidates = candidates[:20]
                
        logger.info(f"Found {total_ai_candidates} tracks needing Gemini metadata backfill. Processing {len(candidates)} in this run.")
        task_state["total_candidates"] = len(candidates)
        append_task_log("info", f"Found {total_ai_candidates} AI candidate tracks; processing {len(candidates)} in this run.")
        language_requests = sum(1 for c in candidates if "language" in c.get("fields_to_fill", []))
        genre_requests = sum(1 for c in candidates if "genre" in c.get("fields_to_fill", []))
        high_evidence = sum(1 for c in candidates if c.get("evidence_score", 0) >= 5)
        append_task_log(
            "info",
            f"Candidate plan: {language_requests} language fields, {genre_requests} genre fields, "
            f"{high_evidence} high-evidence tracks; language-needed tracks are prioritized."
        )
        
        if not candidates:
            append_task_log("success", "No tracks require Gemini backfill.")
            return {"status": "success", "message": "No tracks require Gemini backfill."}

        judge = GeminiJudge()
        chunk_size = 20
        total_updated = 0
        total_fields_updated = 0
        consecutive_failures = 0

        # 3. Process in Chunks
        for i in range(0, len(candidates), chunk_size):
            if cancel_event.is_set():
                logger.info("Gemini backfill run cleanly interrupted by user request.")
                append_task_log("warning", "Backfill cancelled before starting the next Gemini batch.")
                task_state["status"] = "idle"
                return {"status": "cancelled", "message": "Backfill cancelled by user.", "updated": total_updated}

            chunk = candidates[i:i + chunk_size]
            batch_number = i // chunk_size + 1
            batch_total = (len(candidates) + chunk_size - 1) // chunk_size
            logger.info(f"Processing Gemini batch {batch_number} / {batch_total} (Tracks {i+1} to {min(i+chunk_size, len(candidates))})...")
            append_task_log("info", f"Starting Gemini batch {batch_number} of {batch_total} with {len(chunk)} tracks.")
            
            try:
                task_state["api_call_count"] += 1
                response = judge.analyze_tracks_batch(chunk)
                
                if isinstance(response, dict) and response.get("status") == "error":
                    consecutive_failures += 1
                    logger.error(f"Gemini API returned error: {response.get('message')}")
                    append_task_log("error", f"Gemini batch {batch_number} failed: {response.get('message')}")
                    task_state["changelog"].append({
                        "track": f"Batch Failed ({len(chunk)} tracks)",
                        "field": "System",
                        "old": "N/A",
                        "new": f"API Error: {str(response.get('message'))[:30]}",
                        "confidence": 0.0
                    })
                    
                    if mode == "auto" and consecutive_failures >= 3:
                        logger.error("CIRCUIT BREAKER TRIPPED: 3 consecutive API failures.")
                        append_task_log("error", "Circuit breaker tripped after 3 consecutive Gemini API failures.")
                        task_state["changelog"].append({
                            "track": "System Abort",
                            "field": "API",
                            "old": "N/A",
                            "new": "Circuit Breaker Tripped",
                            "confidence": 0.0
                        })
                        task_state["status"] = "idle"
                        return {"status": "error", "message": "Circuit breaker tripped due to consecutive failures."}
                        
                    continue
                    
                consecutive_failures = 0
                    
                if response and getattr(response, "tracks", None):
                    suggestions = response.tracks
                    chunk_by_id = {
                        str(t.get("id") or t.get("driveFileId")): t
                        for t in chunk
                        if t.get("id") or t.get("driveFileId")
                    }
                    
                    # We must re-download the database right before update to prevent race conditions
                    fresh_db_data = download_json(db_file_id)
                    fresh_tracks = fresh_db_data if isinstance(fresh_db_data, list) else fresh_db_data.get('tracks', [])
                    
                    batch_updates = 0
                    batch_fields_updated = 0
                    batch_stats = {
                        "empty": 0,
                        "low_confidence": 0,
                        "invalid": 0,
                        "unchanged": 0,
                        "unknown_track": 0,
                        "missing_from_database": 0
                    }
                    matched_suggestions = 0
                    
                    for suggestion in suggestions:
                        original_track = chunk_by_id.get(str(suggestion.track_id))
                        if not original_track:
                            logger.warning(f"Gemini returned suggestion for unknown track ID {suggestion.track_id}. Skipped.")
                            batch_stats["unknown_track"] += 1
                            continue
                        matched_suggestions += 1
                        track_id = original_track.get("id") or original_track.get("driveFileId")
                        
                        # Find the track in the fresh database
                        fresh_track_ref = next((t for t in fresh_tracks if (t.get("id") == track_id or t.get("driveFileId") == track_id)), None)
                        if not fresh_track_ref:
                            logger.warning(f"Track ID {track_id} not found in fresh database during Gemini backfill. Skipped.")
                            batch_stats["missing_from_database"] += 1
                            continue
                            
                        # Apply suggestions with high confidence
                        applied_any = False
                        requested_fields = set(original_track.get("fields_to_fill") or ["language", "genre"])
                        
                        def apply_change(field_name, suggestion_obj):
                            if not suggestion_obj or not suggestion_obj.value:
                                return "empty"
                            confidence = suggestion_obj.confidence or 0.0
                            if confidence <= 0.6:
                                return "low_confidence"

                            old_val = fresh_track_ref.get(field_name)
                            new_val = suggestion_obj.value
                            if field_name == "language":
                                new_val = normalize_language_value(new_val)
                                if not new_val or new_val == "unknown":
                                    return "invalid"
                            elif field_name == "genre":
                                new_val = normalize_genre_value(new_val)
                                if not new_val:
                                    return "invalid"

                            if old_val == new_val:
                                return "unchanged"

                            fresh_track_ref[field_name] = new_val
                            task_state["changelog"].append({
                                "track": fresh_track_ref.get("title", "Unknown"),
                                "field": field_name,
                                "old": old_val,
                                "new": new_val,
                                "confidence": confidence
                            })
                            return "applied"

                        for field_name, suggestion_obj in (
                            ("language", suggestion.suggested_language),
                            ("genre", suggestion.suggested_genre)
                        ):
                            if field_name not in requested_fields:
                                continue
                            change_result = apply_change(field_name, suggestion_obj)
                            if change_result == "applied":
                                applied_any = True
                                batch_fields_updated += 1
                            else:
                                batch_stats[change_result] = batch_stats.get(change_result, 0) + 1
                            
                        if applied_any:
                            batch_updates += 1
                            logger.debug(f"Gemini updated {fresh_track_ref.get('title')}: {suggestion.reasoning}")

                    missing_response_count = max(0, len(chunk) - matched_suggestions)
                    diagnostics = []
                    if missing_response_count:
                        diagnostics.append(f"{missing_response_count} tracks had no response")
                    diagnostic_labels = {
                        "empty": "empty/null",
                        "low_confidence": "low-confidence",
                        "invalid": "invalid",
                        "unchanged": "unchanged",
                        "unknown_track": "unknown track-id",
                        "missing_from_database": "missing from database"
                    }
                    for key, label in diagnostic_labels.items():
                        if batch_stats.get(key):
                            diagnostics.append(f"{batch_stats[key]} {label}")
                    diagnostic_text = "; ".join(diagnostics) if diagnostics else "no rejected suggestions"
                    append_task_log(
                        "info",
                        f"Batch {batch_number} analysis: {len(chunk)} tracks, {batch_updates} tracks changed, "
                        f"{batch_fields_updated} fields updated; {diagnostic_text}."
                    )

                    if batch_updates > 0:
                        # Re-upload the fresh database with our batch modifications
                        if isinstance(fresh_db_data, dict):
                            fresh_db_data['tracks'] = fresh_tracks
                        try:
                            upload_result = upload_database_json_locked(db_file_id, fresh_db_data, parent_id)
                            if not upload_result:
                                raise Exception("upload_json returned empty or False")
                            total_updated += batch_updates
                            total_fields_updated += batch_fields_updated
                            logger.info(f"Batch successful. Updated {batch_updates} tracks and {batch_fields_updated} fields in database.")
                            append_task_log("success", f"Batch {batch_number} saved successfully. Updated {batch_updates} tracks ({batch_fields_updated} fields).")
                        except Exception as upload_err:
                            logger.error(f"Critical Drive Upload Failure: {upload_err}")
                            append_task_log("error", f"Drive upload failed after batch {batch_number}: {str(upload_err)}")
                            task_state["changelog"].append({
                                "track": "System Abort",
                                "field": "Database",
                                "old": "N/A",
                                "new": "Upload Failed",
                                "confidence": 0.0
                            })
                            task_state["status"] = "idle"
                            return {"status": "error", "message": f"Drive upload failed: {str(upload_err)}"}
                    else:
                        append_task_log("info", f"Batch {batch_number} completed with no high-confidence updates.")
                    
                    task_state["processed"] += len(chunk)
                else:
                    append_task_log("warning", f"Batch {batch_number} returned no track suggestions.")
                    task_state["processed"] += len(chunk)
                        
            except Exception as batch_err:
                consecutive_failures += 1
                logger.error(f"Gemini API batch failed: {batch_err}. Skipping batch and continuing...", exc_info=True)
                append_task_log("error", f"Batch {batch_number} raised an exception: {str(batch_err)}")
                task_state["changelog"].append({
                    "track": f"Batch Failed ({len(chunk)} tracks)",
                    "field": "System",
                    "old": "N/A",
                    "new": f"Exception: {str(batch_err)[:80]}",
                    "confidence": 0.0
                })
                if mode == "auto" and consecutive_failures >= 3:
                    logger.error("CIRCUIT BREAKER TRIPPED: 3 consecutive API failures.")
                    append_task_log("error", "Circuit breaker tripped after 3 consecutive batch exceptions.")
                    task_state["changelog"].append({
                        "track": "System Abort",
                        "field": "API",
                        "old": "N/A",
                        "new": "Circuit Breaker Tripped",
                        "confidence": 0.0
                    })
                    task_state["status"] = "idle"
                    return {"status": "error", "message": "Circuit breaker tripped due to consecutive failures."}
                
            # Light sleep between batches to avoid rate limits
            time.sleep(2.0)

        logger.info(f"Gemini backfill complete. Total tracks successfully updated: {total_updated}")
        append_task_log("success", f"Gemini backfill complete. Updated {total_updated} tracks ({total_fields_updated} fields).")
        return {
            "status": "success",
            "total_candidates": len(candidates),
            "updated": total_updated,
            "fields_updated": total_fields_updated
        }

    except Exception as e:
        logger.error(f"Error in run_gemini_backfill: {e}", exc_info=True)
        append_task_log("error", f"Gemini backfill failed: {str(e)}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    run_scraper()
