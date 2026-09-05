import base64
import os
import unittest
from dashboard.app import app, is_production_environment


class ProductionAdminAuthTestCase(unittest.TestCase):
    """
    Tests for Production environment (Render).
    Admin pages must fail closed and require valid HTTP Basic Auth credentials.
    """
    def setUp(self):
        self.client = app.test_client()
        self.test_password = "super-secret-admin-pass-987"
        self.env_patch = {
            "ADMIN_PASSWORD": self.test_password,
            "RENDER": "true",
            "RENDER_SERVICE_ID": "srv-test-production-12345",
        }
        self.orig_env = {k: os.environ.get(k) for k in self.env_patch.keys()}
        os.environ.update(self.env_patch)

    def tearDown(self):
        for k, v in self.orig_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def _basic_auth_header(self, username="admin", password=""):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {token}"}

    def test_production_environment_detected(self):
        self.assertTrue(is_production_environment())

    def test_admin_page_unauthenticated_returns_401(self):
        """GET /admin without auth returns 401 with WWW-Authenticate header in production."""
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 401)
        self.assertIn('WWW-Authenticate', res.headers)
        self.assertIn('Basic realm="Wavify Admin"', res.headers['WWW-Authenticate'])

    def test_admin_page_authenticated_returns_200(self):
        """GET /admin with valid credentials returns 200 and renders full admin controls."""
        headers = self._basic_auth_header("admin", self.test_password)
        res = self.client.get('/admin', headers=headers)
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        self.assertIn("section-downloader", html)
        self.assertIn("section-settings", html)
        self.assertIn("section-storage", html)
        self.assertIn("section-logs", html)
        self.assertIn("section-data-health", html)
        self.assertIn("Cloud Music Player Admin", html)

    def test_admin_page_invalid_password_returns_401(self):
        """GET /admin with incorrect password returns 401 in production."""
        headers = self._basic_auth_header("admin", "wrong-password")
        res = self.client.get('/admin', headers=headers)
        self.assertEqual(res.status_code, 401)

    def test_admin_page_fails_closed_when_env_unset(self):
        """When ADMIN_PASSWORD is unset in production, /admin returns 401 (never fails open)."""
        os.environ.pop("ADMIN_PASSWORD", None)
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 401)
        headers = self._basic_auth_header("admin", "any-password")
        res = self.client.get('/admin', headers=headers)
        self.assertEqual(res.status_code, 401)

    def test_gemini_backfill_requires_auth(self):
        """GET /gemini-backfill returns 401 without auth, 200 with valid auth in production."""
        res = self.client.get('/gemini-backfill')
        self.assertEqual(res.status_code, 401)

        headers = self._basic_auth_header("admin", self.test_password)
        res = self.client.get('/gemini-backfill', headers=headers)
        self.assertEqual(res.status_code, 200)

    def test_imported_playlists_requires_auth(self):
        """GET /imported-playlists returns 401 without auth, 200 with valid auth in production."""
        res = self.client.get('/imported-playlists')
        self.assertEqual(res.status_code, 401)

        headers = self._basic_auth_header("admin", self.test_password)
        res = self.client.get('/imported-playlists', headers=headers)
        self.assertEqual(res.status_code, 200)


class LocalAdminAuthTestCase(unittest.TestCase):
    """
    Tests for Local development environment (non-production).
    Admin pages should be freely accessible with NO password prompt.
    """
    def setUp(self):
        self.client = app.test_client()
        self.env_keys = ["ADMIN_PASSWORD", "RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID"]
        self.orig_env = {k: os.environ.get(k) for k in self.env_keys}
        for k in self.env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.orig_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_local_environment_detected(self):
        self.assertFalse(is_production_environment())

    def test_admin_page_open_without_auth_and_unset_password(self):
        """On localhost with ADMIN_PASSWORD unset, /admin returns 200 without any auth prompt."""
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('WWW-Authenticate', res.headers)
        html = res.data.decode('utf-8')
        self.assertIn("Cloud Music Player Admin", html)
        self.assertIn("section-downloader", html)

    def test_admin_page_open_even_if_password_configured_locally(self):
        """On localhost, /admin returns 200 without prompts even if ADMIN_PASSWORD is set."""
        os.environ["ADMIN_PASSWORD"] = "local-dummy-password"
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('WWW-Authenticate', res.headers)

    def test_gemini_backfill_open_locally(self):
        """On localhost, /gemini-backfill returns 200 without auth."""
        res = self.client.get('/gemini-backfill')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('WWW-Authenticate', res.headers)

    def test_imported_playlists_open_locally(self):
        """On localhost, /imported-playlists returns 200 without auth."""
        res = self.client.get('/imported-playlists')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('WWW-Authenticate', res.headers)


class PublicRoutesTestCase(unittest.TestCase):
    """
    Verify public listener, app endpoints, and public elements remain accessible.
    """
    def setUp(self):
        self.client = app.test_client()

    def test_public_index_unauthenticated_returns_200(self):
        """GET / is completely public, returning 200 without auth and stripped of admin panels."""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        self.assertIn("Music Library", html)
        self.assertIn("section-library", html)
        self.assertIn("section-artists", html)
        self.assertIn("tracks-table", html)
        self.assertNotIn("section-downloader", html)
        self.assertNotIn("section-settings", html)

    def test_head_index_unauthenticated_returns_200(self):
        """HEAD / returns 200 OK immediately for Android app cold-start prewarming."""
        res = self.client.head('/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, b"")

    def test_public_routes_unaffected(self):
        """Verify public listener and app endpoints require zero admin auth."""
        for path in ['/download', '/app', '/ping']:
            res = self.client.get(path)
            self.assertNotEqual(res.status_code, 401, f"{path} should not require admin auth")


if __name__ == '__main__':
    unittest.main()
