import os
import sys
import subprocess
import logging
import requests
from flask import Flask, render_template, jsonify, request, stream_with_context, Response
from dotenv import load_dotenv

# Load env variables from .env in project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=env_path)

# Import drive client functions
# Since dashboard/ is a package or directory, we can import directly or adjust sys.path
sys.path.append(project_root)
from dashboard.drive_client import list_files, download_json, upload_json, delete_file, get_storage_quota, get_file_metadata, get_oauth_drive_service, get_valid_access_token, refresh_and_get_access_token
from scraper.state_manager import load_config, save_config, load_state, save_state, is_duplicate
from scraper.spotify_charts import get_track_by_spotify_url
from scraper.downloader import download_track
from scraper.drive_uploader import upload_track, update_database, normalize_database, audit_database_fields
from scraper.metadata_enricher import enrich_track_metadata
from scraper.main import run_full_enrichment_pass, run_complete_backfill
from scraper.playlist_importer import get_playlist_preview, start_playlist_import, get_playlist_status, run_playlist_import
import ctypes
import threading
import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, 
            template_folder=os.path.join(project_root, 'dashboard', 'templates'),
            static_folder=os.path.join(project_root, 'dashboard', 'static'))

db_file_id_cache = None

# Global background tasks state tracking
background_tasks = {
    "scraper": {"status": "idle", "started_at": None},
    "playlist_import": {"status": "idle", "started_at": None, "playlist_id": None},
    "backfill": {"status": "idle", "started_at": None, "type": None},
    "single_add": {"status": "idle", "started_at": None}
}

app_import_tasks = {}

def is_scraper_running():
    return background_tasks["scraper"]["status"] == "running"

def get_db_file_id():
    """
    Retrieves the database.json file ID from env, or lists files to find/initialize it.
    """
    global db_file_id_cache
    if db_file_id_cache:
        return db_file_id_cache

    db_file_id = os.environ.get('GDRIVE_DB_FILE_ID')
    if db_file_id:
        db_file_id_cache = db_file_id
        return db_file_id

    folder_id = os.environ.get('GDRIVE_FOLDER_ID')
    logger.info(f"GDRIVE_DB_FILE_ID not set. Searching for 'database.json' in folder: {folder_id}")
    
    files = []
    try:
        files = list_files(folder_id)
    except Exception as e:
        logger.error(f"Failed to list files in folder {folder_id}: {e}")
        return None

    db_folder_id = None
    for f in files:
        if f.get('name') == 'database.json':
            db_file_id = f.get('id')
            logger.info(f"Found 'database.json' with ID: {db_file_id}")
            break
        elif f.get('name') == 'database' and f.get('mimeType') == 'application/vnd.google-apps.folder':
            db_folder_id = f.get('id')

    # Check database subfolder if database.json not found in main folder
    if not db_file_id and db_folder_id:
        logger.info(f"Searching for 'database.json' inside 'database' subfolder: {db_folder_id}")
        try:
            sub_files = list_files(db_folder_id)
            for sf in sub_files:
                if sf.get('name') == 'database.json':
                    db_file_id = sf.get('id')
                    logger.info(f"Found 'database.json' inside subfolder with ID: {db_file_id}")
                    break
        except Exception as e:
            logger.error(f"Failed to list files in subfolder {db_folder_id}: {e}")

    # If still not found, initialize a new database.json on Google Drive!
    if not db_file_id:
        target_folder = db_folder_id if db_folder_id else folder_id
        logger.info(f"database.json not found. Initializing new database.json in folder ID: {target_folder}")
        try:
            new_file = upload_json(None, [], 'database.json', parent_id=target_folder)
            db_file_id = new_file.get('id')
            logger.info(f"Created new database.json on Google Drive with ID: {db_file_id}")
        except Exception as e:
            logger.critical(
                f"Failed to auto-create database.json in Google Drive due to storage/quota restrictions: {e}. "
                "CRITICAL: If using a standard service account without a Shared Drive, please pre-create an empty database.json file "
                "in your Google Drive folder manually, share the folder with the service account email, and set its ID in the GDRIVE_DB_FILE_ID "
                "environment variable in your .env file."
            )
            return None

    db_file_id_cache = db_file_id
    return db_file_id

@app.route('/')
def index():
    """
    GET / — serves the main dashboard page
    """
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error rendering index.html: {e}", exc_info=True)
        return f"Error loading page: {str(e)}", 500

@app.route('/api/tracks', methods=['GET'])
def get_tracks():
    """
    GET /api/tracks — fetches and returns the contents of database.json from Drive.
    Also dynamically maps track file sizes from Drive.
    """
    try:
        db_file_id = get_db_file_id()
        if not db_file_id:
            return jsonify([])
        data = download_json(db_file_id)
        
        # Ensure it is a list
        tracks = []
        if isinstance(data, list):
            tracks = data
        elif isinstance(data, dict):
            if 'tracks' in data and isinstance(data['tracks'], list):
                tracks = data['tracks']
            else:
                tracks = list(data.values())
        
        # Query media folder files to map sizes dynamically
        media_folder_id = os.environ.get('GDRIVE_MEDIA_FOLDER_ID')
        media_files = []
        if media_folder_id:
            try:
                media_files = list_files(media_folder_id)
            except Exception as e:
                logger.error(f"Failed to list media files for size mapping: {e}")
                
        # Create map of file ID to size
        size_map = {}
        for f in media_files:
            fid = f.get('id')
            if fid:
                size_map[fid] = f.get('size')
                
        # Inject size into each track
        for track in tracks:
            fid = track.get('driveFileId') or track.get('id')
            track['size'] = size_map.get(fid)
            
        return jsonify(tracks)
    except Exception as e:
        logger.error(f"Error in GET /api/tracks: {e}", exc_info=True)
        return jsonify([])

