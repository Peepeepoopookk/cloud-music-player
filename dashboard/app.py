import os
import sys
import subprocess
import logging
import requests
import copy
import functools
import secrets
import re
import uuid
import time
from flask import Flask, render_template, jsonify, request, stream_with_context, Response, redirect
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
from scraper.drive_uploader import upload_track, update_database, normalize_database, audit_database_fields, sync_database_lite
from scraper.metadata_enricher import enrich_track_metadata
from scraper.playlist_importer import (
    get_playlist_preview,
    start_playlist_import,
    get_playlist_status,
    run_playlist_import,
    PlaylistAlreadyDownloadedError,
)
from scraper.spotify_library_importer import (
    build_spotify_authorize_url,
    diagnose_spotify_library_playlist,
    exchange_spotify_code_for_refresh_token,
    get_spotify_library_connection_status,
    get_spotify_library_playlist_preview,
    start_spotify_library_import,
)
from scraper.operation_lock import library_write_lock
from dashboard.import_queue import (
    ImportJobConflict,
    ImportJobNotFound,
    claim_next_import_job,
    create_import_job,
    get_import_job,
    set_import_job_result,
)
from scraper.alerting import send_alert
import ctypes
import threading
import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Check for YOUTUBE_COOKIES environment variable and write to /tmp/cookies.txt for yt-dlp
youtube_cookies = os.environ.get('YOUTUBE_COOKIES')
if youtube_cookies and youtube_cookies.strip():
    cookies_path = '/tmp/cookies.txt'
    try:
        os.makedirs(os.path.dirname(cookies_path), exist_ok=True)
        with open(cookies_path, 'w', encoding='utf-8') as f:
            f.write(youtube_cookies)
        logger.info(f"Wrote {cookies_path} from YOUTUBE_COOKIES environment variable")
    except Exception as e:
        logger.error(f"Failed to write {cookies_path} from environment variable: {e}")

app = Flask(__name__, 
            template_folder=os.path.join(project_root, 'dashboard', 'templates'),
            static_folder=os.path.join(project_root, 'dashboard', 'static'))
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "wavify-dashboard-secret-key"
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.jinja_env.auto_reload = True

db_file_id_cache = None

@app.context_processor
def inject_dashboard_write_token():
    return {
        "dashboard_write_token": os.environ.get("DASHBOARD_WRITE_TOKEN") or os.environ.get("API_WRITE_TOKEN") or ""
    }

def _extract_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return None

def _request_token():
    return (
        request.headers.get("X-Dashboard-Key")
        or request.headers.get("X-Dashboard-Token")
        or request.headers.get("X-App-Token")
        or request.headers.get("X-API-Key")
        or _extract_bearer_token()
    )

def require_write_auth(app_endpoint=False):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if app_endpoint:
                expected_token = (
                    os.environ.get("APP_WRITE_TOKEN")
                    or os.environ.get("DASHBOARD_WRITE_TOKEN")
                    or os.environ.get("API_WRITE_TOKEN")
                )
            else:
                expected_token = os.environ.get("DASHBOARD_WRITE_TOKEN") or os.environ.get("API_WRITE_TOKEN")
            if not expected_token:
                return func(*args, **kwargs)
            supplied_token = _request_token()
            if supplied_token and secrets.compare_digest(supplied_token, expected_token):
                return func(*args, **kwargs)
            return jsonify({"error": "Unauthorized"}), 401
        return wrapper
    return decorator


def require_worker_auth(func):
    """Fail-closed authentication for the private home-worker API."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        expected_token = os.environ.get("WAVIFY_WORKER_TOKEN")
        supplied_token = _extract_bearer_token()
        if not expected_token or not supplied_token:
            return jsonify({"error": "Unauthorized"}), 401
        if not secrets.compare_digest(supplied_token, expected_token):
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)
    return wrapper


def is_production_environment():
    """
    Detect whether the application is running in a production environment (Render).
    Render automatically populates RENDER=true and RENDER_SERVICE_ID in service environments.
    Locally, these variables are absent, indicating local execution.
    """
    return bool(
        (os.environ.get("RENDER") and os.environ.get("RENDER").lower() not in ("false", "0", ""))
        or os.environ.get("RENDER_SERVICE_ID")
        or os.environ.get("RENDER_INSTANCE_ID")
    )


def require_admin_auth(func):
    """
    HTTP Basic Auth gate specifically for HTML admin pages (/admin, /gemini-backfill, /imported-playlists).
    In local development (non-production, indicated by absence of Render environment variables),
    admin routes are freely accessible with no authentication prompts.
    In production (Render), validates against the ADMIN_PASSWORD environment variable (fail-closed).
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not is_production_environment():
            return func(*args, **kwargs)

        admin_password = os.environ.get("ADMIN_PASSWORD")
        if not admin_password:
            return Response(
                "Admin access is not configured. Please set ADMIN_PASSWORD.",
                401,
                {"WWW-Authenticate": 'Basic realm="Wavify Admin"'}
            )
        auth = request.authorization
        if auth:
            supplied = auth.password or auth.username or ""
            if secrets.compare_digest(supplied, admin_password):
                return func(*args, **kwargs)
        return Response(
            "Unauthorized: Valid admin credentials required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Wavify Admin"'}
        )
    return wrapper

import time

_db_cache = {"data": None, "timestamp": 0}
CACHE_TTL_SECONDS = 30

def get_database_cached():
    db_file_id = get_db_file_id()
    if not db_file_id:
        return None
        
    if _db_cache["data"] is not None and (time.time() - _db_cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _db_cache["data"]
        
    data = download_json(db_file_id)
    _db_cache["data"] = data
    _db_cache["timestamp"] = time.time()
    return data

def invalidate_db_cache():
    _db_cache["data"] = None
    _db_cache["timestamp"] = 0


# Global background tasks state tracking
background_tasks = {
    "scraper": {"status": "idle", "started_at": None},
    "playlist_import": {
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "queue_id": None,
        "playlist_id": None,
        "current_index": None,
        "current_url": None,
        "current_playlist_name": None,
        "queue": [],
        "queue_total": 0,
        "queue_completed": 0,
        "queue_failed": 0,
        "queue_cancelled": 0,
        "queue_processed_tracks": 0,
        "queue_total_tracks": 0,
        "cancel_requested": False,
        "processed": 0,
        "total_tracks": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "gemini_pending": 0,
        "gemini_deferred": 0,
        "gemini_status": "idle",
        "last_error": None
    },
    "spotify_library_import": {
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "task_id": None,
        "playlist_id": None,
        "current_url": None,
        "current_playlist_name": None,
        "cancel_requested": False,
        "processed": 0,
        "total_tracks": 0,
        "tracks_available_for_import": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "gemini_pending": 0,
        "gemini_deferred": 0,
        "gemini_status": "idle",
        "last_error": None
    },
    "backfill": {
        "status": "idle",
        "started_at": None,
        "type": None,
        "logs": [],
        "changelog": [],
        "processed": 0,
        "total_candidates": 0,
        "api_call_count": 0
    },
    "single_add": {"status": "idle", "started_at": None}
}

backfill_cancel_event = threading.Event()
playlist_queue_lock = threading.Lock()
spotify_library_import_lock = threading.Lock()

app_import_tasks = {}

def is_scraper_running():
    return background_tasks["scraper"]["status"] == "running"

SPOTIFY_PLAYLIST_ID_PATTERN = re.compile(r'(?:playlist/|spotify:playlist:)([A-Za-z0-9]+)', re.IGNORECASE)
SPOTIFY_PLAYLIST_URL_PATTERN = re.compile(
    r'(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?playlist/[A-Za-z0-9]+[^\s<>"\']*|spotify:playlist:[A-Za-z0-9]+)',
    re.IGNORECASE
)
PLAYLIST_QUEUE_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "already_downloaded"}
MAX_DASHBOARD_PLAYLIST_QUEUE_SIZE = 5

def _utc_now_iso():
    return datetime.datetime.utcnow().isoformat() + 'Z'

def _normalize_spotify_playlist_url(raw_url):
    value = str(raw_url or "").strip().strip("'\"()[]{}<>")
    value = value.rstrip(".,;")
    if not value:
        return None

    id_match = SPOTIFY_PLAYLIST_ID_PATTERN.search(value)
    if id_match:
        return f"https://open.spotify.com/playlist/{id_match.group(1)}"

    if re.fullmatch(r"[A-Za-z0-9]{16,32}", value):
        return f"https://open.spotify.com/playlist/{value}"

    return None

def _parse_playlist_urls_payload(body):
    body = body or {}
    candidates = []

    urls = body.get("urls")
    if isinstance(urls, list):
        candidates.extend(urls)

    for key in ("url", "text", "playlist_urls", "playlistUrls"):
        value = body.get(key)
        if isinstance(value, str):
            matches = SPOTIFY_PLAYLIST_URL_PATTERN.findall(value)
            if matches:
                candidates.extend(matches)
            else:
                candidates.extend(re.split(r"[\s,\n\r]+", value))

    normalized_urls = []
    seen = set()
    for candidate in candidates:
        normalized = _normalize_spotify_playlist_url(candidate)
        if normalized and normalized not in seen:
            normalized_urls.append(normalized)
            seen.add(normalized)

    return normalized_urls

def _make_playlist_queue_item(index, url):
    return {
        "index": index,
        "url": url,
        "playlist_id": None,
        "playlist_name": "Queued playlist",
        "status": "queued",
        "processed": 0,
        "total_tracks": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "error": None,
        "started_at": None,
        "finished_at": None
    }

def _copy_playlist_task_state_unlocked():
    clean_state = {k: v for k, v in background_tasks["playlist_import"].items() if k != "thread"}
    return copy.deepcopy(clean_state)

def _playlist_queue_is_running_unlocked():
    pl_state = background_tasks["playlist_import"]
    thread = pl_state.get("thread")
    return pl_state.get("status") == "running" and (thread is None or thread.is_alive())

def _build_playlist_import_task_state(queue_id, urls):
    return {
        "status": "running",
        "started_at": _utc_now_iso(),
        "finished_at": None,
        "queue_id": queue_id,
        "playlist_id": None,
        "current_index": None,
        "current_url": None,
        "current_playlist_name": None,
        "queue": [_make_playlist_queue_item(index, url) for index, url in enumerate(urls)],
        "queue_total": len(urls),
        "queue_completed": 0,
        "queue_failed": 0,
        "queue_cancelled": 0,
        "queue_processed_tracks": 0,
        "queue_total_tracks": 0,
        "cancel_requested": False,
        "processed": 0,
        "total_tracks": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "gemini_pending": 0,
        "gemini_deferred": 0,
        "gemini_status": "idle",
        "last_error": None
    }

