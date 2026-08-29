import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import time

from scraper.playlist_manager import (
    bulk_add_tracks_to_playlist,
    add_track_to_playlist,
    _find_playlists_file,
    _load_playlists_unlocked,
    _save_playlists_unlocked,
    _cached_playlists_file_id,
)
import scraper.playlist_manager as pm
from scraper.playlist_importer import run_playlist_import, active_imports, cancel_events


class PlaylistManagerOptimizationTests(unittest.TestCase):
    def setUp(self):
        pm._cached_playlists_file_id = None
        active_imports.clear()
        cancel_events.clear()

    def tearDown(self):
        pm._cached_playlists_file_id = None
        active_imports.clear()
        cancel_events.clear()

    @patch("scraper.playlist_manager.list_files")
    def test_find_playlists_file_caching(self, mock_list_files):
        mock_list_files.return_value = [
            {"name": "other.json", "id": "other_id"},
            {"name": "playlists.json", "id": "pl_file_123"},
        ]

        # First call lists files
        file_id_1 = _find_playlists_file("parent_123")
        self.assertEqual(file_id_1, "pl_file_123")
        self.assertEqual(mock_list_files.call_count, 1)

        # Second call uses cache without calling list_files
        file_id_2 = _find_playlists_file("parent_123")
        self.assertEqual(file_id_2, "pl_file_123")
        self.assertEqual(mock_list_files.call_count, 1)

        # Force refresh bypasses cache
        file_id_3 = _find_playlists_file("parent_123", force_refresh=True)
        self.assertEqual(file_id_3, "pl_file_123")
        self.assertEqual(mock_list_files.call_count, 2)

    @patch("scraper.playlist_manager.list_files")
    @patch("scraper.playlist_manager.download_json")
    def test_load_playlists_stale_cache_recovery(self, mock_download_json, mock_list_files):
        # 1. Populate cache with a stale ID
        pm._cached_playlists_file_id = "stale_id_999"
        mock_list_files.return_value = [
            {"name": "playlists.json", "id": "fresh_id_888"}
        ]

        def download_side_effect(file_id):
            if file_id == "stale_id_999":
                raise Exception("404 File not found (simulating deleted/stale file)")
            if file_id == "fresh_id_888":
                return [{"id": "pl_recovered", "name": "Recovered Playlist", "track_ids": []}]
            raise ValueError(f"Unexpected file_id: {file_id}")

        mock_download_json.side_effect = download_side_effect

        # 2. Call load
        data = _load_playlists_unlocked("parent_folder_123")

        # 3. Verify it detected the failure, re-queried Drive, loaded the correct data, and updated cache
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "pl_recovered")
        self.assertEqual(mock_list_files.call_count, 1)
        self.assertEqual(pm._cached_playlists_file_id, "fresh_id_888")

    @patch("scraper.playlist_manager.list_files")
    @patch("scraper.playlist_manager.download_json")
    @patch("scraper.playlist_manager.upload_json")
    def test_save_playlists_stale_cache_recovery(self, mock_upload_json, mock_download_json, mock_list_files):
        # 1. Populate cache with a stale ID
        pm._cached_playlists_file_id = "stale_id_999"
        mock_download_json.side_effect = Exception("Backup skipped")
        mock_list_files.return_value = [
            {"name": "playlists.json", "id": "fresh_id_888"}
        ]

        def upload_side_effect(file_id, data, filename, parent_id=None):
            if file_id == "stale_id_999":
                raise Exception("404 File not found (simulating deleted/stale file)")
            if file_id == "fresh_id_888":
                return {"id": "fresh_id_888", "name": filename}
            if file_id is None: # backup upload
                return {"id": "backup_id"}
            raise ValueError(f"Unexpected file_id: {file_id}")

        mock_upload_json.side_effect = upload_side_effect

        # 2. Call save
        res = _save_playlists_unlocked("parent_folder_123", [{"id": "pl_saved"}])

        # 3. Verify it recovered by re-querying Drive, uploaded to the fresh ID, and updated cache
        self.assertEqual(res.get("id"), "fresh_id_888")
        self.assertEqual(mock_list_files.call_count, 1)
        self.assertEqual(pm._cached_playlists_file_id, "fresh_id_888")

    @patch("scraper.playlist_manager._save_playlists_unlocked")
    @patch("scraper.playlist_manager._load_playlists_unlocked")
    @patch("scraper.playlist_manager.get_db_file_id")
    def test_bulk_add_tracks_to_playlist(self, mock_db, mock_load, mock_save):
        mock_db.return_value = ("db_123", "folder_123")
        mock_load.return_value = [
            {
                "id": "pl_abc",
                "name": "My Playlist",
                "track_ids": ["t1", "t2"],
                "total_tracks": 2,
            }
        ]

        # Bulk add with some duplicates and some new tracks
        bulk_add_tracks_to_playlist("pl_abc", ["t2", "t3", "t4", "t1", "t5"])

        self.assertEqual(mock_save.call_count, 1)
        saved_playlists = mock_save.call_args[0][1]
        target = saved_playlists[0]
        self.assertEqual(target["track_ids"], ["t1", "t2", "t3", "t4", "t5"])
        self.assertEqual(target["total_tracks"], 5)

    @patch("scraper.playlist_manager.bulk_add_tracks_to_playlist")
    def test_add_track_to_playlist_delegates(self, mock_bulk):
        add_track_to_playlist("pl_abc", "drive_123")
        mock_bulk.assert_called_once_with("pl_abc", ["drive_123"])

    @patch("scraper.gemini_import_pipeline.apply_gemini_to_import_batch", return_value={"tracks_submitted": 12, "tracks_updated": 0, "fields_updated": 0, "language_updates": 0, "genre_updates": 0, "errors": []})
    @patch("scraper.state_manager.find_duplicate_track", return_value=None)
    @patch("scraper.state_manager.load_state", return_value={})
    @patch("scraper.playlist_importer.bulk_add_tracks_to_playlist")
    @patch("scraper.playlist_importer.download_track")
    @patch("scraper.playlist_importer.enrich_track_metadata")
    @patch("scraper.playlist_importer.upload_track")
    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    @patch("scraper.drive_uploader.bulk_update_database")
    def test_run_playlist_import_throttles_state_writes(
        self,
        mock_bulk_db,
        mock_get_db,
        mock_upload_json,
        mock_download_json,
        mock_search,
        mock_upload_track,
        mock_enrich,
        mock_download_track,
        mock_bulk_add_pl,
        mock_load_state,
        mock_find_dup,
        mock_gemini,
    ):
        mock_get_db.return_value = ("db_123", "folder_123")
        mock_search.return_value = "state_file_id"
        mock_download_track.return_value = "/tmp/fake.opus"
        mock_enrich.return_value = {"album": "Single", "duration": "03:00"}
        mock_upload_track.side_effect = lambda path: f"uploaded_{os.path.basename(path)}"
        mock_bulk_db.return_value = True

        # Simulate 12 tracks to import
        tracks = [
            {"title": f"Song {i}", "artist": "Artist", "spotify_id": f"sp_{i}"}
            for i in range(12)
        ]
        initial_state = {
            "playlist_id": "test_pl_throttle",
            "playlist_name": "Throttle Test",
            "tracks": tracks,
            "processed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "status": "running",
        }
        active_imports["test_pl_throttle"] = initial_state

        run_playlist_import("test_pl_throttle", batch_size=15)

        # Confirm all 12 tracks were processed in memory
        final_state = active_imports["test_pl_throttle"]
        self.assertEqual(final_state["processed"], 12)
        self.assertEqual(final_state["downloaded"], 12)
        self.assertEqual(final_state["status"], "completed")

        # Confirm upload_json was called for checkpoints/flush/completion, far less than old 24+ calls
        self.assertLess(mock_upload_json.call_count, 10)

    @patch("scraper.downloader.time.sleep")
    @patch("scraper.downloader.os.path.exists")
    @patch("scraper.downloader.yt_dlp.YoutubeDL")
    def test_download_track_lifecycle_passes(self, mock_ydl_class, mock_exists, mock_sleep):
        from scraper.downloader import download_track

        mock_exists.return_value = True

        # Mock YoutubeDL instance context manager
        mock_ydl_search = MagicMock()
        mock_ydl_search.extract_info.return_value = {
            "entries": [
                {
                    "title": "Artist - Song Official Audio",
                    "channel": "Artist Topic",
                    "id": "yt_video_123",
                    "url": "https://www.youtube.com/watch?v=yt_video_123",
                }
            ]
        }

        mock_ydl_download = MagicMock()
        mock_ydl_download.extract_info.return_value = {
            "id": "yt_video_123",
            "acodec": "opus",
            "ext": "opus",
        }

        mock_ydl_class.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_search), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=mock_ydl_download), __exit__=MagicMock()),
        ]

        res = download_track("Song", "Artist", "/tmp/music", track_id="sp_track_1")

        # 1. Confirm exactly 2 YoutubeDL passes were executed (Search + Download), NOT 3
        self.assertEqual(mock_ydl_class.call_count, 2)

        # 2. Confirm search pass options (Pass 1)
        search_opts = mock_ydl_class.call_args_list[0][0][0]
        self.assertTrue(search_opts.get("extract_flat"))
        mock_ydl_search.extract_info.assert_called_once()
        self.assertFalse(mock_ydl_search.extract_info.call_args[1].get("download", True))

        # 3. Confirm download pass options (Pass 2)
        download_opts = mock_ydl_class.call_args_list[1][0][0]
        self.assertEqual(download_opts.get("concurrent_fragment_downloads"), 4)
        self.assertIn("bestaudio[acodec=opus]", download_opts.get("format", ""))
        mock_ydl_download.extract_info.assert_called_once_with(
            "https://www.youtube.com/watch?v=yt_video_123", download=True
        )

    @patch("scraper.gemini_import_pipeline.apply_gemini_to_import_batch", return_value={"tracks_submitted": 2, "tracks_updated": 0, "fields_updated": 0, "language_updates": 0, "genre_updates": 0, "errors": []})
    @patch("scraper.state_manager.find_duplicate_track", return_value=None)
    @patch("scraper.state_manager.load_state", return_value={})
    @patch("scraper.playlist_importer.bulk_add_tracks_to_playlist")
    @patch("scraper.playlist_importer.download_track")
    @patch("scraper.playlist_importer.enrich_track_metadata")
    @patch("scraper.playlist_importer.upload_track")
    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    @patch("scraper.drive_uploader.bulk_update_database")
    def test_run_playlist_import_success_timing(
        self, mock_bulk_db, mock_get_db, mock_upload_json, mock_download_json,
        mock_search, mock_upload_track, mock_enrich, mock_download_track,
        mock_bulk_add_pl, mock_load_state, mock_find_dup, mock_gemini
    ):
        mock_get_db.return_value = ("db_123", "folder_123")
        mock_search.return_value = "state_file_id"
        mock_download_track.return_value = "/tmp/fake.opus"
        mock_enrich.return_value = {"album": "Single", "duration": "03:00"}
        mock_upload_track.side_effect = lambda path: f"uploaded_{os.path.basename(path)}"
        mock_bulk_db.return_value = True

        tracks = [{"title": "Song 1", "artist": "Artist", "spotify_id": "sp_1"}]
        active_imports["test_pl_timing_success"] = {
            "playlist_id": "test_pl_timing_success",
            "playlist_name": "Success Timing Test",
            "tracks": tracks,
            "processed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "status": "running",
            "started_at": "2026-08-30T00:00:00.000000Z",
        }

        run_playlist_import("test_pl_timing_success", batch_size=5)

        st = active_imports["test_pl_timing_success"]
        self.assertEqual(st["status"], "completed")
        self.assertIsNotNone(st.get("started_at"))
        self.assertIsNotNone(st.get("completed_at"))
        self.assertIsNotNone(st.get("duration_seconds"))
        self.assertGreaterEqual(st["duration_seconds"], 0)

    @patch("scraper.state_manager.find_duplicate_track", return_value=None)
    @patch("scraper.state_manager.load_state", return_value={})
    @patch("scraper.playlist_importer.download_track")
    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    def test_run_playlist_import_cancellation_timing(
        self, mock_get_db, mock_upload_json, mock_download_json, mock_search, mock_download_track, mock_load_state, mock_find_dup
    ):
        from scraper.playlist_importer import create_cancel_event, set_cancel_event
        mock_get_db.return_value = ("db_123", "folder_123")
        mock_search.return_value = "state_file_id"

        active_imports["test_pl_timing_cancel"] = {
            "playlist_id": "test_pl_timing_cancel",
            "playlist_name": "Cancel Timing Test",
            "tracks": [{"title": "Song 1", "artist": "Artist"}],
            "processed": 0,
            "status": "running",
            "started_at": "2026-08-30T00:00:00.000000Z",
        }
        create_cancel_event("test_pl_timing_cancel")
        set_cancel_event("test_pl_timing_cancel")

        run_playlist_import("test_pl_timing_cancel")

        st = active_imports["test_pl_timing_cancel"]
        self.assertEqual(st["status"], "cancelled")
        self.assertIsNotNone(st.get("started_at"))
        self.assertIsNotNone(st.get("completed_at"))
        self.assertIsNotNone(st.get("duration_seconds"))

    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    def test_run_playlist_import_failure_timing(
        self, mock_get_db, mock_upload_json, mock_download_json, mock_search
    ):
        mock_get_db.side_effect = RuntimeError("Simulated fatal db error")

        active_imports["test_pl_timing_fail"] = {
            "playlist_id": "test_pl_timing_fail",
            "playlist_name": "Failure Timing Test",
            "tracks": [{"title": "Song 1", "artist": "Artist"}],
            "processed": 0,
            "status": "running",
            "started_at": "2026-08-30T00:00:00.000000Z",
        }

        with self.assertRaises(RuntimeError):
            run_playlist_import("test_pl_timing_fail")

        st = active_imports["test_pl_timing_fail"]
        self.assertEqual(st["status"], "failed")
        self.assertIsNotNone(st.get("started_at"))
        self.assertIsNotNone(st.get("completed_at"))
        self.assertIsNotNone(st.get("duration_seconds"))

    @patch("scraper.playlist_manager.load_playlists")
    def test_find_playlist_by_source_url(self, mock_load):
        from scraper.playlist_manager import find_playlist_by_source_url

        mock_load.return_value = [
            {
                "id": "pl_111",
                "name": "My Top Hits",
                "source_url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abcd1234efgh",
                "total_tracks": 50,
                "track_ids": ["t1", "t2"],
            },
            {
                "id": "pl_222",
                "name": "Chill Vibes",
                "source_url": "spotify:playlist:5ABCdEfGhIjKlMnOpQrStU",
                "total_tracks": 25,
                "track_ids": ["t3"],
            },
        ]

        # Match with different query parameter ?si=...
        match1 = find_playlist_by_source_url("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=different_share_token")
        self.assertIsNotNone(match1)
        self.assertEqual(match1["id"], "pl_111")

        # Match with clean URL
        match2 = find_playlist_by_source_url("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        self.assertIsNotNone(match2)
        self.assertEqual(match2["id"], "pl_111")

        # Match with URI format
        match3 = find_playlist_by_source_url("https://open.spotify.com/playlist/5ABCdEfGhIjKlMnOpQrStU")
        self.assertIsNotNone(match3)
        self.assertEqual(match3["id"], "pl_222")

        # No match
        no_match = find_playlist_by_source_url("https://open.spotify.com/playlist/0000000000000000000000")
        self.assertIsNone(no_match)

    @patch("scraper.playlist_importer.search_file_by_name", return_value="state_file_id")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.get_db_file_id", return_value=("db_1", "parent_1"))
    @patch("scraper.playlist_importer.scrape_spotify_embed_playlist")
    @patch("scraper.playlist_importer.get_playlist_preview")
    @patch("scraper.playlist_importer.find_playlist_by_source_url")
    @patch("scraper.playlist_importer.add_playlist")
    def test_start_playlist_import_scenarios(
        self, mock_add_pl, mock_find_pl, mock_preview, mock_scrape,
        mock_get_db, mock_download_json, mock_upload_json, mock_search
    ):
        from scraper.playlist_importer import (
            start_playlist_import,
            PlaylistAlreadyDownloadedError,
        )

        # Mock database.json containing tracks id_0 to id_9 with spotify_ids sp_0 to sp_9
        mock_download_json.return_value = [
            {"driveFileId": f"id_{i}", "spotify_id": f"sp_{i}", "title": f"Song {i}", "artist": "Artist"}
            for i in range(10)
        ]

        mock_preview.return_value = {
            "playlist_id": "37i9dQZF1DXcBWIGoYBM5M",
            "playlist_name": "Test Hits",
            "total_tracks": 10,
            "tracks_available_for_import": 10,
        }

        # --- Scenario A: Brand new playlist ---
        mock_scrape.return_value = [{"title": f"Song {i}", "artist": "Artist", "spotify_id": f"sp_{i}"} for i in range(10)]
        mock_find_pl.return_value = None
        mock_add_pl.return_value = "new_pl_uuid_123"

        pl_id_a = start_playlist_import("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(pl_id_a, "new_pl_uuid_123")
        mock_add_pl.assert_called_once()
        mock_add_pl.reset_mock()

        # --- Scenario B1: Fully downloaded playlist (all 10 tracks match) ---
        mock_find_pl.return_value = {
            "id": "existing_pl_full",
            "name": "Test Hits",
            "source_url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "total_tracks": 10,
            "track_ids": [f"id_{i}" for i in range(10)],
        }
        mock_scrape.return_value = [{"title": f"Song {i}", "artist": "Artist", "spotify_id": f"sp_{i}"} for i in range(10)]

        with self.assertRaises(PlaylistAlreadyDownloadedError) as ctx:
            start_playlist_import("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(ctx.exception.playlist_id, "existing_pl_full")
        self.assertEqual(ctx.exception.total_tracks, 10)
        mock_add_pl.assert_not_called()

        # --- Scenario B2: Spotify shrunk from 10 to 8 tracks (2 removed, 0 added) ---
        mock_scrape.return_value = [{"title": f"Song {i}", "artist": "Artist", "spotify_id": f"sp_{i}"} for i in range(8)]
        with self.assertRaises(PlaylistAlreadyDownloadedError) as ctx:
            start_playlist_import("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(ctx.exception.playlist_id, "existing_pl_full")
        mock_add_pl.assert_not_called()

        # --- Scenario C: Playlist rotated (10 existing tracks, now 9 on Spotify: 3 removed, 2 new added sp_new1, sp_new2) ---
        mock_scrape.return_value = [
            {"title": f"Song {i}", "artist": "Artist", "spotify_id": f"sp_{i}"} for i in range(7)
        ] + [
            {"title": "New Song 1", "artist": "Artist", "spotify_id": "sp_new1"},
            {"title": "New Song 2", "artist": "Artist", "spotify_id": "sp_new2"}
        ]

        pl_id_c = start_playlist_import("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(pl_id_c, "existing_pl_full")
        mock_add_pl.assert_not_called()  # Reuses existing ID, resumes import to fetch the 2 new tracks!

        # --- Scenario D: Simple partial import (4/10 tracks) ---
        mock_find_pl.return_value = {
            "id": "existing_pl_partial",
            "name": "Test Hits",
            "source_url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
            "total_tracks": 4,
            "track_ids": ["id_0", "id_1", "id_2", "id_3"],
        }
        mock_scrape.return_value = [{"title": f"Song {i}", "artist": "Artist", "spotify_id": f"sp_{i}"} for i in range(10)]

        pl_id_d = start_playlist_import("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(pl_id_d, "existing_pl_partial")
        mock_add_pl.assert_not_called()

    @patch("scraper.gemini_import_pipeline.apply_gemini_to_import_batch", return_value={"tracks_submitted": 2, "tracks_updated": 0, "fields_updated": 0, "language_updates": 0, "genre_updates": 0, "errors": []})
    @patch("scraper.state_manager.find_duplicate_track", return_value=None)
    @patch("scraper.state_manager.load_state", return_value={})
    @patch("scraper.playlist_importer.bulk_add_tracks_to_playlist")
    @patch("scraper.playlist_importer.download_track")
    @patch("scraper.playlist_importer.enrich_track_metadata")
    @patch("scraper.playlist_importer.upload_track")
    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    @patch("scraper.drive_uploader.bulk_update_database")
    def test_concurrent_batch_in_flight_deduplication(
        self, mock_bulk_db, mock_get_db, mock_upload_json, mock_download_json,
        mock_search, mock_upload_track, mock_enrich, mock_download_track,
        mock_bulk_add_pl, mock_load_state, mock_find_dup, mock_gemini
    ):
        mock_get_db.return_value = ("db_123", "folder_123")
        mock_search.return_value = "state_file_id"
        mock_download_json.return_value = []
        mock_enrich.return_value = {"album": "Single", "duration": "03:00"}
        mock_upload_track.side_effect = lambda path: f"uploaded_{os.path.basename(path)}"
        mock_bulk_db.return_value = True

        # Simulate slow download so both worker threads overlap
        def slow_download(title, artist, out_dir, track_id=None, cancel_check_callback=None):
            time.sleep(0.05)
            return f"/tmp/music/{track_id}.opus"
        mock_download_track.side_effect = slow_download

        # Two identical tracks in the same batch
        tracks = [
            {"title": "Same Song", "artist": "Same Artist", "spotify_id": "sp_same_1"},
            {"title": "Same Song", "artist": "Same Artist", "spotify_id": "sp_same_1"},
        ]
        active_imports["test_pl_inflight"] = {
            "playlist_id": "test_pl_inflight",
            "playlist_name": "InFlight Test",
            "tracks": tracks,
            "processed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "status": "running",
            "started_at": "2026-08-30T00:00:00.000000Z",
        }

        run_playlist_import("test_pl_inflight", batch_size=4)

        st = active_imports["test_pl_inflight"]
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["processed"], 2)
        # Downloaded 1, skipped 1 duplicate in-flight
        self.assertEqual(st["downloaded"], 1)
        self.assertEqual(st["skipped"], 1)
        self.assertEqual(st["failed"], 0)
        self.assertEqual(mock_download_track.call_count, 1)

    @patch("scraper.gemini_import_pipeline.apply_gemini_to_import_batch", return_value={"tracks_submitted": 3, "tracks_updated": 0, "fields_updated": 0, "language_updates": 0, "genre_updates": 0, "errors": []})
    @patch("scraper.state_manager.find_duplicate_track", return_value=None)
    @patch("scraper.state_manager.load_state", return_value={})
    @patch("scraper.playlist_importer.bulk_add_tracks_to_playlist")
    @patch("scraper.playlist_importer.download_track")
    @patch("scraper.playlist_importer.enrich_track_metadata")
    @patch("scraper.playlist_importer.upload_track")
    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    @patch("scraper.drive_uploader.bulk_update_database")
    def test_concurrent_batch_error_isolation(
        self, mock_bulk_db, mock_get_db, mock_upload_json, mock_download_json,
        mock_search, mock_upload_track, mock_enrich, mock_download_track,
        mock_bulk_add_pl, mock_load_state, mock_find_dup, mock_gemini
    ):
        mock_get_db.return_value = ("db_123", "folder_123")
        mock_search.return_value = "state_file_id"
        mock_download_json.return_value = []
        mock_enrich.return_value = {"album": "Single", "duration": "03:00"}
        mock_upload_track.side_effect = lambda path: f"uploaded_{os.path.basename(path)}"
        mock_bulk_db.return_value = True

        # Track 2 raises an exception during download, others succeed
        def download_with_one_failure(title, artist, out_dir, track_id=None, cancel_check_callback=None):
            if "Fail" in title:
                raise RuntimeError("Simulated yt-dlp 403 Forbidden")
            return f"/tmp/music/{track_id}.opus"
        mock_download_track.side_effect = download_with_one_failure

        tracks = [
            {"title": "Track 1", "artist": "Artist", "spotify_id": "sp_1"},
            {"title": "Fail Track 2", "artist": "Artist", "spotify_id": "sp_2"},
            {"title": "Track 3", "artist": "Artist", "spotify_id": "sp_3"},
            {"title": "Track 4", "artist": "Artist", "spotify_id": "sp_4"},
        ]
        active_imports["test_pl_error_iso"] = {
            "playlist_id": "test_pl_error_iso",
            "playlist_name": "Error Isolation Test",
            "tracks": tracks,
            "processed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "status": "running",
            "started_at": "2026-08-30T00:00:00.000000Z",
        }

        run_playlist_import("test_pl_error_iso", batch_size=4)

        st = active_imports["test_pl_error_iso"]
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["processed"], 4)
        self.assertEqual(st["downloaded"], 3)
        self.assertEqual(st["failed"], 1)
        self.assertEqual(st["skipped"], 0)

    @patch("scraper.gemini_import_pipeline.apply_gemini_to_import_batch", return_value={"tracks_submitted": 2, "tracks_updated": 0, "fields_updated": 0, "language_updates": 0, "genre_updates": 0, "errors": []})
    @patch("scraper.state_manager.find_duplicate_track", return_value=None)
    @patch("scraper.state_manager.load_state", return_value={})
    @patch("scraper.playlist_importer.bulk_add_tracks_to_playlist")
    @patch("scraper.playlist_importer.download_track")
    @patch("scraper.playlist_importer.enrich_track_metadata")
    @patch("scraper.playlist_importer.upload_track")
    @patch("scraper.playlist_importer.search_file_by_name")
    @patch("scraper.playlist_importer.download_json")
    @patch("scraper.playlist_importer.upload_json")
    @patch("scraper.playlist_importer.get_db_file_id")
    @patch("scraper.drive_uploader.bulk_update_database")
    def test_concurrent_batch_cancellation(
        self, mock_bulk_db, mock_get_db, mock_upload_json, mock_download_json,
        mock_search, mock_upload_track, mock_enrich, mock_download_track,
        mock_bulk_add_pl, mock_load_state, mock_find_dup, mock_gemini
    ):
        from scraper.playlist_importer import set_cancel_event, create_cancel_event
        create_cancel_event("test_pl_cancel_mid")
        mock_get_db.return_value = ("db_123", "folder_123")
        mock_search.return_value = "state_file_id"
        mock_download_json.return_value = []
        mock_enrich.return_value = {"album": "Single", "duration": "03:00"}
        mock_upload_track.side_effect = lambda path: f"uploaded_{os.path.basename(path)}"
        mock_bulk_db.return_value = True

        # When first track downloads, trigger set_cancel_event
        def download_and_cancel(title, artist, out_dir, track_id=None, cancel_check_callback=None):
            if "Track 1" in title:
                set_cancel_event("test_pl_cancel_mid")
            return f"/tmp/music/{track_id}.opus"
        mock_download_track.side_effect = download_and_cancel

        tracks = [
            {"title": "Track 1", "artist": "Artist", "spotify_id": "sp_1"},
            {"title": "Track 2", "artist": "Artist", "spotify_id": "sp_2"},
            {"title": "Track 3", "artist": "Artist", "spotify_id": "sp_3"},
            {"title": "Track 4", "artist": "Artist", "spotify_id": "sp_4"},
        ]
        active_imports["test_pl_cancel_mid"] = {
            "playlist_id": "test_pl_cancel_mid",
            "playlist_name": "Cancel Mid Test",
            "tracks": tracks,
            "processed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "status": "running",
            "started_at": "2026-08-30T00:00:00.000000Z",
        }

        run_playlist_import("test_pl_cancel_mid", batch_size=4)

        st = active_imports["test_pl_cancel_mid"]
        self.assertEqual(st["status"], "cancelled")

    @patch("scraper.playlist_manager.get_db_file_id", return_value=("db_file_id", "parent_123"))
    @patch("scraper.playlist_manager._load_playlists_unlocked")
    @patch("scraper.playlist_manager._save_playlists_unlocked")
    def test_delete_playlist(self, mock_save, mock_load, mock_get_db):
        from scraper.playlist_manager import delete_playlist

        # Case 1: Empty playlist_id
        self.assertFalse(delete_playlist(None))
        self.assertFalse(delete_playlist(""))
        mock_save.assert_not_called()

        # Case 2: Playlist not found
        mock_load.return_value = [
            {"id": "pl_1", "name": "Playlist 1", "track_ids": ["t1", "t2"]},
            {"id": "pl_2", "name": "Playlist 2", "track_ids": ["t3"]},
        ]
        result_not_found = delete_playlist("nonexistent_id")
        self.assertFalse(result_not_found)
        mock_save.assert_not_called()

        # Case 3: Playlist found and deleted successfully
        result_deleted = delete_playlist("pl_1")
        self.assertTrue(result_deleted)
        mock_save.assert_called_once()
        saved_playlists = mock_save.call_args[0][1]
        self.assertEqual(len(saved_playlists), 1)
        self.assertEqual(saved_playlists[0]["id"], "pl_2")

    @patch("scraper.playlist_manager.get_playlist")
    @patch("scraper.playlist_manager.delete_playlist")
    def test_delete_playlist_api_routes(self, mock_delete, mock_get):
        from dashboard.app import app
        client = app.test_client()

        # 404 when playlist does not exist
        mock_get.return_value = None
        res_404 = client.delete("/api/playlists/nonexistent_123")
        self.assertEqual(res_404.status_code, 404)
        data_404 = res_404.get_json()
        self.assertIn("not found", data_404.get("error", "").lower())

        # 200 when playlist is successfully deleted via DELETE
        mock_get.return_value = {"id": "pl_123", "name": "Test Playlist"}
        mock_delete.return_value = True
        res_delete = client.delete("/api/playlists/pl_123")
        self.assertEqual(res_delete.status_code, 200)
        data_delete = res_delete.get_json()
        self.assertEqual(data_delete.get("status"), "success")
        self.assertEqual(data_delete.get("playlist_id"), "pl_123")

        # 200 when playlist is deleted via POST /api/playlists/<id>/delete
        res_post_delete = client.post("/api/playlists/pl_123/delete")
        self.assertEqual(res_post_delete.status_code, 200)
        data_post = res_post_delete.get_json()
        self.assertEqual(data_post.get("status"), "success")


if __name__ == "__main__":
    unittest.main()