@app.route('/api/storage', methods=['GET'])
def get_storage():
    """
    GET /api/storage — returns total tracks, media size, quota, and database update timestamp.
    """
    try:
        # 1. Total tracks from tracks list in database.json
        db_file_id = get_db_file_id()
        total_tracks = 0
        album_art_count = 0
        last_updated = None
        if db_file_id:
            try:
                db_data = download_json(db_file_id)
                tracks = []
                if isinstance(db_data, list):
                    tracks = db_data
                elif isinstance(db_data, dict):
                    if 'tracks' in db_data and isinstance(db_data['tracks'], list):
                        tracks = db_data['tracks']
                    else:
                        tracks = list(db_data.values())
                total_tracks = len(tracks)
                album_art_count = sum(1 for t in tracks if t.get('album_art') is not None)
            except Exception as e:
                logger.error(f"Failed to read database.json size: {e}")
            
            # Get modifiedTime from Drive file metadata
            try:
                meta = get_file_metadata(db_file_id)
                last_updated = meta.get('modifiedTime')
            except Exception as e:
                logger.error(f"Failed to get database.json metadata: {e}")
                
        # 2. Sum of all media file sizes from GDRIVE_MEDIA_FOLDER_ID
        media_folder_id = os.environ.get('GDRIVE_MEDIA_FOLDER_ID')
        media_size = 0
        if media_folder_id:
            try:
                media_files = list_files(media_folder_id)
                media_size = sum(int(f.get('size', 0)) for f in media_files if f.get('size'))
            except Exception as e:
                logger.error(f"Failed to list media folder sizes: {e}")
                
        # 3. Google Drive Storage Quota limit and usage using about.get
        drive_limit = 15.0 * 1024 * 1024 * 1024  # Default fallback 15 GB
        drive_usage = 0
        try:
            quota = get_storage_quota()
            drive_limit = int(quota.get('limit', drive_limit))
            drive_usage = int(quota.get('usage', 0))
        except Exception as e:
            logger.error(f"Failed to get storage quota: {e}")
            
        album_art_storage_bytes = album_art_count * 150 * 1024
        return jsonify({
            "total_tracks": total_tracks,
            "media_size_bytes": media_size,
            "media_storage_bytes": media_size,
            "drive_limit_bytes": drive_limit,
            "drive_usage_bytes": drive_usage,
            "last_updated": last_updated,
            "album_art_count": album_art_count,
            "album_art_storage_bytes": album_art_storage_bytes
        })
    except Exception as e:
        logger.error(f"Error in GET /api/storage: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/delete/<file_id>', methods=['POST'])
def delete_track(file_id):
    """
    POST /api/delete/<file_id> — deletes a track from Drive and updates database.json
    """
    try:
        db_file_id = get_db_file_id()
        
        # 1. Download database.json
        db_data = download_json(db_file_id)
        
        # 2. Update database structure (assuming it's a list of tracks or a dict)
        updated = False
        if isinstance(db_data, list):
            new_db_data = []
            for track in db_data:
                if track.get('id') == file_id or track.get('file_id') == file_id:
                    updated = True
                else:
                    new_db_data.append(track)
            db_data = new_db_data
        elif isinstance(db_data, dict):
            if 'tracks' in db_data and isinstance(db_data['tracks'], list):
                db_data['tracks'] = [t for t in db_data['tracks'] if t.get('id') != file_id and t.get('file_id') != file_id]
                updated = True
            elif file_id in db_data:
                del db_data[file_id]
                updated = True
        
        # 3. Save updated database.json back to Drive
        upload_json(db_file_id, db_data, 'database.json')
        
        # 4. Delete media file from Drive
        delete_file(file_id)
        
        return jsonify({"status": "success", "message": f"Track {file_id} deleted successfully."})
    except Exception as e:
        logger.error(f"Error in POST /api/delete/{file_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Error in POST /api/delete/{file_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/scrape', methods=['POST'])
def trigger_scrape():
    """
    POST /api/scrape — triggers the scraper in the background and saves its PID
    """
    if background_tasks["scraper"]["status"] == "running":
        return jsonify({"status": "success", "message": "Scraper is already running."})

    def run_scraper_task():
        background_tasks["scraper"]["status"] = "running"
        background_tasks["scraper"]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        try:
            scraper_script = os.path.join(project_root, 'scraper', 'main.py')
            log_path = os.path.join(project_root, 'scraper.log')
            
            with open(log_path, 'a', encoding='utf-8') as log_file:
                process = subprocess.Popen(
                    [sys.executable, scraper_script],
                    stdout=log_file,
                    stderr=log_file,
                    cwd=project_root
                )
                
                # Save PID to temp file
                pid_file = os.path.join(project_root, 'temp', 'scraper.pid')
                os.makedirs(os.path.dirname(pid_file), exist_ok=True)
                with open(pid_file, 'w') as f:
                    f.write(str(process.pid))
                
                process.wait()
        except Exception as err:
            logger.error(f"Error executing scraper thread: {err}", exc_info=True)
        finally:
            background_tasks["scraper"]["status"] = "idle"

    thread = threading.Thread(target=run_scraper_task)
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "success", "message": "Scraper started in the background."})

