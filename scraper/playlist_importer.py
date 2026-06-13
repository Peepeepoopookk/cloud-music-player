import os
import json
import logging
import time
import re
import requests

from scraper.spotify_charts import scrape_spotify_embed_playlist, HEADERS, fetch_album_art, detect_track_language
from scraper.downloader import download_track
from scraper.main import extract_duration
from scraper.drive_uploader import upload_track, update_database, get_db_file_id
from dashboard.drive_client import upload_json, download_json, search_file_by_name

logger = logging.getLogger(__name__)

def get_playlist_preview(playlist_url):
    match = re.search(r'playlist/([a-zA-Z0-9]+)', playlist_url)
    if not match:
        raise ValueError("Invalid Spotify playlist URL")
    playlist_id = match.group(1)
    
    tracks = scrape_spotify_embed_playlist(playlist_id)
    
    playlist_name = "Spotify Playlist"
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
        
    total_tracks = len(tracks)
    preview_tracks = tracks[:5]
    estimated_mb = total_tracks * 5
    estimated_display = f"~{estimated_mb} MB"
    
    return {
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "total_tracks": total_tracks,
        "estimated_size_mb": estimated_mb,
        "estimated_size_display": estimated_display,
        "preview_tracks": preview_tracks
    }

def start_playlist_import(playlist_url, batch_size=15):
    preview = get_playlist_preview(playlist_url)
    playlist_id = preview["playlist_id"]
    tracks = scrape_spotify_embed_playlist(playlist_id)
    
    state = {
        "playlist_id": playlist_id,
        "playlist_url": playlist_url,
        "playlist_name": preview["playlist_name"],
        "total_tracks": preview["total_tracks"],
        "processed": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "status": "running",
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
    db_file_id, parent_id = get_db_file_id()
    state_filename = f"playlist_import_state_{playlist_id}.json"
    file_id = search_file_by_name(state_filename, parent_id)
    if not file_id:
        return {"status": "not_found"}
    return download_json(file_id)

def run_playlist_import(playlist_id, batch_size=15):
    logger.info(f"Starting background playlist import for {playlist_id}")
    db_file_id, parent_id = get_db_file_id()
    state_filename = f"playlist_import_state_{playlist_id}.json"
    
    temp_dir = os.environ.get('TEMP_DIR', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp'))
    os.makedirs(temp_dir, exist_ok=True)
    
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
            
    while True:
        file_id = search_file_by_name(state_filename, parent_id)
        if not file_id:
            logger.error(f"State file {state_filename} not found.")
            break
            
        state = download_json(file_id)
        if state.get("status") in ("cancelled", "completed"):
            logger.info(f"Import {playlist_id} is {state.get('status')}. Stopping.")
            break
            
        processed = state.get("processed", 0)
        tracks = state.get("tracks", [])
        
        if processed >= len(tracks):
            state["status"] = "completed"
            upload_json(file_id, state, state_filename, parent_id=parent_id)
            break
            
        batch = tracks[processed:processed+batch_size]
        for t in batch:
            current_state = download_json(file_id)
            if current_state.get("status") == "cancelled":
                logger.info(f"Import {playlist_id} cancelled.")
                return
                
            title = t.get("title")
            artist = t.get("artist")
            spotify_id = t.get("spotify_id")
            source = f"Playlist Import ({state.get('playlist_name')})"
            
            logger.info(f"Processing playlist track: {title} by {artist}")
            
            is_dup = False
            title_lower = title.lower() if title else ""
            artist_lower = artist.lower() if artist else ""
            for et in existing_tracks:
                if (et.get("title") or "").lower() == title_lower and (et.get("artist") or "").lower() == artist_lower:
                    is_dup = True
                    break
            
            if is_dup:
                logger.info(f"Skipping duplicate: {title}")
                state["skipped"] += 1
            else:
                local_file_path = None
                try:
                    lang, _ = detect_track_language(title, artist)
                    album_art = fetch_album_art(title, artist)
                    
                    local_file_path = download_track(title, artist, temp_dir)
                    drive_file_id_upload = upload_track(local_file_path)
                    duration, duration_seconds = extract_duration(local_file_path)
                    
                    metadata = {
                        "title": title,
                        "artist": artist,
                        "album": "Single",
                        "genre": "Unknown",
                        "duration": duration,
                        "durationSeconds": duration_seconds,
                        "spotify_id": spotify_id,
                        "album_art": album_art,
                        "language": lang,
                        "source": source
                    }
                    
                    update_database(drive_file_id_upload, metadata)
                    existing_tracks.append(metadata)
                    
                    state["downloaded"] += 1
                except Exception as e:
                    logger.error(f"Failed to process {title}: {e}")
                    state["failed"] += 1
                finally:
                    if local_file_path and os.path.exists(local_file_path):
                        try:
                            os.remove(local_file_path)
                        except:
                            pass
                            
            state["processed"] += 1
            upload_json(file_id, state, state_filename, parent_id=parent_id)
