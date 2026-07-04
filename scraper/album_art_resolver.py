import difflib
import os
import re
import time

import requests

from scraper.track_utils import normalize_artist, normalize_text


HEADERS = {
    "User-Agent": "CloudMusicPlayer/1.0.0 (contact@example.com)",
    "Accept": "application/json",
}


def _split_artists(artist):
    parts = re.split(r"\s*,\s*|\s+&\s+|\s+and\s+|\s+feat\.?\s+|\s+ft\.?\s+", artist or "", flags=re.I)
    return [part.strip() for part in parts if part.strip()]


def _queries(title, artist):
    primary_artist = _split_artists(artist)
    primary_artist = primary_artist[0] if primary_artist else artist
    seen = set()
    for query in (
        f"{artist} {title}",
        f"{primary_artist} {title}",
        f"{title} {primary_artist}",
        title,
    ):
        query = " ".join(str(query or "").split())
        if query and query.lower() not in seen:
            seen.add(query.lower())
            yield query


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


def _upgrade_itunes_art(url):
    if not url:
        return None
    return url.replace("100x100bb", "600x600bb").replace("100x100", "600x600")


def find_itunes_track_metadata(title, artist, min_score=1.35):
    best_item = None
    best_score = 0.0
    for query in _queries(title, artist):
        try:
            response = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "entity": "song", "media": "music", "limit": 10},
                headers=HEADERS,
                timeout=5,
            )
            if response.status_code != 200:
                continue
            for item in response.json().get("results", []):
                score = _score(title, artist, item.get("trackName"), item.get("artistName"))
                if score > best_score:
                    best_score = score
                    best_item = item
        except Exception:
            continue
        time.sleep(0.1)

    if not best_item or best_score < min_score:
        return None

    metadata = {
        "album_art": _upgrade_itunes_art(best_item.get("artworkUrl100")),
        "genre": best_item.get("primaryGenreName"),
        "album": best_item.get("collectionName"),
        "duration_ms": best_item.get("trackTimeMillis"),
        "match_score": best_score,
        "source": "itunes",
    }
    return metadata


def _musicbrainz_release_ids(title, artist):
    query_artist = _split_artists(artist)
    query_artist = query_artist[0] if query_artist else artist
    query = f'recording:"{title}" AND artist:"{query_artist}"'
    try:
        response = requests.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": query, "fmt": "json", "limit": 5},
            headers=HEADERS,
            timeout=6,
        )
        if response.status_code != 200:
            return []
        release_ids = []
        for recording in response.json().get("recordings", []):
            score = _score(title, artist, recording.get("title"), query_artist)
            if score < 1.1:
                continue
            for release in recording.get("releases", []) or []:
                release_id = release.get("id")
                if release_id and release_id not in release_ids:
                    release_ids.append(release_id)
        return release_ids
    except Exception:
        return []


def _cover_art_archive(title, artist):
    for release_id in _musicbrainz_release_ids(title, artist):
        try:
            response = requests.get(
                f"https://coverartarchive.org/release/{release_id}/front-500",
                headers={"User-Agent": HEADERS["User-Agent"]},
                timeout=6,
                allow_redirects=True,
            )
            if response.status_code == 200 and response.url:
                return response.url
        except Exception:
            continue
        time.sleep(0.1)
    return None


def _lastfm_art(title, artist, album=None):
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key:
        return None

    calls = [
        {"method": "track.getInfo", "artist": artist, "track": title, "autocorrect": 1},
    ]
    if album:
        calls.append({"method": "album.getInfo", "artist": artist, "album": album, "autocorrect": 1})

    for params in calls:
        try:
            params = {**params, "api_key": api_key, "format": "json"}
            response = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, timeout=5)
            if response.status_code != 200:
                continue
            data = response.json()
            image_list = []
            if params["method"] == "track.getInfo":
                image_list = data.get("track", {}).get("album", {}).get("image", [])
            else:
                image_list = data.get("album", {}).get("image", [])
            for image in reversed(image_list):
                url = image.get("#text")
                if url:
                    return url
        except Exception:
            continue
    return None


def _deezer_art(title, artist):
    try:
        primary_artist = _split_artists(artist)
        primary_artist = primary_artist[0] if primary_artist else artist
        query = f'artist:"{primary_artist}" track:"{title}"'
        response = requests.get(
            "https://api.deezer.com/search/track",
            params={"q": query, "limit": 5},
            headers=HEADERS,
            timeout=5,
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
        if best and best_score >= 1.25:
            album = best.get("album") or {}
            return album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")
    except Exception:
        return None
    return None


def resolve_album_art(title, artist, album=None):
    itunes = find_itunes_track_metadata(title, artist)
    if itunes and itunes.get("album_art"):
        return itunes["album_art"]

    for resolver in (
        lambda: _cover_art_archive(title, artist),
        lambda: _lastfm_art(title, artist, album=album),
        lambda: _deezer_art(title, artist),
    ):
        art = resolver()
        if art:
            return art
    return None