def _start_dashboard_playlist_queue(urls):
    queue_id = str(uuid.uuid4())

    with playlist_queue_lock:
        if _playlist_queue_is_running_unlocked():
            return None, _copy_playlist_task_state_unlocked()

        background_tasks["playlist_import"].clear()
        background_tasks["playlist_import"].update(_build_playlist_import_task_state(queue_id, urls))

    thread = threading.Thread(target=_run_dashboard_playlist_queue, args=(queue_id,))
    thread.daemon = True
    with playlist_queue_lock:
        background_tasks["playlist_import"]["thread"] = thread
        snapshot = _copy_playlist_task_state_unlocked()
    thread.start()

    return snapshot, None

def _recalculate_playlist_queue_counts_unlocked(pl_state):
    queue = pl_state.get("queue") or []
    pl_state["queue_total"] = len(queue)
    pl_state["queue_completed"] = sum(1 for item in queue if item.get("status") in ("completed", "already_downloaded"))
    pl_state["queue_failed"] = sum(1 for item in queue if item.get("status") == "failed")
    pl_state["queue_cancelled"] = sum(1 for item in queue if item.get("status") == "cancelled")
    pl_state["downloaded"] = sum(int(item.get("downloaded") or 0) for item in queue)
    pl_state["skipped"] = sum(int(item.get("skipped") or 0) for item in queue)
    pl_state["failed"] = sum(int(item.get("failed") or 0) for item in queue)
    pl_state["queue_processed_tracks"] = sum(int(item.get("processed") or 0) for item in queue)
    pl_state["queue_total_tracks"] = sum(int(item.get("total_tracks") or 0) for item in queue)

def _apply_playlist_import_state_to_queue_item_unlocked(pl_state, import_state):
    if not import_state:
        return

    current_index = pl_state.get("current_index")
    queue = pl_state.get("queue") or []
    if current_index is None or current_index < 0 or current_index >= len(queue):
        return

    current_item = queue[current_index]
    current_item["playlist_id"] = import_state.get("playlist_id") or current_item.get("playlist_id")
    current_item["playlist_name"] = import_state.get("playlist_name") or current_item.get("playlist_name")
    current_item["processed"] = import_state.get("processed", 0)
    current_item["total_tracks"] = import_state.get("total_tracks", 0)
    current_item["downloaded"] = import_state.get("downloaded", 0)
    current_item["skipped"] = import_state.get("skipped", 0)
    current_item["failed"] = import_state.get("failed", 0)
    current_item["error"] = import_state.get("error") or current_item.get("error")
    pl_state["gemini_pending"] = import_state.get("gemini_pending", 0)
    pl_state["gemini_deferred"] = import_state.get("gemini_deferred", 0)
    pl_state["gemini_status"] = import_state.get("gemini_status", "idle")

    import_status = import_state.get("status")
    if import_status in PLAYLIST_QUEUE_TERMINAL_STATUSES:
        current_item["status"] = import_status
        current_item["finished_at"] = current_item.get("finished_at") or _utc_now_iso()
    elif current_item.get("status") not in PLAYLIST_QUEUE_TERMINAL_STATUSES:
        current_item["status"] = "running"

    pl_state["playlist_id"] = current_item.get("playlist_id")
    pl_state["current_url"] = current_item.get("url")
    pl_state["current_playlist_name"] = current_item.get("playlist_name")
    pl_state["processed"] = current_item.get("processed", 0)
    pl_state["total_tracks"] = current_item.get("total_tracks", 0)
    _recalculate_playlist_queue_counts_unlocked(pl_state)

def _mark_playlist_import_state(playlist_id, status, error=None):
    if not playlist_id:
        return

    from scraper.playlist_importer import active_imports, set_cancel_event
    from scraper.drive_uploader import get_db_file_id as get_uploader_db_file_id
    from dashboard.drive_client import search_file_by_name

    if status == "cancelled":
        set_cancel_event(playlist_id)

    state_filename = f"playlist_import_state_{playlist_id}.json"
    state = active_imports.get(playlist_id, {}).copy()
    state["playlist_id"] = playlist_id
    state["status"] = status
    if error:
        state["error"] = str(error)
    active_imports[playlist_id] = state

    try:
        _, parent_id = get_uploader_db_file_id()
        if not parent_id:
            return
        file_id = search_file_by_name(state_filename, parent_id)
        if file_id:
            persisted = download_json(file_id) or {}
            persisted.update(state)
            upload_json(file_id, persisted, state_filename, parent_id=parent_id)
            active_imports[playlist_id] = persisted
    except Exception as e:
        logger.warning(f"Could not mark playlist import {playlist_id} as {status}: {e}")

def _cancel_dashboard_playlist_queue(playlist_id=None):
    from scraper.playlist_importer import set_cancel_event
    with playlist_queue_lock:
        pl_state = background_tasks["playlist_import"]
        pl_state["cancel_requested"] = True
        target_playlist_id = playlist_id or pl_state.get("playlist_id")
        for item in pl_state.get("queue", []):
            if item.get("status") == "queued":
                item["status"] = "cancelled"
                item["finished_at"] = _utc_now_iso()
        _recalculate_playlist_queue_counts_unlocked(pl_state)

    if target_playlist_id:
        set_cancel_event(target_playlist_id)
        _mark_playlist_import_state(target_playlist_id, "cancelled")

    return target_playlist_id

def _finish_dashboard_playlist_task_unlocked(pl_state):
    if pl_state.get("cancel_requested"):
        pl_state["status"] = "cancelled"
    elif pl_state.get("queue_failed"):
        pl_state["status"] = "completed_with_errors"
    else:
        pl_state["status"] = "completed"

    pl_state["finished_at"] = _utc_now_iso()
    pl_state.pop("thread", None)

def _run_dashboard_playlist_queue(queue_id):
    logger.info(f"Starting dashboard playlist import queue {queue_id}")
    try:
        log_path = os.path.join(project_root, 'scraper.log')
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n============================================================\nNEW PLAYLIST QUEUE SESSION ({queue_id})\n============================================================\n")
    except Exception as log_err:
        logger.warning(f"Could not append playlist queue marker to scraper.log: {log_err}")

    while True:
        with playlist_queue_lock:
            pl_state = background_tasks["playlist_import"]
            if pl_state.get("queue_id") != queue_id:
                return
            if pl_state.get("cancel_requested"):
                for item in pl_state.get("queue", []):
                    if item.get("status") == "queued":
                        item["status"] = "cancelled"
                        item["finished_at"] = _utc_now_iso()
                _recalculate_playlist_queue_counts_unlocked(pl_state)
                _finish_dashboard_playlist_task_unlocked(pl_state)
                return

            next_index = None
            for idx, item in enumerate(pl_state.get("queue", [])):
                if item.get("status") == "queued":
                    next_index = idx
                    break

            if next_index is None:
                _recalculate_playlist_queue_counts_unlocked(pl_state)
                _finish_dashboard_playlist_task_unlocked(pl_state)
                return

            item = pl_state["queue"][next_index]
            item["status"] = "starting"
            item["started_at"] = _utc_now_iso()
            item["error"] = None
            pl_state["status"] = "running"
            pl_state["current_index"] = next_index
            pl_state["current_url"] = item.get("url")
            pl_state["current_playlist_name"] = item.get("playlist_name")
            pl_state["playlist_id"] = item.get("playlist_id")
            pl_state["processed"] = 0
            pl_state["total_tracks"] = item.get("total_tracks", 0)
            _recalculate_playlist_queue_counts_unlocked(pl_state)

        playlist_id = None
        try:
            url = item.get("url")
            playlist_id = start_playlist_import(url, imported_via="dashboard")
            import_state = get_playlist_status(playlist_id)

            with playlist_queue_lock:
                pl_state = background_tasks["playlist_import"]
                if pl_state.get("queue_id") != queue_id:
                    return
                current_item = pl_state["queue"][next_index]
                current_item["playlist_id"] = playlist_id
                current_item["playlist_name"] = import_state.get("playlist_name") or current_item.get("playlist_name")
                current_item["total_tracks"] = import_state.get("total_tracks", 0)
                current_item["status"] = "running"
                pl_state["playlist_id"] = playlist_id
                pl_state["current_playlist_name"] = current_item["playlist_name"]
                pl_state["total_tracks"] = current_item["total_tracks"]

            if background_tasks["playlist_import"].get("cancel_requested"):
                _mark_playlist_import_state(playlist_id, "cancelled")

            run_playlist_import(playlist_id)
            import_state = get_playlist_status(playlist_id)
            final_status = import_state.get("status") or "completed"
            if final_status not in PLAYLIST_QUEUE_TERMINAL_STATUSES:
                final_status = "completed"

            with playlist_queue_lock:
                pl_state = background_tasks["playlist_import"]
                if pl_state.get("queue_id") != queue_id:
                    return
                _apply_playlist_import_state_to_queue_item_unlocked(pl_state, import_state)
                current_item = pl_state["queue"][next_index]
                current_item["status"] = final_status
                current_item["finished_at"] = _utc_now_iso()
                if final_status == "failed":
                    current_item["error"] = import_state.get("error") or "Playlist import failed"
                _recalculate_playlist_queue_counts_unlocked(pl_state)
        except PlaylistAlreadyDownloadedError as e:
            logger.info(f"Dashboard playlist queue item already downloaded: {e}")
            with playlist_queue_lock:
                pl_state = background_tasks["playlist_import"]
                if pl_state.get("queue_id") != queue_id:
                    return
                current_item = pl_state["queue"][next_index]
                current_item["playlist_id"] = e.playlist_id
                current_item["playlist_name"] = e.playlist_name
                current_item["total_tracks"] = e.total_tracks
                current_item["processed"] = e.total_tracks
                current_item["status"] = "already_downloaded"
                current_item["finished_at"] = _utc_now_iso()
                _recalculate_playlist_queue_counts_unlocked(pl_state)
            continue
        except Exception as e:
            logger.error(f"Dashboard playlist queue item failed: {e}", exc_info=True)
            if playlist_id:
                _mark_playlist_import_state(playlist_id, "failed", error=e)
            with playlist_queue_lock:
                pl_state = background_tasks["playlist_import"]
                if pl_state.get("queue_id") != queue_id:
                    return
                current_item = pl_state["queue"][next_index]
                current_item["playlist_id"] = playlist_id
                current_item["status"] = "failed"
                current_item["error"] = str(e)
                current_item["finished_at"] = _utc_now_iso()
                pl_state["last_error"] = str(e)
                _recalculate_playlist_queue_counts_unlocked(pl_state)

        with playlist_queue_lock:
            pl_state = background_tasks["playlist_import"]
            if pl_state.get("queue_id") != queue_id:
                return
            if pl_state.get("cancel_requested"):
                for queued_item in pl_state.get("queue", []):
                    if queued_item.get("status") == "queued":
                        queued_item["status"] = "cancelled"
                        queued_item["finished_at"] = _utc_now_iso()
                _recalculate_playlist_queue_counts_unlocked(pl_state)
                _finish_dashboard_playlist_task_unlocked(pl_state)
                return

