"""Tests for FocusJudgment.parse — no network, pure string parsing.

The Gemini SDK is the primary input source for the parser, but the parser
itself is the most fragile bit of the pipeline (LLM output drifts in form),
so it's tested in isolation here.

Run from pi/: python -m unittest test_vision_judge.py
"""
from __future__ import annotations

import unittest

from vision_judge import FocusJudgment


class FocusJudgmentParseTest(unittest.TestCase):

    # ------------------------------------------------- happy paths
    def test_clean_json(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 0.92, "observation": "typing on laptop"}'
        )
        self.assertIsNotNone(j)
        self.assertTrue(j.focused)
        self.assertAlmostEqual(j.confidence, 0.92)
        self.assertEqual(j.observation, "typing on laptop")

    def test_clean_json_focused_false(self):
        j = FocusJudgment.parse(
            '{"focused": false, "confidence": 0.8, "observation": "phone in hand"}'
        )
        self.assertIsNotNone(j)
        self.assertFalse(j.focused)

    def test_markdown_fenced_json(self):
        text = '```json\n{"focused": true, "confidence": 0.7, "observation": "ok"}\n```'
        j = FocusJudgment.parse(text)
        self.assertIsNotNone(j)
        self.assertTrue(j.focused)

    def test_markdown_fenced_no_lang(self):
        text = '```\n{"focused": false, "confidence": 0.5, "observation": "x"}\n```'
        j = FocusJudgment.parse(text)
        self.assertIsNotNone(j)
        self.assertFalse(j.focused)

    def test_prose_around_json(self):
        text = (
            'Sure! Here is the JSON you asked for:\n'
            '{"focused": true, "confidence": 0.6, "observation": "reading"}\n'
            'Let me know if you need anything else.'
        )
        j = FocusJudgment.parse(text)
        self.assertIsNotNone(j)
        self.assertTrue(j.focused)
        self.assertEqual(j.observation, "reading")

    def test_extra_keys_ignored(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 0.5, "observation": "ok",'
            ' "rationale": "I think because...", "tokens_used": 42}'
        )
        self.assertIsNotNone(j)
        self.assertTrue(j.focused)

    # ------------------------------------------------- value coercion
    def test_confidence_clamped_high(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 1.7, "observation": "ok"}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(j.confidence, 1.0)

    def test_confidence_clamped_low(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": -0.4, "observation": "ok"}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(j.confidence, 0.0)

    def test_confidence_integer_accepted(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 1, "observation": "ok"}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(j.confidence, 1.0)

    def test_confidence_missing_defaults_to_zero(self):
        j = FocusJudgment.parse(
            '{"focused": true, "observation": "ok"}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(j.confidence, 0.0)

    def test_observation_empty_replaced(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 0.5, "observation": "   "}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(j.observation, "no observation")

    def test_observation_missing_replaced(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 0.5}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(j.observation, "no observation")

    def test_observation_truncated_at_200(self):
        long = "a" * 500
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 0.5, "observation": "' + long + '"}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(len(j.observation), 200)

    def test_observation_non_string_coerced(self):
        j = FocusJudgment.parse(
            '{"focused": true, "confidence": 0.5, "observation": 42}'
        )
        self.assertIsNotNone(j)
        self.assertEqual(j.observation, "42")

    # ------------------------------------------------- rejection paths
    def test_none_input(self):
        self.assertIsNone(FocusJudgment.parse(None))  # type: ignore[arg-type]

    def test_empty_input(self):
        self.assertIsNone(FocusJudgment.parse(""))

    def test_whitespace_only_input(self):
        self.assertIsNone(FocusJudgment.parse("   \n\t  "))

    def test_no_json_at_all(self):
        self.assertIsNone(FocusJudgment.parse("Here is the JSON requested:"))

    def test_truncated_json(self):
        # gemini-2.5-flash truncates on max_output_tokens hit; this used to
        # bite us when thinking_budget wasn't set to 0.
        self.assertIsNone(FocusJudgment.parse(
            '{"focused": true, "confidence": 0.9, "observation": "look'
        ))

    def test_invalid_json_syntax(self):
        self.assertIsNone(FocusJudgment.parse(
            '{"focused": true, "confidence": 0.9, "observation": ,}'
        ))

    def test_list_with_no_dict(self):
        # Parser's brace-scan is forgiving (a list-wrapping-a-dict will still
        # extract the inner dict, which is desired). But a list of primitives
        # has no braces and should bail.
        self.assertIsNone(FocusJudgment.parse('[true, 0.9, "x"]'))

    def test_focused_missing(self):
        self.assertIsNone(FocusJudgment.parse(
            '{"confidence": 0.9, "observation": "x"}'
        ))

    def test_focused_not_bool_string(self):
        self.assertIsNone(FocusJudgment.parse(
            '{"focused": "true", "confidence": 0.9, "observation": "x"}'
        ))

    def test_focused_not_bool_int(self):
        # JSON 1 is not a bool — should be rejected, not coerced.
        self.assertIsNone(FocusJudgment.parse(
            '{"focused": 1, "confidence": 0.9, "observation": "x"}'
        ))

    def test_confidence_not_numeric(self):
        self.assertIsNone(FocusJudgment.parse(
            '{"focused": true, "confidence": "high", "observation": "x"}'
        ))


if __name__ == "__main__":
    unittest.main()
