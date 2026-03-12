"""Tests for cellar.backend.steam fuzzy search."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cellar.backend.steam import fuzzy_search_games


class TestFuzzySearchGames:
    """Tests for fuzzy_search_games retry and re-ranking logic."""

    def test_exact_match_no_retry(self):
        """When the full query returns results, no retry needed."""
        with patch("cellar.backend.steam.search_games") as mock:
            mock.return_value = [{"appid": 1, "name": "Wingspan"}]
            results = fuzzy_search_games("Wingspan")
            mock.assert_called_once_with("Wingspan", 10)
            assert results == [{"appid": 1, "name": "Wingspan"}]

    def test_retry_drops_trailing_words(self):
        """When the full query fails, drops trailing words and retries."""
        call_count = 0

        def fake_search(query, limit=10):
            nonlocal call_count
            call_count += 1
            if query == "Wingspan 295":
                return []
            if query == "Wingspan":
                return [{"appid": 1054430, "name": "Wingspan"}]
            return []

        with patch("cellar.backend.steam.search_games", side_effect=fake_search):
            results = fuzzy_search_games("Wingspan 295")
            assert call_count == 2
            assert results[0]["name"] == "Wingspan"

    def test_reranks_by_similarity(self):
        """Results are re-ranked by fuzzy similarity to the original query."""
        with patch("cellar.backend.steam.search_games") as mock:
            mock.return_value = [
                {"appid": 3, "name": "Wing Commander"},
                {"appid": 1, "name": "Wingspan"},
                {"appid": 2, "name": "Wingspan: Oceania Expansion"},
            ]
            results = fuzzy_search_games("Wingspan")
            # "Wingspan" should rank highest
            assert results[0]["name"] == "Wingspan"

    def test_no_results_returns_empty(self):
        """Returns empty list when all retry attempts find nothing."""
        with patch("cellar.backend.steam.search_games", return_value=[]):
            results = fuzzy_search_games("xyznonexistent123")
            assert results == []

    def test_no_internal_score_key(self):
        """Internal _score key is stripped from returned results."""
        with patch("cellar.backend.steam.search_games") as mock:
            mock.return_value = [{"appid": 1, "name": "Wingspan"}]
            results = fuzzy_search_games("Wingspan")
            assert "_score" not in results[0]

    def test_single_word_no_retry(self):
        """Single-word query doesn't retry with empty string."""
        with patch("cellar.backend.steam.search_games") as mock:
            mock.return_value = []
            fuzzy_search_games("Wingspan")
            mock.assert_called_once_with("Wingspan", 10)
