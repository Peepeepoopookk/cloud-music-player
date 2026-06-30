import os
import json
import logging
from typing import List, Dict, Any, Optional

from google import genai
from google.genai import types

from scraper.gemini_schema import BatchMetadataResponse

logger = logging.getLogger(__name__)

LANGUAGE_VALUE_MAP = {
    "en": "english",
    "eng": "english",
    "english": "english",
    "hi": "hindi",
    "hin": "hindi",
    "hindi": "hindi",
    "ta": "tamil",
    "tam": "tamil",
    "tamil": "tamil",
    "ml": "malayalam",
    "mal": "malayalam",
    "malayalam": "malayalam",
    "es": "spanish",
    "spa": "spanish",
    "spanish": "spanish",
    "ko": "korean",
    "kor": "korean",
    "korean": "korean",
    "fr": "french",
    "fra": "french",
    "fre": "french",
    "french": "french",
    "unknown": "unknown",
    "none": "unknown",
    "null": "unknown"
}

CANONICAL_LANGUAGE_VALUES = {
    "english",
    "hindi",
    "tamil",
    "malayalam",
    "indian",
    "spanish",
    "korean",
    "french",
    "unknown"
}

GENRE_VALUE_MAP = {
    "hip hop": "hip-hop",
    "hiphop": "hip-hop",
    "hip-hop/rap": "hip-hop",
    "rap": "hip-hop",
    "rnb": "r&b",
    "r and b": "r&b",
    "r&b/soul": "r&b",
    "edm": "electronic",
    "dance": "electronic",
    "dance-pop": "pop",
    "electro-pop": "pop",
    "indian pop": "pop",
    "indian-pop": "pop",
    "folk-pop": "folk",
    "indian film": "bollywood",
    "indian film soundtrack": "bollywood",
    "indian soundtrack": "bollywood",
    "film soundtrack": "bollywood",
    "soundtrack": "bollywood",
    "synth wave": "synthwave",
    "lofi": "lo-fi",
    "lo fi": "lo-fi",
    "indian classical": "indian-classical",
    "indian-classical": "indian-classical",
    "carnatic classical": "carnatic",
    "kpop": "k-pop",
    "jpop": "j-pop",
    "cpop": "c-pop",
    "unknown": "Unknown",
    "none": "Unknown",
    "null": "Unknown"
}

CANONICAL_GENRE_VALUES = {
    "pop",
    "hip-hop",
    "r&b",
    "electronic",
    "rock",
    "latin",
    "k-pop",
    "classical",
    "jazz",
    "blues",
    "country",
    "metal",
    "indie",
    "alternative",
    "reggae",
    "soul",
    "funk",
    "disco",
    "house",
    "techno",
    "ambient",
    "folk",
    "punk",
    "gospel",
    "afrobeats",
    "dancehall",
    "trap",
    "drill",
    "phonk",
    "synthwave",
    "lo-fi",
    "bollywood",
    "indian-classical",
    "carnatic",
    "devotional",
    "anime",
    "j-pop",
    "c-pop"
}

def normalize_language_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return LANGUAGE_VALUE_MAP.get(normalized, normalized)

def is_noncanonical_language_value(value: Optional[str]) -> bool:
    if value is None:
        return True
    normalized = normalize_language_value(value)
    if not normalized:
        return True
    return normalized != str(value).strip().lower() or normalized not in CANONICAL_LANGUAGE_VALUES

def normalize_genre_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    normalized = " ".join(normalized.replace("_", "-").split())
    if normalized in GENRE_VALUE_MAP:
        return GENRE_VALUE_MAP[normalized]
    if normalized in CANONICAL_GENRE_VALUES:
        return normalized

    for canonical in sorted(CANONICAL_GENRE_VALUES, key=len, reverse=True):
        if canonical in normalized:
            return canonical

    if "hip" in normalized and "hop" in normalized:
        return "hip-hop"
    if "rhythm" in normalized and "blues" in normalized:
        return "r&b"
    if "bollywood" in normalized:
        return "bollywood"
    if "indian" in normalized and "pop" in normalized:
        return "pop"
    if "film" in normalized and "soundtrack" in normalized:
        return "bollywood"

    return None

def is_noncanonical_genre_value(value: Optional[str]) -> bool:
    if value is None:
        return True
    normalized = normalize_genre_value(value)
    if not normalized:
        return True
    return normalized != str(value).strip().lower() or normalized not in CANONICAL_GENRE_VALUES

def get_gemini_fields_to_fill(track: Dict[str, Any], force_fields: Optional[List[str]] = None) -> List[str]:
    fields = []
    force_fields = force_fields or []

    language_value = normalize_language_value(track.get("language"))
    language_needs_ai = (
        not language_value
        or language_value == "unknown"
        or is_noncanonical_language_value(track.get("language"))
    )
    if "language" in force_fields or language_needs_ai:
        fields.append("language")

    genre_value = normalize_genre_value(track.get("genre"))
    genre_needs_ai = (
        not genre_value
        or genre_value == "Unknown"
        or is_noncanonical_genre_value(track.get("genre"))
    )
    if "genre" in force_fields or genre_needs_ai:
        fields.append("genre")

    return fields

