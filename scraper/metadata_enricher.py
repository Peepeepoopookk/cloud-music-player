import os
import time
import requests
import urllib.parse
import difflib
import logging

from scraper.album_art_resolver import find_itunes_track_metadata, resolve_album_art
from scraper.spotify_charts import detect_track_language
from scraper.utils import extract_duration

logger = logging.getLogger(__name__)

def fetch_with_retry(url, params=None, headers=None, timeout=5, retries=1, delay=1.0):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            elif attempt < retries:
                time.sleep(delay)
            else:
                logger.warning(f"Failed to fetch {url} after {retries+1} attempts. HTTP {r.status_code}")
                return None
        except Exception as e:
            if attempt < retries:
                time.sleep(delay)
            else:
                logger.warning(f"Exception fetching {url} after {retries+1} attempts: {e}")
                return None

def fetch_lrclib_lyrics(title, artist, album, duration_seconds):
    """
    Attempts to fetch lyrics from lrclib.net.
    First tries the exact match `get?` endpoint. If it fails, falls back to `search?`
    and does a fuzzy match.
    """
    headers = {
        "User-Agent": "CloudMusicPlayer/1.0.0 (contact@example.com)",
        "Accept": "application/json"
    }
    
    # 1. Try exact match
    get_url = "https://lrclib.net/api/get"
    params = {
        "artist_name": artist,
        "track_name": title,
        "album_name": album,
        "duration": duration_seconds
    }
    logger.info(f"metadata_enricher: Trying exact lrclib get for '{title}' by '{artist}' (duration: {duration_seconds}s)")
    data = fetch_with_retry(get_url, params=params, headers=headers, timeout=3, retries=0)
    
    if data and isinstance(data, dict) and (data.get("plainLyrics") or data.get("syncedLyrics")):
        logger.info(f"metadata_enricher: Found exact match lyrics on lrclib.")
        return data.get("plainLyrics"), data.get("syncedLyrics")
        
    # 2. Fall back to search
    logger.info(f"metadata_enricher: Exact match failed. Falling back to search for '{title}' by '{artist}'")
    search_url = "https://lrclib.net/api/search"
    search_params = {
        "track_name": title,
        "artist_name": artist
    }
    results = fetch_with_retry(search_url, params=search_params, headers=headers, timeout=3, retries=0)
    
    if results and isinstance(results, list) and len(results) > 0:
        norm_title = title.lower()
        norm_artist = artist.lower()
        
        best_match = None
        best_score = -1.0
        
        for item in results:
            item_title = (item.get("trackName") or "").lower()
            item_artist = (item.get("artistName") or "").lower()
            
            title_ratio = difflib.SequenceMatcher(None, norm_title, item_title).ratio()
            artist_ratio = difflib.SequenceMatcher(None, norm_artist, item_artist).ratio()
            score = title_ratio + artist_ratio
            
            if score > best_score:
                best_score = score
                best_match = item
                
        # Only accept if reasonably close
        if best_match and best_score > 1.2:
            logger.info(f"metadata_enricher: Found fuzzy match lyrics on lrclib (score: {best_score:.2f})")
            return best_match.get("plainLyrics"), best_match.get("syncedLyrics")
            
    logger.info("metadata_enricher: No lyrics found on lrclib.")
    return None, None

def detect_script_language_from_lyrics(lyrics_text):
    if not lyrics_text:
        return None
        
    counts = {
        "hindi": 0,
        "tamil": 0,
        "malayalam": 0,
        "telugu": 0,
        "kannada": 0,
        "latin": 0,
        "total_non_space": 0
    }
    
    for char in lyrics_text:
        if char.isspace():
            continue
            
        code = ord(char)
        counts["total_non_space"] += 1
        
        if 0x0900 <= code <= 0x097F:
            counts["hindi"] += 1
        elif 0x0B80 <= code <= 0x0BFF:
            counts["tamil"] += 1
        elif 0x0D00 <= code <= 0x0D7F:
            counts["malayalam"] += 1
        elif 0x0C00 <= code <= 0x0C7F:
            counts["telugu"] += 1
        elif 0x0C80 <= code <= 0x0CFF:
            counts["kannada"] += 1
        elif (0x0041 <= code <= 0x005A) or (0x0061 <= code <= 0x007A) or (0x00C0 <= code <= 0x024F):
            counts["latin"] += 1
            
    if counts["total_non_space"] == 0:
        return None
        
    for lang in ["hindi", "tamil", "malayalam", "telugu", "kannada"]:
        if counts[lang] / counts["total_non_space"] > 0.15:
            return lang
            
    if counts["latin"] / counts["total_non_space"] > 0.5:
        return "english"
        
    return None

