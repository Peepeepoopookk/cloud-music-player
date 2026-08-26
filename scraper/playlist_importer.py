import os
import sys
import uuid
import json
import logging
import time
import re
import requests
import threading

# Add project root to sys.path to resolve imports when run directly or as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .spotify_charts import scrape_spotify_embed_playlist, HEADERS
from .metadata_enricher import enrich_track_metadata
from .downloader import download_track
from .utils import extract_duration
from .drive_uploader import upload_track, update_database, get_db_file_id
from dashboard.drive_client import upload_json, download_json, search_file_by_name
from .playlist_manager import add_playlist, add_track_to_playlist
from .track_utils import extract_tracks, check_playlist_duplicates

logger = logging.getLogger(__name__)

# In-memory dictionary to track active playlist imports without querying Google Drive repeatedly
active_imports = {}

# In-memory dictionary to hold threading.Event per playlist_id for fast, reliable cancellation
cancel_events = {}

def create_cancel_event(playlist_id):
    ev = threading.Event()
    cancel_events[playlist_id] = ev
    return ev

def set_cancel_event(playlist_id):
    if not playlist_id:
        return
    ev = cancel_events.get(playlist_id)
    if ev:
        ev.set()

def is_cancel_requested(playlist_id):
    if not playlist_id:
        return False
    ev = cancel_events.get(playlist_id)
    if ev and ev.is_set():
        return True
    st = active_imports.get(playlist_id)
    if st and st.get("status") == "cancelled":
        return True
    return False

def cleanup_cancel_event(playlist_id):
    if playlist_id:
        cancel_events.pop(playlist_id, None)

