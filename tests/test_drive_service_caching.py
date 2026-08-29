import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os

import dashboard.drive_client as dc
from dashboard.drive_client import (
    get_oauth_drive_service,
    upload_json,
    download_json,
    list_files,
)


class DriveServiceCachingTests(unittest.TestCase):
    def setUp(self):
        dc._cached_drive_service = None
        dc._cached_credentials = None
        dc._cached_token_path = None

    def tearDown(self):
        dc._cached_drive_service = None
        dc._cached_credentials = None
        dc._cached_token_path = None

    @patch("dashboard.drive_client.build")
    @patch("dashboard.drive_client.Credentials.from_authorized_user_file")
    @patch("dashboard.drive_client.os.path.exists", return_value=True)
    def test_drive_service_singleton_10_consecutive_calls(self, mock_exists, mock_creds_loader, mock_build):
        # Setup valid mock credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_loader.return_value = mock_creds

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        # Perform 10 consecutive calls
        services = [get_oauth_drive_service() for _ in range(10)]

        # All returned services must be the identical cached object
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

        # Next call must detect expired token, refresh credentials, and rebuild service
        s2 = get_oauth_drive_service()
        self.assertIs(s2, mock_service_v2)
        mock_creds.refresh.assert_called_once()
        self.assertEqual(mock_build.call_count, 2)

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
