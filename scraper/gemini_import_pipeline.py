import logging
from typing import Any, Dict, List

from scraper.gemini_metadata_judge import (
    GeminiJudge,
    build_gemini_candidate,
    normalize_genre_value,
    normalize_language_value,
)

GEMINI_IMPORT_BATCH_SIZE = 20
GEMINI_IMPORT_CONFIDENCE_THRESHOLD = 0.6


def apply_gemini_to_import_batch(
    batch: List[Dict[str, Any]],
    logger: logging.Logger,
    force_fields: List[str] | None = None,
) -> Dict[str, Any]:
    """
    Applies Gemini language/genre suggestions to already-downloaded import metadata.
    Duration is intentionally not AI-filled; it must come from the audio metadata extractor.
    """
    stats = {
        "tracks_submitted": len(batch),
        "tracks_updated": 0,
        "fields_updated": 0,
        "language_updates": 0,
        "genre_updates": 0,
        "null_or_low_confidence": 0,
        "ai_failed": False,
        "errors": [],
    }

    if not batch:
        return stats

    force_fields = force_fields or ["language", "genre"]

    try:
        gemini_batch = [
            candidate
            for candidate in (build_gemini_candidate(track, force_fields=force_fields) for track in batch)
            if candidate
        ]

        if not gemini_batch:
            logger.info("Gemini import batch had no requested fields to fill.")
            return stats

        judge = GeminiJudge()
        response = judge.analyze_tracks_batch(gemini_batch)

        if isinstance(response, dict) and response.get("status") == "error":
            message = response.get("message", "Unknown Gemini error")
            stats["ai_failed"] = True
            stats["errors"].append(message)
            logger.error(f"Gemini import batch failed: {message}. Falling back to existing metadata.")
            return stats

        if not response or not getattr(response, "tracks", None):
            stats["ai_failed"] = True
            stats["errors"].append("No Gemini track suggestions returned")
            logger.warning("Gemini import batch returned no track suggestions. Falling back to existing metadata.")
            return stats

        batch_by_id = {
            str(track.get("id") or track.get("driveFileId")): track
            for track in batch
            if track.get("id") or track.get("driveFileId")
        }
        requested_fields_by_id = {
            str(track.get("id") or track.get("driveFileId")): set(track.get("fields_to_fill") or force_fields)
            for track in gemini_batch
            if track.get("id") or track.get("driveFileId")
        }

        for suggestion in response.tracks:
            track_ref = batch_by_id.get(str(suggestion.track_id))
            if not track_ref:
                logger.warning(f"Gemini returned suggestion for unknown import track ID {suggestion.track_id}. Skipped.")
                continue

            requested_fields = requested_fields_by_id.get(str(suggestion.track_id), set(force_fields))
            updated_this_track = False

            if "language" in requested_fields and suggestion.suggested_language.value:
                confidence = suggestion.suggested_language.confidence or 0.0
                if confidence > GEMINI_IMPORT_CONFIDENCE_THRESHOLD:
                    normalized_language = normalize_language_value(suggestion.suggested_language.value)
                    if normalized_language and normalized_language != "unknown" and track_ref.get("language") != normalized_language:
                        track_ref["language"] = normalized_language
                        stats["language_updates"] += 1
                        stats["fields_updated"] += 1
                        updated_this_track = True
                else:
                    stats["null_or_low_confidence"] += 1
            else:
                stats["null_or_low_confidence"] += 1

            if "genre" in requested_fields and suggestion.suggested_genre.value:
                confidence = suggestion.suggested_genre.confidence or 0.0
                if confidence > GEMINI_IMPORT_CONFIDENCE_THRESHOLD:
                    normalized_genre = normalize_genre_value(suggestion.suggested_genre.value)
                    if normalized_genre and track_ref.get("genre") != normalized_genre:
                        track_ref["genre"] = normalized_genre
                        stats["genre_updates"] += 1
                        stats["fields_updated"] += 1
                        updated_this_track = True
                else:
                    stats["null_or_low_confidence"] += 1
            else:
                stats["null_or_low_confidence"] += 1

            if updated_this_track:
                stats["tracks_updated"] += 1
                logger.info(f"Gemini import metadata applied to '{track_ref.get('title', 'Unknown')}'.")

        logger.info(
            "Gemini import batch complete: "
            f"{stats['tracks_updated']} tracks updated, "
            f"{stats['fields_updated']} fields updated "
            f"({stats['language_updates']} language, {stats['genre_updates']} genre)."
        )
        return stats

    except Exception as exc:
        message = str(exc)
        stats["ai_failed"] = True
        stats["errors"].append(message)
        logger.error(f"Gemini import batch raised an exception: {message}. Falling back to existing metadata.", exc_info=True)
        return stats
