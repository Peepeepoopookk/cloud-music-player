import base64
import logging
import os
import re
import time
import urllib.parse

import requests

from dashboard.drive_client import upload_json, search_file_by_name, download_json
from scraper.drive_uploader import get_db_file_id
from scraper.playlist_importer import active_imports, create_cancel_event
from scraper.playlist_manager import add_playlist
from scraper.track_utils import extract_tracks, check_playlist_duplicates

logger = logging.getLogger(__name__)

SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_PLAYLIST_ID_PATTERN = re.compile(r"(?:playlist/|spotify:playlist:)([A-Za-z0-9]+)", re.IGNORECASE)
SPOTIFY_LIBRARY_SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
)

_token_cache = {
    "access_token": None,
    "expires_at": 0,
}


def extract_spotify_playlist_id(playlist_url):
    value = str(playlist_url or "").strip()
    match = SPOTIFY_PLAYLIST_ID_PATTERN.search(value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{16,32}", value):
        return value
    raise ValueError("Invalid Spotify playlist URL")


def get_spotify_library_connection_status(check_token=False):
    client_id_configured = bool(os.environ.get("SPOTIFY_CLIENT_ID"))
    client_secret_configured = bool(os.environ.get("SPOTIFY_CLIENT_SECRET"))
    refresh_token_configured = bool(os.environ.get("SPOTIFY_REFRESH_TOKEN"))
    access_token_configured = bool(os.environ.get("SPOTIFY_ACCESS_TOKEN"))
    status = {
        "client_id_configured": client_id_configured,
        "client_secret_configured": client_secret_configured,
        "refresh_token_configured": refresh_token_configured,
        "access_token_configured": access_token_configured,
        "auth_url_available": client_id_configured and client_secret_configured,
        "missing": [],
        "ready": False,
        "checked": bool(check_token),
        "error": None,
    }

    if not client_id_configured:
        status["missing"].append("SPOTIFY_CLIENT_ID")
    if not client_secret_configured:
        status["missing"].append("SPOTIFY_CLIENT_SECRET")
    if not refresh_token_configured and not access_token_configured:
        status["missing"].append("SPOTIFY_REFRESH_TOKEN")

    status["ready"] = bool(
        access_token_configured
        or (client_id_configured and client_secret_configured and refresh_token_configured)
    )

    if client_id_configured and client_secret_configured and not refresh_token_configured and not access_token_configured:
        status["error"] = "Spotify app is configured. Connect your Spotify account once to generate SPOTIFY_REFRESH_TOKEN."
        return status

    if check_token:
        try:
            _get_access_token()
            status["ready"] = True
        except Exception as exc:
            status["ready"] = False
            status["error"] = str(exc)

    return status


def build_spotify_authorize_url(redirect_uri, state=None):
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise ValueError("SPOTIFY_CLIENT_ID is not configured.")
    if not redirect_uri:
        raise ValueError("A Spotify redirect URI is required.")

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SPOTIFY_LIBRARY_SCOPES),
        "show_dialog": "true",
    }
    if state:
        params["state"] = state

    return f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_spotify_code_for_refresh_token(code, redirect_uri):
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be configured.")
    if not code:
        raise ValueError("Missing Spotify authorization code.")
    if not redirect_uri:
        raise ValueError("Missing Spotify redirect URI.")

    response = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Authorization": _basic_auth_header(client_id, client_secret)},
        timeout=15,
    )
    if response.status_code != 200:
        raise ValueError(f"Spotify token exchange failed with HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def get_spotify_library_playlist_preview(playlist_url):
    playlist_id = extract_spotify_playlist_id(playlist_url)
    metadata = _get_playlist_metadata(playlist_id)
    preview_tracks = fetch_spotify_library_playlist_tracks(playlist_id, limit=5)
    total_tracks = int(metadata.get("total_tracks") or len(preview_tracks))
    estimated_mb = total_tracks * 5

    already_in_library = 0
    try:
        db_file_id, _ = get_db_file_id()
        if db_file_id:
            db_data = download_json(db_file_id)
            lib_tracks, _ = extract_tracks(db_data)
            duplicate_results = check_playlist_duplicates(preview_tracks, lib_tracks)
            already_in_library = sum(1 for r in duplicate_results if r.get("is_duplicate"))
    except Exception as e:
        logger.warning(f"Error checking duplicates for Spotify library preview: {e}")
        already_in_library = 0

    new_tracks_importable = max(0, total_tracks - already_in_library)

    return {
        "playlist_id": playlist_id,
        "playlist_name": metadata.get("playlist_name") or "Spotify Library Playlist",
        "cover_image": metadata.get("cover_image"),
        "owner_name": metadata.get("owner_name"),
        "total_tracks": total_tracks,
        "tracks_available_for_import": total_tracks,
        "already_in_library": already_in_library,
        "new_tracks_importable": new_tracks_importable,
        "truncated": False,
        "truncation_warning": None,
        "estimated_size_mb": estimated_mb,
        "estimated_size_display": f"~{estimated_mb} MB",
        "preview_tracks": preview_tracks,
        "source": "spotify_api",
    }


def diagnose_spotify_library_playlist(playlist_url):
    playlist_id = extract_spotify_playlist_id(playlist_url)
    diagnosis = {
        "playlist_id": playlist_id,
        "connection": get_spotify_library_connection_status(check_token=True),
        "me": None,
        "playlist_metadata": None,
        "playlist_tracks": None,
        "visible_in_user_playlists": None,
    }

    token = _get_access_token()

    me_response = _spotify_raw_get("/me", token)
    diagnosis["me"] = _summarize_spotify_response(me_response, allowed_keys=("id", "display_name", "country", "product"))

    metadata_response = _spotify_raw_get(
        f"/playlists/{playlist_id}",
        token,
        params={"fields": "id,name,public,collaborative,owner(id,display_name),tracks(total)"},
    )
    diagnosis["playlist_metadata"] = _summarize_spotify_response(metadata_response)

    tracks_response = _spotify_raw_get(
        f"/playlists/{playlist_id}/tracks",
        token,
        params={
            "limit": 1,
            "offset": 0,
            "additional_types": "track",
            "fields": "total,next,items(track(id,name,type,is_local))",
        },
    )
    diagnosis["playlist_tracks"] = _summarize_spotify_response(tracks_response)

    user_playlists_response = _spotify_raw_get(
        "/me/playlists",
        token,
        params={"limit": 50, "fields": "items(id,name),next,total"},
    )
    user_playlists_summary = _summarize_spotify_response(user_playlists_response)
    if user_playlists_response.status_code == 200:
        payload = user_playlists_response.json()
        items = payload.get("items") or []
        diagnosis["visible_in_user_playlists"] = any(item.get("id") == playlist_id for item in items)
        user_playlists_summary["sample_playlist_count"] = len(items)
        user_playlists_summary["total"] = payload.get("total")
    diagnosis["user_playlists"] = user_playlists_summary

    return diagnosis


def start_spotify_library_import(playlist_url, batch_size=15, device_id=None, imported_via="spotify_library_dashboard"):
    playlist_id_from_url = extract_spotify_playlist_id(playlist_url)
    metadata = _get_playlist_metadata(playlist_id_from_url)
    tracks = fetch_spotify_library_playlist_tracks(playlist_id_from_url)
    if not tracks:
        raise ValueError("Spotify API returned no importable tracks for this playlist.")

    db_file_id, parent_id = get_db_file_id()
    if not parent_id:
        raise ValueError("Could not determine database folder for Spotify library import state.")

    playlist_name = metadata.get("playlist_name") or "Spotify Library Playlist"
    playlist_id = add_playlist(
        name=playlist_name,
        source_url=playlist_url,
        cover_image=metadata.get("cover_image"),
        imported_via=imported_via,
        requestedBy=device_id,
    )

    state = {
        "playlist_id": playlist_id,
        "playlist_url": playlist_url,
        "spotify_playlist_id": playlist_id_from_url,
        "playlist_name": playlist_name,
        "total_tracks": int(metadata.get("total_tracks") or len(tracks)),
        "tracks_available_for_import": len(tracks),
        "processed": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "gemini_pending": 0,
        "gemini_deferred": 0,
        "gemini_status": "idle",
        "gemini_last_batch": None,
        "status": "running",
        "device_id": device_id,
        "tracks": tracks,
        "import_tool": "spotify_library_importer",
        "metadata_source": "spotify_api",
        "source_label": f"Spotify Library Import ({playlist_name})",
    }

    state_filename = f"playlist_import_state_{playlist_id}.json"
    existing_file_id = search_file_by_name(state_filename, parent_id)
    upload_json(existing_file_id, state, state_filename, parent_id=parent_id)
    active_imports[playlist_id] = state
    create_cancel_event(playlist_id)
    return playlist_id


def fetch_spotify_library_playlist_tracks(playlist_id, limit=None):
    tracks = []
    offset = 0
    page_limit = 50
    fields = (
        "total,next,items(track(id,name,duration_ms,type,is_local,"
        "artists(name),album(name,images)))"
    )

    while True:
        remaining = None if limit is None else max(limit - len(tracks), 0)
        if remaining == 0:
            break
        request_limit = page_limit if remaining is None else min(page_limit, remaining)
        data = _spotify_api_get(
            f"/playlists/{playlist_id}/tracks",
            params={
                "limit": request_limit,
                "offset": offset,
                "additional_types": "track",
                "fields": fields,
            },
        )

        for item in data.get("items") or []:
            track = item.get("track") or {}
            if track.get("type") != "track" or track.get("is_local"):
                continue
            title = track.get("name") or "Unknown Title"
            artists = track.get("artists") or []
            artist_names = [artist.get("name") for artist in artists if artist.get("name")]
            artist = ", ".join(artist_names) if artist_names else "Unknown Artist"
            album = track.get("album") or {}
            images = album.get("images") or []
            album_art = images[0].get("url") if images and images[0].get("url") else None
            duration_ms = track.get("duration_ms")

            tracks.append({
                "title": title,
                "artist": artist,
                "spotify_id": track.get("id") or "UnknownID",
                "album": album.get("name") or "Single",
                "album_art": album_art,
                "durationSeconds": int(duration_ms / 1000) if isinstance(duration_ms, int) else None,
                "genre": "Unknown",
                "language": "unknown",
            })

        if not data.get("next"):
            break
        offset += request_limit

    return tracks


def _get_playlist_metadata(playlist_id):
    data = _spotify_api_get(
        f"/playlists/{playlist_id}",
        params={"fields": "id,name,images,owner(display_name),tracks(total)"},
    )
    images = data.get("images") or []
    owner = data.get("owner") or {}
    tracks = data.get("tracks") or {}
    return {
        "playlist_id": data.get("id") or playlist_id,
        "playlist_name": data.get("name") or "Spotify Library Playlist",
        "cover_image": images[0].get("url") if images and images[0].get("url") else None,
        "owner_name": owner.get("display_name"),
        "total_tracks": tracks.get("total"),
    }


def _spotify_api_get(path, params=None):
    token = _get_access_token()
    response = _spotify_raw_get(path, token, params=params)
    if response.status_code == 401:
        _token_cache["access_token"] = None
        token = _get_access_token(force_refresh=True)
        response = _spotify_raw_get(path, token, params=params)
    if response.status_code == 403:
        message = _spotify_error_message(response)
        raise ValueError(
            f"Spotify API denied access while calling {path}: {message}. "
            "Make sure this playlist is accessible to your Spotify account and your app/user is allowlisted."
        )
    if response.status_code == 404:
        raise ValueError("Spotify playlist was not found or is not accessible to your account.")
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise ValueError(f"Spotify API rate limited this request. Try again after {retry_after or 'a short wait'} seconds.")
    if response.status_code >= 400:
        raise ValueError(f"Spotify API request failed with HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def _spotify_raw_get(path, token, params=None):
    return requests.get(
        f"{SPOTIFY_API_BASE_URL}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )


def _spotify_error_message(response):
    try:
        payload = response.json()
        error = payload.get("error")
        if isinstance(error, dict):
            return error.get("message") or response.text[:300]
        if isinstance(error, str):
            return error
    except Exception:
        pass
    return response.text[:300] or f"HTTP {response.status_code}"


def _summarize_spotify_response(response, allowed_keys=None):
    summary = {
        "status_code": response.status_code,
        "ok": 200 <= response.status_code < 300,
    }
    try:
        payload = response.json()
    except Exception:
        summary["message"] = response.text[:300]
        return summary

    if response.status_code >= 400:
        summary["message"] = _spotify_error_message(response)
        return summary

    if allowed_keys:
        for key in allowed_keys:
            if key in payload:
                summary[key] = payload.get(key)
        return summary

    for key in ("id", "name", "public", "collaborative", "tracks", "owner", "total", "next"):
        if key in payload:
            summary[key] = payload.get(key)
    return summary


def _get_access_token(force_refresh=False):
    now = time.time()
    if not force_refresh and _token_cache["access_token"] and _token_cache["expires_at"] > now + 30:
        return _token_cache["access_token"]

    static_access_token = os.environ.get("SPOTIFY_ACCESS_TOKEN")
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")

    if static_access_token and not refresh_token:
        return static_access_token

    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            "Spotify API is not configured. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, and SPOTIFY_REFRESH_TOKEN."
        )

    response = requests.post(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Authorization": _basic_auth_header(client_id, client_secret)},
        timeout=15,
    )
    if response.status_code != 200:
        raise ValueError(f"Spotify token refresh failed with HTTP {response.status_code}: {response.text[:300]}")

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise ValueError("Spotify token refresh did not return an access token.")

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + int(payload.get("expires_in") or 3600)
    return access_token


def _basic_auth_header(client_id, client_secret):
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")