def get_playlist_preview(playlist_url):
    match = re.search(r'playlist/([a-zA-Z0-9]+)', playlist_url)
    if not match:
        raise ValueError("Invalid Spotify playlist URL")
    playlist_id = match.group(1)
    
    tracks = scrape_spotify_embed_playlist(playlist_id)
    
    playlist_name = "Spotify Playlist"
    true_total_tracks = None
    
    # Try fetching the main playlist page to get the true total tracks count
    try:
        r = requests.get(playlist_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200:
            desc_match = re.findall(r'<meta property="og:description" content="([^"]+)"', r.text)
            if desc_match:
                # E.g. "Playlist · Willis Orr · 10000 items · 5.7K saves"
                # E.g. "Playlist · Spotify · 50 songs · 3.3K likes"
                count_match = re.search(r'(\d+(?:,\d+)?)\s+(?:songs?|tracks?|items?)', desc_match[0])
                if count_match:
                    true_total_tracks = int(count_match.group(1).replace(',', ''))
    except Exception as e:
        logger.warning(f"Error extracting true total tracks: {e}")

    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    try:
        r = requests.get(embed_url, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            next_data = re.findall(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', r.text)
            if next_data:
                data = json.loads(next_data[0])
                entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                playlist_name = entity.get("title", entity.get("name", playlist_name))
    except Exception as e:
        logger.warning(f"Error extracting playlist name: {e}")
        
    tracks_available_for_import = len(tracks)
    
    if true_total_tracks is None:
        true_total_tracks = tracks_available_for_import

    # Duplicate detection against current library
    already_in_library = 0
    try:
        db_file_id, _ = get_db_file_id()
        if db_file_id:
            db_data = download_json(db_file_id)
            lib_tracks, _ = extract_tracks(db_data)
            duplicate_results = check_playlist_duplicates(tracks, lib_tracks)
            already_in_library = sum(1 for r in duplicate_results if r.get("is_duplicate"))
    except Exception as e:
        logger.warning(f"Error checking playlist duplicates against library: {e}")
        already_in_library = 0

    new_tracks_importable = max(0, tracks_available_for_import - already_in_library)

    truncated = true_total_tracks > tracks_available_for_import
    truncation_warning = None
    if truncated:
        truncation_warning = f"This playlist has {true_total_tracks} songs but only the first {tracks_available_for_import} can be imported due to Spotify access limitations."
        
    preview_tracks = tracks[:5]
    estimated_mb = tracks_available_for_import * 5
    estimated_display = f"~{estimated_mb} MB"
    
    return {
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "total_tracks": true_total_tracks,
        "tracks_available_for_import": tracks_available_for_import,
        "already_in_library": already_in_library,
        "new_tracks_importable": new_tracks_importable,
        "truncated": truncated,
        "truncation_warning": truncation_warning,
        "estimated_size_mb": estimated_mb,
        "estimated_size_display": estimated_display,
        "preview_tracks": preview_tracks
    }

def start_playlist_import(playlist_url, batch_size=15, device_id=None, imported_via="dashboard"):
    preview = get_playlist_preview(playlist_url)
    tracks = scrape_spotify_embed_playlist(preview["playlist_id"])
    if not tracks:
        raise ValueError("No importable tracks were found for this Spotify playlist.")

    db_file_id, parent_id = get_db_file_id()
    if not parent_id:
        raise ValueError("Could not determine database folder for playlist import state.")

    # Call add_playlist to create a record and get a unified UUID for this import session
    playlist_id = add_playlist(
        name=preview["playlist_name"],
        source_url=playlist_url,
        cover_image=None,
        imported_via=imported_via,
        requestedBy=device_id
    )

    state = {
        "playlist_id": playlist_id,
        "playlist_url": playlist_url,
        "playlist_name": preview["playlist_name"],
        "total_tracks": preview["total_tracks"],
        "tracks_available_for_import": preview["tracks_available_for_import"],
        "processed": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "gemini_pending": 0,
        "gemini_deferred": 0,
        "gemini_status": "idle",
        "gemini_last_batch": None,
        "status": "running",
        "device_id": device_id,
        "tracks": tracks
    }
    
    state_filename = f"playlist_import_state_{playlist_id}.json"
    existing_file_id = search_file_by_name(state_filename, parent_id)
    if existing_file_id:
        upload_json(existing_file_id, state, state_filename, parent_id=parent_id)
    else:
        upload_json(None, state, state_filename, parent_id=parent_id)
            
    active_imports[playlist_id] = state
    create_cancel_event(playlist_id)
    return playlist_id

def get_playlist_status(playlist_id):
    if playlist_id in active_imports:
        return active_imports[playlist_id]
    db_file_id, parent_id = get_db_file_id()
    state_filename = f"playlist_import_state_{playlist_id}.json"
    file_id = search_file_by_name(state_filename, parent_id)
    if not file_id:
        return {"status": "not_found"}
    return download_json(file_id)

def run_playlist_import(playlist_id, batch_size=15, source_override=None):
    from datetime import datetime
    logger.info(f"Starting background playlist import for {playlist_id}")
    state_filename = f"playlist_import_state_{playlist_id}.json"
    
    file_id = None
    parent_id = None

    def mark_failed_and_raise(e):
        logger.error(f"Error during playlist import: {e}", exc_info=True)
        if file_id and parent_id:
            try:
                st = download_json(file_id)
                if not is_cancel_requested(playlist_id) and st.get("status") not in ("cancelled", "completed"):
                    st["status"] = "failed"
                    st["error"] = str(e)
                    active_imports[playlist_id] = st
                    upload_json(file_id, st, state_filename, parent_id=parent_id)
            except Exception as write_err:
                logger.error(f"Failed to write failure state: {write_err}")
        raise e

    try:
        try:
            db_file_id, parent_id = get_db_file_id()
        except Exception as e:
            mark_failed_and_raise(e)
            return
        
        playlist_name = "Spotify Playlist"
        if parent_id:
            try:
                file_id = search_file_by_name(state_filename, parent_id)
                if file_id:
                    st = download_json(file_id)
                    if st:
                        playlist_name = st.get("playlist_name", playlist_name)
            except Exception as e:
                logger.warning(f"Could not load state to get playlist_name: {e}")
                
        # Write a separator line to scraper.log
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_path = os.path.join(project_root, 'scraper.log')
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\nNEW PLAYLIST IMPORT SESSION: {playlist_name} - {datetime.utcnow().isoformat()}\n{'='*60}\n")
        except Exception as e:
            logger.warning(f"Could not write separator line to scraper.log: {e}")

        try:
            temp_dir = os.environ.get('TEMP_DIR', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp'))
            os.makedirs(temp_dir, exist_ok=True)
        except Exception as e:
            mark_failed_and_raise(e)
        
        existing_tracks = []
        if db_file_id:
            try:
                existing_data = download_json(db_file_id)
                if isinstance(existing_data, list):
                    existing_tracks = existing_data
                elif isinstance(existing_data, dict) and 'tracks' in existing_data:
                    existing_tracks = existing_data['tracks']
            except Exception as e:
                logger.warning(f"Failed to fetch existing tracks: {e}")
                
        from scraper.gemini_import_pipeline import GEMINI_IMPORT_BATCH_SIZE
        pending_gemini_batch = []
        deferred_gemini_tracks = []
        
        def cancel_check():
            return is_cancel_requested(playlist_id)
            
        def _flush_gemini_batch(batch, state, final_attempt=False, force=False, skip_ai=False):
            if (cancel_check() and not force) or not batch:
                return
                 
            logger.info(f"Flushing batch of {len(batch)} tracks to Gemini Judge and Database...")
            deferred_for_retry = False
            try:
                from scraper.drive_uploader import bulk_update_database

                if skip_ai:
                    state["gemini_status"] = "fallback"
                    state["gemini_pending"] = len(batch)
                    state["gemini_deferred"] = len(deferred_gemini_tracks)
                    active_imports[playlist_id] = state
                    try:
                        upload_json(file_id, state, state_filename, parent_id=parent_id)
                    except Exception as e:
                        logger.warning(f"Could not persist fallback flush state before database write: {e}")

                    if not bulk_update_database(batch):
                        raise RuntimeError("bulk_update_database returned False during playlist fallback batch flush")
                    for t in batch:
                        existing_tracks.append(t)
                        add_track_to_playlist(playlist_id, t["id"])
                    return

                from scraper.gemini_import_pipeline import apply_gemini_to_import_batch

                state["gemini_status"] = "processing"
                state["gemini_pending"] = len(batch)
                state["gemini_deferred"] = len(deferred_gemini_tracks)
                active_imports[playlist_id] = state
                try:
                    upload_json(file_id, state, state_filename, parent_id=parent_id)
                except Exception as e:
                    logger.warning(f"Could not persist Gemini processing state before batch flush: {e}")

                gemini_stats = apply_gemini_to_import_batch(batch, logger, force_fields=["language", "genre"])
                state["gemini_last_batch"] = {
                    "submitted": gemini_stats.get("tracks_submitted", len(batch)),
                    "tracksUpdated": gemini_stats.get("tracks_updated", 0),
                    "fieldsUpdated": gemini_stats.get("fields_updated", 0),
                    "languageUpdates": gemini_stats.get("language_updates", 0),
                    "genreUpdates": gemini_stats.get("genre_updates", 0),
                    "errors": gemini_stats.get("errors", []),
                }

                if gemini_stats.get("ai_failed") and not final_attempt:
                    deferred_gemini_tracks.extend(batch)
                    state["gemini_deferred"] = len(deferred_gemini_tracks)
                    state["gemini_status"] = "deferred"
                    active_imports[playlist_id] = state
                    deferred_for_retry = True
                    logger.warning(
                        f"Gemini failed for {len(batch)} playlist import tracks. "
                        "Deferring database write until all downloads finish, then retrying AI."
                    )
                    return

                if gemini_stats.get("ai_failed") and final_attempt:
                    logger.error(
                        f"Final Gemini retry failed for {len(batch)} playlist import tracks. "
                        "Writing fallback metadata so downloaded songs are not lost."
                    )

                if not bulk_update_database(batch):
                    raise RuntimeError("bulk_update_database returned False during playlist Gemini batch flush")
                for t in batch:
                    existing_tracks.append(t)
                    add_track_to_playlist(playlist_id, t["id"])
                    
            except Exception as batch_err:
                mark_failed_and_raise(batch_err)
            finally:
                batch.clear()
                state["gemini_pending"] = 0
                state["gemini_deferred"] = len(deferred_gemini_tracks)
                state["gemini_status"] = "deferred" if deferred_for_retry or deferred_gemini_tracks else "idle"
                try:
                    upload_json(file_id, state, state_filename, parent_id=parent_id)
                except Exception as e:
                    logger.error(f"Failed to update state after flush: {e}")
                    
        while True:
            try:
                file_id = search_file_by_name(state_filename, parent_id)
                if not file_id:
                    logger.error(f"State file {state_filename} not found.")
                    break
                state = download_json(file_id)
            except Exception as e:
                mark_failed_and_raise(e)
                break

            if is_cancel_requested(playlist_id):
                if state:
                    state["status"] = "cancelled"
                active_imports[playlist_id] = state or {"status": "cancelled"}
                logger.info(f"Import {playlist_id} is cancelled. Stopping.")
                break

            active_imports[playlist_id] = state
            if state.get("status") in ("cancelled", "completed"):
                logger.info(f"Import {playlist_id} is {state.get('status')}. Stopping.")
                break
                
            processed = state.get("processed", 0)
            tracks = state.get("tracks", [])
            
            if processed >= len(tracks):
                try:
                    latest_state = download_json(file_id)
                    if is_cancel_requested(playlist_id) or (latest_state and latest_state.get("status") == "cancelled"):
                        if latest_state:
                            latest_state["status"] = "cancelled"
                        active_imports[playlist_id] = latest_state or {"status": "cancelled"}
                        break
                    latest_state["status"] = "completed"
                    active_imports[playlist_id] = latest_state
                    upload_json(file_id, latest_state, state_filename, parent_id=parent_id)
                except Exception as e:
                    mark_failed_and_raise(e)
                break
                
            batch = tracks[processed:processed+batch_size]
            for idx, t in enumerate(batch):
                cursor = processed + idx
                
                try:
                    state = download_json(file_id)
                except Exception as e:
                    mark_failed_and_raise(e)

                if is_cancel_requested(playlist_id):
                    if state:
                        state["status"] = "cancelled"
                    active_imports[playlist_id] = state or {"status": "cancelled"}
                    logger.info(f"Import cancelled by user at cursor {cursor}")
                    break

                active_imports[playlist_id] = state
                if state.get("status") == "cancelled":
                    logger.info(f"Import cancelled by user at cursor {cursor}")
                    break
                    
                title = t.get("title")
                artist = t.get("artist")
                spotify_id = t.get("spotify_id")
                source = source_override or state.get("source_label") or f"Playlist Import ({state.get('playlist_name')})"
                device_id = state.get("device_id")
                
                logger.info(f"Processing playlist track: {title} by {artist}")
                
                from scraper.state_manager import find_duplicate_track, load_state
                track_to_check = {
                    "title": title,
                    "artist": artist,
                    "spotify_id": spotify_id
                }
                try:
                    try:
                        scraper_state = load_state()
                    except Exception:
                        scraper_state = {}
                    duplicate_track = find_duplicate_track(track_to_check, scraper_state, existing_tracks)
                except Exception as e:
                    mark_failed_and_raise(e)
                
                if duplicate_track:
                    state["skipped"] += 1
                    existing_drive_id = duplicate_track.get("driveFileId") or duplicate_track.get("id")
                    if existing_drive_id:
                        try:
                            add_track_to_playlist(playlist_id, existing_drive_id)
                            logger.info(f"Duplicate '{title}' already exists as {existing_drive_id} — linked to playlist {playlist_id} instead of re-downloading")
                        except Exception as link_err:
                            logger.warning(f"Found duplicate '{title}' but failed to link it to playlist {playlist_id}: {link_err}")
                    else:
                        logger.warning(f"Found duplicate '{title}' but could not resolve its driveFileId — skipping link, playlist will not include it")
                else:
                    local_file_path = None
                    drive_file_id_upload = None
                    queued_for_database = False
                    def track_cancel_check():
                        return is_cancel_requested(playlist_id)
                        
                    try:
                        unique_id = spotify_id if (spotify_id and spotify_id not in {"UnknownID", "unknown", "None", ""}) else uuid.uuid4().hex
                        local_file_path = download_track(title, artist, temp_dir, track_id=unique_id, cancel_check_callback=track_cancel_check)
                        enriched = enrich_track_metadata(title, artist, local_file_path=local_file_path, source=source)
                        drive_file_id_upload = upload_track(local_file_path)
                        
                        metadata = {
                            "title": title,
                            "artist": artist,
                            "album": enriched.get("album", "Single"),
                            "genre": enriched.get("genre", "Unknown"),
                            "duration": enriched.get("duration", "--:--"),
                            "durationSeconds": enriched.get("durationSeconds"),
                            "spotify_id": spotify_id,
                            "album_art": enriched.get("album_art"),
                            "language": enriched.get("language", "unknown"),
                            "source": source,
                            "requestedBy": device_id,
                            "lyrics": enriched.get("lyrics"),
                            "syncedLyrics": enriched.get("syncedLyrics"),
                            "lyricsStatus": enriched.get("lyricsStatus", "ok")
                        }
                        metadata["id"] = drive_file_id_upload
                        metadata["driveFileId"] = drive_file_id_upload
                        
                        pending_gemini_batch.append(metadata)
                        queued_for_database = True
                        state["gemini_pending"] = len(pending_gemini_batch)
                        
                        if len(pending_gemini_batch) >= GEMINI_IMPORT_BATCH_SIZE:
                            _flush_gemini_batch(pending_gemini_batch, state)
                            
                        state["downloaded"] += 1
                    except Exception as e:
                        if str(e) == "Download cancelled by user":
                            logger.info(f"Download for {title} aborted: {e}")
                        else:
                            logger.error(f"Failed to process {title}: {e}", exc_info=True)
                            state["failed"] += 1
                        if drive_file_id_upload and not queued_for_database:
                            try:
                                from dashboard.drive_client import delete_file
                                delete_file(drive_file_id_upload)
                                logger.info(f"Deleted uploaded media {drive_file_id_upload} after playlist track failure.")
                            except Exception as cleanup_err:
                                logger.warning(f"Could not delete uploaded media {drive_file_id_upload} after playlist track failure: {cleanup_err}")
                    finally:
                        if local_file_path and os.path.exists(local_file_path):
                            try:
                                os.remove(local_file_path)
                            except:
                                pass
                                
                try:
                    latest_state = download_json(file_id)
                    if is_cancel_requested(playlist_id) or (latest_state and latest_state.get("status") == "cancelled"):
                        if latest_state:
                            latest_state["status"] = "cancelled"
                        active_imports[playlist_id] = latest_state or {"status": "cancelled"}
                        logger.info(f"Import cancelled by user at cursor {cursor}")
                        break
                    latest_state["processed"] = cursor + 1
                    latest_state["downloaded"] = state["downloaded"]
                    latest_state["skipped"] = state["skipped"]
                    latest_state["failed"] = state["failed"]
                    state = latest_state
                    active_imports[playlist_id] = state
                    upload_json(file_id, state, state_filename, parent_id=parent_id)
                except Exception as e:
                    mark_failed_and_raise(e)

        try:
            final_state = download_json(file_id) if file_id else {}
        except Exception as e:
            logger.warning(f"Could not load final playlist state before batch cleanup: {e}")
            final_state = active_imports.get(playlist_id, {})

        if is_cancel_requested(playlist_id):
            if final_state:
                final_state["status"] = "cancelled"
            active_imports[playlist_id] = final_state or {"status": "cancelled"}

        was_cancelled = is_cancel_requested(playlist_id) or (final_state and final_state.get("status") == "cancelled")

        if was_cancelled and deferred_gemini_tracks:
            logger.info(
                f"Writing {len(deferred_gemini_tracks)} deferred playlist tracks with fallback metadata after cancellation."
            )
            try:
                _flush_gemini_batch(deferred_gemini_tracks, final_state, force=True, skip_ai=True)
            except Exception as e:
                logger.error(f"Failed to write deferred tracks after cancellation: {e}", exc_info=True)

        if was_cancelled and pending_gemini_batch:
            logger.info(
                f"Writing {len(pending_gemini_batch)} pending playlist tracks with fallback metadata after cancellation."
            )
            try:
                _flush_gemini_batch(pending_gemini_batch, final_state, force=True, skip_ai=True)
            except Exception as e:
                logger.error(f"Failed to write pending tracks after cancellation: {e}", exc_info=True)

        # Retry any batches whose Gemini call failed earlier, then flush leftovers.
        if deferred_gemini_tracks and not was_cancelled:
            logger.info(f"Retrying {len(deferred_gemini_tracks)} deferred Gemini playlist import tracks after downloads finished.")
            while deferred_gemini_tracks:
                retry_chunk = deferred_gemini_tracks[:GEMINI_IMPORT_BATCH_SIZE]
                retry_count = len(retry_chunk)
                try:
                    state = download_json(file_id)
                    _flush_gemini_batch(retry_chunk, state, final_attempt=True)
                    del deferred_gemini_tracks[:retry_count]
                except Exception as e:
                    logger.error(f"Failed to flush deferred Gemini playlist batch: {e}")
                    break

        # Leftover flush
        if pending_gemini_batch and not was_cancelled:
            logger.info(f"Flushing remaining {len(pending_gemini_batch)} tracks after main loop.")
            try:
                state = download_json(file_id)
                _flush_gemini_batch(pending_gemini_batch, state, final_attempt=True)
            except Exception as e:
                logger.error(f"Failed to flush leftover Gemini batch: {e}")
    finally:
        cleanup_cancel_event(playlist_id)
