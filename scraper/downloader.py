import os
import time
import random
import logging
import subprocess
import yt_dlp

# Configure logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

def check_ffmpeg_available():
    """
    Checks if ffmpeg is available in the system PATH.
    """
    try:
        # Run ffmpeg -version with subprocess to verify if it is in PATH
        result = subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.warning(f"Error checking ffmpeg presence: {e}")
        return False

def sanitize_filename(filename):
    """
    Removes characters that are invalid in Windows/Unix filenames.
    """
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '')
    return filename.strip()

def choose_best_video(entries, artist):
    """
    Heuristically filters search results to prefer official artist uploads
    and music videos over fan uploads or covers.
    """
    if not entries:
        return None
        
    best_entry = None
    highest_score = -100
    artist_lower = artist.lower()
    
    for entry in entries:
        score = 0
        channel = entry.get('channel', '').lower()
        title = entry.get('title', '').lower()
        
        # Positive indicators:
        if artist_lower in channel:
            score += 15
        if 'vevo' in channel or 'vevo' in title:
            score += 8
        if 'topic' in channel:
            score += 8
        if 'official' in title:
            score += 5
        if 'audio' in title:
            score += 3
        if 'video' in title:
            score += 2
            
        # Negative indicators (avoid covers, fan-made, reactions, etc.):
        if 'cover' in title and 'cover' not in artist_lower:
            score -= 25
        if 'fan made' in title or 'fan-made' in title or 'fanmade' in title:
            score -= 20
        if 'reaction' in title:
            score -= 30
        if 'mashup' in title:
            score -= 15
        if 'karaoke' in title:
            score -= 25
            
            
        logger.debug(f"Candidate: '{title}' by channel '{channel}' - Score: {score}")
        
        if score > highest_score:
            highest_score = score
            best_entry = entry
            
    if highest_score < 0:
        return None
        
    return best_entry if best_entry else entries[0]

