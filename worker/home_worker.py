"""Single-job Wavify import relay for a trusted home PC."""

import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from dashboard.drive_client import delete_file, download_json
from scraper.downloader import download_track
from scraper.drive_uploader import get_db_file_id, update_database, upload_track
from scraper.metadata_enricher import enrich_track_metadata
from scraper.playlist_importer import (
    get_playlist_status,
    run_playlist_import,
    start_playlist_import,
    PlaylistAlreadyDownloadedError,
)
from scraper.spotify_charts import get_track_by_spotify_url
from scraper.state_manager import is_duplicate, load_state, save_state
from scraper.track_utils import find_existing_track


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - home_worker - %(levelname)s - %(message)s",
)
logger = logging.getLogger("home_worker")

WORKER_TEMP_SUFFIXES = (".part", ".opus", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".mp4")


def _temp_dir():
    return os.environ.get("TEMP_DIR") or os.path.join(PROJECT_ROOT, "temp")


def _temp_snapshot():
    temp_dir = _temp_dir()
    os.makedirs(temp_dir, exist_ok=True)
    return {entry.name for entry in os.scandir(temp_dir) if entry.is_file()}


def _cleanup_new_temp_files(before):
    temp_dir = _temp_dir()
    for entry in os.scandir(temp_dir):
        if not entry.is_file() or entry.name in before:
            continue
        if not entry.name.lower().endswith(WORKER_TEMP_SUFFIXES):
            continue
        try:
            os.remove(entry.path)
        except OSError as cleanup_error:
            logger.warning("Could not remove worker temporary file %s: %s", entry.name, cleanup_error)


def _existing_tracks():
    db_file_id, _ = get_db_file_id()
    if not db_file_id:
        return []
    data = download_json(db_file_id)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("tracks"), list):
        return data["tracks"]
    return []


def process_song(job):
    metadata = get_track_by_spotify_url(job["url"])
    title = metadata["title"]
    artist = metadata["artist"]
    spotify_id = metadata.get("spotify_id")
    requested_by = job.get("requested_by")
    tracks = _existing_tracks()
    scraper_state = load_state()
    candidate = {"title": title, "artist": artist, "spotify_id": spotify_id}

    if is_duplicate(candidate, scraper_state, tracks):
        existing, _ = find_existing_track(tracks, candidate)
        return {
            "type": "song",
            "title": title,
            "artist": artist,
            "driveFileId": (existing or {}).get("driveFileId") or (existing or {}).get("id"),
            "skipped_duplicate": True,
        }

    temp_dir = _temp_dir()
    os.makedirs(temp_dir, exist_ok=True)
    local_path = None
    uploaded_id = None
    try:
        local_path = download_track(title, artist, temp_dir, track_id=spotify_id)
        enriched = enrich_track_metadata(
            title,
            artist,
            local_file_path=local_path,
            source="home_relay_song",
        )
        uploaded_id = upload_track(local_path)
        db_metadata = {
            "title": title,
            "artist": artist,
            "album": enriched.get("album", "Single"),
            "genre": enriched.get("genre", metadata.get("genre", "Unknown")),
            "duration": enriched.get("duration", "--:--"),
            "durationSeconds": enriched.get("durationSeconds"),
            "spotify_id": spotify_id,
            "album_art": enriched.get("album_art") or metadata.get("album_art"),
            "language": enriched.get("language", metadata.get("language", "unknown")),
            "source": "home_relay_song",
            "requestedBy": requested_by,
            "lyrics": enriched.get("lyrics"),
            "syncedLyrics": enriched.get("syncedLyrics"),
            "lyricsStatus": enriched.get("lyricsStatus", "ok"),
        }

        try:
            update_result = update_database(uploaded_id, db_metadata)
        except Exception:
            try:
                delete_file(uploaded_id)
            except Exception as cleanup_error:
                logger.warning("Could not delete uploaded media after database failure: %s", cleanup_error)
            uploaded_id = None
            raise

        final_drive_id = uploaded_id
        if isinstance(update_result, dict) and update_result.get("duplicate"):
            final_drive_id = update_result.get("track_id") or uploaded_id
            if uploaded_id and final_drive_id != uploaded_id:
                try:
                    delete_file(uploaded_id)
                except Exception as cleanup_error:
                    logger.warning("Could not delete duplicate uploaded media: %s", cleanup_error)
                uploaded_id = None

        latest_state = load_state()
        if spotify_id and spotify_id not in latest_state.setdefault("downloaded_ids", []):
            latest_state["downloaded_ids"].append(spotify_id)
        title_key = f"{title} {artist}"
        if title_key not in latest_state.setdefault("downloaded_titles", []):
            latest_state["downloaded_titles"].append(title_key)
        save_state(latest_state)

        return {
            "type": "song",
            "title": title,
            "artist": artist,
            "driveFileId": final_drive_id,
            "skipped_duplicate": False,
        }
    finally:
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError as cleanup_error:
                logger.warning("Could not remove temporary audio file: %s", cleanup_error)