def detect_script_mixing(lyrics_text):
    if not lyrics_text:
        return False
        
    counts = {
        "malayalam": 0,
        "tamil": 0,
        "telugu": 0,
        "devanagari": 0,
        "kannada": 0
    }
    
    total_valid_chars = 0
    
    for char in lyrics_text:
        if char.isspace() or char in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":
            continue
            
        code = ord(char)
        total_valid_chars += 1
        
        if 0x0D00 <= code <= 0x0D7F:
            counts["malayalam"] += 1
        elif 0x0B80 <= code <= 0x0BFF:
            counts["tamil"] += 1
        elif 0x0C00 <= code <= 0x0C7F:
            counts["telugu"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0C80 <= code <= 0x0CFF:
            counts["kannada"] += 1
            
    if total_valid_chars == 0:
        return False
        
    scripts_above_threshold = 0
    for count in counts.values():
        if (count / total_valid_chars) > 0.08:
            scripts_above_threshold += 1
            
    return scripts_above_threshold >= 2

def enrich_track_metadata(title, artist, local_file_path=None, source="unknown"):
    """
    Master metadata enrichment function.
    Returns a dict with: album_art, duration, durationSeconds, language, genre, album, lyrics, syncedLyrics.
    """
    logger.info(f"metadata_enricher: Enriching metadata for '{title}' by '{artist}' (source: {source})")
    
    metadata = {
        "album_art": None,
        "duration": "--:--",
        "durationSeconds": None,
        "language": "unknown",
        "genre": "Unknown",
        "album": "Unknown Album",
        "lyrics": None,
        "syncedLyrics": None,
        "lyricsStatus": "ok"
    }
    
    # 1. Duration (Local ffprobe priority)
    duration_filled = False
    if local_file_path and os.path.exists(local_file_path):
        d_str, d_sec = extract_duration(local_file_path)
        if d_sec:
            metadata["duration"] = d_str
            metadata["durationSeconds"] = d_sec
            duration_filled = True
            logger.info(f"metadata_enricher: Duration extracted via ffprobe: {d_str}")
            
    # 2. iTunes API for album_art, genre, album, and duration fallback.
    # Only accept matches above the resolver threshold to avoid wrong artwork.
    itunes_match = find_itunes_track_metadata(title, artist)
    if itunes_match:
        if itunes_match.get("album_art"):
            metadata["album_art"] = itunes_match["album_art"]
            logger.info("metadata_enricher: iTunes album art found.")

        if itunes_match.get("genre"):
            metadata["genre"] = itunes_match["genre"]
            logger.info(f"metadata_enricher: iTunes genre found: {metadata['genre']}")

        if itunes_match.get("album"):
            metadata["album"] = itunes_match["album"]
            logger.info(f"metadata_enricher: iTunes album found: {metadata['album']}")

        if not duration_filled and itunes_match.get("duration_ms"):
            duration_seconds = int(itunes_match["duration_ms"]) // 1000
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            metadata["duration"] = f"{minutes:02d}:{seconds:02d}"
            metadata["durationSeconds"] = duration_seconds
            logger.info(f"metadata_enricher: iTunes duration found: {metadata['duration']}")

    if not metadata["album_art"]:
        fallback_art = resolve_album_art(title, artist, album=metadata.get("album"))
        if fallback_art:
            metadata["album_art"] = fallback_art
            logger.info("metadata_enricher: Album art found via fallback resolver.")

    # 3. Lyrics API (lrclib.net)
    d_sec = metadata["durationSeconds"] or 0
    alb = metadata["album"]
    if alb == "Unknown Album":
        alb = ""
        
    plain_lyrics, synced_lyrics = fetch_lrclib_lyrics(title, artist, alb, d_sec)
    metadata["lyrics"] = plain_lyrics
    metadata["syncedLyrics"] = synced_lyrics

    if plain_lyrics and detect_script_mixing(plain_lyrics):
        metadata["lyricsStatus"] = "needs_review"
        logger.warning(f"Mixed-script lyrics detected for '{title}' by '{artist}' - flagged for review")

    # 4. Language Detection
    source_lower = source.lower() if source else "unknown"
    new_lang = "unknown"
    method_used = "unknown"
    
    # Priority A: Script-based detection from lyrics
    if plain_lyrics:
        script_lang = detect_script_language_from_lyrics(plain_lyrics)
        if script_lang:
            new_lang = script_lang
            method_used = "lyrics_script"
            
    # Priority B: Source-based detection (JioSaavn)
    if new_lang == "unknown" and "jiosaavn charts" in source_lower:
        if "malayalam" in source_lower:
            new_lang = "malayalam"
            method_used = "source"
        elif "tamil" in source_lower:
            new_lang = "tamil"
            method_used = "source"
        elif "hindi" in source_lower:
            new_lang = "hindi"
            method_used = "source"
        elif "indian" in source_lower:
            new_lang = "indian"
            method_used = "source"

    # Priority C & D: MusicBrainz / iTunes
    if new_lang == "unknown":
        det_lang, method = detect_track_language(title, artist)
        if method == "musicbrainz":
            if det_lang in ["malayalam", "tamil", "hindi"]:
                new_lang = det_lang
                method_used = "artist_override_musicbrainz"
        elif method == "itunes":
            if det_lang == "hindi":
                new_lang = "indian"
            elif det_lang == "english":
                new_lang = "english"
            else:
                new_lang = det_lang
            method_used = "itunes_storefront"
            
    if new_lang != "unknown":
        metadata["language"] = new_lang
        logger.info(f"metadata_enricher: Language detected: {new_lang} (via {method_used})")
    else:
        logger.info(f"metadata_enricher: Language could not be conclusively determined. Defaulting to unknown.")
    
    # 5. Logging summary
    success_fields = [k for k, v in metadata.items() if v and v not in ["--:--", "unknown", "Unknown", "Unknown Album"]]
    missing_fields = [k for k, v in metadata.items() if not v or v in ["--:--", "unknown", "Unknown", "Unknown Album"]]
    logger.info(f"metadata_enricher: Enrichment complete. Filled: {', '.join(success_fields)}. Missing: {', '.join(missing_fields)}.")
    
    return metadata
