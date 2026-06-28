import os
import re

def fix_1():
    print("Applying Fix 1...")
    with open('dashboard/app.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('data = get_database_cached()', 'data = download_json(db_file_id)')
    with open('dashboard/app.py', 'w', encoding='utf-8') as f:
        f.write(content)

def fix_2():
    print("Applying Fix 2...")
    with open('scraper/main.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('from scraper.drive_uploader import upload_track, update_database, get_db_file_id', 
                              'from scraper.drive_uploader import upload_track, update_database, get_db_file_id, fetch_album_art')
    with open('scraper/main.py', 'w', encoding='utf-8') as f:
        f.write(content)

def fix_3():
    print("Applying Fix 3...")
    with open('dashboard/app.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    route_code = '''
@app.route('/api/backfill/run', methods=['POST'])
def run_backfill_specific():
    if background_tasks["backfill"]["status"] == "running":
        return jsonify({"status": "already_running"}), 400
        
    data = request.json or {}
    btype = data.get("type")
    if not btype:
        return jsonify({"error": "Missing type"}), 400
        
    def run_job():
        background_tasks["backfill"]["status"] = "running"
        background_tasks["backfill"]["started_at"] = datetime.datetime.utcnow().isoformat() + 'Z'
        background_tasks["backfill"]["type"] = btype
        try:
            from scraper.main import backfill_album_art, backfill_durations, backfill_languages, run_complete_backfill
            if btype == "album_art":
                backfill_album_art()
            elif btype == "duration":
                backfill_durations()
            elif btype == "language":
                backfill_languages()
            elif btype == "all":
                run_complete_backfill()
            else:
                logger.warning(f"Unknown backfill type: {btype}")
        except Exception as e:
            logger.error(f"Backfill job {btype} failed: {e}")
        finally:
            background_tasks["backfill"]["status"] = "idle"
            
    import threading
    import datetime
    thread = threading.Thread(target=run_job)
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})
'''
    if '/api/backfill/run' not in content:
        content = content.replace('@app.route(\'/api/backfill/status\', methods=[\'GET\'])', route_code + '\n@app.route(\'/api/backfill/status\', methods=[\'GET\'])')
        with open('dashboard/app.py', 'w', encoding='utf-8') as f:
            f.write(content)

def fix_4():
    print("Applying Fix 4...")
    # scraper/main.py
    with open('scraper/main.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('"syncedLyrics": enriched.get("syncedLyrics")', '"syncedLyrics": enriched.get("syncedLyrics"),\n                "lyricsStatus": enriched.get("lyricsStatus", "ok")')
    with open('scraper/main.py', 'w', encoding='utf-8') as f:
        f.write(content)

    # scraper/playlist_importer.py
    with open('scraper/playlist_importer.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('"syncedLyrics": enriched.get("syncedLyrics")', '"syncedLyrics": enriched.get("syncedLyrics"),\n                        "lyricsStatus": enriched.get("lyricsStatus", "ok")')
    with open('scraper/playlist_importer.py', 'w', encoding='utf-8') as f:
        f.write(content)

    # dashboard/app.py
    with open('dashboard/app.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('"syncedLyrics": enriched.get("syncedLyrics")', '"syncedLyrics": enriched.get("syncedLyrics"),\n            "lyricsStatus": enriched.get("lyricsStatus", "ok")')
    with open('dashboard/app.py', 'w', encoding='utf-8') as f:
        f.write(content)

def fix_5():
    print("Applying Fix 5...")
    with open('scraper/drive_uploader.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    new_logic = '''
        from scraper.metadata_enricher import detect_script_mixing
        for track in tracks:
            old_status = track.get('lyricsStatus')
            lyrics = track.get('lyrics') or ""
            new_status = "needs_review" if detect_script_mixing(lyrics) else "ok"
            if old_status != new_status:
                track['lyricsStatus'] = new_status
                tracks_changed += 1
'''
    old_logic = '''
        for track in tracks:
            if 'lyricsStatus' not in track:
                track['lyricsStatus'] = "ok"
                tracks_changed += 1
'''
    if old_logic in content:
        content = content.replace(old_logic, new_logic)
        with open('scraper/drive_uploader.py', 'w', encoding='utf-8') as f:
            f.write(content)
    else:
        print("old_logic not found in drive_uploader.py")

if __name__ == "__main__":
    fix_1()
    fix_2()
    fix_3()
    fix_4()
    fix_5()
    print("Done")
