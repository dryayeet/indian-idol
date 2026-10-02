import asyncio
import math
import unittest
from unittest.mock import Mock, patch

import agent
import psych_mcp


class BigFiveTests(unittest.TestCase):
    EXPECTED_TRAITS = {
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
    }

    def setUp(self):
        self.raw_scores = {}
        expected = {}
        for index, (trait, (high, low)) in enumerate(psych_mcp.BIG_FIVE_PAIRS.items(), 1):
            high_score = 0.5 + index * 0.05
            low_score = 0.5 - index * 0.03
            self.raw_scores[high] = high_score
            self.raw_scores[low] = low_score
            expected[trait] = round(high_score / (high_score + low_score), 3)
        self.expected = expected

    @patch.object(psych_mcp, "_sampled_chunks", return_value=(["sample text"], 11, False))
    @patch.object(psych_mcp, "_pooled")
    def test_get_big_five_preserves_contract(self, pooled, _sampled):
        pooled.return_value = (self.expected, 2)

        result = psych_mcp.get_big_five("sample text")

        self.assertEqual(set(result["traits"]), self.EXPECTED_TRAITS)
        self.assertEqual(result["traits"], self.expected)
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(result["model"], psych_mcp.BIG_FIVE_MODEL)
        self.assertEqual(result["characters_scored"], 11)
        self.assertEqual(result["characters_total"], 11)
        self.assertFalse(result["sampled"])
        pooled.assert_called_once_with(
            psych_mcp.BIG_FIVE_MODEL,
            "sample text",
            candidate_labels=psych_mcp.BIG_FIVE_LABELS,
            hypothesis_template=psych_mcp.BIG_FIVE_HYPOTHESIS,
            multi_label=True,
            transform=psych_mcp._trait_scores,
            pieces=["sample text"],
            weights=[11],
        )

    def test_trait_scores_require_every_pair(self):
        self.raw_scores.pop(next(iter(self.raw_scores)))

        with self.assertRaisesRegex(RuntimeError, "missing Big Five labels"):
            psych_mcp._trait_scores(self.raw_scores)

    def test_trait_scores_reject_invalid_values(self):
        first = next(iter(self.raw_scores))
        self.raw_scores[first] = math.nan

        with self.assertRaisesRegex(RuntimeError, "invalid Big Five score"):
            psych_mcp._trait_scores(self.raw_scores)

    @patch.object(psych_mcp, "_token", return_value="test-token")
    @patch.object(psych_mcp._http, "post")
    def test_classify_sends_zero_shot_parameters(self, post, _token):
        response = Mock(status_code=200)
        response.json.return_value = [{"label": "high openness", "score": 0.75}]
        post.return_value = response

        result = psych_mcp._classify(
            psych_mcp.BIG_FIVE_MODEL,
            "sample text",
            candidate_labels=["high openness", "low openness"],
            multi_label=True,
        )

        self.assertEqual(result, {"high openness": 0.75})
        post.assert_called_once_with(
            psych_mcp.HF + psych_mcp.BIG_FIVE_MODEL,
            headers={"Authorization": "Bearer test-token"},
            json={
                "inputs": "sample text",
                "parameters": {
                    "candidate_labels": ["high openness", "low openness"],
                    "multi_label": True,
                },
            },
        )

    @patch.object(psych_mcp, "_classify")
    def test_pooled_averages_normalized_chunk_traits(self, classify):
        first = {}
        second = {}
        for high, low in psych_mcp.BIG_FIVE_PAIRS.values():
            first.update({high: 0.9, low: 0.1})
            second.update({high: 0.1, low: 0.1})
        classify.side_effect = [first, second]

        scores, chunks = psych_mcp._pooled(
            psych_mcp.BIG_FIVE_MODEL,
            "aaaaa\nbbbbb",
            chunk_chars=5,
            max_chunks=2,
            transform=psych_mcp._trait_scores,
        )

        self.assertEqual(chunks, 2)
        self.assertTrue(all(value == 0.7 for value in scores.values()))

    @patch.object(psych_mcp, "_classify")
    def test_pooled_weights_samples_by_coverage(self, classify):
        high = {}
        neutral = {}
        for positive, negative in psych_mcp.BIG_FIVE_PAIRS.values():
            high.update({positive: 0.9, negative: 0.1})
            neutral.update({positive: 0.1, negative: 0.1})
        classify.side_effect = [high, neutral]

        scores, chunks = psych_mcp._pooled(
            psych_mcp.BIG_FIVE_MODEL,
            "unused",
            pieces=["a", "bbbb"],
            weights=[1, 4],
            transform=psych_mcp._trait_scores,
        )

        self.assertEqual(chunks, 2)
        self.assertTrue(all(value == 0.58 for value in scores.values()))

    def test_chunks_refuse_silent_truncation(self):
        with self.assertRaisesRegex(ValueError, "text is too long"):
            psych_mcp._chunks("abcdefghijk", chunk_chars=5, max_chunks=2)

    def test_long_big_five_input_is_sampled_evenly(self):
        text = "x" * 5000

        pieces, total, sampled = psych_mcp._sampled_chunks(text)

        self.assertEqual(len(pieces), psych_mcp.BIG_FIVE_SAMPLES)
        self.assertTrue(
            all(0 < len(piece.encode("utf-8")) <= psych_mcp.BIG_FIVE_BYTES for piece in pieces)
        )
        self.assertEqual(total, len(text))
        self.assertTrue(sampled)

    def test_dense_unicode_windows_stay_within_byte_budget(self):
        text = "😀" * 1000

        pieces = psych_mcp._byte_chunks(text)

        self.assertEqual("".join(pieces), text)
        self.assertTrue(
            all(len(piece.encode("utf-8")) <= psych_mcp.BIG_FIVE_BYTES for piece in pieces)
        )

    def test_short_big_five_input_is_fully_scored(self):
        pieces, total, sampled = psych_mcp._sampled_chunks("short text")

        self.assertEqual(pieces, ["short text"])
        self.assertEqual(total, 10)
        self.assertFalse(sampled)

    @patch.object(psych_mcp, "_token", return_value="test-token")
    @patch.object(psych_mcp._http, "post")
    def test_classify_rejects_unexpected_object(self, post, _token):
        response = Mock(status_code=200)
        response.json.return_value = {"unexpected": "shape"}
        post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "unexpected response"):
            psych_mcp._classify(psych_mcp.BIG_FIVE_MODEL, "sample text")

    @patch.object(psych_mcp, "_token", return_value="test-token")
    @patch.object(psych_mcp._http, "post")
    def test_classify_rejects_malformed_rows(self, post, _token):
        response = Mock(status_code=200)
        response.json.return_value = [{"label": "missing score"}]
        post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "unexpected response"):
            psych_mcp._classify(psych_mcp.BIG_FIVE_MODEL, "sample text")

    @patch.object(psych_mcp, "_token", return_value="test-token")
    @patch.object(psych_mcp._http, "post")
    def test_classify_rejects_mismatched_arrays(self, post, _token):
        response = Mock(status_code=200)
        response.json.return_value = {"labels": ["one", "two"], "scores": [0.5]}
        post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "unexpected response"):
            psych_mcp._classify(psych_mcp.BIG_FIVE_MODEL, "sample text")

    def test_big_five_is_registered_with_mcp(self):
        tools = asyncio.run(psych_mcp.app.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("get_big_five", names)

    def test_agent_registers_psych_server(self):
        self.assertIn("psych", agent.SERVERS)
        self.assertTrue(agent.SERVERS["psych"]["args"][0].endswith("psych_mcp.py"))


if __name__ == "__main__":
    unittest.main()
