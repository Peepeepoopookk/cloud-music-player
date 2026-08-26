import os
import sys
import datetime
import logging
from dotenv import load_dotenv

# Configure logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Load env variables from .env in project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=env_path)

# Add project root to sys.path to resolve dashboard package imports
if project_root not in sys.path:
    sys.path.append(project_root)

from dashboard.drive_client import upload_media, download_json, upload_json, list_files, delete_file
from scraper.album_art_resolver import resolve_album_art
from scraper.operation_lock import library_write_lock
from scraper.track_utils import (
    build_track_record,
    drive_id,
    extract_tracks,
    find_existing_track,
    merge_track,
    normalize_track_schema,
    replace_tracks,
    utc_now_iso,
)

_cached_lite_file_id = None


def build_lite_database(db_data):
    """
    Builds a copy of the database structure with 'lyrics' and 'syncedLyrics'
    stripped from each track entry.
    Preserves the exact top-level structure (list of track dicts or dict with 'tracks' key).
    """
    if isinstance(db_data, list):
        return [
            {k: v for k, v in track.items() if k not in ('lyrics', 'syncedLyrics')}
            if isinstance(track, dict) else track
            for track in db_data
        ]
    elif isinstance(db_data, dict) and isinstance(db_data.get("tracks"), list):
        lite_dict = dict(db_data)
        lite_dict["tracks"] = [
            {k: v for k, v in track.items() if k not in ('lyrics', 'syncedLyrics')}
            if isinstance(track, dict) else track
            for track in db_data["tracks"]
        ]
        return lite_dict
    elif isinstance(db_data, dict):
        return {
            k: ({ik: iv for ik, iv in v.items() if ik not in ('lyrics', 'syncedLyrics')} if isinstance(v, dict) else v)
            for k, v in db_data.items()
        }
    return db_data


def get_db_lite_file_id(parent_folder_id=None):
    """
    Finds or resolves the database_lite.json file ID on Google Drive.
    Checks GDRIVE_DB_LITE_FILE_ID env var, then in-memory cache, then searches Drive folder.
    Returns a tuple: (db_lite_file_id, parent_folder_id)
    """
    global _cached_lite_file_id
    lite_file_id = os.environ.get('GDRIVE_DB_LITE_FILE_ID') or _cached_lite_file_id
    folder_id = parent_folder_id or os.environ.get('GDRIVE_FOLDER_ID')

    if lite_file_id:
        return lite_file_id, folder_id

    try:
        if folder_id:
            files = list_files(folder_id)
            for f in files:
                if f.get('name') == 'database_lite.json':
                    lite_file_id = f.get('id')
                    _cached_lite_file_id = lite_file_id
                    logger.info(f"Found 'database_lite.json' with ID: {lite_file_id}")
                    return lite_file_id, folder_id
    except Exception as e:
        logger.warning(f"Could not search for 'database_lite.json': {e}")

    return None, folder_id


def sync_database_lite(db_data, parent_folder_id=None):
    """
    Generates a lyrics-stripped database_lite.json and uploads it to Drive.
    Runs asynchronously/safely without raising errors to ensure the primary
    database.json upload is never interrupted or rolled back.
    """
    global _cached_lite_file_id
    try:
        lite_data = build_lite_database(db_data)
        lite_file_id, target_parent = get_db_lite_file_id(parent_folder_id)
        with library_write_lock("database_lite"):
            res = upload_json(lite_file_id, lite_data, 'database_lite.json', parent_id=target_parent)
            if res and isinstance(res, dict) and res.get('id'):
                _cached_lite_file_id = res.get('id')
                logger.info(f"Successfully synced database_lite.json to Google Drive (ID: {_cached_lite_file_id}).")
            else:
                logger.info("Successfully synced database_lite.json to Google Drive.")
    except Exception as e:
        logger.error(f"Failed to sync database_lite.json (non-fatal): {e}", exc_info=True)


def upload_database_json_locked(db_file_id, db_data, parent_folder_id):
    with library_write_lock("database"):
        result = upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
    sync_database_lite(db_data, parent_folder_id)
    return result


