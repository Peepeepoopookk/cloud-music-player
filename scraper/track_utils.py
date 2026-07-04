import datetime
import difflib
import re
import unicodedata


UNKNOWN_SPOTIFY_IDS = {None, "", "unknown", "UnknownID", "none", "null"}


def utc_now_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"


def is_missing(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "--:--", "Unknown", "unknown", "Unknown Album"}
    return False


def spotify_id_is_real(value):
    return value not in UNKNOWN_SPOTIFY_IDS


def normalize_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"\([^)]*(?:feat|ft|from|version|remix)[^)]*\)", " ", text)
    text = re.sub(r"\b(feat|ft|featuring|from|version|remix|remastered)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_artist(value):
    text = str(value or "")
    text = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+", ",", text, flags=re.I)
    parts = re.split(r"\s*,\s*|\s+&\s+|\s+and\s+", text)
    normalized_parts = [normalize_text(part) for part in parts if normalize_text(part)]
    return " ".join(sorted(set(normalized_parts)))


def track_identity(track):
    return (
        normalize_text(track.get("title") or track.get("name")),
        normalize_artist(track.get("artist") or track.get("artists")),
    )


def drive_id(track):
    return track.get("driveFileId") or track.get("id") or track.get("file_id")


def extract_tracks(db_data):
    if isinstance(db_data, list):
        return db_data, False
    if isinstance(db_data, dict) and isinstance(db_data.get("tracks"), list):
        return db_data["tracks"], True
    return [], False


def replace_tracks(db_data, tracks, was_dict):
    if was_dict:
        db_data["tracks"] = tracks
        return db_data
    return tracks


def find_existing_track(tracks, candidate, fuzzy_threshold=0.94):
    candidate_drive_id = drive_id(candidate)
    candidate_spotify_id = candidate.get("spotify_id")
    candidate_identity = track_identity(candidate)
    candidate_joined = " ".join(candidate_identity).strip()

    for track in tracks:
        if candidate_drive_id and drive_id(track) == candidate_drive_id:
            return track, "driveFileId"

        if spotify_id_is_real(candidate_spotify_id) and track.get("spotify_id") == candidate_spotify_id:
            return track, "spotify_id"

        existing_identity = track_identity(track)
        if candidate_identity[0] and candidate_identity == existing_identity:
            return track, "title_artist"

        existing_joined = " ".join(existing_identity).strip()
        if candidate_joined and existing_joined:
            ratio = difflib.SequenceMatcher(None, candidate_joined, existing_joined).ratio()
            if ratio >= fuzzy_threshold:
                return track, f"fuzzy:{ratio:.3f}"

    return None, None


def normalize_track_schema(track, now=None):
    now = now or utc_now_iso()
    changed = False

    defaults = {
        "title": "Unknown Title",
        "artist": "Unknown Artist",
        "album": "Unknown Album",
        "genre": "Unknown",
        "duration": "--:--",
        "durationSeconds": None,
        "spotify_id": None,
        "album_art": None,
        "albumArt": None,
        "language": "unknown",
        "source": "unknown",
        "requestedBy": None,
        "lyrics": None,
        "syncedLyrics": None,
        "lyricsStatus": "ok",
    }

    for key, default in defaults.items():
        if key not in track:
            track[key] = default
            changed = True

    art = track.get("album_art") or track.get("albumArt")
    if track.get("album_art") != art:
        track["album_art"] = art
        changed = True
    if track.get("albumArt") != art:
        track["albumArt"] = art
        changed = True

    file_id = drive_id(track)
    if file_id:
        if track.get("id") != file_id:
            track["id"] = file_id
            changed = True
        if track.get("driveFileId") != file_id:
            track["driveFileId"] = file_id
            changed = True

    timestamp = track.get("timestamp") or now
    if not track.get("timestamp"):
        track["timestamp"] = timestamp
        changed = True
    if not track.get("addedAt"):
        track["addedAt"] = timestamp
        changed = True
    if not track.get("updatedAt"):
        track["updatedAt"] = timestamp
        changed = True

    return changed


def build_track_record(drive_file_id, metadata, now=None):
    now = now or utc_now_iso()
    art = metadata.get("album_art") or metadata.get("albumArt")
    record = {
        "id": drive_file_id,
        "driveFileId": drive_file_id,
        "title": metadata.get("title", "Unknown Title"),
        "artist": metadata.get("artist", "Unknown Artist"),
        "album": metadata.get("album", "Unknown Album"),
        "genre": metadata.get("genre", "Unknown"),
        "duration": metadata.get("duration", "--:--"),
        "durationSeconds": metadata.get("durationSeconds"),
        "spotify_id": metadata.get("spotify_id"),
        "album_art": art,
        "albumArt": art,
        "language": metadata.get("language", "unknown"),
        "source": metadata.get("source", "unknown"),
        "requestedBy": metadata.get("requestedBy"),
        "lyrics": metadata.get("lyrics"),
        "syncedLyrics": metadata.get("syncedLyrics"),
        "lyricsStatus": metadata.get("lyricsStatus", "ok"),
        "timestamp": metadata.get("timestamp") or now,
        "addedAt": metadata.get("addedAt") or metadata.get("timestamp") or now,
        "updatedAt": now,
    }
    normalize_track_schema(record, now=now)
    return record


def merge_track(existing, incoming, now=None):
    now = now or utc_now_iso()
    changed = normalize_track_schema(existing, now=now)

    for key, value in incoming.items():
        if key in {"id", "driveFileId", "addedAt", "timestamp"}:
            continue
        if is_missing(existing.get(key)) and not is_missing(value):
            existing[key] = value
            changed = True

    art = existing.get("album_art") or existing.get("albumArt") or incoming.get("album_art") or incoming.get("albumArt")
    if art:
        if existing.get("album_art") != art:
            existing["album_art"] = art
            changed = True
        if existing.get("albumArt") != art:
            existing["albumArt"] = art
            changed = True

    if changed:
        existing["updatedAt"] = now
    return changed
