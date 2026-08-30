import unittest
from unittest.mock import patch, MagicMock
from scraper.lyrics_resolver import (
    resolve_lyrics_with_details,
    _lrclib_exact,
    _lrclib_search,
    _lrclib_title_search,
    _jiosaavn_lyrics,
    _lyrics_ovh
)
from scraper.metadata_enricher import enrich_track_metadata


class LyricsResolverChainTestCase(unittest.TestCase):

    @patch("scraper.lyrics_resolver._lyrics_ovh")
    @patch("scraper.lyrics_resolver._lrclib_title_search")
    @patch("scraper.lyrics_resolver._lrclib_search")
    @patch("scraper.lyrics_resolver._jiosaavn_lyrics")
    @patch("scraper.lyrics_resolver._lrclib_exact")
    def test_trace_a_clean_exact_lrclib_match(
        self, mock_exact, mock_saavn, mock_search, mock_title_search, mock_ovh
    ):
        mock_exact.return_value = {
            "lyrics": "Exact plain lyrics content here " * 5,
            "syncedLyrics": "[00:10.00]Exact plain lyrics content here",
            "provider_track": "Shape of You",
            "provider_artist": "Ed Sheeran",
        }

        result = resolve_lyrics_with_details(
            "Shape of You", "Ed Sheeran", album="Divide", duration_seconds=233
        )

        self.assertIsNotNone(result.get("lyrics"))
        self.assertIsNotNone(result.get("syncedLyrics"))
        self.assertEqual(result.get("source"), "lrclib_exact")
        self.assertEqual(len(result.get("attempts", [])), 1)
        self.assertEqual(result["attempts"][0]["source"], "lrclib_exact")
        self.assertEqual(result["attempts"][0]["status"], "hit")

        mock_saavn.assert_not_called()
        mock_search.assert_not_called()
        mock_title_search.assert_not_called()
        mock_ovh.assert_not_called()

    @patch("scraper.lyrics_resolver._lyrics_ovh")
    @patch("scraper.lyrics_resolver._lrclib_title_search")
    @patch("scraper.lyrics_resolver._lrclib_search")
    @patch("scraper.lyrics_resolver._jiosaavn_lyrics")
    @patch("scraper.lyrics_resolver._lrclib_exact")
    def test_trace_b_jiosaavn_fallback_success(
        self, mock_exact, mock_saavn, mock_search, mock_title_search, mock_ovh
    ):
        mock_exact.return_value = None
        mock_saavn.return_value = {
            "lyrics": "JioSaavn regional song lyrics here " * 5,
            "syncedLyrics": None,
            "match_score": 1.6,
        }

        result = resolve_lyrics_with_details(
            "Malare", "Vijay Yesudas", album="Premam", duration_seconds=315
        )

        self.assertIsNotNone(result.get("lyrics"))
        self.assertIsNone(result.get("syncedLyrics"))
        self.assertEqual(result.get("source"), "jiosaavn")
        self.assertEqual(len(result.get("attempts", [])), 2)
        self.assertEqual(result["attempts"][0]["status"], "miss")
        self.assertEqual(result["attempts"][1]["status"], "hit")

        mock_exact.assert_called_once()
        mock_saavn.assert_called_once()
        mock_search.assert_not_called()
        mock_title_search.assert_not_called()
        mock_ovh.assert_not_called()

    @patch("scraper.lyrics_resolver.requests.get")
    def test_trace_c_different_artist_same_title_rejected_by_artist_floor(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "trackName": "Home",
                "artistName": "Edward Sharpe and the Magnetic Zeros",
                "plainLyrics": "Home, let me come home, home is wherever I'm with you " * 3,
                "syncedLyrics": None,
            }
        ]
        mock_get.return_value = mock_response

        result = _lrclib_title_search("Home", "Michael Buble", min_score=0.86, min_artist_ratio=0.3)
        self.assertIsNone(result)

    @patch("scraper.lyrics_resolver.requests.get")
    def test_trace_d_loose_artist_match_accepted(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "trackName": "Beautiful People",
                "artistName": "Ed Sheeran",
                "plainLyrics": "We are, we are, we are not the beautiful people " * 4,
                "syncedLyrics": "[00:15.00]We are, we are",
            }
        ]
        mock_get.return_value = mock_response

        result = _lrclib_title_search(
            "Beautiful People", "Ed Sheeran feat. Khalid", min_score=0.86, min_artist_ratio=0.3
        )

        self.assertIsNotNone(result)
        self.assertIn("beautiful people", result.get("lyrics", "").lower())

    @patch("scraper.metadata_enricher.resolve_lyrics_with_details")
    @patch("scraper.metadata_enricher.find_itunes_track_metadata")
    def test_enrich_track_metadata_integration(self, mock_itunes, mock_lyrics_resolver):
        mock_itunes.return_value = {
            "album_art": "https://example.com/cover.jpg",
            "genre": "Pop",
            "album": "Divide",
            "duration_ms": 233000,
        }
        mock_lyrics_resolver.return_value = {
            "lyrics": "Lyrics content here " * 6,
            "syncedLyrics": "[00:05.00]Lyrics content here",
            "source": "lrclib_exact",
            "metadata": {},
            "attempts": [{"source": "lrclib_exact", "status": "hit"}],
        }

        meta = enrich_track_metadata("Shape of You", "Ed Sheeran")

        self.assertEqual(meta["lyrics"], "Lyrics content here " * 6)
        self.assertEqual(meta["syncedLyrics"], "[00:05.00]Lyrics content here")
        self.assertEqual(meta["lyricsStatus"], "ok")
        self.assertEqual(meta["album"], "Divide")
        self.assertEqual(meta["genre"], "Pop")


if __name__ == "__main__":
    unittest.main()
