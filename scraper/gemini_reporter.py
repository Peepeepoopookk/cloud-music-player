import os
import json
import logging
import time
from typing import List, Dict, Any

from scraper.gemini_metadata_judge import GeminiJudge, normalize_genre_value, normalize_language_value

logger = logging.getLogger(__name__)

def generate_dry_run_report(db_tracks: List[Dict[str, Any]], batch_size: int = 15) -> Dict[str, Any]:
    """
    Chunks tracks into batches, queries Gemini for metadata assessments, 
    and compiles a final dry-run report highlighting discrepancies.
    """
    try:
        judge = GeminiJudge()
    except ValueError as e:
        logger.error(f"Initialization Error: {e}")
        return {}

    report = {
        "summary": {
            "total_tracks_processed": 0,
            "batches_processed": 0,
            "discrepancies_found": 0
        },
        "track_reports": []
    }

    # Split into batches
    for i in range(0, len(db_tracks), batch_size):
        batch = db_tracks[i:i + batch_size]
        logger.info(f"Processing batch {report['summary']['batches_processed'] + 1} ({len(batch)} tracks)...")

        batch_result = judge.analyze_tracks_batch(batch)
        report["summary"]["batches_processed"] += 1

        if not batch_result or not batch_result.tracks:
            logger.error(f"Batch {report['summary']['batches_processed']} failed or returned empty.")
            continue

        # Map original tracks by ID for fast lookup
        original_tracks_map = {t.get("id"): t for t in batch if t.get("id")}

        for suggestion in batch_result.tracks:
            track_id = suggestion.track_id
            original = original_tracks_map.get(track_id)
            if not original:
                continue

            report["summary"]["total_tracks_processed"] += 1
            discrepancies = []

            # Internal helper to detect and log meaningful discrepancies
            def check_discrepancy(field_name: str, orig_val: Any, sugg_obj: Any):
                if sugg_obj.value is None:
                    return
                if field_name == "language":
                    orig_val = normalize_language_value(orig_val) or orig_val
                    sugg_val = normalize_language_value(sugg_obj.value) or sugg_obj.value
                elif field_name == "genre":
                    orig_val = normalize_genre_value(orig_val) or orig_val
                    sugg_val = normalize_genre_value(sugg_obj.value) or sugg_obj.value
                else:
                    sugg_val = sugg_obj.value
                # Check for mismatch with reasonable confidence (e.g. > 0.6)
                if str(sugg_val).lower() != str(orig_val).lower() and sugg_obj.confidence > 0.6:
                    discrepancies.append({
                        "field": field_name,
                        "original": orig_val,
                        "suggested": sugg_val,
                        "confidence": sugg_obj.confidence
                    })

            # Check specific fields
            check_discrepancy("language", original.get("language", "unknown"), suggestion.suggested_language)
            check_discrepancy("genre", original.get("genre", "Unknown"), suggestion.suggested_genre)
            check_discrepancy("title", original.get("title", ""), suggestion.clean_title)
            check_discrepancy("artist", original.get("artist", ""), suggestion.clean_artist)

            if discrepancies:
                report["summary"]["discrepancies_found"] += 1

            report["track_reports"].append({
                "track_id": track_id,
                "original_title": original.get("title"),
                "original_artist": original.get("artist"),
                "reasoning": suggestion.reasoning,
                "discrepancies": discrepancies,
                "full_suggestion": suggestion.model_dump()
            })

        # Sleep briefly to avoid aggressive rate limiting between batches
        time.sleep(2)

    return report


def save_dry_run_report(report: Dict[str, Any]):
    """
    Saves the generated dry-run report to a local JSON file.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_dir = os.path.join(project_root, "scraper", "output")
    os.makedirs(output_dir, exist_ok=True)
    
    output_path = os.path.join(output_dir, "gemini_dry_run_report.json")

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"Success: Dry run report saved locally to {output_path}")
    except Exception as e:
        print(f"Error saving report: {e}")
