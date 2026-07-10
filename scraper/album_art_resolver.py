import difflib
import os
import re
import time
from urllib.parse import quote_plus

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


def _best_image_from_images(images):
    if not images:
        return None
    if isinstance(images, str):
        return images
    if isinstance(images, dict):
        return images.get("url") or images.get("link")
    if isinstance(images, list):
        for wanted in ("500x500", "150x150", "50x50"):
            for image in reversed(images):
                if isinstance(image, dict) and image.get("quality") == wanted and (image.get("url") or image.get("link")):
                    return image.get("url") or image.get("link")
        for image in reversed(images):
            if isinstance(image, dict) and (image.get("url") or image.get("link")):
                return image.get("url") or image.get("link")
            if isinstance(image, str):
                return image
    return None


def _album_is_known(album):
    return bool(album and str(album).strip() and str(album).strip().lower() not in {"unknown", "unknown album", "none", "null"})


def _image_result(url, source, metadata=None, confidence="exact"):
    if not url:
        return None
    return {
        "url": url,
        "source": source,
        "metadata": {**(metadata or {}), "confidence": confidence},
    }


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
    return item.get("primaryArtists") or item.get("primaryArtistsId") or item.get("artist") or item.get("subtitle")


def _jiosaavn_art(title, artist, album=None, min_score=1.15, related=False):
    queries = [f"{title} {artist}", f"{artist} {title}"]
    if _album_is_known(album):
        queries.extend([f"{album} {artist}", str(album)])
    queries.append(title)

    best = None
    best_score = 0.0
    for query in queries:
        try:
            for item in _jiosaavn_search_results(query):
                item_title = item.get("name") or item.get("title")
                item_artist = _jiosaavn_song_artist(item)
                score = _score(title, artist, item_title, item_artist)
                item_album = item.get("album")
                if _album_is_known(album) and isinstance(item_album, dict):
                    album_name = item_album.get("name")
                    if normalize_text(album) and normalize_text(album) == normalize_text(album_name):
                        score += 0.25
                if related and _album_is_known(album):
                    album_name = item_album.get("name") if isinstance(item_album, dict) else item_album
                    album_score = difflib.SequenceMatcher(None, normalize_text(album), normalize_text(album_name)).ratio()
                    score = max(score, album_score + 0.4)
                if score > best_score:
                    best = item
                    best_score = score
        except Exception:
            continue
        time.sleep(0.1)

    if best and best_score >= min_score:
        album_data = best.get("album") if isinstance(best.get("album"), dict) else {}
        art = _best_image_from_images(best.get("image")) or _best_image_from_images(album_data.get("image"))
        if art:
            return _image_result(
                art,
                "jiosaavn_related" if related else "jiosaavn",
                {"match_score": best_score, "album": album_data.get("name") if album_data else None},
                "related" if related else "exact",
            )
    return None


def _itunes_related_art(title, artist, album=None):
    queries = []
    if _album_is_known(album):
        queries.extend([f"{album} {artist}", str(album)])
    queries.extend([f"{title} {artist}", artist, title])

    best = None
    best_score = 0.0
    for query in queries:
        try:
            response = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "media": "music", "entity": "album", "limit": 10},
                headers=HEADERS,
                timeout=6,
            )
            if response.status_code != 200:
                continue
            for item in response.json().get("results", []):
                collection = item.get("collectionName")
                candidate_artist = item.get("artistName")
                score = 0.0
                if _album_is_known(album):
                    score = difflib.SequenceMatcher(None, normalize_text(album), normalize_text(collection)).ratio() + 0.45
                score = max(score, _score(title, artist, collection, candidate_artist) / 1.8)
                if score > best_score:
                    best = item
                    best_score = score
        except Exception:
            continue
        time.sleep(0.1)

    if best and best_score >= 0.72:
        return _image_result(
            _upgrade_itunes_art(best.get("artworkUrl100")),
            "itunes_related",
            {"match_score": best_score, "album": best.get("collectionName")},
            "related",
        )
    return None


