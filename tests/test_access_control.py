import os
import unittest
from unittest.mock import patch, MagicMock
from dashboard.app import app, get_dashboard_access_key


class DashboardAccessControlTestCase(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {"DASHBOARD_ACCESS_KEY": "supersecret123"}
        )
        self.env_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.env_patch.stop()

    def test_public_download_page_accessible_without_auth(self):
        res = self.client.get('/download')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Wavify for Android", res.data)

    def test_public_app_alias_accessible_without_auth(self):
        res = self.client.get('/app')
        self.assertEqual(res.status_code, 200)

    def test_public_download_apk_redirect_without_auth(self):
        res = self.client.get('/download/latest.apk')
        self.assertEqual(res.status_code, 302)

    def test_public_api_app_release_without_auth(self):
        res = self.client.get('/api/app/release')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("tag_name", data)

    def test_dashboard_root_redirects_unauthenticated_user_to_download(self):
        res = self.client.get('/')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get('Location'), '/download')

    def test_imported_playlists_redirects_unauthenticated_user_to_download(self):
        res = self.client.get('/imported-playlists')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get('Location'), '/download')

    def test_api_tracks_rejects_unauthenticated_request(self):
        res = self.client.get('/api/tracks')
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertEqual(data.get("error"), "Unauthorized. Access key required.")

    def test_access_key_in_query_param_authenticates_and_persists_session(self):
        # 1. First visit with ?key=supersecret123
        res = self.client.get('/?key=supersecret123')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Cloud Music Player", res.data)

        # 2. Subsequent visit without ?key on another page using same session cookie
        res_playlists = self.client.get('/imported-playlists')
        self.assertEqual(res_playlists.status_code, 200)

        # 3. Subsequent API call using same session cookie
        with patch('dashboard.app.get_db_file_id', return_value=None):
            res_api = self.client.get('/api/tracks')
            self.assertEqual(res_api.status_code, 200)

    def test_access_key_via_header_grants_access(self):
        # X-Dashboard-Key header
        with patch('dashboard.app.get_db_file_id', return_value=None):
            res = self.client.get('/api/tracks', headers={"X-Dashboard-Key": "supersecret123"})
            self.assertEqual(res.status_code, 200)

        # Authorization: Bearer header
        with patch('dashboard.app.get_db_file_id', return_value=None):
            res_bearer = self.client.get('/api/tracks', headers={"Authorization": "Bearer supersecret123"})
            self.assertEqual(res_bearer.status_code, 200)

    def test_logout_clears_session(self):
        # Authenticate first
        res = self.client.get('/?key=supersecret123')
        self.assertEqual(res.status_code, 200)

        # Log out
        res_logout = self.client.get('/logout')
        self.assertEqual(res_logout.status_code, 302)
        self.assertEqual(res_logout.headers.get('Location'), '/download')

        # Verify subsequent dashboard request is redirected back to /download
        res_after = self.client.get('/')
        self.assertEqual(res_after.status_code, 302)
        self.assertEqual(res_after.headers.get('Location'), '/download')


if __name__ == '__main__':
    unittest.main()