def fetch_album_art(track_name, artist_name):
    """
    Resolves high-resolution album art from the shared multi-provider resolver.
    Returns None if no confident result is found.
    """
    try:
        return resolve_album_art(track_name, artist_name)
    except Exception as e:
        logger.warning(f"fetch_album_art: Failed for '{track_name}' by '{artist_name}': {e}")
        return None

def get_db_file_id():
    """
    Finds the database.json file on Google Drive, checking both the main folder
    and any 'database' subfolder. Returns a tuple: (db_file_id, parent_folder_id)
    """
    db_file_id = os.environ.get('GDRIVE_DB_FILE_ID')
    folder_id = os.environ.get('GDRIVE_FOLDER_ID')
    
    if db_file_id:
        return db_file_id, folder_id
        
    logger.info(f"GDRIVE_DB_FILE_ID not set. Searching for 'database.json' in folder: {folder_id}")
    files = []
    try:
        files = list_files(folder_id)
    except Exception as e:
        logger.error(f"Failed to list files in root folder {folder_id}: {e}")
        
    db_folder_id = None
    for f in files:
        if f.get('name') == 'database.json':
            db_file_id = f.get('id')
            logger.info(f"Found 'database.json' in root folder with ID: {db_file_id}")
            return db_file_id, folder_id
        elif f.get('name') == 'database' and f.get('mimeType') == 'application/vnd.google-apps.folder':
            db_folder_id = f.get('id')
            
    # If not found in root, check inside the 'database' subfolder if it exists
    if db_folder_id:
        logger.info(f"Searching for 'database.json' inside 'database' subfolder: {db_folder_id}")
        try:
            sub_files = list_files(db_folder_id)
            for sf in sub_files:
                if sf.get('name') == 'database.json':
                    db_file_id = sf.get('id')
                    logger.info(f"Found 'database.json' inside database subfolder with ID: {db_file_id}")
                    return db_file_id, db_folder_id
        except Exception as e:
            logger.error(f"Failed to list files in subfolder {db_folder_id}: {e}")
            
    # Default back to folder_id or db_folder_id if not found (for uploader target parent)
    target_parent = db_folder_id if db_folder_id else folder_id
    return None, target_parent

def upload_track(file_path, metadata=None):
    """
    Uploads the .opus file to the Google Drive media folder.
    Returns the Drive file ID of the uploaded file.
    """
    try:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Local file does not exist: {file_path}")
            
        media_folder_id = os.environ.get('GDRIVE_MEDIA_FOLDER_ID')
        if not media_folder_id:
            raise ValueError("GDRIVE_MEDIA_FOLDER_ID environment variable is not set.")
            
        filename = os.path.basename(file_path)
        logger.info(f"Uploading file '{filename}' to Google Drive media folder ID: {media_folder_id}")
        
        drive_file_id = upload_media(file_path, media_folder_id, filename)
        logger.info(f"Successfully uploaded track to Drive. ID: {drive_file_id}")
        return drive_file_id
        
    except Exception as e:
        logger.error(f"Failed to upload track: {e}", exc_info=True)
        raise

def update_database(drive_file_id, metadata):
    """
    Downloads database.json from Drive, appends the new track details,
    and uploads the updated database back to Drive.
    """
    try:
        metadata = dict(metadata)
        title = metadata.get('title', 'Unknown Title')
        artist = metadata.get('artist', 'Unknown Artist')
        metadata["album_art"] = metadata.get("album_art") or metadata.get("albumArt") or fetch_album_art(title, artist)

        with library_write_lock("database"):
            db_file_id, parent_folder_id = get_db_file_id()

            db_data = []
            if db_file_id:
                logger.info(f"Downloading existing database.json (ID: {db_file_id}) from Drive...")
                db_data = download_json(db_file_id)
            else:
                logger.info("database.json not found on Drive. Creating a fresh index list.")

            tracks, was_dict = extract_tracks(db_data)
            now = utc_now_iso()
            new_track = build_track_record(drive_file_id, metadata, now=now)

            existing, reason = find_existing_track(tracks, new_track)
            if existing:
                existing_id = drive_id(existing)
                metadata["id"] = existing_id
                metadata["driveFileId"] = existing_id
                changed = merge_track(existing, new_track, now=now)
                if changed:
                    db_data = replace_tracks(db_data, tracks, was_dict)
                    upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
                    logger.info(f"Merged duplicate track '{title}' by '{artist}' into existing database record ({reason}).")
                    sync_database_lite(db_data, parent_folder_id)
                    return {"id": db_file_id, "duplicate": True, "merged": True, "track_id": existing_id}
                logger.info(f"Skipped duplicate track '{title}' by '{artist}' ({reason}); database already has it.")
                return {"id": db_file_id, "duplicate": True, "merged": False, "track_id": existing_id}

            tracks.append(new_track)
            logger.info(f"Appending track '{new_track['title']}' to database.")

            db_data = replace_tracks(db_data, tracks, was_dict)
            result = upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
            logger.info("Successfully updated database.json on Google Drive.")
            sync_database_lite(db_data, parent_folder_id)
            return result
        
    except Exception as e:
        logger.error(f"Failed to update database: {e}", exc_info=True)
        raise

