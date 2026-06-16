import urllib.parse
from dashboard.app import app

client = app.test_client()

print("Fetching all artists...")
res = client.get('/api/artists')
if res.status_code == 200:
    artists = res.get_json()
    artists.sort(key=lambda x: x['track_count'], reverse=True)
    print('--- Top 10 Artists ---')
    for a in artists[:10]:
        print(f"{a['artist_name']}: {a['track_count']} tracks")
        
    print('\n--- Testing Multi-Artist ---')
    res2 = client.get('/api/artists/search?q=gaga')
    print('Search Gaga:', res2.get_json())
    
    res_bm = client.get('/api/artists/search?q=bruno')
    print('Search Bruno:', res_bm.get_json())
    
    target_artist = None
    for a in artists:
        if a['track_count'] > 1:
            target_artist = a['artist_name']
            break
            
    if target_artist:
        print(f'\n--- Testing /api/artists/{target_artist} ---')
        res3 = client.get('/api/artists/' + urllib.parse.quote(target_artist))
        tracks = res3.get_json()
        print(f'Found {len(tracks)} tracks for {target_artist}')
        for t in tracks[:3]:
            print(' -', t.get('title'), 'by', t.get('artist'))
else:
    print('Failed to get artists:', res.status_code, res.data)
