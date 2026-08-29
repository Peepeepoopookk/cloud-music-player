import os
import sys
import uuid
import datetime
import logging

# Add project root to sys.path to resolve imports when run directly or as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.drive_client import download_json, upload_json, list_files
from scraper.drive_uploader import get_db_file_id
from scraper.operation_lock import library_write_lock

logger = logging.getLogger(__name__)

_cached_playlists_file_id = None

def _find_playlists_file(parent_id, force_refresh=False):
    global _cached_playlists_file_id
    if _cached_playlists_file_id and not force_refresh:
        return _cached_playlists_file_id

    for file_info in list_files(parent_id):
        if file_info.get("name") == "playlists.json":
            _cached_playlists_file_id = file_info.get("id")
            return _cached_playlists_file_id
    _cached_playlists_file_id = None
    return None

def _load_playlists_unlocked(parent_id):
    playlists_file_id = _find_playlists_file(parent_id)
    if not playlists_file_id:
        return []

    try:
        data = download_json(playlists_file_id)
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.warning(f"Failed to download playlists.json with cached ID {playlists_file_id}: {e}. Retrying lookup...")
        fresh_file_id = _find_playlists_file(parent_id, force_refresh=True)
        if not fresh_file_id:
            return []
        data = download_json(fresh_file_id)
        if isinstance(data, list):
            return data
        return []

def _save_playlists_unlocked(parent_id, playlists):
    playlists_file_id = _find_playlists_file(parent_id)

    if playlists_file_id:
        # Create a backup
        try:
            existing_data = download_json(playlists_file_id)
            now = datetime.datetime.utcnow()
            backup_filename = f"playlists_backup_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json"
            upload_json(None, existing_data, backup_filename, parent_id=parent_id)
        except Exception as e:
            logger.warning(f"Failed to create backup of playlists.json: {e}")

    try:
        res = upload_json(playlists_file_id, playlists, "playlists.json", parent_id=parent_id)
        if res and isinstance(res, dict) and res.get("id"):
            global _cached_playlists_file_id
            _cached_playlists_file_id = res.get("id")
        return res
    except Exception as e:
        logger.warning(f"Failed to upload playlists.json with ID {playlists_file_id}: {e}. Retrying with fresh lookup...")
        fresh_file_id = _find_playlists_file(parent_id, force_refresh=True)
        res = upload_json(fresh_file_id, playlists, "playlists.json", parent_id=parent_id)
        if res and isinstance(res, dict) and res.get("id"):
            _cached_playlists_file_id = res.get("id")
        return res

def load_playlists():
    """
    Downloads playlists.json from the Drive database folder.
    Returns an empty list if not found.
    """
    db_file_id, parent_id = get_db_file_id()
    if not parent_id:
        raise ValueError("Could not determine database folder to load playlists.json")
        
    try:
        return _load_playlists_unlocked(parent_id)
    except Exception as e:
        logger.error(f"Failed to load playlists.json: {e}")
        raise

def save_playlists(playlists):
    """
    Uploads playlists.json to the Drive database folder.
    Creates a backup of the existing playlists.json first if it exists.
    """
    db_file_id, parent_id = get_db_file_id()
    if not parent_id:
        raise ValueError("Could not determine database folder to save playlists.json")

    with library_write_lock("playlists"):
        try:
            return _save_playlists_unlocked(parent_id, playlists)
        except Exception as e:
            logger.error(f"Failed to save playlists.json: {e}")
            raise

def find_playlist_by_source_url(source_url):
    """
    Finds and returns the first playlist dict matching the given source_url.
    Normalizes URLs by extracting the Spotify playlist ID using extract_spotify_playlist_id
    so query params (like ?si=...) don't prevent matching. Returns None if no match found.
    """
    if not source_url:
        return None

    from scraper.spotify_library_importer import extract_spotify_playlist_id

    try:
        target_id = extract_spotify_playlist_id(source_url)
    except Exception:
        target_id = str(source_url).split('?')[0].rstrip('/').lower()

    playlists = load_playlists()
    for p in playlists:
        p_url = p.get("source_url")
        if not p_url:
            continue
        try:
            p_sp_id = extract_spotify_playlist_id(p_url)
            if p_sp_id == target_id:
                return p
        except Exception:
            pass

        norm_p_url = str(p_url).split('?')[0].rstrip('/').lower()
        if norm_p_url == target_id:
            return p

    return None

def add_playlist(name, source_url, cover_image, imported_via, requestedBy):
    """
    Creates a new playlist entry with empty track_ids and returns the generated playlist id.
    """
    _, parent_id = get_db_file_id()
    if not parent_id:
        raise ValueError("Could not determine database folder to add playlist")

    with library_write_lock("playlists"):
        playlists = _load_playlists_unlocked(parent_id)

        playlist_id = str(uuid.uuid4())

        new_playlist = {
            "id": playlist_id,
            "name": name,
            "source_url": source_url,
            "cover_image": cover_image,
            "track_ids": [],
            "total_tracks": 0,
            "created_at": datetime.datetime.utcnow().isoformat() + 'Z',
            "imported_via": imported_via,
            "requestedBy": requestedBy
        }

        playlists.append(new_playlist)
        _save_playlists_unlocked(parent_id, playlists)

    return playlist_id