def process_playlist(job):
    try:
        playlist_id = start_playlist_import(
            job["url"],
            device_id=job.get("requested_by"),
            imported_via="home_worker",
        )
    except PlaylistAlreadyDownloadedError as e:
        logger.info("Playlist is already fully downloaded: %s", e)
        return {
            "type": "playlist",
            "playlist_id": e.playlist_id,
            "playlist_name": e.playlist_name,
            "status": "already_downloaded",
            "processed": e.total_tracks,
            "downloaded": 0,
            "skipped": e.total_tracks,
            "failed": 0,
            "total_tracks": e.total_tracks,
        }

    run_playlist_import(playlist_id, source_override="home_relay_playlist")
    state = get_playlist_status(playlist_id)
    if state.get("status") == "failed":
        raise RuntimeError(state.get("error") or "Playlist import failed")
    return {
        "type": "playlist",
        "playlist_id": playlist_id,
        "playlist_name": state.get("playlist_name"),
        "status": state.get("status"),
        "processed": state.get("processed", 0),
        "downloaded": state.get("downloaded", 0),
        "skipped": state.get("skipped", 0),
        "failed": state.get("failed", 0),
    }


def process_job(job):
    if job.get("type") == "song":
        return process_song(job)
    if job.get("type") == "playlist":
        return process_playlist(job)
    raise ValueError(f"Unsupported import job type: {job.get('type')}")


class HomeWorker:
    def __init__(self):
        base_url = os.environ.get("RENDER_BASE_URL", "").strip().rstrip("/")
        token = os.environ.get("WAVIFY_WORKER_TOKEN", "").strip()
        if not base_url:
            raise RuntimeError("RENDER_BASE_URL is required.")
        if not base_url.lower().startswith("https://") and "localhost" not in base_url.lower():
            raise RuntimeError("RENDER_BASE_URL must use HTTPS (localhost is allowed for testing).")
        if not token:
            raise RuntimeError("WAVIFY_WORKER_TOKEN is required.")

        self.base_url = base_url
        self.poll_interval = max(1, int(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "10")))
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def next_job(self):
        response = self.session.get(f"{self.base_url}/api/worker/next", timeout=30)
        response.raise_for_status()
        return response.json().get("job")

    def report_until_accepted(self, payload):
        while True:
            try:
                response = self.session.post(
                    f"{self.base_url}/api/worker/result",
                    json=payload,
                    timeout=30,
                )
                response.raise_for_status()
                return
            except requests.RequestException as error:
                logger.error("Could not report job result; retrying in %s seconds: %s", self.poll_interval, error)
                time.sleep(self.poll_interval)

    def run_forever(self):
        logger.info("Home import worker started; polling %s", self.base_url)
        while True:
            try:
                job = self.next_job()
                if not job:
                    time.sleep(self.poll_interval)
                    continue

                job_id = job.get("job_id")
                logger.info("Processing %s job %s", job.get("type"), job_id)
                temp_before = _temp_snapshot()
                try:
                    result = process_job(job)
                    payload = {
                        "job_id": job_id,
                        "status": "completed",
                        "result": result,
                        "error": None,
                    }
                    logger.info("Job %s completed locally", job_id)
                except Exception as error:
                    logger.exception("Job %s failed", job_id)
                    payload = {
                        "job_id": job_id,
                        "status": "failed",
                        "result": None,
                        "error": str(error),
                    }
                finally:
                    _cleanup_new_temp_files(temp_before)
                self.report_until_accepted(payload)
            except requests.RequestException as error:
                logger.warning("Render is unavailable; retrying in %s seconds: %s", self.poll_interval, error)
                time.sleep(self.poll_interval)
            except Exception:
                logger.exception("Unexpected worker loop error; retrying in %s seconds", self.poll_interval)
                time.sleep(self.poll_interval)


if __name__ == "__main__":
    try:
        HomeWorker().run_forever()
    except KeyboardInterrupt:
        logger.info("Home import worker stopped")
    except Exception as startup_error:
        logger.error("Home import worker could not start: %s", startup_error)
        raise SystemExit(1)
