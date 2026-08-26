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

    def test_find_duplicate_track_layers(self):
        from scraper.state_manager import find_duplicate_track, is_duplicate

        db_tracks = [
            {"id": "drive_id_1", "driveFileId": "drive_id_1", "title": "Levitating", "artist": "Dua Lipa", "spotify_id": "sp_123"},
            {"id": "drive_id_2", "driveFileId": "drive_id_2", "title": "Shape of You", "artist": "Ed Sheeran", "spotify_id": "sp_456"},
            {"id": "drive_id_3", "driveFileId": "drive_id_3", "title": "Billie Jean", "artist": "Michael Jackson", "spotify_id": "sp_789"},
        ]
        state = {"downloaded_ids": ["sp_123", "sp_in_flight_999"]}

        # Layer 1: Spotify ID match
        cand1 = {"title": "Different Title", "artist": "Different Artist", "spotify_id": "sp_123"}
        match1 = find_duplicate_track(cand1, state, db_tracks)
        self.assertIsNotNone(match1)
        self.assertEqual(match1["driveFileId"], "drive_id_1")
        self.assertTrue(is_duplicate(cand1, state, db_tracks))

        # Layer 1: in downloaded_ids but not in db_tracks (falls through and returns None if no title/artist match)
        cand_in_flight = {"title": "Unseen Song", "artist": "Unseen Artist", "spotify_id": "sp_in_flight_999"}
        self.assertIsNone(find_duplicate_track(cand_in_flight, state, db_tracks))
        self.assertFalse(is_duplicate(cand_in_flight, state, db_tracks))

        # Layer 2: Exact title + artist match
        cand2 = {"title": "shape of you", "artist": "ed sheeran", "spotify_id": "sp_different"}
        match2 = find_duplicate_track(cand2, state, db_tracks)
        self.assertIsNotNone(match2)
        self.assertEqual(match2["driveFileId"], "drive_id_2")
        self.assertTrue(is_duplicate(cand2, state, db_tracks))

        # Layer 3: Fuzzy match (difflib ratio >= 0.85)
        cand3 = {"title": "Billie Jean (Remastered)", "artist": "Michael Jackson", "spotify_id": None}
        # "billie jean (remastered) michael jackson" vs "billie jean michael jackson"
        # Let's test standard fuzzy match
        cand_fuzzy = {"title": "Billie Jean", "artist": "Michael Jackson ", "spotify_id": None}
        match3 = find_duplicate_track(cand_fuzzy, state, db_tracks)
        self.assertIsNotNone(match3)
        self.assertEqual(match3["driveFileId"], "drive_id_3")
        self.assertTrue(is_duplicate(cand_fuzzy, state, db_tracks))

        # Completely fresh song
        cand_fresh = {"title": "Brand New Song", "artist": "Brand New Artist", "spotify_id": "sp_new"}
        self.assertIsNone(find_duplicate_track(cand_fresh, state, db_tracks))
        self.assertFalse(is_duplicate(cand_fresh, state, db_tracks))


if __name__ == "__main__":
    unittest.main()