def _copy_spotify_library_task_state_unlocked():
    clean_state = {k: v for k, v in background_tasks["spotify_library_import"].items() if k != "thread"}
    return copy.deepcopy(clean_state)

def _spotify_library_import_is_running_unlocked():
    task_state = background_tasks["spotify_library_import"]
    thread = task_state.get("thread")
    return task_state.get("status") == "running" and (thread is None or thread.is_alive())

def _apply_spotify_library_import_state_unlocked(task_state, import_state):
    if not import_state:
        return

    task_state["playlist_id"] = import_state.get("playlist_id") or task_state.get("playlist_id")
    task_state["current_playlist_name"] = import_state.get("playlist_name") or task_state.get("current_playlist_name")
    task_state["processed"] = int(import_state.get("processed") or 0)
    task_state["total_tracks"] = int(import_state.get("total_tracks") or 0)
    task_state["tracks_available_for_import"] = int(import_state.get("tracks_available_for_import") or 0)
    task_state["downloaded"] = int(import_state.get("downloaded") or 0)
    task_state["skipped"] = int(import_state.get("skipped") or 0)
    task_state["failed"] = int(import_state.get("failed") or 0)
    task_state["gemini_pending"] = int(import_state.get("gemini_pending") or 0)
    task_state["gemini_deferred"] = int(import_state.get("gemini_deferred") or 0)
    task_state["gemini_status"] = import_state.get("gemini_status") or "idle"
    task_state["last_error"] = import_state.get("error") or task_state.get("last_error")

def _finish_spotify_library_import_task_unlocked(task_state, status=None):
    if status:
        task_state["status"] = status
    elif task_state.get("cancel_requested"):
        task_state["status"] = "cancelled"
    elif task_state.get("last_error"):
        task_state["status"] = "failed"
    else:
        task_state["status"] = "completed"
    task_state["finished_at"] = _utc_now_iso()
    task_state.pop("thread", None)

def _start_spotify_library_dashboard_import(url):
    task_id = str(uuid.uuid4())
    with spotify_library_import_lock:
        if _spotify_library_import_is_running_unlocked():
            return None, _copy_spotify_library_task_state_unlocked()

        task_state = background_tasks["spotify_library_import"]
        task_state.clear()
        task_state.update({
            "status": "running",
            "started_at": _utc_now_iso(),
            "finished_at": None,
            "task_id": task_id,
            "playlist_id": None,
            "current_url": url,
            "current_playlist_name": None,
            "cancel_requested": False,
            "processed": 0,
            "total_tracks": 0,
            "tracks_available_for_import": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "gemini_pending": 0,
            "gemini_deferred": 0,
            "gemini_status": "idle",
            "last_error": None
        })

    thread = threading.Thread(target=_run_spotify_library_dashboard_import, args=(task_id, url))
    thread.daemon = True
    with spotify_library_import_lock:
        background_tasks["spotify_library_import"]["thread"] = thread
        snapshot = _copy_spotify_library_task_state_unlocked()
    thread.start()
    return snapshot, None

def _cancel_spotify_library_dashboard_import(playlist_id=None):
    from scraper.playlist_importer import set_cancel_event
    with spotify_library_import_lock:
        task_state = background_tasks["spotify_library_import"]
        task_state["cancel_requested"] = True
        target_playlist_id = playlist_id or task_state.get("playlist_id")
        if task_state.get("status") == "idle":
            task_state["status"] = "cancelled"
            task_state["finished_at"] = _utc_now_iso()

    if target_playlist_id:
        set_cancel_event(target_playlist_id)
        _mark_playlist_import_state(target_playlist_id, "cancelled")

    return target_playlist_id

def _run_spotify_library_dashboard_import(task_id, url):
    logger.info(f"Starting Spotify Library Importer task {task_id}")
    try:
        log_path = os.path.join(project_root, 'scraper.log')
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n============================================================\nNEW SPOTIFY LIBRARY IMPORT SESSION ({task_id})\n============================================================\n")
    except Exception as log_err:
        logger.warning(f"Could not append Spotify library import marker to scraper.log: {log_err}")

    playlist_id = None
    try:
        playlist_id = start_spotify_library_import(url)
        import_state = get_playlist_status(playlist_id)
        with spotify_library_import_lock:
            task_state = background_tasks["spotify_library_import"]
            if task_state.get("task_id") != task_id:
                return
            _apply_spotify_library_import_state_unlocked(task_state, import_state)
            task_state["playlist_id"] = playlist_id

        with spotify_library_import_lock:
            cancel_requested = background_tasks["spotify_library_import"].get("cancel_requested")
        if cancel_requested:
            _mark_playlist_import_state(playlist_id, "cancelled")

        run_playlist_import(playlist_id)
        final_state = get_playlist_status(playlist_id)
        final_status = final_state.get("status") or "completed"
        if final_status not in PLAYLIST_QUEUE_TERMINAL_STATUSES:
            final_status = "completed"

        with spotify_library_import_lock:
            task_state = background_tasks["spotify_library_import"]
            if task_state.get("task_id") != task_id:
                return
            _apply_spotify_library_import_state_unlocked(task_state, final_state)
            _finish_spotify_library_import_task_unlocked(task_state, final_status)
    except Exception as e:
        logger.error(f"Spotify Library Importer task failed: {e}", exc_info=True)
        if playlist_id:
            _mark_playlist_import_state(playlist_id, "failed", error=e)
        with spotify_library_import_lock:
            task_state = background_tasks["spotify_library_import"]
            if task_state.get("task_id") != task_id:
                return
            task_state["last_error"] = str(e)
            _finish_spotify_library_import_task_unlocked(task_state, "failed")

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


@app.route('/', methods=['GET', 'HEAD'])
def index():
    """
    GET / — serves the public read-only library browser
    HEAD / — fast 200 response for cold start wake-up (e.g. Android app)
    """
    if request.method == 'HEAD':
        return ('', 200)
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error rendering index.html: {e}", exc_info=True)
        return f"Error loading page: {str(e)}", 500


@app.route('/admin')
@require_admin_auth
def admin_page():
    """
    GET /admin — serves the full admin control center (Downloader, Storage, Logs, Settings, etc.)
    """
    try:
        return render_template('admin.html')
    except Exception as e:
        logger.error(f"Error rendering admin.html: {e}", exc_info=True)
        return f"Error loading admin page: {str(e)}", 500


@app.route('/gemini-backfill')
@require_admin_auth
def gemini_backfill_page():
    """
    GET /gemini-backfill — serves the Gemini AI Backfill monitor page
    """
    try:
        return render_template('gemini_backfill.html')
    except Exception as e:
        logger.error(f"Error rendering gemini_backfill.html: {e}", exc_info=True)
        return f"Error loading page: {str(e)}", 500


@app.route('/imported-playlists')
@require_admin_auth
def imported_playlists_page():
    """
    GET /imported-playlists - serves the imported playlists browser page.
    """
    try:
        return render_template('imported_playlists.html')
    except Exception as e:
        logger.error(f"Error rendering imported_playlists.html: {e}", exc_info=True)
        return f"Error loading page: {str(e)}", 500

# Cache dictionary and TTL for latest GitHub release of Wavify Android App
_app_release_cache = {
    "data": None,
    "timestamp": 0
}
_APP_RELEASE_CACHE_TTL = 600  # 10 minutes

def get_latest_app_release():
    """
    Fetches latest release metadata for the Wavify Android App from GitHub Releases.
    Cached in-memory for 10 minutes with graceful fallback on rate limits/errors.
    """
    now = time.time()
    if _app_release_cache["data"] and (now - _app_release_cache["timestamp"] < _APP_RELEASE_CACHE_TTL):
        return _app_release_cache["data"]

    url = "https://api.github.com/repos/Peepeepoopookk/wavify/releases/latest"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Wavify-Dashboard"
    }

    try:
        r = requests.get(url, headers=headers, timeout=6)
        if r.status_code == 200:
            raw = r.json()
            tag_name = raw.get("tag_name") or "v1.0.0"
            version_name = raw.get("name") or tag_name
            html_url = raw.get("html_url") or "https://github.com/Peepeepoopookk/wavify/releases"
            body = raw.get("body") or ""
            published_at_raw = raw.get("published_at")
            
            published_at_fmt = "Recent"
            if published_at_raw:
                try:
                    dt = datetime.datetime.fromisoformat(published_at_raw.replace("Z", "+00:00"))
                    published_at_fmt = dt.strftime("%b %d, %Y")
                except Exception:
                    published_at_fmt = published_at_raw[:10]

            assets = raw.get("assets", [])
            apk_asset = next((a for a in assets if (a.get("name") or "").lower().endswith(".apk")), None)
            
            if apk_asset:
                apk_url = apk_asset.get("browser_download_url")
                apk_name = apk_asset.get("name")
                size_bytes = apk_asset.get("size") or 0
                apk_size_mb = f"{size_bytes / (1024 * 1024):.2f} MB" if size_bytes else "Unknown"
            else:
                apk_url = html_url
                apk_name = "Wavify.apk"
                apk_size_mb = "~4.2 MB"

            release_data = {
                "version_name": version_name,
                "tag_name": tag_name,
                "apk_download_url": apk_url,
                "apk_name": apk_name,
                "apk_size_mb": apk_size_mb,
                "published_at": published_at_fmt,
                "published_at_raw": published_at_raw,
                "release_notes": body.strip() if body else "Latest release with offline caching, lossless cloud playback, and synced lyrics.",
                "html_url": html_url,
                "repo_url": "https://github.com/Peepeepoopookk/wavify",
                "releases_url": "https://github.com/Peepeepoopookk/wavify/releases",
                "is_fallback": False
            }
            _app_release_cache["data"] = release_data
            _app_release_cache["timestamp"] = now
            return release_data
    except Exception as e:
        logger.warning(f"Error fetching latest Wavify release from GitHub: {e}")

    if _app_release_cache["data"]:
        return _app_release_cache["data"]

    return {
        "version_name": "v1.0.0",
        "tag_name": "v1.0.0",
        "apk_download_url": "https://github.com/Peepeepoopookk/wavify/releases/latest",
        "apk_name": "Wavify.apk",
        "apk_size_mb": "~4.2 MB",
        "published_at": "August 2026",
        "published_at_raw": None,
        "release_notes": "Latest version with cloud music streaming, offline downloads, and synced lyrics.",
        "html_url": "https://github.com/Peepeepoopookk/wavify/releases",
        "repo_url": "https://github.com/Peepeepoopookk/wavify",
        "releases_url": "https://github.com/Peepeepoopookk/wavify/releases",
        "is_fallback": True
    }

