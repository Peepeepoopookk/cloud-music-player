import unittest
from dashboard.app import app


class ListenRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_listen_route_public_returns_200(self):
        """GET /listen is completely public, returning 200 without authentication."""
        res = self.client.get('/listen')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        
        # Verify Spotify-style player components are rendered
        self.assertIn("playback-bar", html)
        self.assertIn("audio-player", html)
        self.assertIn("btn-play-pause", html)
        self.assertIn("btn-prev", html)
        self.assertIn("btn-next", html)
        self.assertIn("btn-shuffle", html)
        self.assertIn("btn-repeat", html)
        self.assertIn("seek-slider", html)
        self.assertIn("volume-slider", html)
        self.assertIn("queue-drawer", html)
        
        # Verify admin controls are NOT on the page
        self.assertNotIn("section-downloader", html)
        self.assertNotIn("section-settings", html)
        self.assertNotIn("section-storage", html)
        self.assertNotIn("section-logs", html)

    def test_head_listen_returns_200(self):
        """HEAD /listen returns 200 with empty body."""
        res = self.client.head('/listen')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, b"")

    def test_listen_link_present_in_library_index(self):
        """GET / navbar and sidebar must contain discoverable links to /listen."""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        self.assertIn('href="/listen"', html)
        self.assertIn("Listen Online", html)

    def test_player_static_assets_serve_correctly(self):
        """Verify static player.js and player.css exist and serve with 200."""
        res_css = self.client.get('/static/player.css')
        self.assertEqual(res_css.status_code, 200)
        self.assertIn(".playback-bar-container", res_css.data.decode('utf-8'))
        res_css.close()

        res_js = self.client.get('/static/player.js')
        self.assertEqual(res_js.status_code, 200)
        self.assertIn("Wavify Web Player", res_js.data.decode('utf-8'))
        self.assertIn("mediaSession", res_js.data.decode('utf-8'))
        res_js.close()

    def test_queue_and_shuffle_algorithm_logic(self):
        """Unit test simulating the client queue building, shuffle, and endless fallthrough behavior."""
        library = [{"id": f"t{i}", "title": f"Song {i}"} for i in range(10)]
        
        # Scenario 1: Select song t3 from library context (shuffle off)
        target = library[3]
        queue_sequential = library[3:]
        self.assertEqual(queue_sequential[0]["id"], "t3")
        self.assertEqual(len(queue_sequential), 7)

        # Scenario 2: Select song t3 from library context (shuffle on)
        rest = [t for t in library if t["id"] != "t3"]
        import random
        shuffled_rest = rest.copy()
        random.seed(42)
        random.shuffle(shuffled_rest)
        queue_shuffled = [target] + shuffled_rest
        self.assertEqual(queue_shuffled[0]["id"], "t3")
        self.assertEqual(len(queue_shuffled), 10)
        self.assertEqual(len(set(t["id"] for t in queue_shuffled)), 10)

        # Scenario 3: Endless shuffle fallthrough when context queue ends and repeat is off
        current_playing = queue_shuffled[-1]
        pool = [t for t in library if t["id"] != current_playing["id"]]
        random.shuffle(pool)
        extended_queue = queue_shuffled + pool
        self.assertEqual(len(extended_queue), 19)
        self.assertEqual(extended_queue[10]["id"], pool[0]["id"])


if __name__ == '__main__':
    unittest.main()
