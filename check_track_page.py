import requests
import re

url = "https://open.spotify.com/track/20jbSiX29FDX4oQxBXyUEi"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9"
}
try:
    r = requests.get(url, headers=headers, timeout=10)
    print("Status code:", r.status_code)
    print("Length:", len(r.text))
    # Look for genre or schema.org or similar
    with open("track_page.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    
    # Search for "genre" case insensitively
    genres = re.findall(r'(?i)genre', r.text)
    print("Occurrences of 'genre':", len(genres))
    
    # Check if there is some script tag with JSON metadata
    for script in re.findall(r'<script[^>]*>(.*?)</script>', r.text):
        if "schema.org" in script or "MusicRecording" in script or "genre" in script:
            print("Found interesting script tag snippet:")
            print(script[:1000])
except Exception as e:
    print("Error:", e)
