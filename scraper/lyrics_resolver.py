import difflib
import html
import re
import time
from urllib.parse import quote

import requests

from scraper.track_utils import normalize_artist, normalize_text


HEADERS = {
    "User-Agent": "CloudMusicPlayer/1.0.0 (contact@example.com)",
    "Accept": "application/json",
}

MIN_LYRICS_CHARS = 80


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


def _clean_lyrics(value):
    if not value:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\\n", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_LYRICS_CHARS:
        return None
    if "404 not found" in text.lower() or "instrumental" == text.lower().strip():
        return None
    return text


def _lyrics_from_lrclib_payload(data):
    if not isinstance(data, dict):
        return None
    plain = _clean_lyrics(data.get("plainLyrics"))
    synced = data.get("syncedLyrics")
    if plain or synced:
        return {
            "lyrics": plain,
            "syncedLyrics": synced,
            "provider_track": data.get("trackName"),
            "provider_artist": data.get("artistName"),
        }
    return None


def _lrclib_exact(title, artist, album=None, duration_seconds=None):
    params = {
        "artist_name": artist,
        "track_name": title,
    }
    if album:
        params["album_name"] = album
    if duration_seconds:
        params["duration"] = int(duration_seconds)

    response = requests.get("https://lrclib.net/api/get", params=params, headers=HEADERS, timeout=6)
    if response.status_code != 200:
        return None
    return _lyrics_from_lrclib_payload(response.json())


def _lrclib_search(title, artist, min_score=1.18):
    response = requests.get(
        "https://lrclib.net/api/search",
        params={"track_name": title, "artist_name": artist},
        headers=HEADERS,
        timeout=6,
    )
    if response.status_code != 200:
        return None
    best = None
    best_score = 0.0
    for item in response.json() or []:
        score = _score(title, artist, item.get("trackName"), item.get("artistName"))
        if score > best_score:
            best = item
            best_score = score
    if best and best_score >= min_score:
        lyrics = _lyrics_from_lrclib_payload(best)
        if lyrics:
            lyrics["match_score"] = best_score
            return lyrics
    return None


def _lrclib_title_search(title, artist, min_score=0.86):
    response = requests.get(
        "https://lrclib.net/api/search",
        params={"q": title},
        headers=HEADERS,
        timeout=6,
    )
    if response.status_code != 200:
        return None
    best = None
    best_score = 0.0
    title_norm = normalize_text(title)
    for item in response.json() or []:
        item_title = normalize_text(item.get("trackName"))
        score = difflib.SequenceMatcher(None, title_norm, item_title).ratio() if item_title else 0.0
        if artist:
            artist_score = _score(title, artist, item.get("trackName"), item.get("artistName"))
            score = max(score, artist_score / 2)
        if score > best_score:
            best = item
            best_score = score
    if best and best_score >= min_score:
        lyrics = _lyrics_from_lrclib_payload(best)
        if lyrics:
            lyrics["match_score"] = best_score
            return lyrics
    return None


def _lyrics_ovh(title, artist):
    response = requests.get(
        f"https://api.lyrics.ovh/v1/{quote(artist, safe='')}/{quote(title, safe='')}",
        headers=HEADERS,
        timeout=8,
    )
    if response.status_code != 200:
        return None
    data = response.json()
    lyrics = _clean_lyrics(data.get("lyrics")) if isinstance(data, dict) else None
    if lyrics:
        return {"lyrics": lyrics, "syncedLyrics": None}
    return None


def _jiosaavn_search_results(query, limit=10):
    response = requests.get(
        "https://saavn.dev/api/search/songs",
        params={"query": query, "limit": limit},
        headers=HEADERS,
        timeout=7,
    )
    if response.status_code != 200:
        return []
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        results = data.get("results") or data.get("songs") or []
    elif isinstance(data, list):
        results = data
    else:
        results = []
    return results if isinstance(results, list) else []


