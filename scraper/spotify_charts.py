import os
import sys
import json
import re
import time
import random
import logging
import datetime
import requests
import urllib.parse
import difflib

# Configure logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9"
}

# Playlist mappings
REGIONAL_PLAYLISTS = {
    "IN": "37i9dQZEVXbLZ527wRLeb9", # Top 50 India
    "US": "37i9dQZEVXbLRQDuF5jeBp", # Top 50 USA
    "GB": "37i9dQZEVXbLnpxZdf47gP", # Top 50 UK
    "NG": "37i9dQZEVXbM41e1n3n67p", # Top 50 Nigeria
    "BR": "37i9dQZEVXbMXbGo6n65UT"  # Top 50 Brazil
}

GENRE_PLAYLISTS = {
    "pop": "37i9dQZF1DXcBWIGoYBM5M",        # Today's Top Hits
    "hip-hop": "37i9dQZF1DX0XUsuxWHRQd",    # RapCaviar
    "r&b": "37i9dQZF1DX4SBhb3fqCJd",        # Are & Be
    "latin": "37i9dQZF1DX10zKzsJ2jva",      # Viva Latino
    "k-pop": "37i9dQZF1DX9tPFwD00N1G",      # K-Pop ON!
    "electronic": "37i9dQZF1DX4dyzvuaRJ0n"  # mint
}

# Language maps
STOREFRONT_MAP = {
    "USA": "english",
    "GBR": "english",
    "AUS": "english",
    "CAN": "english",
    "NZL": "english",
    "IND": "hindi",
    "ESP": "spanish",
    "MEX": "spanish",
    "KOR": "korean",
    "FRA": "french"
}

LANGUAGE_MAP = {
    "eng": "english",
    "hin": "hindi",
    "mal": "malayalam",
    "tam": "tamil",
    "spa": "spanish",
    "kor": "korean",
    "fra": "french"
}

def scrape_genre_from_track_page(spotify_id):
    """
    Scrapes the individual Spotify track page to extract the genre tag.
    Since Spotify track pages are rendered dynamically on the client,
    this attempts parsing meta tags or JSON blocks, defaulting to 'Unknown'.
    """
    track_url = f"https://open.spotify.com/track/{spotify_id}"
    logger.info(f"scrape_genre_from_track_page: Requesting track page: {track_url}")
    
    try:
        response = requests.get(track_url, headers=HEADERS, timeout=5)
        if response.status_code != 200:
            logger.warning(f"scrape_genre_from_track_page: Failed to fetch track page for {spotify_id}, HTTP status: {response.status_code}")
            return "Unknown"
            
        html = response.text
        genre_matches = re.findall(r'"genre"\s*:\s*"([^"]+)"', html)
        if genre_matches:
            genre = genre_matches[0]
            logger.info(f"scrape_genre_from_track_page: Found genre tag via pattern matching: {genre}")
            return genre
            
        meta_genre = re.findall(r'<meta[^>]+property="music:genre"[^>]+content="([^"]+)"', html)
        if meta_genre:
            genre = meta_genre[0]
            logger.info(f"scrape_genre_from_track_page: Found genre tag via meta property: {genre}")
            return genre
            
    except Exception as e:
        logger.error(f"scrape_genre_from_track_page: Error while scraping track page for {spotify_id}: {e}")
        
    logger.info(f"scrape_genre_from_track_page: Genre not found on page for {spotify_id}. Defaulting to 'Unknown'.")
    return "Unknown"

