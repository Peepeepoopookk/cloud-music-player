import subprocess
import json
import logging

logger = logging.getLogger(__name__)

def extract_duration(file_path):
    """
    Extracts accurate duration using ffprobe.
    Returns (duration_string, duration_seconds) e.g., ("03:35", 215).
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration_float = float(data['streams'][0]['duration'])
            duration_seconds = int(round(duration_float))
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            return f"{minutes:02d}:{seconds:02d}", duration_seconds
    except Exception as e:
        logger.warning(f"Could not read audio duration using ffprobe: {e}")
    return "--:--", None