def bulk_update_database(new_tracks):
    """
    Downloads database.json from Drive, appends all tracks in new_tracks list,
    and uploads the updated database back to Drive.
    """
    try:
        prepared_tracks = []
        for metadata in new_tracks:
            title = metadata.get('title', 'Unknown Title')
            artist = metadata.get('artist', 'Unknown Artist')
            metadata["album_art"] = metadata.get("album_art") or metadata.get("albumArt") or fetch_album_art(title, artist)
            prepared_tracks.append(metadata)

        duplicate_media_to_delete = []
        upload_succeeded = True
        with library_write_lock("database"):
            db_file_id, parent_folder_id = get_db_file_id()

            db_data = []
            if db_file_id:
                logger.info(f"Downloading existing database.json (ID: {db_file_id}) from Drive for bulk update...")
                db_data = download_json(db_file_id)
            else:
                logger.info("database.json not found on Drive. Creating a fresh index list.")

            db_tracks, was_dict = extract_tracks(db_data)
            now = utc_now_iso()
            inserted = 0
            merged = 0
            changed = False

            for metadata in prepared_tracks:
                title = metadata.get('title', 'Unknown Title')
                artist = metadata.get('artist', 'Unknown Artist')
                record = build_track_record(metadata.get('id') or metadata.get('driveFileId'), metadata, now=now)

                existing, reason = find_existing_track(db_tracks, record)
                if existing:
                    incoming_id = drive_id(record)
                    existing_id = drive_id(existing)
                    metadata["id"] = existing_id
                    metadata["driveFileId"] = existing_id
                    if merge_track(existing, record, now=now):
                        changed = True
                        merged += 1
                    logger.info(f"Bulk update reused existing track '{title}' by '{artist}' ({reason}).")
                    if incoming_id and existing_id and incoming_id != existing_id:
                        duplicate_media_to_delete.append((incoming_id, existing_id))
                    continue

                db_tracks.append(record)
                inserted += 1
                changed = True
                logger.info(f"Appending track '{title}' to database in bulk.")

            if not changed:
                logger.info("bulk_update_database: No database changes needed.")
            else:
                db_data = replace_tracks(db_data, db_tracks, was_dict)
                try:
                    result = upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
                    upload_succeeded = True if result else False
                    logger.info(f"Successfully bulk updated database.json. Inserted: {inserted}, merged: {merged}.")
                    if upload_succeeded:
                        sync_database_lite(db_data, parent_folder_id)
                except Exception as e:
                    logger.error(f"Failed to upload bulk update to Drive: {e}", exc_info=True)
                    upload_succeeded = False

        for incoming_id, existing_id in duplicate_media_to_delete:
            try:
                delete_file(incoming_id)
                logger.info(f"Deleted duplicate uploaded media file {incoming_id} after matching existing track {existing_id}.")
            except Exception as cleanup_err:
                logger.warning(f"Could not delete duplicate uploaded media file {incoming_id}: {cleanup_err}")

        return upload_succeeded

    except Exception as e:
        logger.error(f"Failed to bulk update database: {e}", exc_info=True)
        return False
