import sys
import os
import json
sys.path.append(os.getcwd())
from scraper.playlist_importer import get_playlist_preview

playlists = [
    'https://open.spotify.com/playlist/5S8SJdl1BDc0ugpkEvFsIL', # 1000 songs
    'https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M', # Today's Top Hits (small)
]

for url in playlists:
    try:
        preview = get_playlist_preview(url)
        print(f"\nURL: {url}")
        print(f"total_tracks: {preview.get('total_tracks')}")
        print(f"tracks_available_for_import: {preview.get('tracks_available_for_import')}")
        print(f"truncated: {preview.get('truncated')}")
        print(f"truncation_warning: {preview.get('truncation_warning')}")
    except Exception as e:
        print(f"Failed {url}: {e}")