def _deezer_artist_art(artist):
    response = requests.get(
        "https://api.deezer.com/search/artist",
        params={"q": artist, "limit": 5},
        headers=HEADERS,
        timeout=6,
    )
    if response.status_code != 200:
        return None
    best = None
    best_score = 0.0
    artist_norm = normalize_artist(artist)
    for item in response.json().get("data", []):
        score = difflib.SequenceMatcher(None, artist_norm, normalize_artist(item.get("name"))).ratio()
        if score > best_score:
            best = item
            best_score = score
    if best and best_score >= 0.72:
        return _image_result(
            best.get("picture_xl") or best.get("picture_big") or best.get("picture_medium"),
            "deezer_artist",
            {"match_score": best_score, "artist": best.get("name")},
            "related",
        )
    return None


def _lastfm_artist_art(artist):
    api_key = os.environ.get("LASTFM_API_KEY")
    if not api_key:
        return None
    response = requests.get(
        "https://ws.audioscrobbler.com/2.0/",
        params={"method": "artist.getInfo", "artist": artist, "autocorrect": 1, "api_key": api_key, "format": "json"},
        timeout=6,
    )
    if response.status_code != 200:
        return None
    images = response.json().get("artist", {}).get("image", [])
    for image in reversed(images):
        url = image.get("#text")
        if url:
            return _image_result(url, "lastfm_artist", {"artist": artist}, "related")
    return None


def _generated_text_cover(title, artist):
    label = " - ".join(part for part in (title, artist) if part)
    if not label:
        return None
    text = quote_plus(label[:80])
    url = f"https://ui-avatars.com/api/?name={text}&size=512&background=111827&color=ffffff&bold=true&format=png"
    return _image_result(url, "generated_text_cover", {"label": label}, "placeholder")


def resolve_album_art_with_details(title, artist, album=None):
    """
    Resolves album art through multiple providers and returns provider diagnostics.
    The first confident artwork URL wins, while attempts are recorded for UI/logging.
    """
    attempts = []

    try:
        itunes = find_itunes_track_metadata(title, artist)
        if itunes and itunes.get("album_art"):
            return {
                "url": itunes["album_art"],
                "source": "itunes",
                "metadata": itunes,
                "attempts": attempts + [{"source": "itunes", "status": "hit"}],
            }
        attempts.append({"source": "itunes", "status": "miss"})
    except Exception as e:
        attempts.append({"source": "itunes", "status": "error", "error": str(e)})

    resolvers = (
        ("deezer", lambda: _image_result(_deezer_art(title, artist), "deezer")),
        ("jiosaavn", lambda: _jiosaavn_art(title, artist, album=album)),
        ("lastfm", lambda: _image_result(_lastfm_art(title, artist, album=album), "lastfm")),
        ("cover_art_archive", lambda: _image_result(_cover_art_archive(title, artist), "cover_art_archive")),
        ("itunes_related", lambda: _itunes_related_art(title, artist, album=album)),
        ("jiosaavn_related", lambda: _jiosaavn_art(title, artist, album=album, min_score=0.72, related=True)),
        ("deezer_artist", lambda: _deezer_artist_art(artist)),
        ("lastfm_artist", lambda: _lastfm_artist_art(artist)),
        ("generated_text_cover", lambda: _generated_text_cover(title, artist)),
    )

    for source, resolver in resolvers:
        try:
            result = resolver()
            art = result.get("url") if result else None
            if art:
                attempts.append({"source": source, "status": "hit"})
                return {
                    "url": art,
                    "source": result.get("source") or source,
                    "metadata": result.get("metadata") or {},
                    "attempts": attempts,
                }
            attempts.append({"source": source, "status": "miss"})
        except Exception as e:
            attempts.append({"source": source, "status": "error", "error": str(e)})

    return {
        "url": None,
        "source": None,
        "metadata": {},
        "attempts": attempts,
    }


def resolve_album_art(title, artist, album=None):
    result = resolve_album_art_with_details(title, artist, album=album)
    return result.get("url")