def normalize_database():
    """
    Downloads database.json, normalizes all track fields according to the schema rules,
    creates a backup on Drive, and uploads the normalized database back to Drive.
    Returns a tuple: (tracks_changed, total_tracks)
    """
    try:
        db_file_id, parent_folder_id = get_db_file_id()
        if not db_file_id:
            raise ValueError("database.json file ID not found.")
            
        logger.info(f"normalize_database: Downloading database.json (ID: {db_file_id})...")
        db_data = download_json(db_file_id)
        
        is_dict = False
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
            is_dict = True
        else:
            raise ValueError("Database format is neither a list nor a dictionary containing 'tracks'.")
            
        tracks_changed = 0
        current_time = datetime.datetime.utcnow().isoformat() + 'Z'
        
        for track in tracks:
            changed = False
            
            if 'album_art' not in track:
                track['album_art'] = None
                changed = True
                
            if 'albumArt' not in track:
                track['albumArt'] = track['album_art']
                changed = True
                
            if 'duration' not in track or track['duration'] is None:
                track['duration'] = "--:--"
                changed = True
                
            if 'durationSeconds' not in track:
                track['durationSeconds'] = None
                changed = True
                
            if 'artist' not in track or track['artist'] is None:
                track['artist'] = "Unknown Artist"
                changed = True
                
            if 'title' not in track or track['title'] is None:
                track['title'] = "Unknown Title"
                changed = True
                
            if 'album' not in track or track['album'] is None:
                track['album'] = "Unknown Album"
                changed = True
                
            if 'genre' not in track or track['genre'] is None:
                track['genre'] = "Unknown"
                changed = True
                
            if 'language' not in track or track['language'] is None:
                track['language'] = "Unknown"
                changed = True
                
            if 'source' not in track or track['source'] is None:
                track['source'] = "unknown"
                changed = True
                
            if 'requestedBy' not in track:
                track['requestedBy'] = None
                changed = True
                
            if 'addedAt' not in track:
                track['addedAt'] = track.get('timestamp') or current_time
                changed = True
                
            if 'updatedAt' not in track:
                track['updatedAt'] = current_time
                changed = True
                
            if 'spotify_id' not in track:
                track['spotify_id'] = None
                changed = True
                
            if 'lyrics' not in track:
                track['lyrics'] = None
                changed = True
                
            if 'syncedLyrics' not in track:
                track['syncedLyrics'] = None
                changed = True
                
            if changed:
                tracks_changed += 1
                
        now = datetime.datetime.utcnow()
        backup_filename = f"database_backup_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json"
        logger.info(f"normalize_database: Creating backup '{backup_filename}' in parent folder {parent_folder_id}...")
        upload_json(None, db_data, backup_filename, parent_id=parent_folder_id)
        
        logger.info("normalize_database: Uploading normalized database.json...")
        if is_dict:
            db_data['tracks'] = tracks
            upload_database_json_locked(db_file_id, db_data, parent_folder_id)
        else:
            upload_database_json_locked(db_file_id, tracks, parent_folder_id)
            
        logger.info(f"normalize_database: Finished. Normalized {tracks_changed} of {len(tracks)} tracks.")
        return tracks_changed, len(tracks)
        
    except Exception as e:
        logger.error(f"normalize_database failed: {e}", exc_info=True)
        raise

def backfill_lyrics_status():
    """
    Downloads database.json, adds the 'lyricsStatus' field (default 'ok') 
    to all tracks that don't have it, and uploads the database back to Drive.
    """
    try:
        db_file_id, parent_folder_id = get_db_file_id()
        if not db_file_id:
            raise ValueError("database.json file ID not found.")
            
        logger.info(f"backfill_lyrics_status: Downloading database.json (ID: {db_file_id})...")
        db_data = download_json(db_file_id)
        
        is_dict = False
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
            is_dict = True
        else:
            raise ValueError("Database format is neither a list nor a dictionary containing 'tracks'.")
            
        tracks_changed = 0
        
        from scraper.metadata_enricher import detect_script_mixing
        for track in tracks:
            old_status = track.get('lyricsStatus')
            lyrics = track.get('lyrics') or ""
            new_status = "needs_review" if detect_script_mixing(lyrics) else "ok"
            if old_status != new_status:
                track['lyricsStatus'] = new_status
                tracks_changed += 1
                
        if tracks_changed == 0:
            logger.info("backfill_lyrics_status: No tracks needed updating.")
            return 0
            
        logger.info(f"backfill_lyrics_status: Uploading updated database.json ({tracks_changed} tracks modified)...")
        if is_dict:
            db_data['tracks'] = tracks
            upload_database_json_locked(db_file_id, db_data, parent_folder_id)
        else:
            upload_database_json_locked(db_file_id, tracks, parent_folder_id)
            
        logger.info(f"backfill_lyrics_status: Finished backfilling {tracks_changed} tracks.")
        return tracks_changed
        
    except Exception as e:
        logger.error(f"backfill_lyrics_status failed: {e}", exc_info=True)
        raise

