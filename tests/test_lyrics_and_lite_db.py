import unittest
from unittest.mock import patch, MagicMock
from scraper.drive_uploader import build_lite_database, sync_database_lite
from dashboard.app import app, _db_cache


class LyricsAndLiteDbTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        _db_cache["data"] = None
        _db_cache["timestamp"] = 0
        self.sample_tracks = [
            {
                "id": "drive-file-123",
                "driveFileId": "drive-file-123",
                "title": "Blinding Lights",
                "artist": "The Weeknd",
                "album": "After Hours",
                "genre": "Pop",
                "duration": "03:20",
                "durationSeconds": 200,
                "spotify_id": "0VjIjW4GlUZAMYd2vXMi3b",
                "album_art": "https://example.com/art.jpg",
                "albumArt": "https://example.com/art.jpg",
                "language": "english",
                "source": "spotify_charts",
                "lyrics": "I been tryna call...",
                "syncedLyrics": "[00:12.50]I been tryna call...",
                "lyricsStatus": "ok",
                "timestamp": "2026-08-25T12:00:00Z",
                "addedAt": "2026-08-25T12:00:00Z",
                "updatedAt": "2026-08-25T12:00:00Z",
            },
            {
                "id": "drive-file-456",
                "driveFileId": "drive-file-456",
                "title": "Levitating",
                "artist": "Dua Lipa",
                "album": "Future Nostalgia",
                "genre": "Pop",
                "duration": "03:23",
                "durationSeconds": 203,
                "lyrics": None,
                "syncedLyrics": None,
                "lyricsStatus": "ok",
            },
        ]

    def tearDown(self):
        _db_cache["data"] = None
        _db_cache["timestamp"] = 0

    def test_build_lite_database_list(self):
        lite = build_lite_database(self.sample_tracks)
        self.assertIsInstance(lite, list)
        self.assertEqual(len(lite), 2)
        for track in lite:
            self.assertNotIn("lyrics", track)
            self.assertNotIn("syncedLyrics", track)
            self.assertIn("title", track)
            self.assertIn("lyricsStatus", track)
        self.assertEqual(lite[0]["title"], "Blinding Lights")

    def test_build_lite_database_dict(self):
        dict_data = {"tracks": self.sample_tracks, "version": "1.0"}
        lite = build_lite_database(dict_data)
        self.assertIsInstance(lite, dict)
        self.assertIn("tracks", lite)
        self.assertEqual(len(lite["tracks"]), 2)
        for track in lite["tracks"]:
            self.assertNotIn("lyrics", track)
            self.assertNotIn("syncedLyrics", track)
            self.assertIn("title", track)

    @patch("scraper.drive_uploader.upload_json")
    @patch("scraper.drive_uploader.get_db_lite_file_id")
    def test_sync_database_lite_non_fatal_on_error(self, mock_get_id, mock_upload):
        mock_get_id.return_value = ("lite-id-1", "folder-1")
        mock_upload.side_effect = Exception("Drive API Error")
        # Should not raise exception
        sync_database_lite(self.sample_tracks, "folder-1")

    def test_get_track_lyrics_endpoint_found(self):
        _db_cache["data"] = self.sample_tracks
        _db_cache["timestamp"] = 999999999999.0

        res = self.client.get("/api/tracks/drive-file-123/lyrics")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Cache-Control"), "public, max-age=86400")
        data = res.get_json()
        self.assertEqual(data["lyrics"], "I been tryna call...")
        self.assertEqual(data["syncedLyrics"], "[00:12.50]I been tryna call...")
        self.assertEqual(data["lyricsStatus"], "ok")

    def test_get_track_lyrics_endpoint_not_found(self):
        _db_cache["data"] = self.sample_tracks
        _db_cache["timestamp"] = 999999999999.0

        res = self.client.get("/api/tracks/non-existent-id/lyrics")
        self.assertEqual(res.status_code, 404)
        data = res.get_json()
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
