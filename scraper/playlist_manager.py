import os
import sys
import uuid
import datetime
import logging

# Add project root to sys.path to resolve imports when run directly or as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.drive_client import download_json, upload_json, search_file_by_name
from scraper.drive_uploader import get_db_file_id

logger = logging.getLogger(__name__)

def load_playlists():
    """
    Downloads playlists.json from the Drive database folder.
    Returns an empty list if not found.
    """
    db_file_id, parent_id = get_db_file_id()
    if not parent_id:
        logger.warning("Could not determine database folder to load playlists.json")
        return []
        
    playlists_file_id = search_file_by_name("playlists.json", parent_id)
    if not playlists_file_id:
        return []
        
    try:
        data = download_json(playlists_file_id)
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.error(f"Failed to load playlists.json: {e}")
        return []

def save_playlists(playlists):
    """
    Uploads playlists.json to the Drive database folder.
    Creates a backup of the existing playlists.json first if it exists.
    """
    db_file_id, parent_id = get_db_file_id()
    if not parent_id:
        logger.error("Could not determine database folder to save playlists.json")
        return
        
    playlists_file_id = search_file_by_name("playlists.json", parent_id)
    
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
        upload_json(playlists_file_id, playlists, "playlists.json", parent_id=parent_id)
    except Exception as e:
        logger.error(f"Failed to save playlists.json: {e}")

def add_playlist(name, source_url, cover_image, imported_via, requestedBy):
    """
    Creates a new playlist entry with empty track_ids and returns the generated playlist id.
    """
    playlists = load_playlists()
    
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
    save_playlists(playlists)
    
    return playlist_id

def add_track_to_playlist(playlist_id, drive_file_id):
    """
    Appends drive_file_id to the playlist's track_ids if not already present,
    updates total_tracks, and saves.
    """
    playlists = load_playlists()
    updated = False
    
    for playlist in playlists:
        if playlist.get("id") == playlist_id:
            if drive_file_id not in playlist.get("track_ids", []):
                if "track_ids" not in playlist:
                    playlist["track_ids"] = []
                playlist["track_ids"].append(drive_file_id)
                playlist["total_tracks"] = len(playlist["track_ids"])
                updated = True
            break
            
    if updated:
        save_playlists(playlists)

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
            db_data = download_json(db_file_id)
            if isinstance(db_data, list):
                all_tracks = db_data
            elif isinstance(db_data, dict) and 'tracks' in db_data:
                all_tracks = db_data['tracks']
        except Exception as e:
            logger.error(f"Failed to load database.json: {e}")
            
    track_map = {t.get("driveFileId", t.get("id")): t for t in all_tracks}
    
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
