import os
import sys
import uuid
import json
import logging
import time
import re
import requests
import threading
import concurrent.futures
from datetime import datetime

# Add project root to sys.path to resolve imports when run directly or as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .spotify_charts import scrape_spotify_embed_playlist, HEADERS
from .metadata_enricher import enrich_track_metadata
from .downloader import download_track
from .utils import extract_duration
from .drive_uploader import upload_track, update_database, get_db_file_id
from dashboard.drive_client import upload_json, download_json, search_file_by_name
from .playlist_manager import (
    add_playlist,
    add_track_to_playlist,
    bulk_add_tracks_to_playlist,
    find_playlist_by_source_url,
)
from .track_utils import extract_tracks, check_playlist_duplicates
from .alerting import send_alert

logger = logging.getLogger(__name__)

class PlaylistAlreadyDownloadedError(Exception):
    """
    Raised when attempting to import a Spotify playlist that is already fully present in playlists.json.
    """
    def __init__(self, playlist_id, playlist_name, total_tracks):
        self.playlist_id = playlist_id
        self.playlist_name = playlist_name
        self.total_tracks = total_tracks
        super().__init__(
            f"Playlist '{playlist_name}' (ID: {playlist_id}) is already fully downloaded with {total_tracks} tracks."
        )

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

    # Check for existing playlist with same source_url
    existing_playlist = find_playlist_by_source_url(playlist_url)
    if existing_playlist:
        existing_id = existing_playlist.get("id")
        existing_track_ids = set(existing_playlist.get("track_ids", []))
        existing_total = int(existing_playlist.get("total_tracks") or len(existing_track_ids))

        # Resolve existing playlist's track_ids to spotify_ids using database.json
        existing_spotify_ids = set()
        if db_file_id and existing_track_ids:
            try:
                db_data = download_json(db_file_id)
                db_tracks, _ = extract_tracks(db_data)
                for t in db_tracks:
                    tid = t.get("driveFileId") or t.get("id")
                    if tid in existing_track_ids:
                        sp_id = t.get("spotify_id")
                        if sp_id and sp_id != "UnknownID":
                            existing_spotify_ids.add(sp_id)
            except Exception as e:
                logger.warning(f"Failed to resolve existing playlist tracks from database.json: {e}")

        # Build set of current Spotify track IDs from embed scrape
        scraped_spotify_ids = {
            t.get("spotify_id") for t in tracks if t.get("spotify_id") and t.get("spotify_id") != "UnknownID"
        }

        # Case 1: Fully downloaded already (all current Spotify tracks are in existing playlist)
        if scraped_spotify_ids and scraped_spotify_ids.issubset(existing_spotify_ids):
            logger.info(
                f"Playlist '{preview['playlist_name']}' (ID: {existing_id}) is already fully up to date "
                f"({len(existing_spotify_ids)} existing tracks, all {len(scraped_spotify_ids)} current Spotify tracks present). Skipping import."
            )
            raise PlaylistAlreadyDownloadedError(
                playlist_id=existing_id,
                playlist_name=existing_playlist.get("name") or preview["playlist_name"],
                total_tracks=existing_total
            )

        # Fallback check if scraped_spotify_ids was empty (e.g. embed failed to parse IDs)
        if not scraped_spotify_ids and existing_total >= len(tracks):
            logger.info(
                f"Playlist '{preview['playlist_name']}' (ID: {existing_id}) count matches ({existing_total}/{len(tracks)}). Skipping import."
            )
            raise PlaylistAlreadyDownloadedError(
                playlist_id=existing_id,
                playlist_name=existing_playlist.get("name") or preview["playlist_name"],
                total_tracks=existing_total
            )

        # Case 2: Partially downloaded (new/rotated tracks present on Spotify)
        # Reuse existing playlist_id without creating a new UUID or duplicate record
        missing_count = len(scraped_spotify_ids - existing_spotify_ids) if scraped_spotify_ids else len(tracks)
        logger.info(
            f"Found existing playlist '{preview['playlist_name']}' (ID: {existing_id}) "
            f"with {missing_count} new/missing track(s) out of {len(tracks)} on Spotify. Resuming import with existing ID."
        )
        playlist_id = existing_id
    else:
        # Case 3: Brand new playlist
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
        "started_at": datetime.utcnow().isoformat() + 'Z',
        "completed_at": None,
        "duration_seconds": None,
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