def build_gemini_candidate(track: Dict[str, Any], force_fields: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    fields_to_fill = get_gemini_fields_to_fill(track, force_fields=force_fields)
    if not fields_to_fill:
        return None

    candidate = dict(track)
    lyrics_present = bool(candidate.get("lyrics"))
    synced_lyrics_present = bool(candidate.get("syncedLyrics"))
    source_present = bool(candidate.get("source") and candidate.get("source") != "unknown")
    title_present = bool(candidate.get("title"))
    artist_present = bool(candidate.get("artist"))
    album_present = bool(candidate.get("album") and candidate.get("album") != "Unknown Album")

    evidence_signals = []
    if lyrics_present:
        evidence_signals.append("lyrics")
    if synced_lyrics_present:
        evidence_signals.append("syncedLyrics")
    if title_present:
        evidence_signals.append("title")
    if artist_present:
        evidence_signals.append("artist")
    if album_present:
        evidence_signals.append("album")
    if source_present:
        evidence_signals.append("source")

    evidence_score = 0
    if "language" in fields_to_fill:
        evidence_score += 5 if lyrics_present or synced_lyrics_present else 0
        evidence_score += 2 if title_present else 0
        evidence_score += 1 if artist_present else 0
        evidence_score += 1 if source_present else 0
    if "genre" in fields_to_fill:
        evidence_score += 3 if artist_present else 0
        evidence_score += 2 if title_present else 0
        evidence_score += 2 if album_present else 0
        evidence_score += 1 if source_present else 0
        evidence_score += 1 if lyrics_present or synced_lyrics_present else 0

    candidate["fields_to_fill"] = fields_to_fill
    candidate["evidence_signals"] = evidence_signals
    candidate["evidence_score"] = evidence_score
    has_language = "language" in fields_to_fill
    has_genre = "genre" in fields_to_fill
    if has_language and has_genre:
        field_priority = 0
    elif has_language:
        field_priority = 1
    else:
        field_priority = 2

    candidate["_gemini_priority"] = (
        field_priority,
        -evidence_score,
        0 if lyrics_present or synced_lyrics_present else 1,
        str(candidate.get("title") or "").lower()
    )
    return candidate

def build_gemini_candidates(tracks: List[Dict[str, Any]], force_fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    candidates = []
    for track in tracks:
        candidate = build_gemini_candidate(track, force_fields=force_fields)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda item: item.get("_gemini_priority", (9, 0, 9, "")))
    return candidates

class GeminiJudge:
    def __init__(self, model_name: str = "gemini-3.1-flash-lite"):
        self.api_key = os.environ.get('GEMINI_API_KEY')
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable is missing. Cannot initialize GeminiJudge.")
            
        self.client = genai.Client()
        self.model_name = model_name

    def _build_prompt(self, tracks: List[Dict[str, Any]]) -> str:
        # Prepare a streamlined view of tracks to avoid bloating the prompt
        tracks_for_prompt = []
        for t in tracks:
            tracks_for_prompt.append({
                "track_id": t.get("id") or t.get("driveFileId"),
                "title": t.get("title", ""),
                "artist": t.get("artist", ""),
                "album": t.get("album", ""),
                "genre": t.get("genre", "Unknown"),
                "language": t.get("language", "unknown"),
                "fields_to_fill": t.get("fields_to_fill") or ["language", "genre"],
                "evidence_signals": t.get("evidence_signals") or [],
                "evidence_score": t.get("evidence_score", 0),
                "source": t.get("source", "unknown"),
                "lyrics": t.get("lyrics", "")[:500] if t.get("lyrics") else None # truncate lyrics if present
            })

        return f"""
You are an expert, unopinionated music metadata classifier.
Your task is to evaluate a batch of music tracks and provide structured metadata suggestions for the Wavify music database.

Batch of Tracks:
{json.dumps(tracks_for_prompt, indent=2, ensure_ascii=False)}

Rules:
1. Act purely as a classifier. Do not hallucinate or guess if you lack information.
2. Rely strictly on the provided fields (especially title, artist, and lyrics if present).
3. Treat source/playlist text as weak context only. Never infer language solely because source contains words like Tamil, Hindi, Malayalam, Indian, or Global.
4. Lyrics are the strongest language signal. If lyrics are absent and title/artist are not enough, output null for suggested_language.value.
5. Every suggestion MUST include your confidence score (0.0 to 1.0).
6. If you cannot make a determination for a field, output null for its value.
7. Return exactly one suggestion object per input track, using the same track_id.
8. Provide a succinct overall 'reasoning' for your assessment per track.
9. Only fill fields listed in each track's fields_to_fill array. For fields not listed there, output null.
10. Use evidence_signals and evidence_score to understand how much supporting context exists, but still make the final decision from the actual metadata text.

Language rules:
- For suggested_language.value, use Wavify's lowercase full-name values only:
  english, hindi, tamil, malayalam, indian, spanish, korean, french, or unknown.
- Do not return ISO language codes such as en, hi, ta, ml, es, ko, or fr.
- Prefer null over unknown when there is not enough evidence to improve the existing field.

Genre rules:
- For suggested_genre.value, use one Wavify genre bucket only:
  pop, hip-hop, r&b, electronic, rock, latin, k-pop, classical, jazz, blues,
  country, metal, indie, alternative, reggae, soul, funk, disco, house, techno,
  ambient, folk, punk, gospel, afrobeats, dancehall, trap, drill, phonk,
  synthwave, lo-fi, bollywood, indian-classical, carnatic, devotional, anime,
  j-pop, or c-pop.
- Do not invent hybrid labels such as folk-pop or electro-pop. Pick the closest bucket, or output null.

Title/artist rules:
- Titles and artists are context only. Do not correct, rewrite, normalize, or restyle them.
- Always output null for clean_title.value and clean_artist.value.
"""

    def _normalize_response(self, response: BatchMetadataResponse, tracks: List[Dict[str, Any]]) -> BatchMetadataResponse:
        track_fields_by_id = {
            str(t.get("id") or t.get("driveFileId")): set(t.get("fields_to_fill") or ["language", "genre"])
            for t in tracks
            if t.get("id") or t.get("driveFileId")
        }
        input_ids = set(track_fields_by_id.keys())
        response_ids = {str(t.track_id) for t in response.tracks if t.track_id}

        missing_ids = input_ids - response_ids
        extra_ids = response_ids - input_ids
        if missing_ids:
            logger.warning(f"Gemini response missing track IDs: {sorted(missing_ids)}")
        if extra_ids:
            logger.warning(f"Gemini response included unexpected track IDs: {sorted(extra_ids)}")

        for suggestion in response.tracks:
            requested_fields = track_fields_by_id.get(str(suggestion.track_id), {"language", "genre"})

            if "language" not in requested_fields:
                suggestion.suggested_language.value = None
            if suggestion.suggested_language.value is not None:
                normalized_language = normalize_language_value(suggestion.suggested_language.value)
                if normalized_language in CANONICAL_LANGUAGE_VALUES and normalized_language != "unknown":
                    suggestion.suggested_language.value = normalized_language
                else:
                    suggestion.suggested_language.value = None

            if "genre" not in requested_fields:
                suggestion.suggested_genre.value = None
            if suggestion.suggested_genre.value is not None:
                normalized_genre = normalize_genre_value(suggestion.suggested_genre.value)
                if normalized_genre and normalized_genre in CANONICAL_GENRE_VALUES:
                    suggestion.suggested_genre.value = normalized_genre
                else:
                    suggestion.suggested_genre.value = None

            suggestion.clean_title.value = None
            suggestion.clean_artist.value = None

            for confidence_obj in (
                suggestion.suggested_language,
                suggestion.suggested_genre,
                suggestion.suggested_mood,
                suggestion.clean_title,
                suggestion.clean_artist,
                suggestion.is_remix_or_live
            ):
                confidence_obj.confidence = max(0.0, min(1.0, float(confidence_obj.confidence)))

        return response

    def _clean_json_text(self, text: str) -> str:
        """Strips markdown formatting from the response if present."""
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    def analyze_tracks_batch(self, tracks: List[Dict[str, Any]]) -> Any:
        """
        Accepts a chunk of tracks (e.g., 10-20), formats them into a prompt,
        calls the Gemini API, and parses the JSON response using Structured Outputs.
        """
        if not tracks:
            return None

        prompt = self._build_prompt(tracks)

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=BatchMetadataResponse,
                    temperature=0.0
                ),
            )

            if response.text:
                cleaned_text = self._clean_json_text(response.text)
                parsed_json = json.loads(cleaned_text)
                parsed_response = BatchMetadataResponse(**parsed_json)
                return self._normalize_response(parsed_response, tracks)
            else:
                logger.warning("Empty text returned from Gemini API.")
                return {
                    "status": "error",
                    "message": "Empty response from Gemini API.",
                    "failed_track_ids": [t.get('id') for t in tracks]
                }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini response as JSON: {e}")
            return {
                "status": "error",
                "message": f"JSON Decode Error: {str(e)}",
                "failed_track_ids": [t.get('id') for t in tracks]
            }
        except Exception as e:
            logger.error(f"Error during Gemini API call for batch size {len(tracks)}: {e}")
            return {
                "status": "error",
                "message": str(e),
                "failed_track_ids": [t.get('id') for t in tracks]
            }
