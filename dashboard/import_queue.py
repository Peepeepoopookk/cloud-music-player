"""Small persistent import queue stored alongside database.json in Google Drive."""

import datetime
import uuid

from dashboard.drive_client import download_json, search_file_by_name, upload_json
from scraper.drive_uploader import get_db_file_id
from scraper.operation_lock import library_write_lock


QUEUE_FILENAME = "import_jobs.json"
TERMINAL_STATUSES = {"completed", "failed"}


class ImportJobNotFound(KeyError):
    pass


class ImportJobConflict(RuntimeError):
    pass


def _utc_now():
    return datetime.datetime.utcnow().isoformat() + "Z"


def _queue_location():
    _, parent_id = get_db_file_id()
    if not parent_id:
        raise RuntimeError("Could not determine the Google Drive database folder for import jobs.")
    return parent_id, search_file_by_name(QUEUE_FILENAME, parent_id)


def _load_jobs_unlocked(parent_id, file_id):
    if not file_id:
        return []
    data = download_json(file_id)
    if not isinstance(data, list):
        raise RuntimeError(f"{QUEUE_FILENAME} must contain a JSON array.")
    return data


def _save_jobs_unlocked(parent_id, file_id, jobs):
    return upload_json(file_id, jobs, QUEUE_FILENAME, parent_id=parent_id)


def create_import_job(url, job_type, requested_by=None):
    now = _utc_now()
    job = {
        "job_id": str(uuid.uuid4()),
        "url": url,
        "type": job_type,
        "requested_by": requested_by,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }

    with library_write_lock("import_jobs"):
        parent_id, file_id = _queue_location()
        jobs = _load_jobs_unlocked(parent_id, file_id)
        jobs.append(job)
        _save_jobs_unlocked(parent_id, file_id, jobs)
    return job


def get_import_job(job_id):
    parent_id, file_id = _queue_location()
    jobs = _load_jobs_unlocked(parent_id, file_id)
    for job in jobs:
        if job.get("job_id") == job_id:
            return job
    raise ImportJobNotFound(job_id)


def claim_next_import_job():
    """Claim the oldest pending job. This relay intentionally supports one worker."""
    with library_write_lock("import_jobs"):
        parent_id, file_id = _queue_location()
        jobs = _load_jobs_unlocked(parent_id, file_id)
        pending = [job for job in jobs if job.get("status") == "pending"]
        if not pending:
            return None

        job = min(pending, key=lambda item: item.get("created_at") or "")
        now = _utc_now()
        job["status"] = "processing"
        job["started_at"] = now
        job["updated_at"] = now
        job["error"] = None
        _save_jobs_unlocked(parent_id, file_id, jobs)
        return job


def set_import_job_result(job_id, status, result=None, error=None):
    if status not in TERMINAL_STATUSES:
        raise ValueError("Worker result status must be 'completed' or 'failed'.")

    with library_write_lock("import_jobs"):
        parent_id, file_id = _queue_location()
        jobs = _load_jobs_unlocked(parent_id, file_id)
        job = next((item for item in jobs if item.get("job_id") == job_id), None)
        if not job:
            raise ImportJobNotFound(job_id)

        # Result reporting is idempotent so the home worker can safely retry HTTPS.
        if job.get("status") in TERMINAL_STATUSES:
            if job.get("status") == status:
                return job
            raise ImportJobConflict(f"Job {job_id} is already {job.get('status')}.")
        if job.get("status") != "processing":
            raise ImportJobConflict(f"Job {job_id} is not processing.")

        now = _utc_now()
        job["status"] = status
        job["result"] = result if status == "completed" else None
        job["error"] = str(error) if error else None
        job["updated_at"] = now
        job["finished_at"] = now
        _save_jobs_unlocked(parent_id, file_id, jobs)
        return job
