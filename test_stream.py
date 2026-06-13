import urllib.request
import json
req = urllib.request.urlopen('http://localhost:5000/api/tracks')
tracks = json.loads(req.read())
url = 'http://localhost:5000/stream/' + tracks[0]['driveFileId']
r = urllib.request.urlopen(url)
print(r.headers['Content-Type'])
r.close()
