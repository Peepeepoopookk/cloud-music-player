import unittest
from unittest.mock import patch

from scraper.track_utils import check_playlist_duplicates
from scraper.playlist_importer import get_playlist_preview


class DuplicateDetectionTests(unittest.TestCase):
    def test_check_playlist_duplicates_matching(self):
        library_tracks = [
            {"title": "Blinding Lights", "artist": "The Weeknd", "spotify_id": "0VjIjW4GlUZAMYd2vXMi3b"},
            {"title": "Save Your Tears", "artist": "The Weeknd feat. Ariana Grande", "spotify_id": "UnknownID"},
            {"title": "Levitating", "artist": "Dua Lipa", "spotify_id": "463CkQjx2Zk1yXoBuierM9"},
        ]

        playlist_tracks = [
            {"title": "Blinding Lights", "artist": "The Weeknd", "spotify_id": "0VjIjW4GlUZAMYd2vXMi3b"},
            {"title": "Save Your Tears", "artist": "Ariana Grande & The Weeknd", "spotify_id": "different_id_999"},
            {"title": "Starboy", "artist": "The Weeknd", "spotify_id": "7MXVkk9YM5GM0ILBt52zWi"},
        ]

        results = check_playlist_duplicates(playlist_tracks, library_tracks)
        self.assertEqual(len(results), 3)

        # 1. Matches via spotify_id
        self.assertTrue(results[0]["is_duplicate"])
        self.assertEqual(results[0]["match_type"], "spotify_id")

        # 2. Matches via exact normalized title+artist
        self.assertTrue(results[1]["is_duplicate"])
        self.assertEqual(results[1]["match_type"], "exact_title_artist")

        # 3. New track
        self.assertFalse(results[2]["is_duplicate"])
        self.assertIsNone(results[2]["match_type"])

    def test_check_playlist_duplicates_empty_library(self):
        playlist_tracks = [
            {"title": "Track 1", "artist": "Artist 1", "spotify_id": "id1"},
        ]
        results = check_playlist_duplicates(playlist_tracks, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["is_duplicate"])

    @patch("scraper.playlist_importer.scrape_spotify_embed_playlist")
    @patch("scraper.playlist_importer.get_db_file_id")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.requests.get")
    def test_get_playlist_preview_duplicate_counts(self, mock_requests, mock_download, mock_db_id, mock_scrape):
        mock_requests.return_value.status_code = 404
        mock_scrape.return_value = [
            {"title": "Song A", "artist": "Artist A", "spotify_id": "id_a"},
            {"title": "Song B", "artist": "Artist B", "spotify_id": "id_b"},
            {"title": "Song C", "artist": "Artist C", "spotify_id": "id_c"},
        ]
        mock_db_id.return_value = ("db_file_123", "parent_123")
        mock_download.return_value = [
            {"title": "Song A", "artist": "Artist A", "spotify_id": "id_a"},
        ]

        preview = get_playlist_preview("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(preview["tracks_available_for_import"], 3)
        self.assertEqual(preview["already_in_library"], 1)
        self.assertEqual(preview["new_tracks_importable"], 2)


if __name__ == "__main__":
    unittest.main()