def download_track(title, artist, output_dir, track_id=None, cancel_check_callback=None):
    """
    Downloads the best quality audio for a track using yt-dlp.
    Searches YouTube, enforces quality controls, and saves as '{track_id}.opus' (or fallback).
    Returns the absolute path of the downloaded file.
    """
    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    # Small randomized jitter delay to prevent rapid-fire burst queries while avoiding long idle overhead
    delay = random.uniform(0.2, 0.5)
    logger.info(f"Waiting {delay:.2f} seconds before searching YouTube...")
    time.sleep(delay)
    if cancel_check_callback and cancel_check_callback():
        raise Exception("Download cancelled by user")
    
    # Clean the artist/title to form a safe query
    safe_artist = sanitize_filename(artist)
    safe_title = sanitize_filename(title)
    
    if track_id:
        out_filename = sanitize_filename(str(track_id))
    else:
        import uuid
        out_filename = sanitize_filename(f"{safe_artist}_{safe_title}_{uuid.uuid4().hex[:8]}")
    
    search_query = f"ytsearch5:{safe_artist} - {safe_title} official audio"
    logger.info(f"Searching YouTube for: '{search_query}'")
    
    # Search pass (Pass 1)
    ydl_opts_search = {
        'format': 'bestaudio/best',
        'quiet': True,
        'extract_flat': True,  # Fetch metadata without downloading
        'prefer_free_formats': True,
        'socket_timeout': 30,
        'retries': 3,
        'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
    }
    if os.path.exists('/tmp/cookies.txt'):
        ydl_opts_search['cookiefile'] = '/tmp/cookies.txt'
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
            search_results = ydl.extract_info(search_query, download=False)
            entries = search_results.get('entries', [])
            
        if cancel_check_callback and cancel_check_callback():
            raise Exception("Download cancelled by user")

        if not entries:
            raise ValueError(f"No search results found on YouTube for query: {search_query}")
            
        # Score and sort all entries
        scored_entries = []
        artist_lower = artist.lower()
        for entry in entries:
            score = 0
            channel = entry.get('channel', '').lower()
            title = entry.get('title', '').lower()
            
            # Positive indicators:
            if artist_lower in channel:
                score += 15
            if 'vevo' in channel or 'vevo' in title:
                score += 8
            if 'topic' in channel:
                score += 8
            if 'official' in title:
                score += 5
            if 'audio' in title:
                score += 3
            if 'video' in title:
                score += 2
                
            # Negative indicators (avoid covers, fan-made, reactions, etc.):
            if 'cover' in title and 'cover' not in artist_lower:
                score -= 25
            if 'fan made' in title or 'fan-made' in title or 'fanmade' in title:
                score -= 20
            if 'reaction' in title:
                score -= 30
            if 'mashup' in title:
                score -= 15
            if 'karaoke' in title:
                score -= 25
                
            scored_entries.append((score, entry))
            
        # Sort by score descending
        scored_entries.sort(key=lambda x: x[0], reverse=True)
        
        # Filter out candidates that do not meet the minimum score threshold
        MIN_SCORE_THRESHOLD = 0
        valid_entries = [(s, e) for s, e in scored_entries if s >= MIN_SCORE_THRESHOLD]
        
        if not valid_entries:
            raise ValueError(f"No YouTube candidates met the minimum score threshold of {MIN_SCORE_THRESHOLD} for query: {search_query}")
        
        ffmpeg_available = check_ffmpeg_available()
        final_opus_path = os.path.join(output_dir, f"{out_filename}.opus")

        last_error = None
        for score, entry in valid_entries:
            video_url = entry.get('url') or entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
            logger.info(f"Attempting download for: '{entry.get('title')}' ({video_url}) [Score: {score}]")
            
            try:
                if cancel_check_callback and cancel_check_callback():
                    raise Exception("Download cancelled by user")

                # Configure the downloader options directly for a single download pass (Pass 2)
                # Formats: prioritize native Opus streams >= 128k, fallback to other Opus, then M4A >= 128k, then best
                ydl_opts_download = {
                    'format': 'bestaudio[acodec=opus][abr>=128]/bestaudio[acodec=opus]/bestaudio[ext=m4a][abr>=128]/bestaudio[ext=m4a]/bestaudio/best',
                    'outtmpl': os.path.join(output_dir, f"{out_filename}.%(ext)s"),
                    'max_filesize': 20 * 1024 * 1024,
                    'concurrent_fragment_downloads': 4,
                    'quiet': False,
                    'prefer_free_formats': True,
                    'socket_timeout': 30,
                    'retries': 3,
                    'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
                }
                if os.path.exists('/tmp/cookies.txt'):
                    ydl_opts_download['cookiefile'] = '/tmp/cookies.txt'
                    
                if cancel_check_callback:
                    def yt_dlp_progress_hook(d):
                        if cancel_check_callback():
                            raise Exception("Download cancelled by user")
                    ydl_opts_download['progress_hooks'] = [yt_dlp_progress_hook]

                if ffmpeg_available:
                    pp_opts = {
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'opus',
                        'preferredquality': '192',
                    }
                    ydl_opts_download['postprocessors'] = [pp_opts]
                else:
                    logger.warning("ffmpeg is not available. Downloading track in native format.")
                    
                # Execute the download in a single lifecycle pass (Pass 2)
                with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
                    info_dict = ydl.extract_info(video_url, download=True)
                    
                if cancel_check_callback and cancel_check_callback():
                    raise Exception("Download cancelled by user")

                # Verify output file path
                if os.path.exists(final_opus_path):
                    logger.info(f"Successfully saved track as Opus: {final_opus_path}")
                    return os.path.abspath(final_opus_path)
                    
                # If yt-dlp failed to convert, or if ffmpeg was not available during download, search for native file formats
                for ext in ['webm', 'm4a', 'mp3', 'ogg', 'wav']:
                    native_path = os.path.join(output_dir, f"{out_filename}.{ext}")
                    if os.path.exists(native_path):
                        if ffmpeg_available:
                            logger.info(f"Converting {ext} to opus using ffmpeg directly: {native_path}")
                            try:
                                subprocess.run([
                                    'ffmpeg', '-y', '-i', native_path,
                                    '-c:a', 'libopus', '-b:a', '192k',
                                    final_opus_path
                                ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                                os.remove(native_path)
                                return os.path.abspath(final_opus_path)
                            except subprocess.CalledProcessError as e:
                                logger.error(f"FFmpeg manual conversion failed: {e.stderr.decode('utf-8', errors='ignore')}")
                                return os.path.abspath(native_path)
                        elif ext == 'webm':
                            acodec = (info_dict or {}).get('acodec', '')
                            if 'opus' in str(acodec).lower():
                                os.rename(native_path, final_opus_path)
                                logger.info(f"Renamed native webm/opus file to .opus: {final_opus_path}")
                                return os.path.abspath(final_opus_path)
                            logger.warning(f"Saved track in native format due to missing ffmpeg: {native_path}")
                            return os.path.abspath(native_path)
                        else:
                            logger.warning(f"Saved track in native format due to missing ffmpeg: {native_path}")
                            return os.path.abspath(native_path)
                            
            except Exception as e:
                logger.warning(f"Failed download attempt for '{entry.get('title')}' using url '{video_url}': {e}")
                last_error = e
                continue
                
        if last_error:
            raise last_error
        raise ValueError(f"No suitable search results could be successfully downloaded for: {search_query}")
        
    except Exception as e:
        logger.error(f"Failed to download track '{artist} - {title}': {e}", exc_info=True)
        raise
