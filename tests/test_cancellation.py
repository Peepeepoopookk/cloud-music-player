import unittest
from unittest.mock import patch, MagicMock
import threading

from scraper.playlist_importer import (
    cancel_events,
    create_cancel_event,
    set_cancel_event,
    is_cancel_requested,
    cleanup_cancel_event,
    run_playlist_import,
)
from scraper.downloader import download_track


class CancellationMechanismTests(unittest.TestCase):
    def setUp(self):
        cancel_events.clear()

    def tearDown(self):
        cancel_events.clear()

    def test_cancel_event_lifecycle(self):
        playlist_id = "test-pl-123"
        ev = create_cancel_event(playlist_id)
        self.assertIsInstance(ev, threading.Event)
        self.assertFalse(is_cancel_requested(playlist_id))

        set_cancel_event(playlist_id)
        self.assertTrue(is_cancel_requested(playlist_id))

        cleanup_cancel_event(playlist_id)
        self.assertNotIn(playlist_id, cancel_events)

    def test_downloader_cancel_callback_triggers_at_search(self):
        callback = MagicMock(return_value=True)
        with self.assertRaises(Exception) as ctx:
            download_track("Title", "Artist", "/tmp/dummy", cancel_check_callback=callback)
        self.assertEqual(str(ctx.exception), "Download cancelled by user")

    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    def test_run_playlist_import_honors_cancel_event(self, mock_db, mock_upload, mock_download, mock_search):
        mock_db.return_value = ("db_123", "folder_123")
        mock_search.return_value = "file_123"

        # Mock a state that claims to still be running on Drive
        mock_download.return_value = {
            "playlist_id": "test-pl-456",
            "playlist_name": "Test Playlist",
            "total_tracks": 5,
            "tracks_available_for_import": 5,
            "processed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "gemini_pending": 0,
            "gemini_deferred": 0,
            "gemini_status": "idle",
            "status": "running",
            "tracks": [
                {"title": f"Song {i}", "artist": "Artist", "spotify_id": f"id_{i}"}
                for i in range(5)
            ],
        }

        # Set in-memory cancel event BEFORE/DURING run
        create_cancel_event("test-pl-456")
        set_cancel_event("test-pl-456")

        run_playlist_import("test-pl-456")

        # Confirm import stopped cleanly without processing tracks
        from scraper.playlist_importer import active_imports
        self.assertEqual(active_imports["test-pl-456"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
