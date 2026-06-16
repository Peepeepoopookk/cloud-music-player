from dashboard.app import app

client = app.test_client()
res = client.get('/api/playlists')
if res.status_code == 200:
    playlists = res.get_json()
    print('Playlists count:', len(playlists))
    for p in playlists:
        print(f"- {p['name']} ({p['total_tracks']} tracks)")
else:
    print('Error:', res.status_code)