def _format_duration(duration_seconds):
    if duration_seconds is None:
        return "0s"
    minutes = int(duration_seconds) // 60
    seconds = int(duration_seconds) % 60
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

def _finalize_state_timing(st, status=None):
    from datetime import datetime
    now_iso = datetime.utcnow().isoformat() + 'Z'
    st["completed_at"] = now_iso
    if status:
        st["status"] = status
    started_iso = st.get("started_at")
    if started_iso:
        try:
            start_dt = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            duration_sec = max(0, int((end_dt - start_dt).total_seconds()))
            st["duration_seconds"] = duration_sec
        except Exception:
            pass
    return st

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
    start_time_iso = datetime.utcnow().isoformat() + 'Z'
    logger.info(f"Starting background playlist import for {playlist_id}")
    state_filename = f"playlist_import_state_{playlist_id}.json"
    
    file_id = None
    parent_id = None

    def mark_failed_and_raise(e):
        logger.error(f"Error during playlist import: {e}", exc_info=True)
        st = active_imports.get(playlist_id)
        if not st and file_id and parent_id:
            try:
                st = download_json(file_id)
            except Exception:
                st = {}
        if not st:
            st = {"playlist_id": playlist_id, "status": "failed", "error": str(e), "started_at": start_time_iso}
        if not is_cancel_requested(playlist_id) and st.get("status") not in ("cancelled", "completed"):
            _finalize_state_timing(st, "failed")
            st["error"] = str(e)
            active_imports[playlist_id] = st
            dur_str = _format_duration(st.get("duration_seconds"))
            logger.error(f"Playlist import for {playlist_id} failed after {dur_str}: {e}")
            if file_id and parent_id:
                try:
                    upload_json(file_id, st, state_filename, parent_id=parent_id)
                except Exception as write_err:
                    logger.error(f"Failed to write failure state: {write_err}")
            pl_name = st.get("playlist_name") or playlist_id
            send_alert(
                f"Playlist Import Failed: {pl_name}",
                f"Playlist: {pl_name} (ID: {playlist_id})\nDuration: {dur_str}\nError: {e}",
                level="error"
            )
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
                
        import_lock = threading.RLock()
        in_flight_tracks = set()

        from scraper.gemini_import_pipeline import GEMINI_IMPORT_BATCH_SIZE
        pending_gemini_batch = []
        deferred_gemini_tracks = []
        
        def cancel_check():
            return is_cancel_requested(playlist_id)
            
        def _check_and_sync_state_locked(cursor, is_batch_end=False):
            should_sync = ((cursor + 1) % 5 == 0) or is_batch_end
            if should_sync and file_id and parent_id:
                try:
                    upload_json(file_id, state, state_filename, parent_id=parent_id)
                except Exception as e:
                    logger.warning(f"Could not persist state checkpoint at cursor {cursor}: {e}")

        def _flush_gemini_batch(batch, state, final_attempt=False, force=False, skip_ai=False):
            with import_lock:
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
                        batch_ids = []
                        for t in batch:
                            existing_tracks.append(t)
                            tid = t.get("id") or t.get("driveFileId")
                            if tid:
                                batch_ids.append(tid)
                        if batch_ids:
                            bulk_add_tracks_to_playlist(playlist_id, batch_ids)
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
                    batch_ids = []
                    for t in batch:
                        existing_tracks.append(t)
                        tid = t.get("id") or t.get("driveFileId")
                        if tid:
                            batch_ids.append(tid)
                    if batch_ids:
                        bulk_add_tracks_to_playlist(playlist_id, batch_ids)
                        
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

        def _process_track_worker(cursor, t, idx, batch_len):
            title = t.get("title")
            artist = t.get("artist")
            spotify_id = t.get("spotify_id")
            source = source_override or state.get("source_label") or f"Playlist Import ({state.get('playlist_name')})"
            device_id = state.get("device_id")

            if is_cancel_requested(playlist_id):
                with import_lock:
                    state["processed"] = max(state.get("processed", 0), cursor + 1)
                    active_imports[playlist_id] = state
                return

            logger.info(f"Processing playlist track: {title} by {artist}")

            from scraper.state_manager import find_duplicate_track, load_state
            track_to_check = {
                "title": title,
                "artist": artist,
                "spotify_id": spotify_id
            }

            norm_title_artist = f"{(title or '').lower().strip()}__{(artist or '').lower().strip()}"
            sp_key = f"sp:{spotify_id}" if (spotify_id and spotify_id not in {"UnknownID", "unknown", "None", ""}) else None
            keys_to_register = [k for k in [sp_key, norm_title_artist] if k]

            # 1. Dedup check + in-flight check & registration under lock
            with import_lock:
                try:
                    try:
                        scraper_state = load_state()
                    except Exception:
                        scraper_state = {}
                    duplicate_track = find_duplicate_track(track_to_check, scraper_state, existing_tracks)
                except Exception as e:
                    logger.error(f"Error checking duplicates for {title}: {e}")
                    duplicate_track = None

                if duplicate_track:
                    state["skipped"] += 1
                    active_imports[playlist_id] = state
                    existing_drive_id = duplicate_track.get("driveFileId") or duplicate_track.get("id")
                    if existing_drive_id:
                        try:
                            bulk_add_tracks_to_playlist(playlist_id, [existing_drive_id])
                            logger.info(f"Duplicate '{title}' already exists as {existing_drive_id} — linked to playlist {playlist_id} instead of re-downloading")
                        except Exception as link_err:
                            logger.warning(f"Found duplicate '{title}' but failed to link it to playlist {playlist_id}: {link_err}")
                    else:
                        logger.warning(f"Found duplicate '{title}' but could not resolve its driveFileId — skipping link")
                    state["processed"] = max(state.get("processed", 0), cursor + 1)
                    _check_and_sync_state_locked(cursor, idx == batch_len - 1)
                    return

                # In-flight check: another concurrent worker in this run is already processing this exact song
                if any(k in in_flight_tracks for k in keys_to_register):
                    logger.info(f"Track '{title}' by '{artist}' is already in-flight in another worker — skipping concurrent duplicate download")
                    state["skipped"] += 1
                    state["processed"] = max(state.get("processed", 0), cursor + 1)
                    active_imports[playlist_id] = state
                    _check_and_sync_state_locked(cursor, idx == batch_len - 1)
                    return

                # Register in-flight
                for k in keys_to_register:
                    in_flight_tracks.add(k)

            # 2. Download, enrich, upload (Executed concurrently without holding lock)
            local_file_path = None
            drive_file_id_upload = None
            queued_for_database = False

            def track_cancel_check():
                return is_cancel_requested(playlist_id)

            try:
                if is_cancel_requested(playlist_id):
                    logger.info(f"Download for {title} aborted: Cancel requested")
                    return

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

                # 3. Update pending_gemini_batch and state under lock
                with import_lock:
                    pending_gemini_batch.append(metadata)
                    queued_for_database = True
                    state["gemini_pending"] = len(pending_gemini_batch)
                    state["downloaded"] += 1
                    state["processed"] = max(state.get("processed", 0), cursor + 1)
                    active_imports[playlist_id] = state

                    if len(pending_gemini_batch) >= GEMINI_IMPORT_BATCH_SIZE:
                        _flush_gemini_batch(pending_gemini_batch, state)

                    _check_and_sync_state_locked(cursor, idx == batch_len - 1)

            except Exception as e:
                if str(e) == "Download cancelled by user":
                    logger.info(f"Download for {title} aborted: {e}")
                else:
                    logger.error(f"Failed to process {title}: {e}", exc_info=True)
                    with import_lock:
                        state["failed"] += 1
                        state["processed"] = max(state.get("processed", 0), cursor + 1)
                        active_imports[playlist_id] = state
                        _check_and_sync_state_locked(cursor, idx == batch_len - 1)

                # Cleanup uploaded media if failed before database queue
                if drive_file_id_upload and not queued_for_database:
                    try:
                        from dashboard.drive_client import delete_file
                        delete_file(drive_file_id_upload)
                        logger.info(f"Deleted uploaded media {drive_file_id_upload} after playlist track failure.")
                    except Exception as cleanup_err:
                        logger.warning(f"Could not delete uploaded media {drive_file_id_upload} after playlist track failure: {cleanup_err}")
            finally:
                # 4. Deregister in-flight and cleanup local temp audio file
                with import_lock:
                    for k in keys_to_register:
                        in_flight_tracks.discard(k)
                if local_file_path and os.path.exists(local_file_path):
                    try:
                        os.remove(local_file_path)
                    except Exception:
                        pass
                    
        while True:
            if not file_id and parent_id:
                try:
                    file_id = search_file_by_name(state_filename, parent_id)
                except Exception as e:
                    mark_failed_and_raise(e)
                    break

            with import_lock:
                state = active_imports.get(playlist_id)
                if not state and file_id:
                    try:
                        state = download_json(file_id)
                        if state:
                            active_imports[playlist_id] = state
                    except Exception as e:
                        mark_failed_and_raise(e)
                        break

                if not state:
                    logger.error(f"State file {state_filename} not found.")
                    break

                if not state.get("started_at"):
                    state["started_at"] = start_time_iso

                if is_cancel_requested(playlist_id):
                    state["status"] = "cancelled"
                    active_imports[playlist_id] = state
                    try:
                        if file_id and parent_id:
                            upload_json(file_id, state, state_filename, parent_id=parent_id)
                    except Exception as e:
                        logger.warning(f"Could not persist cancelled state: {e}")
                    logger.info(f"Import {playlist_id} is cancelled. Stopping.")
                    break

                if state.get("status") in ("cancelled", "completed"):
                    logger.info(f"Import {playlist_id} is {state.get('status')}. Stopping.")
                    break
                    
                processed = state.get("processed", 0)
                tracks = state.get("tracks", [])
                
                if processed >= len(tracks):
                    if is_cancel_requested(playlist_id):
                        state["status"] = "cancelled"
                    else:
                        state["status"] = "completed"
                    active_imports[playlist_id] = state
                    try:
                        if file_id and parent_id:
                            upload_json(file_id, state, state_filename, parent_id=parent_id)
                    except Exception as e:
                        mark_failed_and_raise(e)
                    break
                    
                batch = tracks[processed:processed+batch_size]

            batch_len = len(batch)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = []
                for idx, t in enumerate(batch):
                    cursor = processed + idx
                    if is_cancel_requested(playlist_id):
                        break
                    future = executor.submit(_process_track_worker, cursor, t, idx, batch_len)
                    futures.append(future)

                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as worker_err:
                        logger.error(f"Worker task error in playlist import: {worker_err}", exc_info=True)

            if is_cancel_requested(playlist_id):
                with import_lock:
                    state["status"] = "cancelled"
                    active_imports[playlist_id] = state
                    try:
                        if file_id and parent_id:
                            upload_json(file_id, state, state_filename, parent_id=parent_id)
                    except Exception as e:
                        logger.warning(f"Could not persist cancelled state: {e}")
                logger.info(f"Import {playlist_id} is cancelled after batch. Stopping.")
                break

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
                    _flush_gemini_batch(retry_chunk, final_state, final_attempt=True)
                    del deferred_gemini_tracks[:retry_count]
                except Exception as e:
                    logger.error(f"Failed to flush deferred Gemini playlist batch: {e}")
                    break

        # Leftover flush
        if pending_gemini_batch and not was_cancelled:
            logger.info(f"Flushing remaining {len(pending_gemini_batch)} tracks after main loop.")
            try:
                _flush_gemini_batch(pending_gemini_batch, final_state, final_attempt=True)
            except Exception as e:
                logger.error(f"Failed to flush leftover Gemini batch: {e}")

        # Final state persistence guarantee & duration logging
        target_status = "cancelled" if was_cancelled else ("failed" if final_state.get("status") == "failed" else "completed")
        _finalize_state_timing(final_state, target_status)
        active_imports[playlist_id] = final_state

        dur_str = _format_duration(final_state.get("duration_seconds"))
        if was_cancelled:
            logger.info(
                f"Playlist import for {playlist_id} cancelled after {dur_str} "
                f"(processed: {final_state.get('processed', 0)}/{final_state.get('total_tracks', 0)})."
            )
        elif target_status == "completed":
            logger.info(
                f"Playlist import completed in {dur_str} "
                f"(processed: {final_state.get('processed', 0)}, downloaded: {final_state.get('downloaded', 0)}, "
                f"skipped: {final_state.get('skipped', 0)}, failed: {final_state.get('failed', 0)})."
            )

        if file_id and parent_id:
            try:
                upload_json(file_id, final_state, state_filename, parent_id=parent_id)
            except Exception as e:
                logger.warning(f"Could not persist final playlist import state: {e}")
    finally:
        cleanup_cancel_event(playlist_id)
