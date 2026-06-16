import os
import sys
import datetime
import logging
import requests
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

from dashboard.drive_client import upload_media, download_json, upload_json, list_files


def fetch_album_art(track_name, artist_name):
    """
    Queries the iTunes Search API for a matching song and returns a high-resolution
    album art URL (600x600). Returns None if no results or the request fails.
    """
    try:
        query = f"{artist_name} {track_name}"
        url = "https://itunes.apple.com/search"
        params = {"term": query, "entity": "song", "limit": 5}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        results = response.json().get("results", [])
        if not results:
            logger.debug(f"fetch_album_art: No iTunes results for '{query}'")
            return None
        artwork = results[0].get("artworkUrl100")
        if artwork:
            artwork = artwork.replace("100x100bb", "600x600bb")
        return artwork
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
        db_file_id, parent_folder_id = get_db_file_id()
        
        db_data = []
        if db_file_id:
            logger.info(f"Downloading existing database.json (ID: {db_file_id}) from Drive...")
            db_data = download_json(db_file_id)
        else:
            logger.info("database.json not found on Drive. Creating a fresh index list.")

        # Ensure database structure is a list
        if not isinstance(db_data, list):
            if isinstance(db_data, dict) and 'tracks' in db_data:
                db_data = db_data['tracks']
            else:
                db_data = []
                
        # Build new track entry
        timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
        title = metadata.get('title', 'Unknown Title')
        artist = metadata.get('artist', 'Unknown Artist')
        # Fetch album art from iTunes unless the caller already supplied one
        resolved_art = metadata.get('album_art') or fetch_album_art(title, artist)
        new_track = {
            "id": drive_file_id,
            "driveFileId": drive_file_id,
            "title": title,
            "artist": artist,
            "album": metadata.get('album', 'Unknown Album'),
            "genre": metadata.get('genre', 'Unknown'),
            "duration": metadata.get('duration', '--:--'),
            "spotify_id": metadata.get('spotify_id'),
            "album_art": resolved_art,
            "albumArt": resolved_art,
            "source": metadata.get('source', 'unknown'),
            "requestedBy": metadata.get('requestedBy'),
            "timestamp": timestamp
        }
        
        db_data.append(new_track)
        logger.info(f"Appending track '{new_track['title']}' to database.")
        
        result = upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
        logger.info("Successfully updated database.json on Google Drive.")
        return result
        
    except Exception as e:
        logger.error(f"Failed to update database: {e}", exc_info=True)
        raise

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
            upload_json(db_file_id, db_data, 'database.json', parent_id=parent_folder_id)
        else:
            upload_json(db_file_id, tracks, 'database.json', parent_id=parent_folder_id)
            
        logger.info(f"normalize_database: Finished. Normalized {tracks_changed} of {len(tracks)} tracks.")
        return tracks_changed, len(tracks)
        
    except Exception as e:
        logger.error(f"normalize_database failed: {e}", exc_info=True)
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
        upload_json(db_file_id, backup_data, 'database.json', parent_id=parent_folder_id)
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

