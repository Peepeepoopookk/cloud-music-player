import os
import sys
import time
import datetime
import logging

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard.drive_client import download_json, upload_json
from scraper.drive_uploader import get_db_file_id
from scraper.operation_lock import library_write_lock
from scraper.playlist_manager import (
    _find_playlists_file,
    _load_playlists_unlocked,
    load_playlists,
)
from scraper.spotify_charts import scrape_spotify_embed_playlist
from scraper.spotify_library_importer import extract_spotify_playlist_id
from scraper.state_manager import find_duplicate_track

# Configure logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _batch_add_tracks_to_playlist(parent_id, playlist_id, new_track_ids):
    """
    Appends multiple new_track_ids to the specified playlist in a single
    locked read-modify-write operation on playlists.json, without creating
    per-write backup files.
    """
    if not new_track_ids:
        return 0

    with library_write_lock("playlists"):
        playlists_file_id = _find_playlists_file(parent_id)
        if not playlists_file_id:
            raise ValueError("Could not find playlists.json in Drive folder to update.")

        playlists = _load_playlists_unlocked(parent_id)
        actually_added = 0

        for p in playlists:
            if p.get("id") == playlist_id:
                if "track_ids" not in p or not isinstance(p["track_ids"], list):
                    p["track_ids"] = []
                existing_set = set(p["track_ids"])
                for tid in new_track_ids:
                    if tid not in existing_set:
                        p["track_ids"].append(tid)
                        existing_set.add(tid)
                        actually_added += 1
                if actually_added > 0:
                    p["total_tracks"] = len(p["track_ids"])
                break

        if actually_added > 0:
            upload_json(playlists_file_id, playlists, "playlists.json", parent_id=parent_id)

        return actually_added


