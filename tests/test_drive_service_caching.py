import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os
import threading

import dashboard.drive_client as dc
from dashboard.drive_client import (
    get_oauth_drive_service,
    upload_json,
    download_json,
    list_files,
)


class DriveServiceCachingTests(unittest.TestCase):
    def setUp(self):
        dc._cached_credentials = None
        dc._cached_token_path = None
        if hasattr(dc._thread_local, "oauth_drive_service"):
            del dc._thread_local.oauth_drive_service
        if hasattr(dc._thread_local, "drive_service"):
            del dc._thread_local.drive_service

    def tearDown(self):
        dc._cached_credentials = None
        dc._cached_token_path = None
        if hasattr(dc._thread_local, "oauth_drive_service"):
            del dc._thread_local.oauth_drive_service
        if hasattr(dc._thread_local, "drive_service"):
            del dc._thread_local.drive_service

    @patch("dashboard.drive_client.build")
    @patch("dashboard.drive_client.Credentials.from_authorized_user_file")
    @patch("dashboard.drive_client.os.path.exists", return_value=True)
    def test_drive_service_singleton_10_consecutive_calls_single_thread(self, mock_exists, mock_creds_loader, mock_build):
        # Setup valid mock credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_loader.return_value = mock_creds

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        # Perform 10 consecutive calls in the same thread
        services = [get_oauth_drive_service() for _ in range(10)]

        # All returned services must be the identical cached object within this thread
        for s in services:
            self.assertIs(s, mock_service)

        # build() must only be called ONCE
        self.assertEqual(mock_build.call_count, 1)

    @patch("dashboard.drive_client.open", create=True)
    @patch("dashboard.drive_client.build")
    @patch("dashboard.drive_client.Credentials.from_authorized_user_file")
    @patch("dashboard.drive_client.os.path.exists", return_value=True)
    def test_drive_service_refreshes_on_token_expiry_mid_run(
        self, mock_exists, mock_creds_loader, mock_build, mock_open
    ):
        # 1. Initial valid credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds.refresh_token = "valid_refresh_token"
        mock_creds_loader.return_value = mock_creds

        mock_service_v1 = MagicMock(name="ServiceV1")
        mock_service_v2 = MagicMock(name="ServiceV2")
        mock_build.side_effect = [mock_service_v1, mock_service_v2]

        s1 = get_oauth_drive_service()
        self.assertIs(s1, mock_service_v1)
        self.assertEqual(mock_build.call_count, 1)

        # 2. Simulate token expiration mid-run
        mock_creds.valid = False
        mock_creds.expired = True

        # When refresh() is called, mark it valid again
        def refresh_side_effect(request):
            mock_creds.valid = True
            mock_creds.expired = False
        mock_creds.refresh.side_effect = refresh_side_effect

        # Next call must detect expired token, refresh credentials, and rebuild service for this thread
        s2 = get_oauth_drive_service()
        self.assertIs(s2, mock_service_v2)
        mock_creds.refresh.assert_called_once()
        self.assertEqual(mock_build.call_count, 2)

    @patch("dashboard.drive_client.build")
    @patch("dashboard.drive_client.Credentials.from_authorized_user_file")
    @patch("dashboard.drive_client.os.path.exists", return_value=True)
    def test_drive_service_multithreaded_isolation(self, mock_exists, mock_creds_loader, mock_build):
        # Setup valid mock credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_loader.return_value = mock_creds

        # Each thread will get its own separate mock service instance from build()
        mock_service_t1 = MagicMock(name="ServiceThread1")
        mock_service_t2 = MagicMock(name="ServiceThread2")
        mock_build.side_effect = [mock_service_t1, mock_service_t2]

        results = {}

        def thread_task(thread_id):
            # Each thread calls get_oauth_drive_service multiple times
            s_first = get_oauth_drive_service()
            s_second = get_oauth_drive_service()
            results[thread_id] = (s_first, s_second)

        t1 = threading.Thread(target=thread_task, args=("t1",))
        t2 = threading.Thread(target=thread_task, args=("t2",))

        t1.start()
        t1.join()
        t2.start()
        t2.join()

        # Thread 1's multiple calls reused its own single service instance
        self.assertIs(results["t1"][0], results["t1"][1])
        self.assertIs(results["t1"][0], mock_service_t1)

        # Thread 2's multiple calls reused its own single service instance
        self.assertIs(results["t2"][0], results["t2"][1])
        self.assertIs(results["t2"][0], mock_service_t2)

        # Thread 1 and Thread 2 have separate service objects (no shared socket/Resource)
        self.assertIsNot(results["t1"][0], results["t2"][0])

        # build() was called exactly twice (once per thread)
        self.assertEqual(mock_build.call_count, 2)

        # Credentials loader from disk was only called ONCE across all threads (shared credentials singleton)
        self.assertEqual(mock_creds_loader.call_count, 1)

    @patch("dashboard.drive_client.MediaInMemoryUpload")
    @patch("dashboard.drive_client.get_oauth_drive_service")
    def test_upload_json_uses_non_resumable_upload(self, mock_get_service, mock_media_upload):
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        upload_json("file_123", {"key": "val"}, "test.json")

        # Verify MediaInMemoryUpload was called with resumable=False
        mock_media_upload.assert_called_once()
        _, kwargs = mock_media_upload.call_args
        self.assertFalse(kwargs.get("resumable"))


if __name__ == "__main__":
    unittest.main()
