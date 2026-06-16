import os
import sys
import datetime
import logging

# Add project root to sys.path to resolve imports when run directly or as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.drive_client import download_json
from scraper.drive_uploader import get_db_file_id

logger = logging.getLogger(__name__)

def parse_iso_time(ts_str):
    if not ts_str:
        return datetime.datetime.min
    ts_str = ts_str.replace('Z', '+00:00')
    try:
        return datetime.datetime.fromisoformat(ts_str)
    except Exception:
        return datetime.datetime.min

def load_all_tracks():
    db_file_id, parent_id = get_db_file_id()
    if not db_file_id:
        logger.warning("artist_manager: Could not determine database file ID.")
        return []
        
    try:
        data = download_json(db_file_id)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and 'tracks' in data:
            return data['tracks']
        return []
    except Exception as e:
        logger.error(f"artist_manager: Failed to load database.json: {e}")
        return []

def get_all_artists():
    """
    Returns a list of unique artists with track_count and cover_image.
    Sorted alphabetically by artist name.
    """
    tracks = load_all_tracks()
    
    # Sort tracks descending by addedAt so we can grab the most recent album_art
    tracks.sort(key=lambda x: parse_iso_time(x.get("addedAt", x.get("timestamp"))), reverse=True)
    
    artists_dict = {}
    
    for t in tracks:
        artist_str = t.get("artist", "")
        if not artist_str:
            artist_str = "Unknown Artist"
            
        # Split by comma and strip
        artist_names = [a.strip() for a in artist_str.split(",") if a.strip()]
        if not artist_names:
            artist_names = ["Unknown Artist"]
            
        album_art = t.get("album_art") or t.get("albumArt")
        
        for name in artist_names:
            if name not in artists_dict:
                artists_dict[name] = {
                    "artist_name": name,
                    "track_count": 0,
                    "cover_image": None
                }
            
            artists_dict[name]["track_count"] += 1
            
            # Set cover_image to the most recent track's art if not already set
            if not artists_dict[name]["cover_image"] and album_art:
                artists_dict[name]["cover_image"] = album_art
                
    # Convert dict to list
    artists_list = list(artists_dict.values())
    
    # Sort alphabetically
    artists_list.sort(key=lambda x: x["artist_name"].lower())
    
    return artists_list

def get_artist_tracks(artist_name):
    """
    Returns all tracks where this artist_name appears anywhere in the artist field.
    Sorted by addedAt descending.
    """
    if not artist_name:
        return []
        
    target_name_lower = artist_name.strip().lower()
    tracks = load_all_tracks()
    
    result = []
    for t in tracks:
        artist_str = t.get("artist", "")
        if not artist_str:
            artist_names = ["unknown artist"]
        else:
            artist_names = [a.strip().lower() for a in artist_str.split(",")]
            
        if target_name_lower in artist_names:
            result.append(t)
            
    # Sort tracks descending by addedAt
    result.sort(key=lambda x: parse_iso_time(x.get("addedAt", x.get("timestamp"))), reverse=True)
    
    return result

def search_artists(query):
    """
    Returns matching artist objects based on partial string match against artist_name.
    """
    if not query:
        return []
        
    query_lower = query.lower()
    all_artists = get_all_artists()
    
    result = []
    for a in all_artists:
        if query_lower in a["artist_name"].lower():
            result.append(a)
            
    return result