def audit_database_fields():
    """
    Downloads database.json (read only) and audits track fields for missing values.
    Returns a dictionary with total tracks, missing counts per field, and complete tracks.
    """
    try:
        db_file_id, _ = get_db_file_id()
        if not db_file_id:
            raise ValueError("database.json file ID not found.")
            
        logger.info(f"audit_database_fields: Downloading database.json (ID: {db_file_id})...")
        db_data = download_json(db_file_id)
        
        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict) and 'tracks' in db_data:
            tracks = db_data['tracks']
        else:
            raise ValueError("Database format is neither a list nor a dictionary containing 'tracks'.")
            
        results = {
            "total_tracks": len(tracks),
            "missing_counts": {
                "album_art": 0,
                "duration": 0,
                "durationSeconds": 0,
                "language": 0,
                "genre": 0,
                "album": 0,
                "lyrics": 0,
                "syncedLyrics": 0,
                "source": 0,
                "spotify_id": 0
            },
            "complete_tracks": 0,
            "tracks_with_any_missing_field": 0
        }
        
        for track in tracks:
            missing_any = False
            
            # album_art / albumArt (missing if both are null/empty)
            album_art = track.get('album_art')
            album_art_camel = track.get('albumArt')
            if not album_art and not album_art_camel:
                results["missing_counts"]["album_art"] += 1
                missing_any = True
                
            # duration (missing if "--:--" or null/empty)
            duration = track.get('duration')
            if not duration or duration == "--:--":
                results["missing_counts"]["duration"] += 1
                missing_any = True
                
            # durationSeconds (missing if null)
            if track.get('durationSeconds') is None:
                results["missing_counts"]["durationSeconds"] += 1
                missing_any = True
                
            # language (missing if "Unknown"/"unknown"/null/empty)
            lang = track.get('language')
            if not lang or lang.lower() == "unknown":
                results["missing_counts"]["language"] += 1
                missing_any = True
                
            # genre (missing if "Unknown"/null/empty)
            genre = track.get('genre')
            if not genre or genre == "Unknown":
                results["missing_counts"]["genre"] += 1
                missing_any = True
                
            # album (missing if "Unknown Album"/null/empty)
            album = track.get('album')
            if not album or album == "Unknown Album":
                results["missing_counts"]["album"] += 1
                missing_any = True
                
            # lyrics (missing if null/empty)
            if not track.get('lyrics'):
                results["missing_counts"]["lyrics"] += 1
                missing_any = True
                
            # syncedLyrics (missing if null/empty)
            if not track.get('syncedLyrics'):
                results["missing_counts"]["syncedLyrics"] += 1
                missing_any = True
                
            # source (missing if "unknown"/null/empty)
            source = track.get('source')
            if not source or source == "unknown":
                results["missing_counts"]["source"] += 1
                missing_any = True
                
            # spotify_id (missing if null/empty) - don't count towards missing_any
            if not track.get('spotify_id'):
                results["missing_counts"]["spotify_id"] += 1
                
            if missing_any:
                results["tracks_with_any_missing_field"] += 1
            else:
                results["complete_tracks"] += 1
                
        logger.info(f"audit_database_fields: Audited {len(tracks)} tracks. {results['tracks_with_any_missing_field']} tracks have missing fields.")
        return results
        
    except Exception as e:
        logger.error(f"audit_database_fields failed: {e}", exc_info=True)
        raise

