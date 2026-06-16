import os
import sys
import datetime
import uuid
import re

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper.drive_uploader import get_db_file_id
from dashboard.drive_client import download_json
from scraper.playlist_manager import add_playlist, add_track_to_playlist

def parse_iso_time(ts_str):
    if not ts_str:
        return datetime.datetime.min
    # handle python 3.11 fromisoformat 'Z' suffix, or strip it
    ts_str = ts_str.replace('Z', '+00:00')
    try:
        return datetime.datetime.fromisoformat(ts_str)
    except Exception:
        return datetime.datetime.min

def run_backfill():
    print("Starting playlist backfill...")
    db_file_id, parent_id = get_db_file_id()
    if not db_file_id:
        print("Database not found.")
        return
        
    data = download_json(db_file_id)
    tracks = data if isinstance(data, list) else data.get('tracks', [])
    
    # Filter playlist tracks
    playlist_tracks = []
    for t in tracks:
        source = t.get("source", "")
        if source == "playlist_import":
            playlist_tracks.append(t)
            
    print(f"Found {len(playlist_tracks)} tracks from playlist imports.")
    
    # Sort tracks by timestamp to make clustering easier
    playlist_tracks.sort(key=lambda x: parse_iso_time(x.get("addedAt", x.get("timestamp"))))
    
    clusters = []
    current_cluster = []
    
    # 30 minutes window
    WINDOW = datetime.timedelta(minutes=30)
    
    for t in playlist_tracks:
        if not current_cluster:
            current_cluster.append(t)
        else:
            last_t = current_cluster[-1]
            t_time = parse_iso_time(t.get("addedAt", t.get("timestamp")))
            last_time = parse_iso_time(last_t.get("addedAt", last_t.get("timestamp")))
            
            if abs((t_time - last_time).total_seconds()) <= WINDOW.total_seconds():
                current_cluster.append(t)
            else:
                clusters.append(current_cluster)
                current_cluster = [t]
                
    if current_cluster:
        clusters.append(current_cluster)
                
    print(f"Detected {len(clusters)} clusters.")
    
    for idx, cluster in enumerate(clusters):
        first_track = cluster[0]
        cluster_time = parse_iso_time(first_track.get("addedAt", first_track.get("timestamp")))
        name = f"Imported Playlist - {cluster_time.strftime('%b %d')}"
        
        # Check if we have cover image
        cover_image = None
        for t in cluster:
            if t.get("album_art"):
                cover_image = t.get("album_art")
                break
                
        print(f"Cluster {idx+1}: {name} with {len(cluster)} tracks. Starting track at {first_track.get('addedAt')}")
        
        # create playlist record
        playlist_id = add_playlist(
            name=name,
            source_url=None,
            cover_image=cover_image,
            imported_via="dashboard",
            requestedBy=None
        )
        
        # add tracks
        for t in cluster:
            drive_id = t.get("driveFileId", t.get("id"))
            add_track_to_playlist(playlist_id, drive_id)
            
    print("Successfully finished backfilling playlists.")

if __name__ == "__main__":
    run_backfill()
