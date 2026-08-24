import unittest
from unittest.mock import patch, MagicMock
from dashboard.app import app, get_latest_app_release, _app_release_cache


class DownloadRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        _app_release_cache["data"] = None
        _app_release_cache["timestamp"] = 0

    def tearDown(self):
        _app_release_cache["data"] = None
        _app_release_cache["timestamp"] = 0

    @patch('requests.get')
    def test_get_latest_app_release_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tag_name": "v1.2.3",
            "name": "Wavify v1.2.3",
            "published_at": "2026-08-24T14:00:00Z",
            "html_url": "https://github.com/Peepeepoopookk/wavify/releases/tag/v1.2.3",
            "body": "Fixed audio buffer underruns.",
            "assets": [
                {
                    "name": "Wavify-1.2.3.apk",
                    "size": 5242880,  # 5 MB
                    "browser_download_url": "https://github.com/Peepeepoopookk/wavify/releases/download/v1.2.3/Wavify-1.2.3.apk"
                }
            ]
        }
        mock_get.return_value = mock_response

        release = get_latest_app_release()
        self.assertEqual(release["tag_name"], "v1.2.3")
        self.assertEqual(release["version_name"], "Wavify v1.2.3")
        self.assertEqual(release["apk_name"], "Wavify-1.2.3.apk")
        self.assertEqual(release["apk_size_mb"], "5.00 MB")
        self.assertEqual(release["apk_download_url"], "https://github.com/Peepeepoopookk/wavify/releases/download/v1.2.3/Wavify-1.2.3.apk")
        self.assertFalse(release["is_fallback"])

    @patch('requests.get')
    def test_get_latest_app_release_fallback_on_error(self, mock_get):
        mock_get.side_effect = Exception("Network timeout")
        release = get_latest_app_release()
        self.assertTrue(release["is_fallback"])
        self.assertIn("v1.0.0", release["tag_name"])
        self.assertIn("github.com/Peepeepoopookk/wavify", release["apk_download_url"])

    @patch('requests.get')
    def test_download_page_renders(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tag_name": "v1.0.0",
            "name": "Wavify v1.0.0",
            "published_at": "2026-08-24T14:00:00Z",
            "html_url": "https://github.com/Peepeepoopookk/wavify/releases/tag/v1.0.0",
            "body": "Initial release",
            "assets": [
                {
                    "name": "Wavify-1.0.0.apk",
                    "size": 4374891,
                    "browser_download_url": "https://github.com/Peepeepoopookk/wavify/releases/download/v1.0.0/Wavify-1.0.0.apk"
                }
            ]
        }
        mock_get.return_value = mock_response

        # Test /download
        res = self.client.get('/download')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Wavify for Android", res.data)
        self.assertIn(b"v1.0.0", res.data)
        self.assertIn(b"/download/latest.apk", res.data)

        # Test /app alias
        res_app = self.client.get('/app')
        self.assertEqual(res_app.status_code, 200)
        self.assertIn(b"Wavify for Android", res_app.data)

    @patch('requests.get')
    def test_download_apk_redirect(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tag_name": "v1.0.0",
            "name": "Wavify v1.0.0",
            "published_at": "2026-08-24T14:00:00Z",
            "html_url": "https://github.com/Peepeepoopookk/wavify/releases/tag/v1.0.0",
            "body": "Initial release",
            "assets": [
                {
                    "name": "Wavify-1.0.0.apk",
                    "size": 4374891,
                    "browser_download_url": "https://github.com/Peepeepoopookk/wavify/releases/download/v1.0.0/Wavify-1.0.0.apk"
                }
            ]
        }
        mock_get.return_value = mock_response

        # Test /download/latest.apk
        res = self.client.get('/download/latest.apk')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(
            res.headers['Location'],
            "https://github.com/Peepeepoopookk/wavify/releases/download/v1.0.0/Wavify-1.0.0.apk"
        )

        # Test /app/download alias
        res_alias = self.client.get('/app/download')
        self.assertEqual(res_alias.status_code, 302)
        self.assertEqual(
            res_alias.headers['Location'],
            "https://github.com/Peepeepoopookk/wavify/releases/download/v1.0.0/Wavify-1.0.0.apk"
        )

    @patch('requests.get')
    def test_api_app_release_endpoint(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tag_name": "v1.0.0",
            "name": "Wavify v1.0.0",
            "published_at": "2026-08-24T14:00:00Z",
            "html_url": "https://github.com/Peepeepoopookk/wavify/releases/tag/v1.0.0",
            "body": "Initial release",
            "assets": [
                {
                    "name": "Wavify-1.0.0.apk",
                    "size": 4374891,
                    "browser_download_url": "https://github.com/Peepeepoopookk/wavify/releases/download/v1.0.0/Wavify-1.0.0.apk"
                }
            ]
        }
        mock_get.return_value = mock_response

        res = self.client.get('/api/app/release')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["tag_name"], "v1.0.0")
        self.assertEqual(data["apk_name"], "Wavify-1.0.0.apk")


if __name__ == '__main__':
    unittest.main()