def find_orphan_media_files():
    """
    Compares Drive media files against database.json driveFileId/id values.
    Returns a read-only report of media files that are not referenced by the DB.
    """
    db_file_id, _ = get_db_file_id()
    if not db_file_id:
        raise ValueError("database.json file ID not found.")

    media_folder_id = os.environ.get('GDRIVE_MEDIA_FOLDER_ID')
    if not media_folder_id:
        raise ValueError("GDRIVE_MEDIA_FOLDER_ID environment variable is not set.")

    db_data = download_json(db_file_id)
    tracks, _ = extract_tracks(db_data)
    referenced_ids = {drive_id(track) for track in tracks if drive_id(track)}
    media_files = list_files(media_folder_id)
    orphans = [file_info for file_info in media_files if file_info.get("id") not in referenced_ids]

    return {
        "database_tracks": len(tracks),
        "media_files": len(media_files),
        "orphan_count": len(orphans),
        "orphans": orphans,
    }

def cleanup_orphan_media_files(dry_run=True):
    """
    Deletes media files not referenced by database.json when dry_run is False.
    Defaults to dry-run to prevent accidental destructive cleanup.
    """
    report = find_orphan_media_files()
    if dry_run:
        report["deleted_count"] = 0
        report["dry_run"] = True
        return report

    deleted = []
    errors = []
    for file_info in report["orphans"]:
        file_id = file_info.get("id")
        try:
            delete_file(file_id)
            deleted.append(file_info)
        except Exception as e:
            errors.append({"id": file_id, "name": file_info.get("name"), "error": str(e)})

    report["dry_run"] = False
    report["deleted_count"] = len(deleted)
    report["deleted"] = deleted
    report["errors"] = errors
    return report

def list_database_backups():
    """
    Lists all database_backup_*.json files in the database folder.
    Returns a list of dictionaries with 'id', 'name', and 'createdTime'.
    """
    try:
        _, parent_folder_id = get_db_file_id()
        files = list_files(parent_folder_id)
        backups = []
        for f in files:
            if 'backup' in f.get('name', '').lower() and f.get('name', '').endswith('.json'):
                backups.append(f)
        
        # Sort by createdTime descending
        backups.sort(key=lambda x: x.get('createdTime', ''), reverse=True)
        return backups
    except Exception as e:
        logger.error(f"list_database_backups failed: {e}", exc_info=True)
        return []

def restore_database_backup(backup_file_id):
    """
    Restores a specific backup to replace database.json.
    """
    try:
        db_file_id, parent_folder_id = get_db_file_id()
        if not db_file_id:
            logger.error("restore_database_backup: database.json not found on Drive.")
            return False
            
        logger.info(f"restore_database_backup: Downloading backup from ID {backup_file_id}...")
        backup_data = download_json(backup_file_id)
        
        if not backup_data:
            logger.error("restore_database_backup: Backup data is empty or failed to download.")
            return False
            
        logger.info(f"restore_database_backup: Uploading restored data to database.json (ID {db_file_id})...")
        upload_database_json_locked(db_file_id, backup_data, parent_folder_id)
        logger.info("restore_database_backup: Successfully restored database.json from backup.")
        return True
    except Exception as e:
        logger.error(f"restore_database_backup failed: {e}", exc_info=True)
        return False


if __name__ == '__main__':
    # Run a test upload of a small dummy file to the media folder
    import tempfile
    from dashboard.drive_client import get_oauth_drive_service

    print("Running Drive OAuth integration test...")
    
    # 1. Verify Drive Service connection and check storage quota
    try:
        service = get_oauth_drive_service()
        about = service.about().get(fields="storageQuota").execute()
        quota = about.get('storageQuota', {})
        limit = int(quota.get('limit', 0))
        usage = int(quota.get('usage', 0))
        limit_tb = limit / (1024**4)
        usage_gb = usage / (1024**3)
        print(f"Drive Storage Quota: Limit = {limit_tb:.2f} TB, Usage = {usage_gb:.2f} GB")
    except Exception as e:
        print(f"Failed to check storage quota: {e}")
        
    # 2. Upload a small dummy file
    temp_fd, temp_path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(temp_fd, 'w') as f:
            f.write("Google Drive API OAuth 2.0 Test Upload\n")
            
        filename = "test_oauth_dummy.txt"
        print(f"Uploading temporary file to Google Drive...")
        
        # We upload the dummy file using upload_track
        drive_file_id = upload_track(temp_path, metadata={'title': 'Test OAuth Dummy'})
        print(f"Success! Dummy file uploaded. Drive File ID: {drive_file_id}")
    except Exception as e:
        print(f"Upload failed: {e}")
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