def _jiosaavn_song_artist(item):
    artists = item.get("artists") if isinstance(item, dict) else None
    if isinstance(artists, dict):
        primary = artists.get("primary") or artists.get("all") or []
        if isinstance(primary, list):
            names = [artist.get("name") for artist in primary if isinstance(artist, dict) and artist.get("name")]
            if names:
                return ", ".join(names)
    if isinstance(artists, list):
        names = [artist.get("name") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        if names:
            return ", ".join(names)
    return item.get("primaryArtists") or item.get("artist") or item.get("subtitle")


def _lyrics_from_jiosaavn_payload(payload):
    data = payload.get("data") if isinstance(payload, dict) else payload
    candidates = []
    if isinstance(data, dict):
        candidates.extend([data.get("lyrics"), data.get("text"), data.get("snippet")])
        nested = data.get("lyricsData") or data.get("lyrics_data")
        if isinstance(nested, dict):
            candidates.extend([nested.get("lyrics"), nested.get("text")])
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                candidates.extend([item.get("lyrics"), item.get("text"), item.get("snippet")])

    if isinstance(payload, dict):
        candidates.extend([payload.get("lyrics"), payload.get("text"), payload.get("snippet")])

    for candidate in candidates:
        lyrics = _clean_lyrics(candidate)
        if lyrics:
            return lyrics
    return None


def _jiosaavn_lyrics(title, artist, min_score=1.1):
    best = None
    best_score = 0.0
    for query in (f"{title} {artist}", f"{artist} {title}", title):
        try:
            for item in _jiosaavn_search_results(query):
                score = _score(title, artist, item.get("name") or item.get("title"), _jiosaavn_song_artist(item))
                if score > best_score:
                    best = item
                    best_score = score
        except Exception:
            continue
        time.sleep(0.1)

    if not best or best_score < min_score:
        return None

    direct_lyrics = _clean_lyrics(best.get("lyrics") or best.get("snippet"))
    if direct_lyrics:
        return {"lyrics": direct_lyrics, "syncedLyrics": None, "match_score": best_score}

    song_id = best.get("id")
    if not song_id:
        return None

    urls = [
        (f"https://saavn.dev/api/songs/{song_id}/lyrics", None),
        ("https://saavn.dev/api/songs/lyrics", {"id": song_id}),
        (f"https://saavn.dev/api/songs/{song_id}", None),
    ]
    for url, params in urls:
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=7)
            if response.status_code != 200:
                continue
            lyrics = _lyrics_from_jiosaavn_payload(response.json())
            if lyrics:
                return {"lyrics": lyrics, "syncedLyrics": None, "match_score": best_score}
        except Exception:
            continue
        time.sleep(0.1)
    return None


def resolve_lyrics_with_details(title, artist, album=None, duration_seconds=None):
    attempts = []
    resolvers = (
        ("lrclib_exact", lambda: _lrclib_exact(title, artist, album=album, duration_seconds=duration_seconds)),
        ("jiosaavn", lambda: _jiosaavn_lyrics(title, artist)),
        ("lrclib_search", lambda: _lrclib_search(title, artist)),
        ("lrclib_title_search", lambda: _lrclib_title_search(title, artist)),
        ("lyrics_ovh", lambda: _lyrics_ovh(title, artist)),
    )

    for source, resolver in resolvers:
        try:
            result = resolver()
            if result and (result.get("lyrics") or result.get("syncedLyrics")):
                attempts.append({"source": source, "status": "hit"})
                return {
                    "lyrics": result.get("lyrics"),
                    "syncedLyrics": result.get("syncedLyrics"),
                    "source": source,
                    "metadata": result,
                    "attempts": attempts,
                }
            attempts.append({"source": source, "status": "miss"})
        except Exception as e:
            attempts.append({"source": source, "status": "error", "error": str(e)})
        time.sleep(0.1)

    return {
        "lyrics": None,
        "syncedLyrics": None,
        "source": None,
        "metadata": {},
        "attempts": attempts,
    }
