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
                "track_id": t.get("id"),
                "title": t.get("title", ""),
                "artist": t.get("artist", ""),
                "album": t.get("album", ""),
                "genre": t.get("genre", "Unknown"),
                "language": t.get("language", "unknown"),
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

Title/artist cleanup rules:
- Only clean obvious formatting noise, casing, separators, or duplicated artist text.
- Do not rewrite stylized titles or artist names unless the correction is obvious.
"""

    def _normalize_response(self, response: BatchMetadataResponse, tracks: List[Dict[str, Any]]) -> BatchMetadataResponse:
        input_ids = {str(t.get("id")) for t in tracks if t.get("id")}
        response_ids = {str(t.track_id) for t in response.tracks if t.track_id}

        missing_ids = input_ids - response_ids
        extra_ids = response_ids - input_ids
        if missing_ids:
            logger.warning(f"Gemini response missing track IDs: {sorted(missing_ids)}")
        if extra_ids:
            logger.warning(f"Gemini response included unexpected track IDs: {sorted(extra_ids)}")

        for suggestion in response.tracks:
            if suggestion.suggested_language.value is not None:
                normalized_language = normalize_language_value(suggestion.suggested_language.value)
                if normalized_language in CANONICAL_LANGUAGE_VALUES and normalized_language != "unknown":
                    suggestion.suggested_language.value = normalized_language
                else:
                    suggestion.suggested_language.value = None

            if suggestion.suggested_genre.value is not None:
                normalized_genre = normalize_genre_value(suggestion.suggested_genre.value)
                if normalized_genre and normalized_genre in CANONICAL_GENRE_VALUES:
                    suggestion.suggested_genre.value = normalized_genre
                else:
                    suggestion.suggested_genre.value = None

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
