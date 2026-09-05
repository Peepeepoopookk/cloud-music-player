import unittest
from unittest.mock import patch, MagicMock
from dashboard.app import app, _db_cache, invalidate_db_cache


class PerformanceQuickWinsTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        invalidate_db_cache()
        self.sample_db = [
            {
                "id": "track-1",
                "driveFileId": "drive-file-1",
                "title": "Starboy",
                "artist": "The Weeknd",
                "album": "Starboy",
                "duration": "03:50",
                "lyrics": "I am trying to put you in the worst mood",
                "syncedLyrics": "[00:10.00]I am trying",
                "lyricsStatus": "ok",
                "album_art": "https://example.com/art1.jpg"
            },
            {
                "id": "track-2",
                "driveFileId": "drive-file-2",
                "title": "Save Your Tears",
                "artist": "The Weeknd",
                "album": "After Hours",
                "duration": "03:35",
                "lyrics": "I saw you dancing",
                "syncedLyrics": "[00:08.00]I saw you",
                "lyricsStatus": "ok",
                "album_art": "https://example.com/art2.jpg"
            }
        ]

    def tearDown(self):
        invalidate_db_cache()

    def test_api_tracks_returns_lite_database_by_default(self):
        _db_cache["data"] = self.sample_db
        _db_cache["timestamp"] = 999999999999.0

        res = self.client.get("/api/tracks")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data), 2)
        
        for track in data:
            self.assertNotIn("lyrics", track)
            self.assertNotIn("syncedLyrics", track)
            self.assertIn("title", track)
            self.assertIn("artist", track)

    def test_api_tracks_returns_full_database_when_requested(self):
        _db_cache["data"] = self.sample_db
        _db_cache["timestamp"] = 999999999999.0

        res = self.client.get("/api/tracks?full=true")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data), 2)
        
        self.assertEqual(data[0]["lyrics"], "I am trying to put you in the worst mood")
        self.assertEqual(data[0]["syncedLyrics"], "[00:10.00]I am trying")

    def test_api_tracks_uses_cache_and_avoids_repeated_downloads(self):
        with patch("dashboard.app.download_json") as mock_download:
            mock_download.return_value = self.sample_db
            with patch("dashboard.app.get_db_file_id", return_value="mock-db-id"):
                res1 = self.client.get("/api/tracks")
                self.assertEqual(res1.status_code, 200)
                self.assertEqual(mock_download.call_count, 1)

                res2 = self.client.get("/api/tracks")
                self.assertEqual(res2.status_code, 200)
                self.assertEqual(mock_download.call_count, 1)

    def test_api_tracks_does_not_call_list_files_unless_requested(self):
        _db_cache["data"] = self.sample_db
        _db_cache["timestamp"] = 999999999999.0

        with patch("dashboard.app.list_files") as mock_list:
            res = self.client.get("/api/tracks")
            self.assertEqual(res.status_code, 200)
            mock_list.assert_not_called()


if __name__ == "__main__":
    unittest.main()
