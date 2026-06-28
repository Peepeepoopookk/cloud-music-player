import time
import os

with open('dashboard/app.py', 'r', encoding='utf-8') as f:
    content = f.read()

cache_code = """
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
"""

content = content.replace('db_file_id_cache = None\n', 'db_file_id_cache = None\n' + cache_code + '\n')
content = content.replace('download_json(db_file_id)', 'get_database_cached()')
content = content.replace("upload_json(db_file_id, db_data, 'database.json')", "upload_json(db_file_id, db_data, 'database.json')\n        invalidate_db_cache()")
content = content.replace('update_database(drive_file_id, db_metadata)', 'update_database(drive_file_id, db_metadata)\n        invalidate_db_cache()')
content = content.replace('normalize_database()', 'normalize_database()\n        invalidate_db_cache()')
content = content.replace('backfill_lyrics_status()', 'backfill_lyrics_status()\n        invalidate_db_cache()')
content = content.replace('restore_database_backup(file_id)', 'restore_database_backup(file_id)\n        invalidate_db_cache()')

with open('dashboard/app.py', 'w', encoding='utf-8') as f:
    f.write(content)
