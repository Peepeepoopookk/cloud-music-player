import difflib
import time

import requests

from scraper.album_art_resolver import find_itunes_track_metadata
from scraper.track_utils import normalize_artist, normalize_text


HEADERS = {
    "User-Agent": "CloudMusicPlayer/1.0.0 (contact@example.com)",
    "Accept": "application/json",
}


def _format_duration(seconds):
    seconds = int(seconds)
    minutes = seconds // 60
    rest = seconds % 60
    return f"{minutes:02d}:{rest:02d}", seconds


def _score(title, artist, candidate_title, candidate_artist):
    title_norm = normalize_text(title)
    artist_norm = normalize_artist(artist)
    candidate_title_norm = normalize_text(candidate_title)
    candidate_artist_norm = normalize_artist(candidate_artist)
    if not title_norm or not candidate_title_norm:
        return 0.0
    title_score = difflib.SequenceMatcher(None, title_norm, candidate_title_norm).ratio()
    artist_score = 0.0
    if artist_norm and candidate_artist_norm:
        artist_score = difflib.SequenceMatcher(None, artist_norm, candidate_artist_norm).ratio()
        if artist_norm in candidate_artist_norm or candidate_artist_norm in artist_norm:
            artist_score = max(artist_score, 0.86)
    return title_score + artist_score


def _itunes_track_duration(title, artist):
    metadata = find_itunes_track_metadata(title, artist)
    duration_ms = metadata.get("duration_ms") if metadata else None
    if duration_ms:
        duration, seconds = _format_duration(int(duration_ms) // 1000)
        return {"duration": duration, "durationSeconds": seconds, "metadata": metadata}
    return None


def _itunes_artist_duration(title, artist):
    response = requests.get(
        "https://itunes.apple.com/search",
        params={"term": artist, "media": "music", "limit": 25},
        headers=HEADERS,
        timeout=6,
    )
    if response.status_code != 200:
        return None
    best = None
    best_score = 0.0
    title_norm = normalize_text(title)
    for item in response.json().get("results", []):
        candidate_title = normalize_text(item.get("trackName"))
        score = difflib.SequenceMatcher(None, title_norm, candidate_title).ratio() if candidate_title else 0.0
        if score > best_score:
            best = item
            best_score = score
    duration_ms = best.get("trackTimeMillis") if best and best_score >= 0.68 else None
    if duration_ms:
        duration, seconds = _format_duration(int(duration_ms) // 1000)
        return {"duration": duration, "durationSeconds": seconds, "metadata": {"match_score": best_score}}
    return None


def _deezer_duration(title, artist):
    response = requests.get(
        "https://api.deezer.com/search/track",
        params={"q": f'artist:"{artist}" track:"{title}"', "limit": 10},
        headers=HEADERS,
        timeout=6,
    )
    if response.status_code != 200:
        return None
    best = None
    best_score = 0.0
    for item in response.json().get("data", []):
        score = _score(title, artist, item.get("title"), item.get("artist", {}).get("name"))
        if score > best_score:
            best = item
            best_score = score
    if best and best_score >= 1.25 and best.get("duration"):
        duration, seconds = _format_duration(int(best["duration"]))
        return {"duration": duration, "durationSeconds": seconds, "metadata": {"match_score": best_score}}
    return None


def _musicbrainz_duration(title, artist):
    query = f'recording:"{title}" AND artist:"{artist}"'
    response = requests.get(
        "https://musicbrainz.org/ws/2/recording/",
        params={"query": query, "fmt": "json", "limit": 10},
        headers=HEADERS,
        timeout=7,
    )
    if response.status_code != 200:
        return None
    best = None
    best_score = 0.0
    for item in response.json().get("recordings", []):
        score = _score(title, artist, item.get("title"), artist)
        if score > best_score:
            best = item
            best_score = score
    duration_ms = best.get("length") if best and best_score >= 1.1 else None
    if duration_ms:
        duration, seconds = _format_duration(int(duration_ms) // 1000)
        return {"duration": duration, "durationSeconds": seconds, "metadata": {"match_score": best_score}}
    return None


def resolve_duration_with_details(title, artist):
    attempts = []
    resolvers = (
        ("itunes_track", lambda: _itunes_track_duration(title, artist)),
        ("itunes_artist", lambda: _itunes_artist_duration(title, artist)),
        ("deezer", lambda: _deezer_duration(title, artist)),
        ("musicbrainz", lambda: _musicbrainz_duration(title, artist)),
    )

    for source, resolver in resolvers:
        try:
            result = resolver()
            if result and result.get("durationSeconds"):
                attempts.append({"source": source, "status": "hit"})
                return {
                    "duration": result["duration"],
                    "durationSeconds": result["durationSeconds"],
                    "source": source,
                    "metadata": result.get("metadata") or {},
                    "attempts": attempts,
                }
            attempts.append({"source": source, "status": "miss"})
        except Exception as e:
            attempts.append({"source": source, "status": "error", "error": str(e)})
        time.sleep(0.1)

    return {
        "duration": None,
        "durationSeconds": None,
        "source": None,
        "metadata": {},
        "attempts": attempts,
    }