@app.route('/download')
@app.route('/app')
def download_page():
    """
    GET /download, GET /app — serves the public Wavify Android App download page.
    No admin authentication required.
    """
    try:
        release = get_latest_app_release()
        return render_template('download.html', release=release)
    except Exception as e:
        logger.error(f"Error rendering download.html: {e}", exc_info=True)
        return f"Error loading download page: {str(e)}", 500

@app.route('/download/latest.apk')
@app.route('/app/download')
@app.route('/app/latest.apk')
def download_apk_redirect():
    """
    GET /download/latest.apk, GET /app/download — redirects directly to latest APK asset.
    """
    try:
        release = get_latest_app_release()
        apk_url = release.get("apk_download_url") or "https://github.com/Peepeepoopookk/wavify/releases/latest"
        return redirect(apk_url, code=302)
    except Exception as e:
        logger.error(f"Error redirecting to APK download: {e}", exc_info=True)
        return redirect("https://github.com/Peepeepoopookk/wavify/releases/latest", code=302)

@app.route('/api/app/release', methods=['GET'])
def api_app_release():
    """
    GET /api/app/release — JSON API for latest app release metadata.
    """
    return jsonify(get_latest_app_release())

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
@require_write_auth()
def delete_track(file_id):
    """
    POST /api/delete/<file_id> — deletes a track from Drive and updates database.json
    """
    try:
        db_file_id = get_db_file_id()

        with library_write_lock("database"):
            # 1. Download database.json
            db_data = download_json(db_file_id)
            original_db_data = copy.deepcopy(db_data)

            # 2. Update database structure (assuming it's a list of tracks or a dict)
            updated = False
            if isinstance(db_data, list):
                new_db_data = []
                for track in db_data:
                    if track.get('id') == file_id or track.get('driveFileId') == file_id or track.get('file_id') == file_id:
                        updated = True
                    else:
                        new_db_data.append(track)
                db_data = new_db_data
            elif isinstance(db_data, dict):
                if 'tracks' in db_data and isinstance(db_data['tracks'], list):
                    before_count = len(db_data['tracks'])
                    db_data['tracks'] = [
                        t for t in db_data['tracks']
                        if t.get('id') != file_id and t.get('driveFileId') != file_id and t.get('file_id') != file_id
                    ]
                    updated = len(db_data['tracks']) != before_count
                elif file_id in db_data:
                    del db_data[file_id]
                    updated = True

            if not updated:
                return jsonify({"error": f"Track {file_id} not found in database."}), 404

            # 3. Save updated database.json back to Drive
            upload_json(db_file_id, db_data, 'database.json')
            sync_database_lite(db_data)
            invalidate_db_cache()

            # 4. Delete media file from Drive; restore DB if media deletion fails.
            try:
                delete_file(file_id)
            except Exception:
                logger.error(f"Media delete failed for {file_id}; attempting database rollback.", exc_info=True)
                try:
                    upload_json(db_file_id, original_db_data, 'database.json')
                    sync_database_lite(original_db_data)
                    invalidate_db_cache()
                except Exception as rollback_err:
                    logger.error(f"Database rollback failed after delete error for {file_id}: {rollback_err}", exc_info=True)
                raise
        
        return jsonify({"status": "success", "message": f"Track {file_id} deleted successfully."})
    except Exception as e:
        logger.error(f"Error in POST /api/delete/{file_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/scrape', methods=['POST'])
@require_write_auth()
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
            if (
                "NEW SESSION" in line_upper
                or "NEW SCRAPER SESSION" in line_upper
                or "NEW PLAYLIST IMPORT SESSION" in line_upper
                or "NEW PLAYLIST QUEUE SESSION" in line_upper
                or "NEW SPOTIFY LIBRARY IMPORT SESSION" in line_upper
            ):
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
@require_write_auth()
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
@require_write_auth()
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
@require_write_auth()
def normalize_library():
    """
    POST /api/library/normalize — Normalizes all database tracks fields with defaults and backs up the database.
    """
    try:
        tracks_changed, total_tracks = normalize_database()
        invalidate_db_cache()
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

@app.route('/api/library/orphans', methods=['GET'])
def get_library_orphans():
    """
    GET /api/library/orphans - Finds media files not referenced by database.json.
    """
    try:
        from scraper.drive_uploader import find_orphan_media_files
        return jsonify({"status": "success", "data": find_orphan_media_files()})
    except Exception as e:
        logger.error(f"Error in GET /api/library/orphans: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/library/orphans/cleanup', methods=['POST'])
@require_write_auth()
def cleanup_library_orphans():
    """
    POST /api/library/orphans/cleanup - Deletes orphaned media only when dry_run is false.
    """
    try:
        body = request.get_json(silent=True) or {}
        dry_run = body.get("dry_run", True)
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in {"false", "0", "no"}
        from scraper.drive_uploader import cleanup_orphan_media_files
        return jsonify({"status": "success", "data": cleanup_orphan_media_files(dry_run=bool(dry_run))})
    except Exception as e:
        logger.error(f"Error in POST /api/library/orphans/cleanup: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/library/field-completeness', methods=['GET'])
def get_field_completeness():
    try:
        from scraper.drive_uploader import get_db_file_id
        db_file_id, _ = get_db_file_id()
        if not db_file_id:
            return jsonify({"error": "No database found"}), 404
            
        db_data = download_json(db_file_id)
        tracks = db_data.get('tracks', db_data) if isinstance(db_data, dict) else db_data
        if not isinstance(tracks, list):
            return jsonify({"error": "Invalid database format"}), 500
            
        total = len(tracks)
        fields_set = set()
        for t in tracks:
            fields_set.update(t.keys())
            
        fields_data = {}
        for f in fields_set:
            present = sum(1 for t in tracks if t.get(f) not in [None, "", "Unknown", "unknown", "--:--"])
            fields_data[f] = {
                "present": present,
                "missing": total - present,
                "percentage": round(present / total * 100, 1) if total > 0 else 0
            }
            
        import datetime
        return jsonify({
            "total_tracks": total,
            "fields": fields_data,
            "generated_at": datetime.datetime.utcnow().isoformat() + 'Z'
        })
    except Exception as e:
        logger.error(f"Error getting field completeness: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/library/field-completeness/<field_name>/missing-tracks', methods=['GET'])
def get_missing_tracks(field_name):
    try:
        from scraper.drive_uploader import get_db_file_id
        db_file_id, _ = get_db_file_id()
        if not db_file_id:
            return jsonify({"error": "No database found"}), 404
            
        db_data = download_json(db_file_id)
        tracks = db_data.get('tracks', db_data) if isinstance(db_data, dict) else db_data
        if not isinstance(tracks, list):
            return jsonify({"error": "Invalid database format"}), 500
            
        missing = []
        for t in tracks:
            if t.get(field_name) in [None, "", "Unknown", "unknown", "--:--"]:
                missing.append({
                    "id": t.get("id"),
                    "title": t.get("title", "Unknown Title"),
                    "artist": t.get("artist", "Unknown Artist")
                })
        return jsonify(missing)
    except Exception as e:
        logger.error(f"Error getting missing tracks: {e}")
        return jsonify({"error": str(e)}), 500

def _build_gemini_backfill_summary():
    from scraper.drive_uploader import get_db_file_id
    from scraper.gemini_metadata_judge import (
        build_gemini_candidates
    )

    db_file_id, _ = get_db_file_id()
    if not db_file_id:
        return {"error": "No database found"}, 404

    db_data = download_json(db_file_id)
    tracks = db_data.get('tracks', db_data) if isinstance(db_data, dict) else db_data
    if not isinstance(tracks, list):
        return {"error": "Invalid database format"}, 500

    candidates = build_gemini_candidates(tracks)
    language_missing = sum(1 for track in candidates if "language" in track.get("fields_to_fill", []))
    genre_missing = sum(1 for track in candidates if "genre" in track.get("fields_to_fill", []))
    candidate_tracks = len(candidates)

    return {
        "total_tracks": len(tracks),
        "candidate_tracks": candidate_tracks,
        "field_count": language_missing + genre_missing,
        "fields": {
            "language": language_missing,
            "genre": genre_missing
        },
        "generated_at": datetime.datetime.utcnow().isoformat() + 'Z'
    }, 200

@app.route('/api/backfill/gemini/summary', methods=['GET'])
def get_gemini_backfill_summary():
    try:
        payload, status_code = _build_gemini_backfill_summary()
        return jsonify(payload), status_code
    except Exception as e:
        logger.error(f"Error getting Gemini backfill summary: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/backfill/complete', methods=['POST'])
@require_write_auth()
def complete_backfill():
    """
    POST /api/backfill/complete - Runs the complete backfill engine on all tracks in the background.
    """
    try:
        if background_tasks["backfill"]["status"] == "running":
            return jsonify({"status": "already_running", "type": background_tasks["backfill"].get("type")}), 409

        logger.info("Starting complete backfill engine in a background thread...")
        backfill_cancel_event.clear()
        background_tasks["backfill"]["status"] = "running"
        background_tasks["backfill"]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        background_tasks["backfill"]["type"] = "complete"

        def run_complete_job():
            try:
                result = run_complete_backfill() or {}
                background_tasks["backfill"].setdefault("logs", []).append({
                    "time": datetime.datetime.utcnow().isoformat() + 'Z',
                    "level": "success" if result.get("status") != "error" else "error",
                    "message": result.get("message", "Complete backfill finished.")
                })
            except Exception as e:
                logger.error(f"Complete backfill failed: {e}", exc_info=True)
                background_tasks["backfill"].setdefault("logs", []).append({
                    "time": datetime.datetime.utcnow().isoformat() + 'Z',
                    "level": "error",
                    "message": f"Complete backfill crashed: {str(e)}"
                })
            finally:
                if background_tasks["backfill"].get("status") != "cancelled":
                    background_tasks["backfill"]["status"] = "idle"

        thread = threading.Thread(target=run_complete_job)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "success",
            "message": "Complete backfill started in the background."
        })
    except Exception as e:
        logger.error(f"Error starting complete backfill: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
@app.route('/api/backfill/gemini', methods=['POST'])
@require_write_auth()
def gemini_backfill():
    """
    POST /api/backfill/gemini - Runs the Gemini LLM backfill engine in the background.
    """
    try:
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "auto")
        if background_tasks["backfill"]["status"] == "running":
            return jsonify({"status": "already_running", "type": background_tasks["backfill"].get("type")}), 409
        
        from scraper.main import run_gemini_backfill
        logger.info(f"Starting Gemini backfill engine ({mode} mode) in a background thread...")
        backfill_cancel_event.clear()
        background_tasks["backfill"]["status"] = "running"
        background_tasks["backfill"]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        background_tasks["backfill"]["type"] = f"gemini-{mode}"
        background_tasks["backfill"]["logs"] = [{
            "time": datetime.datetime.utcnow().isoformat() + 'Z',
            "level": "info",
            "message": f"Starting Gemini backfill in {mode} mode."
        }]
        background_tasks["backfill"]["changelog"] = []
        background_tasks["backfill"]["processed"] = 0
        background_tasks["backfill"]["total_candidates"] = 0
        background_tasks["backfill"]["api_call_count"] = 0
        
        def run_gemini_job():
            try:
                result = run_gemini_backfill(
                    mode=mode,
                    task_state=background_tasks["backfill"],
                    cancel_event=backfill_cancel_event
                )
                if result.get("status") == "cancelled":
                    background_tasks["backfill"]["status"] = "cancelled"
                    background_tasks["backfill"]["logs"].append({
                        "time": datetime.datetime.utcnow().isoformat() + 'Z',
                        "level": "warning",
                        "message": result.get("message", "Gemini backfill was cancelled.")
                    })
                else:
                    background_tasks["backfill"]["status"] = "idle"
                    level = "error" if result.get("status") == "error" else "success"
                    default_message = (
                        f"Gemini backfill finished. Updated {result.get('updated', 0)} tracks "
                        f"({result.get('fields_updated', 0)} fields)."
                    )
                    background_tasks["backfill"]["logs"].append({
                        "time": datetime.datetime.utcnow().isoformat() + 'Z',
                        "level": level,
                        "message": result.get("message") or default_message
                    })
            except Exception as e:
                logger.error(f"Gemini job failed: {e}", exc_info=True)
                background_tasks["backfill"].setdefault("logs", []).append({
                    "time": datetime.datetime.utcnow().isoformat() + 'Z',
                    "level": "error",
                    "message": f"Gemini job crashed: {str(e)}"
                })
                background_tasks["backfill"]["status"] = "idle"
                
        thread = threading.Thread(target=run_gemini_job)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "success",
            "message": "Gemini backfill started in the background."
        })
    except Exception as e:
        logger.error(f"Error starting Gemini backfill: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/backfill/cancel', methods=['POST'])
@require_write_auth()
def cancel_backfill():
    """
    POST /api/backfill/cancel - Cancels the running backfill engine.
    """
    backfill_cancel_event.set()
    if background_tasks["backfill"].get("status") == "running":
        background_tasks["backfill"]["cancel_requested"] = True
    else:
        background_tasks["backfill"]["status"] = "cancelled"
    background_tasks["backfill"].setdefault("logs", []).append({
        "time": datetime.datetime.utcnow().isoformat() + 'Z',
        "level": "warning",
        "message": "Cancellation requested by user."
    })
    return jsonify({"status": "success", "message": "Backfill cancellation requested."})


@app.route('/api/library/backup', methods=['POST'])
@require_write_auth()
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

def _process_single_song(spotify_url, task_key, source, device_id=None):
    background_tasks[task_key]["status"] = "running"
    background_tasks[task_key]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
    temp_file_path = None
    drive_file_id = None
    try:
        # 1. Fetch metadata
        metadata = get_track_by_spotify_url(spotify_url)
        title = metadata["title"]
        artist = metadata["artist"]
        spotify_id = metadata["spotify_id"]
        genre = metadata["genre"]
        
        background_tasks[task_key]["track_name"] = f"{title} - {artist}"
        
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
                logger.error(f"Could not retrieve existing tracks for duplicate check: {e}", exc_info=True)
                raise
                
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
        
        # 5. Enrich metadata before uploading, so failures do not orphan media.
        enriched = enrich_track_metadata(title, artist, local_file_path=temp_file_path, source=source)

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
            "source": source,
            "lyrics": enriched.get("lyrics"),
            "syncedLyrics": enriched.get("syncedLyrics"),
            "lyricsStatus": enriched.get("lyricsStatus", "ok")
        }
        if device_id:
            db_metadata["requestedBy"] = device_id

        # 6. Upload track only after local enrichment succeeds.
        logger.info(f"Uploading file '{temp_file_path}' to Google Drive...")
        drive_file_id = upload_track(temp_file_path)
        
        # --- GEMINI INTERCEPTION START ---
        db_metadata["id"] = drive_file_id
        db_metadata["driveFileId"] = drive_file_id
        try:
            from scraper.gemini_metadata_judge import GeminiJudge, build_gemini_candidate, normalize_genre_value, normalize_language_value
            judge = GeminiJudge()
            logger.info(f"Invoking GeminiJudge for single track: {title} - {artist}")
            gemini_candidate = build_gemini_candidate(db_metadata, force_fields=["language", "genre"])
            gemini_response = judge.analyze_tracks_batch([gemini_candidate] if gemini_candidate else [])
            
            if isinstance(gemini_response, dict) and gemini_response.get("status") == "error":
                logger.error(f"Gemini single-track analysis failed for {title}: {gemini_response.get('message')}")
            elif gemini_response and getattr(gemini_response, "tracks", None):
                suggestion = gemini_response.tracks[0]
                requested_fields = set(gemini_candidate.get("fields_to_fill") or ["language", "genre"]) if gemini_candidate else set()
                # Apply high confidence suggestions
                if "language" in requested_fields and suggestion.suggested_language.value and suggestion.suggested_language.confidence > 0.6:
                    normalized_language = normalize_language_value(suggestion.suggested_language.value)
                    if normalized_language and normalized_language != "unknown":
                        db_metadata["language"] = normalized_language
                if "genre" in requested_fields and suggestion.suggested_genre.value and suggestion.suggested_genre.confidence > 0.6:
                    normalized_genre = normalize_genre_value(suggestion.suggested_genre.value)
                    if normalized_genre:
                        db_metadata["genre"] = normalized_genre
                logger.info(f"Applied Gemini suggestions: {suggestion.reasoning}")
        except Exception as gemini_err:
            logger.error(f"Gemini API interception failed for {title}: {gemini_err}. Proceeding with original metadata.")
        # --- GEMINI INTERCEPTION END ---
        
        # 7. Update database on Drive. If this fails, clean up the uploaded media.
        try:
            update_result = update_database(drive_file_id, db_metadata)
        except Exception:
            if drive_file_id:
                try:
                    delete_file(drive_file_id)
                    logger.info(f"Deleted uploaded media {drive_file_id} after database update failure.")
                except Exception as cleanup_err:
                    logger.warning(f"Could not delete uploaded media {drive_file_id} after database update failure: {cleanup_err}")
            raise

        if isinstance(update_result, dict) and update_result.get("duplicate") and update_result.get("track_id") != drive_file_id:
            try:
                delete_file(drive_file_id)
                logger.info(f"Deleted duplicate uploaded media {drive_file_id}; existing track is {update_result.get('track_id')}.")
            except Exception as cleanup_err:
                logger.warning(f"Could not delete duplicate uploaded media {drive_file_id}: {cleanup_err}")
        invalidate_db_cache()
        
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
        logger.error(f"Error in background _process_single_song: {e}", exc_info=True)
        background_tasks[task_key]["status"] = "error"
        background_tasks[task_key]["last_error"] = str(e)
    finally:
        # Cleanup temp file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.info(f"Cleaned up temp audio file: {temp_file_path}")
            except Exception as cleanup_err:
                logger.warning(f"Could not remove local temp file {temp_file_path}: {cleanup_err}")
        if background_tasks[task_key].get("status") != "error":
            background_tasks[task_key]["status"] = "idle"


@app.route('/api/add-song', methods=['POST'])
@require_write_auth()
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

    thread = threading.Thread(target=_process_single_song, args=(spotify_url, "single_add", "dashboard_single", None))
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Song download started in background."})


@app.route('/api/app/song/add', methods=['POST'])
@require_write_auth(app_endpoint=True)
def app_add_song():
    """
    POST /api/app/song/add
    App route to add a single song. Pre-fetches metadata to return synchronously, 
    and handles the download in a background thread.
    """
    body = request.json or {}
    spotify_url = body.get('spotify_url')
    device_id = body.get('device_id')
    
    if not spotify_url or not device_id:
        return jsonify({"error": "Missing 'spotify_url' or 'device_id'"}), 400

    task_key = f"app_single_{device_id}"
    
    if task_key not in background_tasks:
        background_tasks[task_key] = {"status": "idle", "started_at": None}
        
    if background_tasks[task_key]["status"] == "running":
        return jsonify({"error": "A track import is already in progress for this device"}), 409

    try:
        metadata = get_track_by_spotify_url(spotify_url)
    except Exception as e:
        logger.error(f"Error fetching metadata for {spotify_url}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 400

    thread = threading.Thread(target=_process_single_song, args=(spotify_url, task_key, "app_single", device_id))
    thread.daemon = True
    thread.start()

    # The prompt requires returning the metadata synchronously
    return jsonify({
        "status": "success", 
        "message": f"Successfully added: {metadata.get('title')}",
        "metadata": metadata
    })


@app.route('/api/app/playlist/start', methods=['POST'])
@require_write_auth(app_endpoint=True)
def app_playlist_start():
    """
    POST /api/app/playlist/start
    App route to start importing a playlist.
    """
    try:
        body = request.json or {}
        url = body.get('url')
        device_id = body.get('device_id')
        if not url or not device_id:
            return jsonify({"error": "Missing 'url' or 'device_id'"}), 400

        task_key = f"playlist_import_{device_id}"
        if task_key not in app_import_tasks:
            app_import_tasks[task_key] = {"status": "idle"}

        if app_import_tasks[task_key].get("status") == "running":
            return jsonify({"error": "A playlist import is already in progress for this device"}), 409

        try:
            playlist_id = start_playlist_import(url, device_id=device_id, imported_via="app")
        except PlaylistAlreadyDownloadedError as e:
            return jsonify({
                "status": "already_downloaded",
                "message": f"Playlist '{e.playlist_name}' is already fully downloaded ({e.total_tracks} tracks).",
                "playlist_id": e.playlist_id,
                "playlist_name": e.playlist_name,
                "total_tracks": e.total_tracks
            }), 200

        app_import_tasks[task_key]["status"] = "running"
        app_import_tasks[task_key]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        app_import_tasks[task_key]["playlist_id"] = playlist_id
        
        def run_playlist_import_wrapper():
            try:
                run_playlist_import(playlist_id, source_override="app_playlist")
            except Exception as e:
                logger.error(f"Error in background playlist import: {e}", exc_info=True)
                from scraper.playlist_importer import active_imports, get_db_file_id
                from dashboard.drive_client import upload_json, search_file_by_name
                state = active_imports.get(playlist_id, {})
                if state.get("status") not in ("cancelled", "completed"):
                    state["status"] = "failed"
                    active_imports[playlist_id] = state
                    db_file_id, parent_id = get_db_file_id()
                    if parent_id:
                        state_filename = f"playlist_import_state_{playlist_id}.json"
                        file_id = search_file_by_name(state_filename, parent_id)
                        if file_id:
                            upload_json(file_id, state, state_filename, parent_id=parent_id)
            finally:
                from scraper.playlist_importer import active_imports
                state = active_imports.get(playlist_id)
                if state and state.get("status") == "cancelled":
                    app_import_tasks[task_key]["status"] = "cancelled"
                elif state and state.get("status") == "failed":
                    app_import_tasks[task_key]["status"] = "failed"
                else:
                    app_import_tasks[task_key]["status"] = "completed"
                app_import_tasks[task_key].pop("thread", None)

        thread = threading.Thread(target=run_playlist_import_wrapper)
        thread.daemon = True
        app_import_tasks[task_key]["thread"] = thread
        thread.start()
        
        # Append clear session marker to scraper.log
        log_path = os.path.join(project_root, 'scraper.log')
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n============================================================\nNEW APP PLAYLIST IMPORT SESSION ({playlist_id}) for {device_id}\n============================================================\n")
        except Exception as log_err:
            logger.warning(f"Could not append session marker to scraper.log: {log_err}")

        return jsonify({"status": "success", "playlist_id": playlist_id, "message": "Playlist import started."})
    except Exception as e:
        logger.error(f"Error in app playlist start: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/app/my-imports', methods=['GET'])
def get_app_my_imports():
    """
    GET /api/app/my-imports?device_id=...
    Returns history of imported tracks for a specific device.
    """
    try:
        device_id = request.args.get('device_id')
        if not device_id:
            return jsonify({"error": "Missing 'device_id'"}), 400
            
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
                
        # Filter for this device
        device_tracks = []
        for t in tracks:
            if t.get('requestedBy') == device_id:
                # Return basic summary stats
                summary = {
                    "title": t.get("title", "Unknown"),
                    "artist": t.get("artist", "Unknown"),
                    "albumArt": t.get("album_art", t.get("albumArt", "")),
                    "addedAt": t.get("addedAt", ""),
                    "source": t.get("source", "app_single")
                }
                device_tracks.append(summary)
                
        return jsonify(device_tracks)
    except Exception as e:
        logger.error(f"Error in GET /api/app/my-imports: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/app/import-stats', methods=['GET'])
def get_app_import_stats():
    """
    GET /api/app/import-stats?device_id=...
    Returns statistics on a user's imports.
    """
    try:
        device_id = request.args.get('device_id')
        if not device_id:
            return jsonify({"error": "Missing 'device_id'"}), 400
            
        db_file_id = get_db_file_id()
        if not db_file_id:
            return jsonify({"total_songs": 0, "total_playlists": 0, "last_import_date": None})
            
        data = download_json(db_file_id)
        
        tracks = []
        if isinstance(data, list):
            tracks = data
        elif isinstance(data, dict):
            if 'tracks' in data and isinstance(data['tracks'], list):
                tracks = data['tracks']
            else:
                tracks = list(data.values())
                
        device_tracks = [t for t in tracks if t.get('requestedBy') == device_id]
        
        total_songs = len(device_tracks)
        
        # We can estimate playlists by counting unique playlist names in "source" if it says "app_playlist" or "Playlist Import"
        playlist_sources = set()
        latest_date = None
        
        for t in device_tracks:
            source = t.get("source", "")
            if source == "app_playlist" or "Playlist Import" in source:
                playlist_sources.add(source)
                
            added_at = t.get("addedAt", "")
            if added_at:
                if not latest_date or added_at > latest_date:
                    latest_date = added_at
                    
        return jsonify({
            "total_songs": total_songs,
            "total_playlists": len(playlist_sources),
            "last_import_date": latest_date
        })
    except Exception as e:
        logger.error(f"Error in GET /api/app/import-stats: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


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
        url = (request.json or {}).get('url')
        if not url:
            return jsonify({"error": "Missing url"}), 400
        preview = get_playlist_preview(url)
        return jsonify(preview)
    except Exception as e:
        logger.error(f"Error in preview: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlist/queue/preview', methods=['POST'])
def playlist_queue_preview():
    try:
        urls = _parse_playlist_urls_payload(request.json)
        if not urls:
            return jsonify({"error": "No valid Spotify playlist URLs found."}), 400
        if len(urls) > MAX_DASHBOARD_PLAYLIST_QUEUE_SIZE:
            return jsonify({"error": f"Only up to {MAX_DASHBOARD_PLAYLIST_QUEUE_SIZE} playlists can be queued at once."}), 400

        previews = []
        ready_count = 0
        total_tracks = 0
        total_already_in_library = 0
        total_new_tracks_importable = 0
        estimated_size_mb = 0

        for index, url in enumerate(urls):
            item = {
                "index": index,
                "url": url,
                "status": "ready",
                "error": None
            }
            try:
                preview = get_playlist_preview(url)
                item.update(preview)
                item["url"] = url
                ready_count += 1
                total_tracks += int(preview.get("tracks_available_for_import") or preview.get("total_tracks") or 0)
                total_already_in_library += int(preview.get("already_in_library") or 0)
                total_new_tracks_importable += int(preview.get("new_tracks_importable") or 0)
                estimated_size_mb += int(preview.get("estimated_size_mb") or 0)
            except Exception as preview_err:
                logger.warning(f"Playlist queue preview failed for {url}: {preview_err}")
                item["status"] = "error"
                item["error"] = str(preview_err)
            previews.append(item)

        return jsonify({
            "status": "success",
            "total": len(previews),
            "ready_count": ready_count,
            "error_count": len(previews) - ready_count,
            "total_tracks": total_tracks,
            "total_already_in_library": total_already_in_library,
            "total_new_tracks_importable": total_new_tracks_importable,
            "estimated_size_mb": estimated_size_mb,
            "estimated_size_display": f"~{estimated_size_mb} MB",
            "previews": previews
        })
    except Exception as e:
        logger.error(f"Error in queue preview: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlist/queue/start', methods=['POST'])
@require_write_auth()
def playlist_queue_start():
    try:
        urls = _parse_playlist_urls_payload(request.json)
        if not urls:
            return jsonify({"error": "No valid Spotify playlist URLs found."}), 400
        if len(urls) > MAX_DASHBOARD_PLAYLIST_QUEUE_SIZE:
            return jsonify({"error": f"Only up to {MAX_DASHBOARD_PLAYLIST_QUEUE_SIZE} playlists can be queued at once."}), 400

        snapshot, running_state = _start_dashboard_playlist_queue(urls)
        if running_state is not None:
            return jsonify({
                "status": "already_running",
                "queue_id": running_state.get("queue_id"),
                "playlist_id": running_state.get("playlist_id"),
                "queue": running_state.get("queue", [])
            }), 409

        return jsonify({
            "status": "success",
            "queue_id": snapshot.get("queue_id"),
            "playlist_id": snapshot.get("playlist_id"),
            "queue_total": snapshot.get("queue_total"),
            "queue": snapshot.get("queue", [])
        })
    except Exception as e:
        logger.error(f"Error starting playlist queue: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/playlist/start', methods=['POST'])
@require_write_auth()
def playlist_start():
    try:
        url = (request.json or {}).get('url')
        if not url:
            return jsonify({"error": "Missing url"}), 400

        normalized_url = _normalize_spotify_playlist_url(url)
        if not normalized_url:
            return jsonify({"error": "Invalid Spotify playlist URL"}), 400

        snapshot, running_state = _start_dashboard_playlist_queue([normalized_url])
        if running_state is not None:
            return jsonify({
                "status": "already_running",
                "queue_id": running_state.get("queue_id"),
                "playlist_id": running_state.get("playlist_id")
            }), 409

        return jsonify({
            "status": "success",
            "queue_id": snapshot.get("queue_id"),
            "playlist_id": snapshot.get("playlist_id"),
            "queue_total": snapshot.get("queue_total"),
            "queue": snapshot.get("queue", [])
        })
    except Exception as e:
        logger.error(f"Error in start: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-library/connection', methods=['GET'])
def spotify_library_connection():
    try:
        check_token = request.args.get("check") in ("1", "true", "yes")
        return jsonify(get_spotify_library_connection_status(check_token=check_token))
    except Exception as e:
        logger.error(f"Error checking Spotify library connection: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-library/auth-url', methods=['GET'])
def spotify_library_auth_url():
    try:
        redirect_uri = (
            request.args.get("redirect_uri")
            or os.environ.get("SPOTIFY_REDIRECT_URI")
            or f"{request.url_root.rstrip('/')}/api/spotify-library/callback"
        )
        return jsonify({
            "auth_url": build_spotify_authorize_url(redirect_uri),
            "redirect_uri": redirect_uri,
            "scopes": ["playlist-read-private", "playlist-read-collaborative", "user-library-read"]
        })
    except Exception as e:
        logger.error(f"Error building Spotify library auth URL: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-library/callback', methods=['GET'])
def spotify_library_callback():
    try:
        error = request.args.get("error")
        if error:
            return jsonify({"error": error}), 400
        code = request.args.get("code")
        redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI") or f"{request.url_root.rstrip('/')}/api/spotify-library/callback"
        token_payload = exchange_spotify_code_for_refresh_token(code, redirect_uri)
        refresh_token = token_payload.get("refresh_token")
        if not refresh_token:
            return jsonify({
                "error": "Spotify did not return a refresh token. Reopen the auth URL with show_dialog=true and try again."
            }), 400
        return jsonify({
            "status": "success",
            "message": "Set this value as SPOTIFY_REFRESH_TOKEN in Render/local environment, then restart the service.",
            "refresh_token": refresh_token
        })
    except Exception as e:
        logger.error(f"Error in Spotify library callback: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-library/preview', methods=['POST'])
def spotify_library_preview():
    try:
        url = (request.json or {}).get('url')
        normalized_url = _normalize_spotify_playlist_url(url)
        if not normalized_url:
            return jsonify({"error": "Invalid Spotify playlist URL"}), 400
        preview = get_spotify_library_playlist_preview(normalized_url)
        preview["url"] = normalized_url
        return jsonify(preview)
    except Exception as e:
        logger.error(f"Error in Spotify Library Importer preview: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-library/diagnose', methods=['POST'])
def spotify_library_diagnose():
    try:
        url = (request.json or {}).get('url')
        normalized_url = _normalize_spotify_playlist_url(url)
        if not normalized_url:
            return jsonify({"error": "Invalid Spotify playlist URL"}), 400
        return jsonify(diagnose_spotify_library_playlist(normalized_url))
    except Exception as e:
        logger.error(f"Error diagnosing Spotify Library Importer access: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-library/start', methods=['POST'])
@require_write_auth()
def spotify_library_start():
    try:
        url = (request.json or {}).get('url')
        normalized_url = _normalize_spotify_playlist_url(url)
        if not normalized_url:
            return jsonify({"error": "Invalid Spotify playlist URL"}), 400

        snapshot, running_state = _start_spotify_library_dashboard_import(normalized_url)
        if running_state is not None:
            return jsonify({
                "status": "already_running",
                "task_id": running_state.get("task_id"),
                "playlist_id": running_state.get("playlist_id")
            }), 409

        return jsonify({
            "status": "success",
            "task_id": snapshot.get("task_id"),
            "playlist_id": snapshot.get("playlist_id")
        })
    except Exception as e:
        logger.error(f"Error starting Spotify Library Importer: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-library/cancel', methods=['POST'])
@require_write_auth()
def spotify_library_cancel():
    try:
        body = request.json or {}
        playlist_id = body.get('playlist_id')
        cancelled_playlist_id = _cancel_spotify_library_dashboard_import(playlist_id)
        return jsonify({
            "status": "success",
            "playlist_id": cancelled_playlist_id,
            "message": "Spotify Library Importer cancellation requested."
        })
    except Exception as e:
        logger.error(f"Error cancelling Spotify Library Importer: {e}", exc_info=True)
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

@app.route('/api/tracks/<path:file_id>/lyrics', methods=['GET'])
def get_track_lyrics(file_id):
    """
    GET /api/tracks/<file_id>/lyrics
    Returns the lyrics, syncedLyrics, and lyricsStatus for a specific track.
    Reuses get_database_cached() to avoid redundant Google Drive downloads.
    """
    try:
        db_data = get_database_cached()
        if not db_data:
            return jsonify({"error": "Database not available"}), 503

        tracks = []
        if isinstance(db_data, list):
            tracks = db_data
        elif isinstance(db_data, dict):
            if 'tracks' in db_data and isinstance(db_data['tracks'], list):
                tracks = db_data['tracks']
            else:
                tracks = list(db_data.values())

        target_track = None
        target_file_id_str = str(file_id).strip()

        for track in tracks:
            if not isinstance(track, dict):
                continue
            tid = str(track.get('id') or track.get('driveFileId') or track.get('file_id') or track.get('spotify_id') or '')
            if tid == target_file_id_str or str(track.get('driveFileId') or '') == target_file_id_str or str(track.get('id') or '') == target_file_id_str:
                target_track = track
                break

        if not target_track:
            return jsonify({"error": f"Track with ID '{file_id}' not found."}), 404

        response = jsonify({
            "lyrics": target_track.get("lyrics"),
            "syncedLyrics": target_track.get("syncedLyrics"),
            "lyricsStatus": target_track.get("lyricsStatus", "ok")
        })
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    except Exception as e:
        logger.error(f"Error in GET /api/tracks/{file_id}/lyrics: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/imported-playlists/details', methods=['GET'])
def get_imported_playlists_details():
    try:
        from scraper.playlist_manager import get_all_playlists

        playlists = get_all_playlists()
        db_file_id = get_db_file_id()
        all_tracks = []
        if db_file_id:
            db_data = get_database_cached()
            if isinstance(db_data, list):
                all_tracks = db_data
            elif isinstance(db_data, dict) and 'tracks' in db_data:
                all_tracks = db_data['tracks']

        track_map = {}
        for track in all_tracks:
            for key in (track.get("driveFileId"), track.get("id")):
                if key:
                    track_map[str(key)] = track

        detailed_playlists = []
        for playlist in playlists:
            track_ids = playlist.get("track_ids", []) or []
            tracks = []
            missing_track_ids = []

            for track_id in track_ids:
                track = track_map.get(str(track_id))
                if not track:
                    missing_track_ids.append(track_id)
                    continue
                tracks.append({
                    "id": track.get("id"),
                    "driveFileId": track.get("driveFileId") or track.get("id"),
                    "title": track.get("title", "Unknown Title"),
                    "artist": track.get("artist", "Unknown Artist"),
                    "album": track.get("album", "Unknown Album"),
                    "genre": track.get("genre", "Unknown"),
                    "language": track.get("language", "unknown"),
                    "duration": track.get("duration", "--:--"),
                    "durationSeconds": track.get("durationSeconds"),
                    "album_art": track.get("album_art"),
                    "albumArt": track.get("albumArt") or track.get("album_art"),
                    "source": track.get("source", "unknown"),
                    "requestedBy": track.get("requestedBy"),
                    "spotify_id": track.get("spotify_id"),
                    "lyricsStatus": track.get("lyricsStatus", "ok"),
                    "timestamp": track.get("timestamp"),
                    "addedAt": track.get("addedAt") or track.get("timestamp")
                })

            if not tracks:
                continue

            cover_candidates = []
            if playlist.get("cover_image"):
                cover_candidates.append(playlist.get("cover_image"))
            for track in tracks:
                art = track.get("albumArt") or track.get("album_art")
                if art and art not in cover_candidates:
                    cover_candidates.append(art)
                if len(cover_candidates) >= 4:
                    break

            detailed_playlists.append({
                "id": playlist.get("id"),
                "name": playlist.get("name", "Untitled Playlist"),
                "source_url": playlist.get("source_url"),
                "cover_image": cover_candidates[0] if cover_candidates else None,
                "cover_collage": cover_candidates[:4],
                "track_ids": track_ids,
                "total_tracks": playlist.get("total_tracks", len(track_ids)),
                "resolved_tracks": len(tracks),
                "missing_track_ids": missing_track_ids,
                "created_at": playlist.get("created_at"),
                "imported_via": playlist.get("imported_via"),
                "requestedBy": playlist.get("requestedBy"),
                "tracks": tracks
            })

        detailed_playlists.sort(key=lambda p: p.get("created_at") or "", reverse=True)
        return jsonify({
            "playlists": detailed_playlists,
            "total_playlists": len(detailed_playlists),
            "total_tracks": sum(p.get("resolved_tracks", 0) for p in detailed_playlists)
        })
    except Exception as e:
        logger.error(f"Error in GET /api/imported-playlists/details: {e}", exc_info=True)
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

@app.route('/api/playlists/<playlist_id>', methods=['DELETE'])
@app.route('/api/playlists/<playlist_id>/delete', methods=['POST', 'DELETE'])
@require_write_auth()
def delete_playlist_route(playlist_id):
    try:
        from scraper.playlist_manager import delete_playlist, get_playlist
        existing = get_playlist(playlist_id)
        if not existing:
            return jsonify({"error": f"Playlist with ID '{playlist_id}' not found."}), 404

        deleted = delete_playlist(playlist_id)
        if not deleted:
            return jsonify({"error": f"Failed to delete playlist '{playlist_id}'."}), 404

        return jsonify({
            "status": "success",
            "message": f"Playlist '{existing.get('name', playlist_id)}' deleted successfully.",
            "playlist_id": playlist_id
        })
    except Exception as e:
        logger.error(f"Error in DELETE /api/playlists/{playlist_id}: {e}", exc_info=True)
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
@require_write_auth()
def playlist_cancel():
    try:
        body = request.json or {}
        playlist_id = body.get('playlist_id')

        cancelled_playlist_id = _cancel_dashboard_playlist_queue(playlist_id)
        return jsonify({
            "status": "success",
            "playlist_id": cancelled_playlist_id,
            "message": "Playlist import cancellation requested."
        })
    except Exception as e:
        logger.error(f"Error in cancel: {e}", exc_info=True)
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


@app.route('/api/backfill/run', methods=['POST'])
@require_write_auth()
def run_backfill_specific():
    if background_tasks["backfill"]["status"] == "running":
        return jsonify({"status": "already_running"}), 400
        
    data = request.json or {}
    btype = data.get("type")
    if not btype:
        return jsonify({"error": "Missing type"}), 400

    backfill_cancel_event.clear()
    background_tasks["backfill"].clear()
    background_tasks["backfill"].update({
        "status": "running",
        "started_at": datetime.datetime.utcnow().isoformat() + 'Z',
        "finished_at": None,
        "type": btype,
        "logs": [{
            "time": datetime.datetime.utcnow().isoformat() + "Z",
            "level": "info",
            "message": f"Starting {btype} backfill."
        }],
        "changelog": [],
        "processed": 0,
        "total_candidates": 0,
        "api_call_count": 0,
        "cancel_requested": False,
        "last_result": None,
        "last_error": None
    })
        
    def run_job():
        try:
            from scraper.main import backfill_album_art, backfill_durations, backfill_languages, backfill_lyrics, run_complete_backfill
            if btype == "album_art":
                result = backfill_album_art(
                    task_state=background_tasks["backfill"],
                    cancel_event=backfill_cancel_event
                )
            elif btype == "duration":
                result = backfill_durations(
                    task_state=background_tasks["backfill"],
                    cancel_event=backfill_cancel_event
                )
            elif btype == "language":
                result = backfill_languages(
                    task_state=background_tasks["backfill"],
                    cancel_event=backfill_cancel_event
                )
            elif btype == "lyrics":
                result = backfill_lyrics(
                    task_state=background_tasks["backfill"],
                    cancel_event=backfill_cancel_event
                )
            elif btype == "all":
                result = run_complete_backfill()
            elif btype == "lyrics_status":
                from scraper.drive_uploader import backfill_lyrics_status
                result = backfill_lyrics_status()
            elif btype == "normalize":
                from scraper.drive_uploader import normalize_database
                result = normalize_database()
            else:
                logger.warning(f"Unknown backfill type: {btype}")
                result = {"status": "error", "message": f"Unknown backfill type: {btype}"}
            background_tasks["backfill"]["last_result"] = result
            if isinstance(result, dict) and result.get("status") == "error":
                level = "error"
            elif isinstance(result, dict) and result.get("status") == "cancelled":
                level = "warning"
            else:
                level = "success"
            background_tasks["backfill"].setdefault("logs", []).append({
                "time": datetime.datetime.utcnow().isoformat() + "Z",
                "level": level,
                "message": (result or {}).get("message") if isinstance(result, dict) and result.get("message") else f"{btype} backfill finished."
            })
        except Exception as e:
            logger.error(f"Backfill job {btype} failed: {e}")
            background_tasks["backfill"]["last_error"] = str(e)
            background_tasks["backfill"].setdefault("logs", []).append({
                "time": datetime.datetime.utcnow().isoformat() + "Z",
                "level": "error",
                "message": f"Backfill job failed: {e}"
            })
        finally:
            background_tasks["backfill"]["status"] = "cancelled" if backfill_cancel_event.is_set() else "idle"
            background_tasks["backfill"]["finished_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
            
    thread = threading.Thread(target=run_job)
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})

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
        "album_art": {"missing": 0, "present": 0, "total": len(tracks)},
        "duration": {"missing": 0, "present": 0, "total": len(tracks)},
        "lyrics": {"missing": 0, "present": 0, "synced_present": 0, "total": len(tracks)},
        "language": {"missing": 0, "present": 0, "total": len(tracks)},
        "running": background_tasks["backfill"].copy()
    }
    
    for track in tracks:
        art = track.get("album_art") or track.get("albumArt")
        if art is None or art == "":
            status["album_art"]["missing"] += 1
        else:
            status["album_art"]["present"] += 1
            
        if track.get("durationSeconds") is None or track.get("duration") == "--:--":
            status["duration"]["missing"] += 1
        else:
            status["duration"]["present"] += 1

        if track.get("lyrics") or track.get("syncedLyrics"):
            status["lyrics"]["present"] += 1
        else:
            status["lyrics"]["missing"] += 1
        if track.get("syncedLyrics"):
            status["lyrics"]["synced_present"] += 1
            
        lang = track.get("language")
        if lang is None or lang == "Unknown" or lang == "unknown" or lang == "":
            status["language"]["missing"] += 1
        else:
            status["language"]["present"] += 1
            
    return jsonify(status)

@app.route('/api/backfill/full-enrichment', methods=['POST'])
@require_write_auth()
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
        task_logs = background_tasks.get("backfill", {}).get("logs") or []
        if task_logs:
            return jsonify(task_logs[-100:])

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
    with playlist_queue_lock:
        playlist_id = background_tasks["playlist_import"].get("playlist_id")
    with spotify_library_import_lock:
        spotify_library_playlist_id = background_tasks["spotify_library_import"].get("playlist_id")

    if playlist_id:
        try:
            from scraper.playlist_importer import active_imports
            state = active_imports.get(playlist_id)
            if not state:
                state = get_playlist_status(playlist_id)
            if state and state.get("status") != "not_found":
                with playlist_queue_lock:
                    pl_state = background_tasks["playlist_import"]
                    if pl_state.get("playlist_id") == playlist_id:
                        _apply_playlist_import_state_to_queue_item_unlocked(pl_state, state)
        except Exception as e:
            logger.warning(f"Failed to fetch playlist status from Drive: {e}")

    if spotify_library_playlist_id:
        try:
            from scraper.playlist_importer import active_imports
            state = active_imports.get(spotify_library_playlist_id)
            if not state:
                state = get_playlist_status(spotify_library_playlist_id)
            if state and state.get("status") != "not_found":
                with spotify_library_import_lock:
                    task_state = background_tasks["spotify_library_import"]
                    if task_state.get("playlist_id") == spotify_library_playlist_id:
                        _apply_spotify_library_import_state_unlocked(task_state, state)
        except Exception as e:
            logger.warning(f"Failed to fetch Spotify library import status from Drive: {e}")
                
    clean_tasks = {}
    for task_name, state_dict in background_tasks.items():
        if task_name == "playlist_import":
            with playlist_queue_lock:
                clean_state = _copy_playlist_task_state_unlocked()
                thread = background_tasks["playlist_import"].get("thread")
                if thread:
                    clean_state["is_alive"] = thread.is_alive()
        elif task_name == "spotify_library_import":
            with spotify_library_import_lock:
                clean_state = _copy_spotify_library_task_state_unlocked()
                thread = background_tasks["spotify_library_import"].get("thread")
                if thread:
                    clean_state["is_alive"] = thread.is_alive()
        else:
            clean_state = {k: v for k, v in state_dict.items() if k != "thread"}
            if "thread" in state_dict:
                clean_state["is_alive"] = state_dict["thread"].is_alive()
        clean_tasks[task_name] = clean_state
        
    return jsonify(clean_tasks)


SPOTIFY_IMPORT_PATTERNS = {
    "song": re.compile(
        r"^(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?track/|spotify:track:)([A-Za-z0-9]+)",
        re.IGNORECASE,
    ),
    "playlist": re.compile(
        r"^(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?playlist/|spotify:playlist:)([A-Za-z0-9]+)",
        re.IGNORECASE,
    ),
}


def _canonical_import_url(raw_url, job_type):
    pattern = SPOTIFY_IMPORT_PATTERNS.get(job_type)
    match = pattern.match(str(raw_url or "").strip()) if pattern else None
    if not match:
        return None
    resource = "track" if job_type == "song" else "playlist"
    return f"https://open.spotify.com/{resource}/{match.group(1)}"


@app.route('/api/import/request', methods=['POST'])
@require_write_auth(app_endpoint=True)
def request_import_job():
    body = request.json or {}
    job_type = str(body.get("type") or "").strip().lower()
    canonical_url = _canonical_import_url(body.get("url"), job_type)
    if job_type not in ("song", "playlist"):
        return jsonify({"error": "type must be 'song' or 'playlist'"}), 400
    if not canonical_url:
        return jsonify({"error": f"Invalid Spotify {job_type} URL"}), 400

    try:
        job = create_import_job(
            canonical_url,
            job_type,
            requested_by=body.get("requested_by"),
        )
        return jsonify({"job_id": job["job_id"], "status": job["status"]}), 201
    except Exception as e:
        logger.error(f"Could not create import job: {e}", exc_info=True)
        return jsonify({"error": "Could not persist import job"}), 500


@app.route('/api/import/status/<job_id>', methods=['GET'])
def import_job_status(job_id):
    try:
        return jsonify(get_import_job(job_id))
    except ImportJobNotFound:
        return jsonify({"error": "Import job not found"}), 404
    except Exception as e:
        logger.error(f"Could not read import job {job_id}: {e}", exc_info=True)
        return jsonify({"error": "Could not read import job"}), 500


@app.route('/api/worker/next', methods=['GET'])
@require_worker_auth
def worker_next_import_job():
    try:
        return jsonify({"job": claim_next_import_job()})
    except Exception as e:
        logger.error(f"Could not claim the next import job: {e}", exc_info=True)
        return jsonify({"error": "Could not claim import job"}), 500


@app.route('/api/worker/result', methods=['POST'])
@require_worker_auth
def worker_import_result():
    body = request.json or {}
    job_id = str(body.get("job_id") or "").strip()
    status = str(body.get("status") or "").strip().lower()
    result = body.get("result")
    error = body.get("error")
    if not job_id:
        return jsonify({"error": "Missing job_id"}), 400
    if status not in ("completed", "failed"):
        return jsonify({"error": "status must be 'completed' or 'failed'"}), 400
    if result is not None and not isinstance(result, dict):
        return jsonify({"error": "result must be an object or null"}), 400

    try:
        job = set_import_job_result(job_id, status, result=result, error=error)
        return jsonify({"job_id": job_id, "status": job["status"]})
    except ImportJobNotFound:
        return jsonify({"error": "Import job not found"}), 404
    except ImportJobConflict as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        logger.error(f"Could not update import job {job_id}: {e}", exc_info=True)
        return jsonify({"error": "Could not update import job"}), 500

@app.route('/ping', methods=['GET'])
def ping():
    import datetime
    return jsonify({
        "status": "alive",
        "timestamp": datetime.datetime.utcnow().isoformat() + 'Z'
    })


@app.route('/api/tools/test-alert', methods=['POST'])
@require_write_auth()
def test_discord_alert():
    body = request.json or {}
    msg = body.get("message", "This is a test notification from Cloud Music Player.")
    title = body.get("title", "Test Alert")
    level = body.get("level", "info")
    webhook_configured = bool(os.environ.get("DISCORD_ALERT_WEBHOOK_URL"))
    sent = send_alert(title, msg, level=level)
    return jsonify({
        "status": "ok",
        "webhook_configured": webhook_configured,
        "sent": sent,
        "message": "Alert sent successfully" if sent else ("Webhook URL not configured" if not webhook_configured else "Failed to send alert")
    })


@app.errorhandler(500)
def handle_internal_server_error(e):
    logger.error(f"Unhandled 500 internal server error on {request.method} {request.path}: {e}", exc_info=True)
    try:
        error_msg = str(getattr(e, "original_exception", e))
        send_alert(
            f"HTTP 500: {request.method} {request.path}",
            f"Endpoint: {request.method} {request.path}\nError: {error_msg}",
            level="error"
        )
    except Exception as alert_err:
        logger.warning(f"Error handler alert failed: {alert_err}")
    return jsonify({"error": "Internal Server Error"}), 500


@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if e.code == 500:
            return handle_internal_server_error(e)
        return jsonify({"error": e.description}), e.code

    logger.error(f"Uncaught exception on {request.method} {request.path}: {e}", exc_info=True)
    try:
        send_alert(
            f"Unhandled Exception: {request.method} {request.path}",
            f"Endpoint: {request.method} {request.path}\nType: {type(e).__name__}\nError: {e}",
            level="error"
        )
    except Exception as alert_err:
        logger.warning(f"Error handler alert failed: {alert_err}")
    return jsonify({"error": "Internal Server Error"}), 500


# ==============================================================================
# APP INTEGRATION ROUTES (PART 1 & 2)
# ==============================================================================
import uuid
app_import_tasks = {}



@app.route('/api/app/song/status', methods=['GET'])
def app_song_status():
    task_id = request.args.get('taskId')
    if not task_id:
        return jsonify({"error": "Missing taskId"}), 400
    
    if task_id not in app_import_tasks:
        return jsonify({"error": "Task not found"}), 404
        
    state_dict = app_import_tasks[task_id]
    clean_state = {k: v for k, v in state_dict.items() if k != "thread"}
    if "thread" in state_dict:
        clean_state["is_alive"] = state_dict["thread"].is_alive()
        
    return jsonify(clean_state)



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