@app.route('/api/scraper/status', methods=['GET'])
def scraper_status():
    """
    GET /api/scraper/status — checks if background scraper is currently running
    """
    running = is_scraper_running()
    return jsonify({"status": "running" if running else "idle"})

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """
    GET /api/logs — returns the last 50 lines of scraper.log from the current session
    """
    try:
        log_path = os.path.join(project_root, 'scraper.log')
        if not os.path.exists(log_path):
            return jsonify({"logs": []})
            
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            
        cleaned_lines = [line.rstrip('\r\n') for line in lines]
        
        # Find the last session header index
        header_idx = -1
        for i, line in enumerate(cleaned_lines):
            line_upper = line.upper()
            if "NEW SESSION" in line_upper or "NEW SCRAPER SESSION" in line_upper or "NEW PLAYLIST IMPORT SESSION" in line_upper:
                header_idx = i
                
        if header_idx != -1:
            current_session_lines = cleaned_lines[header_idx:]
        else:
            current_session_lines = cleaned_lines
            
        last_50 = current_session_lines[-50:]
        return jsonify({"logs": last_50})
    except Exception as e:
        logger.error(f"Error in GET /api/logs: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/config', methods=['GET'])
def get_config():
    """
    GET /api/config — downloads and returns scraper_config.json from Drive, 
    along with state metadata for dashboard display.
    """
    try:
        config = load_config()
        state = load_state()
        
        # Merge state info into config response structure under a 'state' key
        config['state'] = {
            "cursor": state.get("cursor", 0),
            "pool_size": len(state.get("pool", []))
        }
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error in GET /api/config: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/config', methods=['POST'])
def post_config():
    """
    POST /api/config — receives updated config JSON and saves it to Drive using save_config
    """
    try:
        new_config = request.json
        if not new_config:
            return jsonify({"error": "No JSON payload provided"}), 400
            
        # Clean config from state key if it was passed by client
        new_config.pop('state', None)
        
        # Validate data types
        if 'songs_per_run' in new_config:
            new_config['songs_per_run'] = int(new_config['songs_per_run'])
        if 'auto_refresh_days' in new_config:
            new_config['auto_refresh_days'] = int(new_config['auto_refresh_days'])
            
        save_config(new_config)
        
        # Reset pool_date in state to force a pool refresh on the next run
        try:
            state = load_state()
            state["pool_date"] = None
            save_state(state)
            logger.info("post_config: Reset pool_date in scraper_state.json to null to force rebuild on next run.")
        except Exception as state_err:
            logger.error(f"post_config: Failed to reset pool_date in scraper_state.json: {state_err}")
            
        return jsonify({"status": "success", "message": "Scraper configuration saved successfully and pool reset."})
    except Exception as e:
        logger.error(f"Error in POST /api/config: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/pool/refresh', methods=['POST'])
def refresh_pool():
    """
    POST /api/pool/refresh — sets pool_date to null in scraper_state.json to force a pool refresh on next scraper run
    """
    try:
        state = load_state()
        state["pool_date"] = None
        save_state(state)
        return jsonify({"status": "success", "message": "Pool expiration state set. A fresh song pool will be built on the next run."})
    except Exception as e:
        logger.error(f"Error in POST /api/pool/refresh: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/library/normalize', methods=['POST'])
def normalize_library():
    """
    POST /api/library/normalize — Normalizes all database tracks fields with defaults and backs up the database.
    """
    try:
        tracks_changed, total_tracks = normalize_database()
        return jsonify({
            "status": "success",
            "message": f"Database normalized successfully. {tracks_changed} of {total_tracks} tracks were updated.",
            "tracks_changed": tracks_changed,
            "total_tracks": total_tracks
        })
    except Exception as e:
        logger.error(f"Error in POST /api/library/normalize: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/library/audit', methods=['GET'])
def audit_library():
    """
    GET /api/library/audit — Audits all database tracks fields with defaults without modifying the database.
    """
    try:
        results = audit_database_fields()
        return jsonify({
            "status": "success",
            "data": results
        })
    except Exception as e:
        logger.error(f"Error in GET /api/library/audit: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/backfill/complete', methods=['POST'])
def complete_backfill():
    """
    POST /api/backfill/complete - Runs the complete backfill engine on all tracks in the background.
    """
    try:
        logger.info("Starting complete backfill engine in a background thread...")
        thread = threading.Thread(target=run_complete_backfill)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "success",
            "message": "Complete backfill started in the background."
        })
    except Exception as e:
        logger.error(f"Error starting complete backfill: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/library/backup', methods=['POST'])
def backup_library():
    """
    POST /api/library/backup — Creates a backup of database.json in the Drive database folder.
    """
    try:
        import datetime
        from scraper.drive_uploader import get_db_file_id
        db_file_id, parent_folder_id = get_db_file_id()
        if not db_file_id:
            return jsonify({"error": "database.json not found"}), 404
            
        db_data = download_json(db_file_id)
        now = datetime.datetime.now()
        backup_filename = f"database_backup_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json"
        upload_json(None, db_data, backup_filename, parent_id=parent_folder_id)
        
        return jsonify({
            "status": "success",
            "message": f"Backup created successfully: {backup_filename}"
        })
    except Exception as e:
        logger.error(f"Error in POST /api/library/backup: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500



@app.route('/api/preview-song', methods=['GET'])
def preview_song():
    """
    GET /api/preview-song?url=...
    Calls get_track_by_spotify_url and returns metadata without downloading anything.
    """
    try:
        url = request.args.get('url')
        if not url:
            return jsonify({"error": "Missing 'url' query parameter"}), 400
        
        metadata = get_track_by_spotify_url(url)
        return jsonify(metadata)
    except ValueError as ve:
        logger.warning(f"Validation error in GET /api/preview-song: {ve}")
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        logger.error(f"Error in GET /api/preview-song: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/add-song', methods=['POST'])
def add_song():
    """
    POST /api/add-song
    Receives spotify_url in json body, and starts a background thread to process it.
    """
    body = request.json or {}
    spotify_url = body.get('spotify_url')
    if not spotify_url:
        return jsonify({"error": "Missing 'spotify_url' in request body"}), 400

    if background_tasks["single_add"]["status"] == "running":
        return jsonify({"error": "A single track import is already in progress"}), 409

    def run_add_song_task():
        background_tasks["single_add"]["status"] = "running"
        background_tasks["single_add"]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        temp_file_path = None
        try:
            # 1. Fetch metadata
            metadata = get_track_by_spotify_url(spotify_url)
            title = metadata["title"]
            artist = metadata["artist"]
            spotify_id = metadata["spotify_id"]
            genre = metadata["genre"]
            
            background_tasks["single_add"]["track_name"] = f"{title} - {artist}"
            
            # No manual album_art or language fetching here; handled by enricher
            state = load_state()
            existing_tracks = []
            db_file_id = get_db_file_id()
            if db_file_id:
                try:
                    existing_data = download_json(db_file_id)
                    if isinstance(existing_data, list):
                        existing_tracks = existing_data
                    elif isinstance(existing_data, dict) and 'tracks' in existing_data:
                        existing_tracks = existing_data['tracks']
                except Exception as e:
                    logger.warning(f"Could not retrieve existing tracks for duplicate check: {e}")
                    
            # Format track search object for duplicate check
            track_to_check = {
                "title": title,
                "artist": artist,
                "spotify_id": spotify_id
            }
            
            if is_duplicate(track_to_check, state, existing_tracks):
                logger.warning(f"Song already exists in database: {title} by {artist}")
                return
                
            # 3. Create temp download folder
            temp_dir = os.environ.get('TEMP_DIR')
            if not temp_dir:
                temp_dir = os.path.join(project_root, 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            
            # 4. Download track
            logger.info(f"Downloading track '{title}' by '{artist}' to {temp_dir}")
            temp_file_path = download_track(title, artist, temp_dir)
            
            # 5. Upload track
            logger.info(f"Uploading file '{temp_file_path}' to Google Drive...")
            drive_file_id = upload_track(temp_file_path)
            
            # 6. Enrich metadata
            enriched = enrich_track_metadata(title, artist, local_file_path=temp_file_path, source="dashboard_single")
            
            # Add to metadata dict
            db_metadata = {
                "title": title,
                "artist": artist,
                "album": enriched.get("album", "Single"),
                "genre": enriched.get("genre", genre),
                "duration": enriched.get("duration", "--:--"),
                "durationSeconds": enriched.get("durationSeconds"),
                "spotify_id": spotify_id,
                "album_art": enriched.get("album_art"),
                "language": enriched.get("language", "unknown"),
                "source": "dashboard_single",
                "lyrics": enriched.get("lyrics"),
                "syncedLyrics": enriched.get("syncedLyrics")
            }
            
            # 7. Update database on Drive
            update_database(drive_file_id, db_metadata)
            
            # Update scraper state on Drive
            try:
                current_state = load_state()
                if spotify_id is not None:
                    current_state.setdefault("downloaded_ids", []).append(spotify_id)
                current_state.setdefault("downloaded_titles", []).append(f"{title} {artist}")
                save_state(current_state)
            except Exception as state_err:
                logger.error(f"Failed to update scraper state: {state_err}", exc_info=True)
        except Exception as e:
            logger.error(f"Error in background add_song: {e}", exc_info=True)
        finally:
            # Cleanup temp file
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logger.info(f"Cleaned up temp audio file: {temp_file_path}")
                except Exception as cleanup_err:
                    logger.warning(f"Could not remove local temp file {temp_file_path}: {cleanup_err}")
            background_tasks["single_add"]["status"] = "idle"

    thread = threading.Thread(target=run_add_song_task)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Song download started in background."})


@app.route('/stream/<drive_file_id>', methods=['GET'])
def stream_track(drive_file_id):
    """
    GET /stream/<drive_file_id>
    Streams a publicly shared Google Drive file, supporting HTTP Range requests.
    """
    import re
    
    # Try newer Google Drive download endpoint first
    primary_url = f"https://drive.usercontent.google.com/download?id={drive_file_id}&export=download&authuser=0&confirm=t"
    
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    
    # Forward the incoming Range header if present
    range_header = request.headers.get('Range')
    if range_header:
        headers['Range'] = range_header
        
    try:
        res = requests.get(primary_url, headers=headers, stream=True, timeout=30, allow_redirects=True)
        used_url_desc = "drive.usercontent.google.com"

        content_type = res.headers.get('Content-Type', '').lower()
        
        # If it returns HTML (confirmation page), fallback
        if res.status_code == 200 and 'text/html' in content_type:
            logger.info(f"Primary URL returned HTML confirmation page for {drive_file_id}. Attempting fallback...")
            html_content = res.text
            res.close()
            
            download_url = None
            # Look for typical Google Drive confirmation link or form action
            match = re.search(r'href="(/uc\?export=download(?:&amp;|&)confirm=[^"]+)"', html_content)
            if match:
                download_url = "https://drive.google.com" + match.group(1).replace('&amp;', '&')
            else:
                match = re.search(r'action="([^"]+)"', html_content)
                if match and 'export=download' in match.group(1):
                    download_url = match.group(1).replace('&amp;', '&')
                    if download_url.startswith('/'):
                        download_url = "https://drive.google.com" + download_url

            if download_url:
                logger.info(f"Extracted real download URL from HTML for {drive_file_id}")
                used_url_desc = "Extracted confirmation URL"
                res = requests.get(download_url, headers=headers, stream=True, timeout=30, allow_redirects=True)
            else:
                # Fallback to googleapis approach if we have an API key
                api_key = os.environ.get('GOOGLE_API_KEY')
                if api_key:
                    logger.info(f"Using googleapis fallback with API key for {drive_file_id}")
                    api_url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}?alt=media&key={api_key}"
                    used_url_desc = "googleapis API"
                    res = requests.get(api_url, headers=headers, stream=True, timeout=30, allow_redirects=True)
                else:
                    logger.error(f"Could not extract download URL and no GOOGLE_API_KEY available for {drive_file_id}")
                    return jsonify({"error": "Failed to bypass Google Drive confirmation page and no API key available"}), 500

        # If Drive returns any error, return the same status code
        if res.status_code >= 400:
            logger.error(f"Google Drive error for file {drive_file_id} via {used_url_desc}: Status {res.status_code}")
            return Response(res.content, status=res.status_code, headers={'Content-Type': res.headers.get('Content-Type', 'application/json')})
            
        logger.info(f"Successfully established stream for {drive_file_id} via {used_url_desc}")
            
        # Prepare headers to forward
        response_headers = {
            'Content-Type': res.headers.get('Content-Type') or 'audio/ogg',
            'Accept-Ranges': 'bytes'
        }
        if 'Content-Range' in res.headers:
            response_headers['Content-Range'] = res.headers['Content-Range']
        if 'Content-Length' in res.headers:
            response_headers['Content-Length'] = res.headers['Content-Length']
            
        def generate():
            try:
                for chunk in res.iter_content(chunk_size=40960):  # 40 KB chunk size
                    if chunk:
                        yield chunk
            finally:
                res.close()
                
        status_code = 206 if range_header else 200
        return Response(
            stream_with_context(generate()),
            status=status_code,
            headers=response_headers
        )
    except Exception as e:
        logger.error(f"Error occurred while streaming file {drive_file_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/playlist/preview', methods=['POST'])
def playlist_preview():
    try:
        url = request.json.get('url')
        if not url:
            return jsonify({"error": "Missing url"}), 400
        preview = get_playlist_preview(url)
        return jsonify(preview)
    except Exception as e:
        logger.error(f"Error in preview: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlist/start', methods=['POST'])
def playlist_start():
    try:
        url = request.json.get('url')
        if not url:
            return jsonify({"error": "Missing url"}), 400
        playlist_id = start_playlist_import(url, imported_via="dashboard")
        
        # Update background tasks dict
        background_tasks["playlist_import"]["status"] = "running"
        background_tasks["playlist_import"]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        background_tasks["playlist_import"]["playlist_id"] = playlist_id
        
        # Start background thread with a wrapper to capture exit status
        def run_playlist_import_wrapper():
            try:
                run_playlist_import(playlist_id)
            except Exception as e:
                logger.error(f"Error in background playlist import: {e}", exc_info=True)
            finally:
                from scraper.playlist_importer import active_imports
                state = active_imports.get(playlist_id)
                if state and state.get("status") == "cancelled":
                    background_tasks["playlist_import"]["status"] = "cancelled"
                else:
                    background_tasks["playlist_import"]["status"] = "completed"

        thread = threading.Thread(target=run_playlist_import_wrapper)
        thread.daemon = True
        thread.start()
        
        # Append clear session marker to scraper.log
        log_path = os.path.join(project_root, 'scraper.log')
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n============================================================\nNEW SESSION: PLAYLIST IMPORT STARTED ({playlist_id})\n============================================================\n")
        except Exception as log_err:
            logger.warning(f"Could not append session marker to scraper.log: {log_err}")
            
        return jsonify({"status": "success", "playlist_id": playlist_id})
    except Exception as e:
        logger.error(f"Error in start: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlist/status', methods=['GET'])
def playlist_status():
    try:
        playlist_id = request.args.get('playlist_id')
        if not playlist_id:
            return jsonify({"error": "Missing playlist_id"}), 400
        status = get_playlist_status(playlist_id)
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error in status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlists', methods=['GET'])
def get_playlists():
    try:
        from scraper.playlist_manager import get_all_playlists
        playlists = get_all_playlists()
        return jsonify(playlists)
    except Exception as e:
        logger.error(f"Error in GET /api/playlists: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlists/<playlist_id>', methods=['GET'])
def get_single_playlist(playlist_id):
    try:
        from scraper.playlist_manager import get_playlist
        playlist = get_playlist(playlist_id)
        if not playlist:
            return jsonify({"error": "Playlist not found"}), 404
        return jsonify(playlist)
    except Exception as e:
        logger.error(f"Error in GET /api/playlists/{playlist_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/artists', methods=['GET'])
def get_artists():
    try:
        from scraper.artist_manager import get_all_artists
        artists = get_all_artists()
        return jsonify(artists)
    except Exception as e:
        logger.error(f"Error in GET /api/artists: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/artists/search', methods=['GET'])
def search_artists_route():
    try:
        query = request.args.get('q', '')
        from scraper.artist_manager import search_artists
        artists = search_artists(query)
        return jsonify(artists)
    except Exception as e:
        logger.error(f"Error in GET /api/artists/search: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

import urllib.parse
@app.route('/api/artists/<artist_name>', methods=['GET'])
def get_single_artist(artist_name):
    try:
        artist_name = urllib.parse.unquote(artist_name)
        from scraper.artist_manager import get_artist_tracks
        tracks = get_artist_tracks(artist_name)
        return jsonify(tracks)
    except Exception as e:
        logger.error(f"Error in GET /api/artists/{artist_name}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlist/cancel', methods=['POST'])
def playlist_cancel():
    try:
        playlist_id = request.json.get('playlist_id')
        if not playlist_id:
            return jsonify({"error": "Missing playlist_id"}), 400
            
        from dashboard.drive_client import search_file_by_name
        from scraper.drive_uploader import get_db_file_id
        db_file_id, parent_id = get_db_file_id()
        file_id = search_file_by_name(f"playlist_import_state_{playlist_id}.json", parent_id)
        if file_id:
            state = download_json(file_id)
            state["status"] = "cancelled"
            upload_json(file_id, state, f"playlist_import_state_{playlist_id}.json", parent_id=parent_id)
            
            # Update active_imports and background_tasks immediately
            from scraper.playlist_importer import active_imports
            if playlist_id in active_imports:
                active_imports[playlist_id]["status"] = "cancelled"
            background_tasks["playlist_import"]["status"] = "cancelled"
            
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Error in cancel: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlist/logs', methods=['GET'])
def get_playlist_logs():
    try:
        log_path = os.path.join(project_root, 'scraper.log')
        if not os.path.exists(log_path):
            return jsonify([])
            
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            
        cleaned_lines = [line.rstrip('\r\n') for line in lines]
        
        # Find the last session header index
        header_idx = -1
        for i, line in enumerate(cleaned_lines):
            line_upper = line.upper()
            if "NEW SESSION" in line_upper or "NEW SCRAPER SESSION" in line_upper or "NEW PLAYLIST IMPORT SESSION" in line_upper:
                header_idx = i
                
        if header_idx != -1:
            current_session_lines = cleaned_lines[header_idx:]
        else:
            current_session_lines = cleaned_lines
            
        last_150 = current_session_lines[-150:]
        
        keywords = {"playlist", "import", "download", "track", "failed", "error", "skipped"}
        filtered = [
            line for line in last_150
            if any(kw in line.lower() for kw in keywords)
        ]
        
        if not filtered:
            return jsonify(last_150)
        return jsonify(filtered)
    except Exception as e:
        logger.error(f"Error in GET /api/playlist/logs: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/download-logs', methods=['GET'])
def download_logs():
    try:
        db_file_id = get_db_file_id()
        if not db_file_id:
            return jsonify([])
        data = download_json(db_file_id)
        
        tracks = []
        if isinstance(data, list):
            tracks = data
        elif isinstance(data, dict):
            if 'tracks' in data and isinstance(data['tracks'], list):
                tracks = data['tracks']
            else:
                tracks = list(data.values())
                
        # Sort by timestamp descending
        def get_ts(t):
            ts = t.get('timestamp')
            if not ts: return ""
            return ts
            
        tracks.sort(key=get_ts, reverse=True)
        return jsonify(tracks)
    except Exception as e:
        logger.error(f"Error in download_logs: {e}")
        return jsonify([]), 500

# --- Backfill Engine Routes ---

@app.route('/api/backfill/status', methods=['GET'])
def get_backfill_status():
    tracks = []
    try:
        from scraper.drive_uploader import get_db_file_id
        db_file_id, _ = get_db_file_id()
        if db_file_id:
            db_data = download_json(db_file_id)
            if db_data:
                if isinstance(db_data, list):
                    tracks = db_data
                elif isinstance(db_data, dict) and 'tracks' in db_data:
                    tracks = db_data['tracks']
    except Exception as e:
        logger.error(f"Error reading database for backfill status: {e}")

    status = {
        "success": True,
        "album_art": {"missing": 0, "total": len(tracks)},
        "duration": {"missing": 0, "total": len(tracks)},
        "language": {"missing": 0, "total": len(tracks)}
    }
    
    for track in tracks:
        art = track.get("album_art")
        if art is None or art == "":
            status["album_art"]["missing"] += 1
            
        if track.get("durationSeconds") is None:
            status["duration"]["missing"] += 1
            
        lang = track.get("language")
        if lang is None or lang == "Unknown" or lang == "unknown" or lang == "":
            status["language"]["missing"] += 1
            
    return jsonify(status)

@app.route('/api/backfill/full-enrichment', methods=['POST'])
def run_backfill_enrichment():
    if background_tasks["backfill"]["status"] == "running":
        return jsonify({"status": "already_running", "type": background_tasks["backfill"]["type"]})

    def run_job():
        background_tasks["backfill"]["status"] = "running"
        background_tasks["backfill"]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        background_tasks["backfill"]["type"] = "full-enrichment"
        try:
            run_full_enrichment_pass()
        except Exception as e:
            logger.error(f"Enrichment job failed: {e}")
        finally:
            background_tasks["backfill"]["status"] = "idle"
            
    thread = threading.Thread(target=run_job)
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started", "message": "Full metadata enrichment pass started in background."})

@app.route('/api/backfill/logs', methods=['GET'])
def get_backfill_logs():
    try:
        log_path = os.path.join(project_root, 'scraper.log')
        if not os.path.exists(log_path):
            return jsonify([])
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        # Filter for backfill related lines
        backfill_lines = [line.strip() for line in lines if 'backfill' in line.lower()]
        
        # Return last 100
        return jsonify(backfill_lines[-100:])
    except Exception as e:
        logger.error(f"Error reading backfill logs: {e}")
        return jsonify([])

@app.route('/api/background/status', methods=['GET'])
def get_background_status():
    pl_state = background_tasks["playlist_import"]
    if pl_state["status"] == "running" and pl_state["playlist_id"]:
        from scraper.playlist_importer import active_imports
        state = active_imports.get(pl_state["playlist_id"])
        if state:
            pl_state["processed"] = state.get("processed", 0)
            pl_state["total_tracks"] = state.get("total_tracks", 0)
            pl_state["downloaded"] = state.get("downloaded", 0)
            pl_state["skipped"] = state.get("skipped", 0)
            pl_state["failed"] = state.get("failed", 0)
            if state.get("status") in ("completed", "cancelled"):
                pl_state["status"] = state.get("status")
        else:
            try:
                st = get_playlist_status(pl_state["playlist_id"])
                if st and st.get("status") != "not_found":
                    pl_state["processed"] = st.get("processed", 0)
                    pl_state["total_tracks"] = st.get("total_tracks", 0)
                    pl_state["downloaded"] = st.get("downloaded", 0)
                    pl_state["skipped"] = st.get("skipped", 0)
                    pl_state["failed"] = st.get("failed", 0)
                    if st.get("status") in ("completed", "cancelled"):
                        pl_state["status"] = st.get("status")
            except Exception as e:
                logger.warning(f"Failed to fetch playlist status from Drive: {e}")
    return jsonify(background_tasks)

@app.route('/ping', methods=['GET'])
def ping():
    import datetime
    return jsonify({
        "status": "alive",
        "timestamp": datetime.datetime.utcnow().isoformat() + 'Z'
    })

# ==============================================================================
# APP INTEGRATION ROUTES (PART 1 & 2)
# ==============================================================================
import uuid
app_import_tasks = {}

@app.route('/api/app/song/add', methods=['POST'])
def app_song_add():
    body = request.json or {}
    spotify_url = body.get('spotifyUrl')
    device_id = body.get('deviceId')
    
    if not spotify_url or not device_id:
        return jsonify({"error": "Missing 'spotifyUrl' or 'deviceId'"}), 400
        
    task_id = str(uuid.uuid4())
    app_import_tasks[task_id] = {"status": "running", "track": None, "error": None}
    
    def run_app_song_task(tid, url, dev_id):
        temp_file_path = None
        try:
            metadata = get_track_by_spotify_url(url)
            title = metadata["title"]
            artist = metadata["artist"]
            spotify_id = metadata["spotify_id"]
            genre = metadata["genre"]
            
            # No manual album_art or language fetching here; handled by enricher
                
            state = load_state()
            existing_tracks = []
            db_file_id = get_db_file_id()
            if db_file_id:
                try:
                    existing_data = download_json(db_file_id)
                    if isinstance(existing_data, list):
                        existing_tracks = existing_data
                    elif isinstance(existing_data, dict) and 'tracks' in existing_data:
                        existing_tracks = existing_data['tracks']
                except Exception:
                    pass
                    
            track_to_check = {
                "title": title,
                "artist": artist,
                "spotify_id": spotify_id
            }
            
            if is_duplicate(track_to_check, state, existing_tracks):
                app_import_tasks[tid]["status"] = "duplicate"
                app_import_tasks[tid]["error"] = "Song already exists in database"
                return
                
            temp_dir = os.environ.get('TEMP_DIR', os.path.join(project_root, 'temp'))
            os.makedirs(temp_dir, exist_ok=True)
            
            temp_file_path = download_track(title, artist, temp_dir)
            drive_file_id = upload_track(temp_file_path)
            enriched = enrich_track_metadata(title, artist, local_file_path=temp_file_path, source="app_single")
            
            db_metadata = {
                "title": title,
                "artist": artist,
                "album": enriched.get("album", "Single"),
                "genre": enriched.get("genre", genre),
                "duration": enriched.get("duration", "--:--"),
                "durationSeconds": enriched.get("durationSeconds"),
                "spotify_id": spotify_id,
                "album_art": enriched.get("album_art"),
                "language": enriched.get("language", "unknown"),
                "source": "app_single",
                "requestedBy": dev_id,
                "lyrics": enriched.get("lyrics"),
                "syncedLyrics": enriched.get("syncedLyrics")
            }
            
            update_database(drive_file_id, db_metadata)
            
            try:
                current_state = load_state()
                if spotify_id is not None:
                    current_state.setdefault("downloaded_ids", []).append(spotify_id)
                current_state.setdefault("downloaded_titles", []).append(f"{title} {artist}")
                save_state(current_state)
            except Exception:
                pass
                
            app_import_tasks[tid]["status"] = "completed"
            app_import_tasks[tid]["track"] = db_metadata
            
        except Exception as e:
            logger.error(f"Error in app_song_add: {e}", exc_info=True)
            app_import_tasks[tid]["status"] = "failed"
            app_import_tasks[tid]["error"] = str(e)
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except Exception:
                    pass

    thread = threading.Thread(target=run_app_song_task, args=(task_id, spotify_url, device_id))
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "started", "taskId": task_id})

@app.route('/api/app/song/status', methods=['GET'])
def app_song_status():
    task_id = request.args.get('taskId')
    if not task_id:
        return jsonify({"error": "Missing taskId"}), 400
    
    if task_id not in app_import_tasks:
        return jsonify({"error": "Task not found"}), 404
        
    return jsonify(app_import_tasks[task_id])

@app.route('/api/app/playlist/start', methods=['POST'])
def app_playlist_start():
    body = request.json or {}
    playlist_url = body.get('playlistUrl')
    device_id = body.get('deviceId')
    
    if not playlist_url or not device_id:
        return jsonify({"error": "Missing 'playlistUrl' or 'deviceId'"}), 400
        
    try:
        playlist_id = start_playlist_import(playlist_url, device_id=device_id, imported_via="app")
        
        def run_app_playlist_import():
            try:
                run_playlist_import(playlist_id, source_override="app_playlist")
            except Exception as e:
                logger.error(f"Error in background app playlist import: {e}", exc_info=True)

        thread = threading.Thread(target=run_app_playlist_import)
        thread.daemon = True
        thread.start()
        
        return jsonify({"status": "started", "playlistId": playlist_id})
    except Exception as e:
        logger.error(f"Error in app_playlist_start: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/app/playlist/status', methods=['GET'])
def app_playlist_status():
    playlist_id = request.args.get('playlistId')
    device_id = request.args.get('deviceId')
    
    if not playlist_id or not device_id:
        return jsonify({"error": "Missing 'playlistId' or 'deviceId'"}), 400
        
    try:
        status = get_playlist_status(playlist_id)
        if status.get("status") == "not_found":
            return jsonify(status)
            
        # Optional: Filter the tracks included in the status to only those attributed to this deviceId
        # However, playlist imports store device_id at the playlist level, so we just return the full status
        if status.get("device_id") != device_id:
            return jsonify({"error": "Not authorized to view this playlist import"}), 403
            
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error in app_playlist_status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/app/playlist/preview', methods=['POST'])
def app_playlist_preview():
    body = request.json or {}
    playlist_url = body.get('playlistUrl')
    if not playlist_url:
        return jsonify({"error": "Missing playlistUrl"}), 400
    try:
        preview = get_playlist_preview(playlist_url)
        return jsonify(preview)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/app/playlists', methods=['GET'])
def app_get_playlists():
    device_id = request.args.get('deviceId')
    if not device_id:
        return jsonify({"error": "Missing deviceId"}), 400
    try:
        from scraper.playlist_manager import get_all_playlists
        playlists = get_all_playlists()
        my_playlists = [p for p in playlists if p.get('requestedBy') == device_id]
        # sort by created_at descending
        my_playlists.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        return jsonify(my_playlists)
    except Exception as e:
        logger.error(f"Error in GET /api/app/playlists: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/app/my-imports', methods=['GET'])
def app_my_imports():
    device_id = request.args.get('deviceId')
    if not device_id:
        return jsonify({"error": "Missing deviceId"}), 400
        
    try:
        db_file_id = get_db_file_id()
        if not db_file_id:
            return jsonify([])
        data = download_json(db_file_id)
        tracks = data.get('tracks', data) if isinstance(data, dict) else data
        
        my_tracks = [t for t in tracks if t.get('requestedBy') == device_id]
        my_tracks.sort(key=lambda x: x.get('addedAt', x.get('timestamp', '')), reverse=True)
        
        return jsonify(my_tracks)
    except Exception as e:
        logger.error(f"Error in app_my_imports: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/app-imports', methods=['GET'])
def get_app_imports():
    try:
        db_file_id = get_db_file_id()
        if not db_file_id:
            return jsonify([])
        data = download_json(db_file_id)
        tracks = data.get('tracks', data) if isinstance(data, dict) else data
        
        app_tracks = [t for t in tracks if t.get('source') in ("app_single", "app_playlist")]
        app_tracks.sort(key=lambda x: x.get('addedAt', x.get('timestamp', '')), reverse=True)
        
        return jsonify(app_tracks)
    except Exception as e:
        logger.error(f"Error in get_app_imports: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
