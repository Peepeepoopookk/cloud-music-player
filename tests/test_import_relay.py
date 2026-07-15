import os
import unittest
from unittest.mock import patch


from dashboard.app import app


class ImportRelayEndpointTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {"APP_WRITE_TOKEN": "test-app-token", "WAVIFY_WORKER_TOKEN": "test-worker-token"},
        )
        self.env_patch.start()
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.app_headers = {"Authorization": "Bearer test-app-token"}
        self.worker_headers = {"Authorization": "Bearer test-worker-token"}

    def tearDown(self):
        self.env_patch.stop()

    @patch("dashboard.app.create_import_job")
    def test_request_song_job(self, create_job):
        create_job.return_value = {"job_id": "job-1", "status": "pending"}
        response = self.client.post(
            "/api/import/request",
            headers=self.app_headers,
            json={
                "url": "https://open.spotify.com/track/abc123?si=test",
                "type": "song",
                "requested_by": "phone-1",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), {"job_id": "job-1", "status": "pending"})
        create_job.assert_called_once_with(
            "https://open.spotify.com/track/abc123",
            "song",
            requested_by="phone-1",
        )

    def test_request_rejects_malformed_url(self):
        response = self.client.post(
            "/api/import/request",
            headers=self.app_headers,
            json={"url": "https://example.com/file", "type": "song"},
        )
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.app.get_import_job")
    def test_status_returns_persisted_job(self, get_job):
        get_job.return_value = {"job_id": "job-1", "status": "completed"}
        response = self.client.get("/api/import/status/job-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "completed")

    def test_worker_endpoint_requires_token(self):
        response = self.client.get("/api/worker/next")
        self.assertEqual(response.status_code, 401)

    @patch("dashboard.app.claim_next_import_job")
    def test_worker_claims_one_job(self, claim):
        claim.return_value = {"job_id": "job-1", "status": "processing"}
        response = self.client.get("/api/worker/next", headers=self.worker_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job"]["job_id"], "job-1")

    @patch("dashboard.app.claim_next_import_job")
    def test_worker_empty_queue(self, claim):
        claim.return_value = None
        response = self.client.get("/api/worker/next", headers=self.worker_headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["job"])

    @patch("dashboard.app.set_import_job_result")
    def test_worker_reports_result(self, set_result):
        set_result.return_value = {"job_id": "job-1", "status": "completed"}
        response = self.client.post(
            "/api/worker/result",
            headers=self.worker_headers,
            json={
                "job_id": "job-1",
                "status": "completed",
                "result": {"title": "Song", "artist": "Artist", "driveFileId": "drive-1"},
                "error": None,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "completed")


if __name__ == "__main__":
    unittest.main()
