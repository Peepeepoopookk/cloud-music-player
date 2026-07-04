import contextlib
import datetime
import os
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_DIR = os.path.join(PROJECT_ROOT, "temp")


@contextlib.contextmanager
def library_write_lock(name="library", timeout=120, stale_after=900):
    """
    Lightweight filesystem lock for Drive JSON write operations.
    It prevents overlapping Flask threads and local scraper subprocesses from
    rewriting database/state files at the same time on the same host.

    Limitation: this is a filesystem lock, not a distributed lock. It only
    coordinates processes that share this workspace/temp directory; it does not
    protect multiple Render instances, containers, or hosts.
    """
    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, f"{name}.lock")
    deadline = time.monotonic() + timeout
    fd = None

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = f"{os.getpid()} {datetime.datetime.utcnow().isoformat()}Z\n"
            os.write(fd, payload.encode("utf-8"))
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > stale_after:
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue

            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for library write lock: {lock_path}")
            time.sleep(0.25)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
