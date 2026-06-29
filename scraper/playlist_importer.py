import os
import sys
import json
import logging
import time
import re
import requests

# Add project root to sys.path to resolve imports when run directly or as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .spotify_charts import scrape_spotify_embed_playlist, HEADERS
from .metadata_enricher import enrich_track_metadata
from .downloader import download_track
from .utils import extract_duration
from .drive_uploader import upload_track, update_database, get_db_file_id
from dashboard.drive_client import upload_json, download_json, search_file_by_name
from .playlist_manager import add_playlist, add_track_to_playlist

logger = logging.getLogger(__name__)

# In-memory dictionary to track active playlist imports without querying Google Drive repeatedly
active_imports = {}

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
        "truncated": truncated,
        "truncation_warning": truncation_warning,
        "estimated_size_mb": estimated_mb,
        "estimated_size_display": estimated_display,
        "preview_tracks": preview_tracks
    }

def start_playlist_import(playlist_url, batch_size=15, device_id=None, imported_via="dashboard"):
    preview = get_playlist_preview(playlist_url)
    
    # Call add_playlist to create a record and get a unified UUID for this import session
    playlist_id = add_playlist(
        name=preview["playlist_name"],
        source_url=playlist_url,
        cover_image=None,
        imported_via=imported_via,
        requestedBy=device_id
    )
    
    tracks = scrape_spotify_embed_playlist(preview["playlist_id"])
    
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
        "status": "running",
        "device_id": device_id,
        "tracks": tracks
    }
    
    db_file_id, parent_id = get_db_file_id()
    if parent_id:
        existing_file_id = search_file_by_name(f"playlist_import_state_{playlist_id}.json", parent_id)
        if existing_file_id:
            upload_json(existing_file_id, state, f"playlist_import_state_{playlist_id}.json", parent_id=parent_id)
        else:
            upload_json(None, state, f"playlist_import_state_{playlist_id}.json", parent_id=parent_id)
            
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
                if st.get("status") not in ("cancelled", "completed"):
                    st["status"] = "failed"
                    st["error"] = str(e)
                    active_imports[playlist_id] = st
                    upload_json(file_id, st, state_filename, parent_id=parent_id)
            except Exception as write_err:
                logger.error(f"Failed to write failure state: {write_err}")
        raise e

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
            
    pending_gemini_batch = []
    
    def cancel_check():
        st = active_imports.get(playlist_id)
        if st and st.get("status") == "cancelled":
            return True
        return False
        
    def _flush_gemini_batch(batch, state):
        if cancel_check() or not batch:
            return
            
        logger.info(f"Flushing batch of {len(batch)} tracks to Gemini Judge and Database...")
        try:
            from scraper.gemini_metadata_judge import GeminiJudge, normalize_genre_value, normalize_language_value
            from scraper.drive_uploader import bulk_update_database
            judge = GeminiJudge()
            
            try:
                response = judge.analyze_tracks_batch(batch)
                if response and response.tracks:
                    suggestions = response.tracks
                    batch_by_id = {
                        str(t.get("id") or t.get("driveFileId")): t
                        for t in batch
                        if t.get("id") or t.get("driveFileId")
                    }
                    for suggestion in suggestions:
                        track_ref = batch_by_id.get(str(suggestion.track_id))
                        if not track_ref:
                            logger.warning(f"Gemini returned suggestion for unknown playlist track ID {suggestion.track_id}. Skipped.")
                            continue
                        if suggestion.suggested_language.value and suggestion.suggested_language.confidence > 0.6:
                            normalized_language = normalize_language_value(suggestion.suggested_language.value)
                            if normalized_language and normalized_language != "unknown":
                                track_ref["language"] = normalized_language
                        if suggestion.suggested_genre.value and suggestion.suggested_genre.confidence > 0.6:
                            normalized_genre = normalize_genre_value(suggestion.suggested_genre.value)
                            if normalized_genre:
                                track_ref["genre"] = normalized_genre
                        if suggestion.clean_title.value and suggestion.clean_title.confidence > 0.6:
                            track_ref["title"] = suggestion.clean_title.value
                        if suggestion.clean_artist.value and suggestion.clean_artist.confidence > 0.6:
                            track_ref["artist"] = suggestion.clean_artist.value
            except Exception as e:
                logger.error(f"Gemini processing failed during batch flush: {e}. Falling back to scraped metadata.")
                
            bulk_update_database(batch)
            for t in batch:
                existing_tracks.append(t)
                add_track_to_playlist(playlist_id, t["id"])
                
        except Exception as batch_err:
            mark_failed_and_raise(batch_err)
        finally:
            batch.clear()
            state["gemini_pending"] = 0
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

        active_imports[playlist_id] = state
        if state.get("status") in ("cancelled", "completed"):
            logger.info(f"Import {playlist_id} is {state.get('status')}. Stopping.")
            break
            
        processed = state.get("processed", 0)
        tracks = state.get("tracks", [])
        
        if processed >= len(tracks):
            try:
                latest_state = download_json(file_id)
                if latest_state.get("status") == "cancelled":
                    active_imports[playlist_id] = latest_state
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

            active_imports[playlist_id] = state
            if state.get("status") == "cancelled":
                logger.info(f"Import cancelled by user at cursor {cursor}")
                break
                
            title = t.get("title")
            artist = t.get("artist")
            spotify_id = t.get("spotify_id")
            source = source_override if source_override else f"Playlist Import ({state.get('playlist_name')})"
            device_id = state.get("device_id")
            
            logger.info(f"Processing playlist track: {title} by {artist}")
            
            from scraper.state_manager import is_duplicate, load_state
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
                is_dup = is_duplicate(track_to_check, scraper_state, existing_tracks)
            except Exception as e:
                mark_failed_and_raise(e)
            
            if is_dup:
                logger.info(f"Skipping duplicate: {title}")
                state["skipped"] += 1
            else:
                local_file_path = None
                def cancel_check():
                    st = active_imports.get(playlist_id)
                    if st and st.get("status") == "cancelled":
                        return True
                    return False
                    
                try:
                    local_file_path = download_track(title, artist, temp_dir, cancel_check_callback=cancel_check)
                    drive_file_id_upload = upload_track(local_file_path)
                    
                    enriched = enrich_track_metadata(title, artist, local_file_path=local_file_path, source=source)
                    
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
                    state["gemini_pending"] = len(pending_gemini_batch)
                    
                    if len(pending_gemini_batch) >= 20:
                        _flush_gemini_batch(pending_gemini_batch, state)
                        
                    state["downloaded"] += 1
                except Exception as e:
                    if str(e) == "Download cancelled by user":
                        logger.info(f"Download for {title} aborted: {e}")
                    else:
                        logger.error(f"Failed to process {title}: {e}", exc_info=True)
                        state["failed"] += 1
                finally:
                    if local_file_path and os.path.exists(local_file_path):
                        try:
                            os.remove(local_file_path)
                        except:
                            pass
                            
            try:
                latest_state = download_json(file_id)
                if latest_state.get("status") == "cancelled":
                    active_imports[playlist_id] = latest_state
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

    # Leftover flush
    if pending_gemini_batch:
        logger.info(f"Flushing remaining {len(pending_gemini_batch)} tracks after main loop.")
        try:
            state = download_json(file_id)
            _flush_gemini_batch(pending_gemini_batch, state)
        except Exception as e:
            logger.error(f"Failed to flush leftover Gemini batch: {e}")