def scrape_spotify_embed_playlist(playlist_id):
    """
    Scrapes a public Spotify playlist embed page to extract the tracks list.
    """
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    logger.info(f"scrape_spotify_embed_playlist: Scraping embed playlist: {url}")
    tracks = []
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        if response.status_code != 200:
            logger.warning(f"scrape_spotify_embed_playlist: Failed to fetch embed playlist {playlist_id}, HTTP status: {response.status_code}")
            return []
            
        html = response.text
        next_data = re.findall(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', html)
        if next_data:
            data = json.loads(next_data[0]) if 'json' in sys.modules else __import__('json').loads(next_data[0])
            props = data.get("props", {})
            page_props = props.get("pageProps", {})
            state = page_props.get("state", {})
            state_data = state.get("data", {})
            entity = state_data.get("entity", {})
            track_list = entity.get("trackList", [])
            for track in track_list:
                uri = track.get("uri", "")
                spotify_id = uri.split(":")[-1] if uri else "UnknownID"
                title = track.get("title", "Unknown Title")
                artist = track.get("subtitle", "Unknown Artist")
                tracks.append({
                    "title": title,
                    "artist": artist,
                    "spotify_id": spotify_id,
                    "genre": "Unknown",
                    "language": "unknown"
                })
            logger.info(f"scrape_spotify_embed_playlist: Successfully scraped {len(tracks)} tracks from embed playlist {playlist_id}.")
        else:
            logger.warning(f"scrape_spotify_embed_playlist: Could not find __NEXT_DATA__ in embed playlist {playlist_id}.")
    except Exception as e:
        logger.error(f"scrape_spotify_embed_playlist: Error scraping embed playlist {playlist_id}: {e}", exc_info=True)
    return tracks

def get_trending_tracks(limit=10):
    """
    Scrapes the Spotify Weekly Top 50 Global charts.
    Returns a list of dicts: rank, title, artist, genre, spotify_id, source.
    """
    charts_api_url = "https://charts-spotify-com-service.spotify.com/public/v0/charts"
    logger.info("get_trending_tracks: Fetching weekly global charts data...")
    
    try:
        response = requests.get(charts_api_url, headers=HEADERS, timeout=5)
        if response.status_code != 200:
            raise requests.HTTPError(f"Charts service returned status code {response.status_code}")
            
        data = response.json()
        chart_responses = data.get("chartEntryViewResponses", [])
        if not chart_responses:
            raise ValueError("No charts found in response data")
            
        target_chart = chart_responses[0]
        entries = target_chart.get("entries", [])
        logger.info(f"get_trending_tracks: Found {len(entries)} tracks in Spotify global weekly chart.")
        
        trending_tracks = []
        for i, entry in enumerate(entries[:limit]):
            try:
                chart_data = entry.get("chartEntryData", {})
                track_metadata = entry.get("trackMetadata", {})
                
                rank = chart_data.get("currentRank")
                title = track_metadata.get("trackName", "Unknown Title")
                
                artists = track_metadata.get("artists", [])
                artist_names = ", ".join(a.get("name", "Unknown Artist") for a in artists)
                
                track_uri = track_metadata.get("trackUri", "")
                spotify_id = track_uri.split(":")[-1] if track_uri else "UnknownID"
                
                # Respect request delays
                delay = random.uniform(0.5, 1.2)
                time.sleep(delay)
                
                genre = "Unknown"
                if spotify_id != "UnknownID":
                    genre = scrape_genre_from_track_page(spotify_id)
                
                track_info = {
                    "rank": rank,
                    "title": title,
                    "artist": artist_names,
                    "genre": genre,
                    "spotify_id": spotify_id,
                    "source": "Global Charts",
                    "language": "english" # Default global chart fallback
                }
                
                logger.info(f"get_trending_tracks: Processed track #{rank}: {title} by {artist_names} [{genre}]")
                trending_tracks.append(track_info)
                
            except Exception as entry_error:
                logger.error(f"get_trending_tracks: Error parsing chart entry {i}: {entry_error}", exc_info=True)
                
        return trending_tracks
        
    except Exception as e:
        logger.error(f"get_trending_tracks: Failed to retrieve trending tracks: {e}", exc_info=True)
        return []

def fetch_regional_charts(regions=["IN", "US", "GB", "NG", "BR"]):
    """
    fetches top 50 from each region's weekly Spotify chart using public embeds, returns combined list
    """
    logger.info("fetch_regional_charts: Fetching regional charts...")
    combined_tracks = []
    
    for region in regions:
        playlist_id = REGIONAL_PLAYLISTS.get(region)
        if not playlist_id:
            logger.warning(f"fetch_regional_charts: Unknown region code '{region}'")
            continue
            
        time.sleep(random.uniform(0.5, 1.5))
        tracks = scrape_spotify_embed_playlist(playlist_id)
        for t in tracks:
            t["source"] = f"Regional Chart ({region})"
            t["language"] = STOREFRONT_MAP.get(region, "unknown") if region in STOREFRONT_MAP else "unknown"
            if region == "IN":
                t["language"] = "hindi" # Default India to Hindi
            combined_tracks.append(t)
            
    return combined_tracks

def fetch_genre_charts(genres=["pop", "hip-hop", "r&b", "latin", "k-pop", "electronic"]):
    """
    fetches top songs from genre playlists, returns combined list
    """
    logger.info("fetch_genre_charts: Fetching genre charts...")
    combined_tracks = []
    
    for genre in genres:
        playlist_id = GENRE_PLAYLISTS.get(genre)
        if not playlist_id:
            logger.warning(f"fetch_genre_charts: Unknown genre name '{genre}'")
            continue
            
        time.sleep(random.uniform(0.5, 1.5))
        tracks = scrape_spotify_embed_playlist(playlist_id)
        for t in tracks:
            t["source"] = f"Genre Chart ({genre})"
            t["genre"] = genre
            if genre == "k-pop":
                t["language"] = "korean"
            elif genre == "latin":
                t["language"] = "spanish"
            else:
                t["language"] = "english"
            combined_tracks.append(t)
            
    return combined_tracks

def fetch_new_releases():
    """
    fetches recently released songs from the past 7 days using iTunes Search API RSS feed
    """
    logger.info("fetch_new_releases: Fetching iTunes new releases...")
    url = "https://rss.applemarketingtools.com/api/v2/us/music/most-played/100/songs.json"
    new_tracks = []
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        if response.status_code != 200:
            logger.warning(f"fetch_new_releases: iTunes RSS feed status: {response.status_code}")
            return []
            
        data = response.json()
        feed = data.get("feed", {})
        results = feed.get("results", [])
        now_date = datetime.date.today()
        
        for track in results:
            release_date_str = track.get("releaseDate")
            if release_date_str:
                try:
                    rel_date = datetime.date.fromisoformat(release_date_str)
                    delta = now_date - rel_date
                    if delta.days <= 7:
                        title = track.get("name")
                        artist = track.get("artistName")
                        
                        genres_list = track.get("genres", [])
                        genre_name = "Unknown"
                        if genres_list:
                            genre_name = genres_list[0].get("name", "Unknown")
                            
                        new_tracks.append({
                            "title": title,
                            "artist": artist,
                            "genre": genre_name,
                            "spotify_id": "UnknownID",
                            "source": "iTunes New Releases",
                            "language": "english" # Default US storefront
                        })
                except Exception as parse_err:
                    logger.warning(f"fetch_new_releases: Error parsing date '{release_date_str}': {parse_err}")
    except Exception as e:
        logger.error(f"fetch_new_releases: Error loading iTunes new releases: {e}", exc_info=True)
        
    return new_tracks

def resolve_spotify_id(title, artist):
    """
    Queries DuckDuckGo search to locate the Spotify track URL and extract its ID.
    """
    logger.info(f"resolve_spotify_id: Resolving Spotify ID for '{title}' by '{artist}'...")
    query = f"site:open.spotify.com/track {title} {artist}"
    url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
    
    for attempt in range(2):
        try:
            if attempt > 0:
                time.sleep(random.uniform(1.0, 2.0))
            r = requests.get(url, headers=HEADERS, timeout=5)
            if r.status_code == 200:
                spotify_ids = re.findall(r'open\.spotify\.com/track/([a-zA-Z0-9]+)', r.text)
                if spotify_ids:
                    logger.info(f"resolve_spotify_id: Successfully resolved Spotify ID: {spotify_ids[0]}")
                    return spotify_ids[0]
        except Exception as e:
            logger.warning(f"resolve_spotify_id: Attempt {attempt + 1} failed for '{title}': {e}")
            
    logger.info(f"resolve_spotify_id: Failed after 2 attempts for '{title}'. Returning None.")
    return None

def get_track_by_spotify_url(spotify_url):
    """
    Extracts the Spotify track ID from the URL, scrapes title/artist from Spotify,
    detects language, and searches iTunes for genre and album art.
    """
    logger.info(f"get_track_by_spotify_url: Processing URL: {spotify_url}")
    match = re.search(r'(?:open\.spotify\.com|spotify\.com)/track/([a-zA-Z0-9]+)', spotify_url)
    if not match:
        raise ValueError("Invalid Spotify track URL. Please check the link and try again.")
    spotify_id = match.group(1)
    logger.info(f"get_track_by_spotify_url: Extracted Spotify Track ID: {spotify_id}")
    
    title = None
    artist = None
    
    # Try embedding page scrape first (contains structured NEXT_DATA)
    embed_url = f"https://open.spotify.com/embed/track/{spotify_id}"
    try:
        r = requests.get(embed_url, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            next_data_match = re.findall(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', r.text)
            if next_data_match:
                data = json.loads(next_data_match[0])
                entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                if entity:
                    title = entity.get("title") or entity.get("name")
                    artists_list = entity.get("artists", [])
                    if artists_list:
                        artist = ", ".join(a.get("name", "") for a in artists_list if a.get("name"))
                    logger.info(f"get_track_by_spotify_url: Successfully scraped metadata from embed page. Title: '{title}', Artist: '{artist}'")
    except Exception as e:
        logger.warning(f"get_track_by_spotify_url: Error scraping embed page: {e}")

    # Fallback to standard track page scrape
    if not title or not artist:
        track_url = f"https://open.spotify.com/track/{spotify_id}"
        try:
            r = requests.get(track_url, headers=HEADERS, timeout=5)
            if r.status_code == 200:
                og_title_match = re.findall(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', r.text)
                if og_title_match:
                    title = og_title_match[0]
                og_desc_match = re.findall(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', r.text)
                if og_desc_match:
                    parts = [p.strip() for p in og_desc_match[0].split("·")]
                    if len(parts) >= 2:
                        artist = parts[1]
                logger.info(f"get_track_by_spotify_url: Scraped standard track page. Title: '{title}', Artist: '{artist}'")
        except Exception as e:
            logger.warning(f"get_track_by_spotify_url: Error scraping standard page: {e}")

    if not title:
        title = "Unknown Title"
    if not artist:
        artist = "Unknown Artist"
        
    # Detect language
    lang, _ = detect_track_language(title, artist)
    
    # Query iTunes Search API to get genre and high-res album art
    genre = "Unknown"
    album_art = ""
    try:
        search_term = f"{artist} {title}"
        itunes_url = "https://itunes.apple.com/search"
        params = {"term": search_term, "media": "music", "limit": 5}
        r = requests.get(itunes_url, params=params, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results:
                best_match = results[0]
                genre = best_match.get("primaryGenreName", "Unknown")
                artwork_url = best_match.get("artworkUrl100", "")
                if artwork_url:
                    album_art = artwork_url.replace("100x100", "600x600")
                logger.info(f"get_track_by_spotify_url: iTunes API result. Genre: '{genre}', Album Art: '{album_art}'")
    except Exception as e:
        logger.warning(f"get_track_by_spotify_url: Error querying iTunes Search API: {e}")

    return {
        "title": title,
        "artist": artist,
        "genre": genre,
        "language": lang,
        "spotify_id": spotify_id,
        "album_art": album_art
    }

def fetch_album_art(title, artist):
    """
    Searches iTunes Search API and finds the best matching result using difflib.SequenceMatcher.
    Returns the artworkUrl100 field with "100x100" replaced with "600x600" for high resolution, or None.
    """
    import difflib
    if not title or not artist:
        return None
    
    # Add random delay and proper error handling
    delay = random.uniform(0.5, 1.5)
    time.sleep(delay)
    
    logger.info(f"fetch_album_art: Searching iTunes for '{title}' by '{artist}'...")
    url = "https://itunes.apple.com/search"
    params = {
        "term": f"{artist} {title}",
        "media": "music",
        "limit": 5
    }
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=5)
        response.raise_for_status()
        results = response.json().get("results", [])
        if not results:
            logger.info(f"fetch_album_art: No results found for '{title}' by '{artist}'.")
            return None
            
          
        best_match = None
        best_score = -1.0
        
        norm_title = title.lower()
        norm_artist = artist.lower()
        
        for item in results:
            res_title = (item.get("trackName") or "").lower()
            res_artist = (item.get("artistName") or "").lower()
            
            title_ratio = difflib.SequenceMatcher(None, norm_title, res_title).ratio()
            artist_ratio = difflib.SequenceMatcher(None, norm_artist, res_artist).ratio()
            
            score = title_ratio + artist_ratio
            if score > best_score:
                best_score = score
                best_match = item
                
        if best_match:
            artwork_url = best_match.get("artworkUrl100")
            if artwork_url:
                high_res_url = artwork_url.replace("100x100", "600x600")
                logger.info(f"fetch_album_art: Found best matching artwork (score={best_score:.3f}): {high_res_url}")
                return high_res_url
                
    except Exception as e:
        logger.warning(f"fetch_album_art: Error fetching album art: {e}")
        
    return None

def detect_track_language(title, artist):
    """
    Detects language of a track using iTunes Search API country storefront first,
    then MusicBrainz API, and falls back to 'unknown'.
    Returns a tuple (language, method).
    """
    logger.info(f"detect_track_language: Checking language for '{title}' by '{artist}'...")
    
    # Priority 1: Check iTunes Search API
    try:
        time.sleep(random.uniform(0.5, 1.0))
        url = "https://itunes.apple.com/search"
        params = {"term": f"{title} {artist}", "entity": "song", "limit": 1}
        r = requests.get(url, params=params, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results:
                country = results[0].get("country")
                if country:
                    country_upper = country.upper()
                    if country_upper in STOREFRONT_MAP:
                        detected = STOREFRONT_MAP[country_upper]
                        logger.info(f"detect_track_language: Language detected via iTunes storefront ({country}): {detected}")
                        return detected, "itunes"
    except Exception as e:
        logger.warning(f"detect_track_language: iTunes Search API language detection failed: {e}")

    # Priority 2: Check MusicBrainz API
    try:
        time.sleep(random.uniform(1.0, 1.5))
        clean_title = re.sub(r'[\"\/\:]', ' ', title)
        clean_artist = re.sub(r'[\"\/\:]', ' ', artist)
        url = "https://musicbrainz.org/ws/2/recording/"
        params = {
            "query": f"{clean_title} {clean_artist}",
            "fmt": "json"
        }
        mb_headers = {
            "User-Agent": "CloudMusicPlayer/1.0.0 (contact@example.com)",
            "Accept": "application/json"
        }
        r = requests.get(url, params=params, headers=mb_headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            recordings = data.get("recordings", [])
            for rec in recordings:
                works = rec.get("relations", []) or rec.get("works", [])
                for work in works:
                    lang = work.get("language")
                    if lang and lang in LANGUAGE_MAP:
                        detected = LANGUAGE_MAP[lang]
                        logger.info(f"detect_track_language: Language detected via MusicBrainz work: {detected}")
                        return detected, "musicbrainz"
                
                releases = rec.get("releases", [])
                for rel in releases:
                    text_rep = rel.get("text-representation", {})
                    lang = text_rep.get("language")
                    if lang and lang in LANGUAGE_MAP:
                        detected = LANGUAGE_MAP[lang]
                        logger.info(f"detect_track_language: Language detected via MusicBrainz release: {detected}")
                        return detected, "musicbrainz"
    except Exception as e:
        logger.warning(f"detect_track_language: MusicBrainz API language detection failed: {e}")

    logger.info(f"detect_track_language: Language fallback to 'unknown' for '{title}'.")
    return "unknown", "fallback"

def is_fuzzy_duplicate_in_pool(track, pool):
    """
    Helper to check if track is fuzzy duplicate in pool
    """
    title = (track.get("title") or "").strip().lower()
    artist = (track.get("artist") or "").strip().lower()
    
    for item in pool:
        item_title = (item.get("title") or "").strip().lower()
        item_artist = (item.get("artist") or "").strip().lower()
        
        if title == item_title and artist == item_artist:
            return True
            
        matcher = difflib.SequenceMatcher(None, title, item_title)
        if matcher.ratio() >= 0.85:
            artist_matcher = difflib.SequenceMatcher(None, artist, item_artist)
            if artist_matcher.ratio() >= 0.70:
                return True
    return False

def fetch_jiosaavn_charts(languages=["malayalam", "tamil", "hindi"]):
    """
    Scrapes JioSaavn charts for each language using these URLs.
    Extracts title, artist, language tag for each track.
    Returns combined list with language field set correctly.
    """
    logger.info(f"fetch_jiosaavn_charts: Initiating fetch for languages: {languages}")
    combined_tracks = []
    
    jiosaavn_configs = {
        "malayalam": {
            "url": "https://www.jiosaavn.com/featured/trending-malayalam/ITLMx7sLNQA_",
            "fallback": "https://www.jiosaavn.com/play/featured/malayalam/malayalam-viral-hits/H-9bnU8t0nNieSJqt9HmOQ__"
        },
        "tamil": {
            "url": "https://www.jiosaavn.com/featured/trending-tamil/EhkJLyKPSek_",
            "fallback": "https://www.jiosaavn.com/play/featured/tamil/trending-tamil-songs/,TFI7S,BUZwLtNrz-hs7eg__"
        },
        "hindi": {
            "url": "https://www.jiosaavn.com/featured/trending-hindi/dFErDMPFcmk_",
            "fallback": "https://www.jiosaavn.com/play/featured/hindi/now-trending/BECHl0fsh08_"
        },
        "indian": {
            "url": "https://www.jiosaavn.com/featured/top-50-songs/kpQEiFLWybs_",
            "fallback": "https://www.jiosaavn.com/play/featured/hindi/india-superhits-top-50/VuJUPQ9ch77bB,U5Yp5iAA__"
        }
    }
    
    langs_to_fetch = list(languages)
    # Ensure "indian" is included if it was requested or implied
    
    for lang in langs_to_fetch:
        cfg = jiosaavn_configs.get(lang.lower())
        if not cfg:
            logger.warning(f"fetch_jiosaavn_charts: Language '{lang}' not in JioSaavn configs.")
            continue
            
        target_url = cfg["url"]
        fallback_url = cfg["fallback"]
        
        def extract_json_block(html):
            start_str = "window.__INITIAL_DATA__ ="
            idx = html.find(start_str)
            if idx == -1:
                return None
            start_brace = html.find("{", idx)
            if start_brace == -1:
                return None
            brace_count = 0
            in_string = False
            escape = False
            for i in range(start_brace, len(html)):
                char = html[i]
                if escape:
                    escape = False
                    continue
                if char == '\\':
                    escape = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            return html[start_brace:i+1]
            return None

        def parse_tracks(html):
            json_str = extract_json_block(html)
            if not json_str:
                return []
            
            cleaned_str = re.sub(r'new\s+Date\([^)]*\)', 'null', json_str)
            cleaned_str = re.sub(r':\s*undefined', ':null', cleaned_str)
            
            try:
                data = json.loads(cleaned_str)
                playlist_data = data.get("playlist", {}).get("playlist", {})
                if not playlist_data:
                    return []
                
                songs = playlist_data.get("list", [])
                extracted = []
                for s in songs:
                    title = s.get("title", {}).get("text", "")
                    if not title:
                        title = s.get("song", "")
                    
                    artists_list = s.get("artists", [])
                    if isinstance(artists_list, list):
                        artist_names = []
                        for a in artists_list:
                            name = a.get("name")
                            if name and name not in artist_names:
                                artist_names.append(name)
                        artist = ", ".join(artist_names)
                    else:
                        artist = ""
                        
                    if not artist:
                        sub = s.get("subtitle")
                        if isinstance(sub, list):
                            artist = ", ".join([item.get("text", "") for item in sub if isinstance(item, dict) and item.get("text")])
                        elif isinstance(sub, str):
                            artist = sub
                    
                    extracted.append({
                        "title": title,
                        "artist": artist,
                        "genre": "Unknown",
                        "spotify_id": "UnknownID",
                        "source": f"JioSaavn Charts ({lang})",
                        "language": lang.lower().strip()
                    })
                return extracted
            except Exception as parse_err:
                logger.error(f"fetch_jiosaavn_charts: Error parsing JSON for {lang}: {parse_err}")
                return []

        tracks = []
        time.sleep(random.uniform(0.5, 1.2))
        logger.info(f"fetch_jiosaavn_charts: Requesting main URL for {lang}: {target_url}")
        
        try:
            r = requests.get(target_url, headers=HEADERS, timeout=5)
            if r.status_code == 200:
                tracks = parse_tracks(r.text)
            
            if not tracks:
                logger.info(f"fetch_jiosaavn_charts: Main URL failed or returned empty. Trying fallback URL for {lang}: {fallback_url}")
                time.sleep(random.uniform(0.5, 1.2))
                r_fallback = requests.get(fallback_url, headers=HEADERS, timeout=5)
                if r_fallback.status_code == 200:
                    tracks = parse_tracks(r_fallback.text)
        except Exception as e:
            logger.error(f"fetch_jiosaavn_charts: Error requesting URLs for {lang}: {e}")
            try:
                logger.info(f"fetch_jiosaavn_charts: Exception on main. Trying fallback URL for {lang}: {fallback_url}")
                time.sleep(random.uniform(0.5, 1.2))
                r_fallback = requests.get(fallback_url, headers=HEADERS, timeout=5)
                if r_fallback.status_code == 200:
                    tracks = parse_tracks(r_fallback.text)
            except Exception as fallback_err:
                logger.error(f"fetch_jiosaavn_charts: Error requesting fallback URL for {lang}: {fallback_err}")
                
        logger.info(f"fetch_jiosaavn_charts: Retrieved {len(tracks)} tracks for {lang}")
        combined_tracks.extend(tracks)
        
    return combined_tracks

def fetch_indian_charts():
    """
    Fetches from iTunes India RSS and Spotify India regional chart.
    Tags results with language "indian" as fallback if specific language unknown.
    Returns combined list.
    """
    logger.info("fetch_indian_charts: Initiating fetch for Indian charts...")
    combined_tracks = []
    
    # 1. Fetch iTunes India RSS
    itunes_url = "https://rss.applemarketingtools.com/api/v2/in/music/most-played/50/songs.json"
    logger.info(f"fetch_indian_charts: Fetching iTunes India charts: {itunes_url}")
    try:
        response = requests.get(itunes_url, headers=HEADERS, timeout=5)
        if response.status_code == 200:
            data = response.json()
            feed = data.get("feed", {})
            results = feed.get("results", [])
            for track in results:
                title = track.get("name")
                artist = track.get("artistName")
                
                genres_list = track.get("genres", [])
                genre_name = "Unknown"
                if genres_list:
                    genre_name = genres_list[0].get("name", "Unknown")
                
                lang, detection_method = detect_track_language(title, artist)
                if lang not in ("hindi", "malayalam", "tamil"):
                    lang = "indian"
                    
                combined_tracks.append({
                    "title": title,
                    "artist": artist,
                    "genre": genre_name,
                    "spotify_id": "UnknownID",
                    "source": "iTunes India Charts",
                    "language": lang
                })
            logger.info(f"fetch_indian_charts: Successfully retrieved {len(results)} tracks from iTunes India RSS.")
    except Exception as e:
        logger.error(f"fetch_indian_charts: Error fetching iTunes India RSS: {e}", exc_info=True)
        
    # 2. Fetch Spotify India regional chart
    spotify_in_playlist_id = "37i9dQZEVXbLZ527wRLeb9" # Top 50 India
    logger.info(f"fetch_indian_charts: Fetching Spotify India regional charts...")
    try:
        time.sleep(random.uniform(0.5, 1.2))
        spotify_tracks = scrape_spotify_embed_playlist(spotify_in_playlist_id)
        for t in spotify_tracks:
            title = t.get("title")
            artist = t.get("artist")
            
            lang, detection_method = detect_track_language(title, artist)
            if lang not in ("hindi", "malayalam", "tamil"):
                lang = "indian"
                
            t["source"] = "Spotify India Charts"
            t["language"] = lang
            combined_tracks.append(t)
        logger.info(f"fetch_indian_charts: Successfully retrieved {len(spotify_tracks)} tracks from Spotify India regional chart.")
    except Exception as e:
        logger.error(f"fetch_indian_charts: Error fetching Spotify India regional chart: {e}", exc_info=True)
        
    return combined_tracks

def is_indian_source(track):
    """
    Helper to check if track is from an Indian source (JioSaavn, iTunes India, Spotify India, Regional Chart (IN)).
    """
    source = track.get("source", "")
    return "JioSaavn" in source or "iTunes India" in source or "Spotify India" in source or "Regional Chart (IN)" in source

def build_song_pool(config):
    """
    Orchestrator to construct the deduplicated, filtered, shuffled candidate tracks pool.
    """
    logger.info("build_song_pool: Initiating diverse song pool construction.")
    
    filter_mode = config.get("filter_mode", "filtered")
    allowed_languages = [l.lower().strip() for l in (config.get("allowed_languages") or [])]
    allowed_genres = [g.lower().strip() for g in (config.get("allowed_genres") or [])]
    
    # 1. Fetch only necessary streams based on allowed_languages and filter_mode
    trending_tracks = []
    regional_tracks = []
    genre_tracks = []
    new_releases = []
    
    if "english" in allowed_languages or filter_mode == "random":
        logger.info("build_song_pool: Fetching English/Global chart sources...")
        trending_tracks = get_trending_tracks(limit=50)
        regional_tracks = fetch_regional_charts()
        genre_tracks = fetch_genre_charts()
        new_releases = fetch_new_releases()
        
    jiosaavn_tracks = []
    jiosaavn_langs = [l for l in ["malayalam", "tamil", "hindi", "indian"] if l in allowed_languages]
    if filter_mode == "random":
        jiosaavn_langs = ["malayalam", "tamil", "hindi", "indian"]
    if jiosaavn_langs:
        logger.info(f"build_song_pool: Fetching JioSaavn charts for languages: {jiosaavn_langs}...")
        jiosaavn_tracks = fetch_jiosaavn_charts(languages=jiosaavn_langs)
        
    indian_tracks = []
    if "indian" in allowed_languages or filter_mode == "random":
        logger.info("build_song_pool: Fetching Indian chart sources...")
        indian_tracks = fetch_indian_charts()
        
    # Combine
    all_raw_tracks = trending_tracks + regional_tracks + genre_tracks + new_releases + jiosaavn_tracks + indian_tracks
    logger.info(f"build_song_pool: Aggregated {len(all_raw_tracks)} tracks from all streams.")
    
    # 2. Deduplicate
    unique_tracks = []
    seen_ids = set()
    
    for track in all_raw_tracks:
        sp_id = track.get("spotify_id")
        if sp_id and sp_id != "UnknownID":
            if sp_id in seen_ids:
                continue
                
        if is_fuzzy_duplicate_in_pool(track, unique_tracks):
            continue
            
        if sp_id and sp_id != "UnknownID":
            seen_ids.add(sp_id)
            
        unique_tracks.append(track)
        
    logger.info(f"build_song_pool: Deduplication completed. Unique candidates: {len(unique_tracks)}")
    
    # 3. Filter and resolve missing fields
    logger.info(f"build_song_pool: Processing pool with filter_mode: '{filter_mode}'")
    
    filtered_pool = []
    for track in unique_tracks:
        title = track.get("title")
        artist = track.get("artist")
        
        if filter_mode == "filtered":
            # Determine genre mapping
            genre = track.get("genre", "Unknown")
            genre_lower = genre.lower()
            if "hip-hop" in genre_lower or "rap" in genre_lower:
                genre = "hip-hop"
            elif "r&b" in genre_lower or "soul" in genre_lower:
                genre = "r&b"
            elif "pop" in genre_lower:
                genre = "pop"
            elif "latin" in genre_lower:
                genre = "latin"
            elif "k-pop" in genre_lower or "kpop" in genre_lower:
                genre = "k-pop"
            elif "electronic" in genre_lower or "dance" in genre_lower or "edm" in genre_lower:
                genre = "electronic"
            elif "rock" in genre_lower:
                genre = "rock"
            elif "classical" in genre_lower:
                genre = "classical"
                
            track["genre"] = genre
            
            # Filter genre (bypass for Indian sources with "Unknown" genre)
            if allowed_genres and genre.lower() not in allowed_genres:
                if not (is_indian_source(track) and genre.lower() == "unknown"):
                    logger.info(f"build_song_pool: Skip '{title}' - genre '{genre}' not allowed.")
                    continue
                
            # Determine language and detection method
            language = track.get("language")
            if isinstance(language, str):
                language = language.lower().strip()
            detection_method = "preset"
            if not language or language == "unknown":
                language, detection_method = detect_track_language(title, artist)
                if isinstance(language, str):
                    language = language.lower().strip()
            track["language"] = language
            
            # Filter language based on custom rules (comparing in lowercase)
            if allowed_languages:
                is_matched = False
                for allowed_lang in allowed_languages:
                    allowed_lang = allowed_lang.lower().strip()
                    if allowed_lang == "english":
                        if language in ("english", "unknown") and not is_indian_source(track):
                            is_matched = True
                            break
                    elif allowed_lang == "malayalam":
                        if language == "malayalam" and ("jiosaavn" in track.get("source", "").lower() or detection_method == "musicbrainz"):
                            is_matched = True
                            break
                    elif allowed_lang == "tamil":
                        if language == "tamil":
                            is_matched = True
                            break
                    elif allowed_lang == "hindi":
                        if language == "hindi":
                            is_matched = True
                            break
                    elif allowed_lang == "indian":
                        if is_indian_source(track):
                            is_matched = True
                            break
                
                if not is_matched:
                    logger.info(f"build_song_pool: Skip '{title}' - language '{language}' (source '{track.get('source')}') not matched by allowed_languages.")
                    continue
                
        # Resolve missing Spotify ID
        sp_id = track.get("spotify_id")
        if not sp_id or sp_id == "UnknownID":
            sp_id = resolve_spotify_id(title, artist)
            track["spotify_id"] = sp_id
            
        filtered_pool.append(track)
        
    logger.info(f"build_song_pool: Filtering completed. Active pool count: {len(filtered_pool)}")
    
    # Count songs per language and source in the final pool
    composition = {}
    for track in filtered_pool:
        lang = track.get("language", "unknown").lower().strip()
        src = track.get("source", "unknown")
        key = f"{lang} ({src})"
        composition[key] = composition.get(key, 0) + 1
        
    logger.info(f"build_song_pool: Final pool composition: {composition}")
    
    # 4. Shuffle slightly
    random.shuffle(filtered_pool)
    return filtered_pool

if __name__ == "__main__":
    print("Testing build_song_pool...")
    test_config = {
        "allowed_genres": ["pop", "hip-hop", "electronic"],
        "allowed_languages": ["english"]
    }
    pool = build_song_pool(test_config)
    print(f"\nFinal pool tracks count: {len(pool)}")
    for track in pool[:5]:
        print(track)