def bulk_add_tracks_to_playlist(playlist_id, track_ids: list):
    """
    Appends multiple track_ids to the playlist's track_ids (skipping any already present),
    updates total_tracks once, and saves playlists.json once under library_write_lock.
    """
    if not track_ids:
        return

    _, parent_id = get_db_file_id()
    if not parent_id:
        raise ValueError("Could not determine database folder to update playlist")

    with library_write_lock("playlists"):
        playlists = _load_playlists_unlocked(parent_id)
        updated = False

        for playlist in playlists:
            if playlist.get("id") == playlist_id:
                if "track_ids" not in playlist:
                    playlist["track_ids"] = []
                current_ids = set(playlist.get("track_ids", []))
                for tid in track_ids:
                    if tid and tid not in current_ids:
                        playlist["track_ids"].append(tid)
                        current_ids.add(tid)
                        updated = True
                if updated:
                    playlist["total_tracks"] = len(playlist["track_ids"])
                break

        if updated:
            _save_playlists_unlocked(parent_id, playlists)

def add_track_to_playlist(playlist_id, drive_file_id):
    """
    Appends drive_file_id to the playlist's track_ids if not already present,
    updates total_tracks, and saves.
    """
    if not drive_file_id:
        return
    bulk_add_tracks_to_playlist(playlist_id, [drive_file_id])

import time

_db_cache = {"data": None, "timestamp": 0}
CACHE_TTL_SECONDS = 30

def get_database_cached(db_file_id=None):
    """
    Returns database.json data, caching it in memory for CACHE_TTL_SECONDS (30s)
    to prevent hammering Google Drive under concurrent playlist reads.
    """
    global _db_cache
    if not db_file_id:
        db_file_id, _ = get_db_file_id()
    if not db_file_id:
        return None

    if _db_cache["data"] is not None and (time.time() - _db_cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _db_cache["data"]

    data = download_json(db_file_id)
    _db_cache["data"] = data
    _db_cache["timestamp"] = time.time()
    return data

def invalidate_db_cache():
    global _db_cache
    _db_cache["data"] = None
    _db_cache["timestamp"] = 0

def get_playlist(playlist_id):
    """
    Returns a single playlist with full track objects populated by
    cross-referencing track_ids against database.json tracks.
    """
    playlists = load_playlists()
    target_playlist = None
    
    for p in playlists:
        if p.get("id") == playlist_id:
            target_playlist = p
            break
            
    if target_playlist is None:
        return None
        
    db_file_id, parent_id = get_db_file_id()
    all_tracks = []
    if db_file_id:
        try:
            db_data = get_database_cached(db_file_id)
            if isinstance(db_data, list):
                all_tracks = db_data
            elif isinstance(db_data, dict) and 'tracks' in db_data:
                all_tracks = db_data['tracks']
        except Exception as e:
            logger.error(f"Failed to load database.json: {e}")
            raise
            
    track_map = {}
    for t in all_tracks:
        if not isinstance(t, dict):
            continue
        key1 = t.get("driveFileId")
        key2 = t.get("id")
        if key1:
            track_map[key1] = t
            track_map[str(key1)] = t
        if key2:
            track_map[key2] = t
            track_map[str(key2)] = t
    
    populated_tracks = []
    for tid in target_playlist.get("track_ids", []):
        if tid in track_map:
            populated_tracks.append(track_map[tid])
            
    # Return a copy with tracks array instead of just ids
    result = dict(target_playlist)
    result["tracks"] = populated_tracks
    return result

def get_all_playlists():
    """
    Returns all playlists, lightweight for listing (doesn't populate full track objects).
    """
    return load_playlists()

def delete_playlist(playlist_id):
    """
    Removes the playlist with the specified playlist_id from playlists.json.
    Does NOT modify database.json or delete any song audio files from Drive.
    Returns True if a playlist was found and deleted, False otherwise.
    """
    if not playlist_id:
        return False

    _, parent_id = get_db_file_id()
    if not parent_id:
        raise ValueError("Could not determine database folder to delete playlist")

    with library_write_lock("playlists"):
        playlists = _load_playlists_unlocked(parent_id)
        original_count = len(playlists)
        playlists = [p for p in playlists if p.get("id") != playlist_id]

        if len(playlists) == original_count:
            logger.info(f"delete_playlist: Playlist ID {playlist_id} not found.")
            return False

        _save_playlists_unlocked(parent_id, playlists)
        logger.info(f"delete_playlist: Successfully deleted playlist ID {playlist_id}.")
        return True