def run():
    print("=" * 80)
    print("STARTING PLAYLIST LINK BACKFILL (BATCHED, READ-ONLY FOR MEDIA)")
    print("=" * 80)

    db_file_id, parent_id = get_db_file_id()
    if not db_file_id or not parent_id:
        print("Error: Could not locate database folder on Google Drive.")
        return

    print("Downloading current database.json from Drive...")
    db_data = download_json(db_file_id)
    if isinstance(db_data, list):
        database_tracks = db_data
    elif isinstance(db_data, dict) and "tracks" in db_data:
        database_tracks = db_data["tracks"]
    else:
        database_tracks = []

    print(f"Loaded {len(database_tracks)} tracks from database.json.")

    print("Loading playlists from playlists.json...")
    playlists = load_playlists()
    print(f"Loaded {len(playlists)} playlists from playlists.json.")

    # Create ONE full safety-net backup of playlists.json before processing
    playlists_file_id = _find_playlists_file(parent_id)
    if playlists_file_id:
        try:
            now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
            backup_filename = f"playlists_bulk_backup_{now_str}.json"
            upload_json(None, playlists, backup_filename, parent_id=parent_id)
            print(f"Created pre-migration backup of playlists.json as: {backup_filename}\n")
        except Exception as e:
            print(f"Warning: Could not create initial backup: {e}\n")

    summary_records = []

    for idx, playlist in enumerate(playlists):
        playlist_id = playlist.get("id")
        name = playlist.get("name", "Unnamed Playlist")
        source_url = playlist.get("source_url")
        current_track_ids = set(playlist.get("track_ids", []))

        print(f"[{idx + 1}/{len(playlists)}] Checking playlist: '{name}' (ID: {playlist_id})")

        if not source_url:
            print(f"  -> Skipping '{name}' — no source_url (legacy / clustered playlist)")
            summary_records.append({
                "name": name,
                "status": "Skipped (no URL)",
                "newly_linked": 0,
                "already_linked": len(current_track_ids),
                "not_in_library": 0,
                "total_scraped": 0,
            })
            continue

        try:
            sp_playlist_id = extract_spotify_playlist_id(source_url)
        except Exception as e:
            print(f"  -> Skipping '{name}' — could not parse Spotify URL ({source_url}): {e}")
            summary_records.append({
                "name": name,
                "status": "Invalid URL",
                "newly_linked": 0,
                "already_linked": len(current_track_ids),
                "not_in_library": 0,
                "total_scraped": 0,
            })
            continue

        try:
            scraped_tracks = scrape_spotify_embed_playlist(sp_playlist_id)
        except Exception as e:
            print(f"  -> Error scraping Spotify embed for '{name}': {e}")
            summary_records.append({
                "name": name,
                "status": "Scrape error",
                "newly_linked": 0,
                "already_linked": len(current_track_ids),
                "not_in_library": 0,
                "total_scraped": 0,
            })
            continue

        if not scraped_tracks:
            print(f"  -> No tracks returned from Spotify embed for '{name}'.")
            summary_records.append({
                "name": name,
                "status": "0 scraped tracks",
                "newly_linked": 0,
                "already_linked": len(current_track_ids),
                "not_in_library": 0,
                "total_scraped": 0,
            })
            continue

        new_links_for_this_playlist = []
        already_linked = 0
        not_in_library = 0

        for track in scraped_tracks:
            title = track.get("title", "Unknown Title")
            artist = track.get("artist", "Unknown Artist")

            # Check if this track already exists in database.json
            duplicate_track = find_duplicate_track(track, {}, database_tracks)
            if duplicate_track:
                existing_drive_id = duplicate_track.get("driveFileId") or duplicate_track.get("id")
                if existing_drive_id:
                    if existing_drive_id in current_track_ids or existing_drive_id in new_links_for_this_playlist:
                        already_linked += 1
                    else:
                        new_links_for_this_playlist.append(existing_drive_id)
                        print(f"     + Queued: '{title}' by '{artist}' -> {existing_drive_id}")
                else:
                    print(f"     ! Match found for '{title}' but no driveFileId present.")
            else:
                not_in_library += 1

        # Perform single batched write if new links were found
        newly_linked = 0
        if new_links_for_this_playlist:
            try:
                newly_linked = _batch_add_tracks_to_playlist(parent_id, playlist_id, new_links_for_this_playlist)
                current_track_ids.update(new_links_for_this_playlist)
                print(f"  -> Saved {newly_linked} new link(s) to playlists.json in 1 batch write.")
            except Exception as batch_err:
                print(f"  -> Failed to batch-save links for '{name}': {batch_err}")
                raise batch_err

        print(f"  -> Result: {newly_linked} newly linked, {already_linked} already linked, {not_in_library} not yet in library (out of {len(scraped_tracks)} scraped).")

        summary_records.append({
            "name": name,
            "status": "Processed",
            "newly_linked": newly_linked,
            "already_linked": already_linked,
            "not_in_library": not_in_library,
            "total_scraped": len(scraped_tracks),
        })

        # Shorter polite delay between Spotify scrapes
        time.sleep(0.5)

    print("\n" + "=" * 90)
    print("BACKFILL PLAYLIST LINKS SUMMARY")
    print("=" * 90)
    print(f"{'Playlist Name':<42} | {'Status':<16} | {'New':<5} | {'Already':<7} | {'Missing':<7} | {'Scraped':<7}")
    print("-" * 90)

    total_new = 0
    total_already = 0
    total_missing = 0

    for rec in summary_records:
        name_trunc = (rec["name"][:39] + "...") if len(rec["name"]) > 42 else rec["name"]
        print(f"{name_trunc:<42} | {rec['status']:<16} | {rec['newly_linked']:<5} | {rec['already_linked']:<7} | {rec['not_in_library']:<7} | {rec['total_scraped']:<7}")
        total_new += rec["newly_linked"]
        total_already += rec["already_linked"]
        total_missing += rec["not_in_library"]

    print("-" * 90)
    print(f"TOTALS: {total_new} newly linked | {total_already} already linked | {total_missing} not yet in library")
    print("=" * 90)


if __name__ == "__main__":
    run()
