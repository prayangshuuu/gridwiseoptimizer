"""Tests for Gemini → OpenRouter LLM provider fallback."""
from __future__ import annotations

import json
import logging
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gridwise.directive_interpreter import DirectiveInterpreter, clear_cache  # noqa: E402
from gridwise.llm import (  # noqa: E402
    LLMError,
    get_last_provider_used,
    interpret_notes,
    reset_interpreter_for_tests,
)
from gridwise.llm_contract import validate_interpretation_schema  # noqa: E402
from gridwise.llm_errors import LLMError as LLMErrorAlias  # noqa: E402
from gridwise.llm_providers import GeminiProvider, OpenRouterProvider  # noqa: E402
from gridwise.guardrails import validate_directives  # noqa: E402

assert LLMError is LLMErrorAlias

BATTERY = {"capacity_kwh": 100.0}


def _no_op_result(note_index: int = 0) -> dict:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "No actionable directive.",
    }


def _solar_result() -> dict:
    return {
        "note_index": 0,
        "applies": True,
        "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [12, 13, 14, 15], "factor": 0.7},
        "explanation": "Solar reduced.",
    }


class RecordingProvider:
    name = "recording"

    def __init__(self, results=None, error=None, label="recording"):
        self.label = label
        self.name = label
        self.results = results or [_no_op_result()]
        self.error = error
        self.calls = 0

    def interpret(self, operator_notes, timeout, battery=None):
        self.calls += 1
        if self.error:
            raise self.error
        return [dict(r, note_index=i) for i, r in enumerate(self.results)]


class TestDirectiveInterpreter(unittest.TestCase):
    def setUp(self):
        clear_cache()
        reset_interpreter_for_tests(DirectiveInterpreter.from_env())
        self._saved_log_levels = {}
        for name in ("gridwise.directive_interpreter", "gridwise.llm_providers"):
            log = logging.getLogger(name)
            self._saved_log_levels[name] = log.level
            log.setLevel(logging.CRITICAL)

    def tearDown(self):
        for name, level in self._saved_log_levels.items():
            logging.getLogger(name).setLevel(level)
        clear_cache()
        reset_interpreter_for_tests(DirectiveInterpreter.from_env())

    def test_gemini_success_openrouter_not_called(self):
        primary = RecordingProvider(results=[_solar_result()], label="gemini")
        fallback = RecordingProvider(label="openrouter")
        interp = DirectiveInterpreter(primary=primary, fallback=fallback)
        out = interp.interpret(["cut solar noon to 4pm"])
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(out.provider_used, "gemini")
        self.assertEqual(out.results[0]["directive_type"], "solar_reduction")

    def test_gemini_429_calls_openrouter(self):
        err = LLMError("LLM call failed: RateLimitError (429)")
        err.transient = True
        primary = RecordingProvider(error=err, label="gemini")
        fallback = RecordingProvider(results=[_solar_result()], label="openrouter")
        out = DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(out.provider_used, "openrouter")

    def test_gemini_timeout_calls_openrouter(self):
        err = LLMError("LLM call failed: APITimeoutError")
        primary = RecordingProvider(error=err, label="gemini")
        fallback = RecordingProvider(results=[_no_op_result()], label="openrouter")
        DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["thanks"])
        self.assertEqual(fallback.calls, 1)

    def test_gemini_auth_failure_calls_openrouter(self):
        err = LLMError("LLM call failed: AuthenticationError (401)")
        err.auth_failure = True
        primary = RecordingProvider(error=err, label="gemini")
        fallback = RecordingProvider(label="openrouter")
        DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        self.assertEqual(fallback.calls, 1)

    def test_gemini_5xx_calls_openrouter(self):
        err = LLMError("LLM call failed: InternalServerError (503)")
        primary = RecordingProvider(error=err, label="gemini")
        fallback = RecordingProvider(label="openrouter")
        DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        self.assertEqual(fallback.calls, 1)

    def test_gemini_malformed_json_calls_openrouter(self):
        err = LLMError("LLM response was not valid JSON.")
        primary = RecordingProvider(error=err, label="gemini")
        fallback = RecordingProvider(label="openrouter")
        DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        self.assertEqual(fallback.calls, 1)

    def test_gemini_schema_failure_calls_openrouter(self):
        err = LLMError("LLM schema validation failed: invalid hours.")
        primary = RecordingProvider(error=err, label="gemini")
        fallback = RecordingProvider(results=[_solar_result()], label="openrouter")
        out = DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(out.results[0]["directive_type"], "solar_reduction")

    def test_gemini_network_failure_calls_openrouter(self):
        err = LLMError("LLM call failed: APIConnectionError")
        primary = RecordingProvider(error=err, label="gemini")
        fallback = RecordingProvider(label="openrouter")
        DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        self.assertEqual(fallback.calls, 1)

    def test_openrouter_success_after_gemini_failure(self):
        primary = RecordingProvider(error=LLMError("fail"), label="gemini")
        fallback = RecordingProvider(results=[_solar_result()], label="openrouter")
        out = DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        self.assertEqual(out.provider_used, "openrouter")

    def test_both_providers_fail_raises(self):
        primary = RecordingProvider(error=LLMError("primary down"), label="gemini")
        fallback = RecordingProvider(error=LLMError("backup down"), label="openrouter")
        with self.assertRaises(LLMError):
            DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])

    def test_valid_no_op_from_gemini_no_fallback(self):
        primary = RecordingProvider(results=[_no_op_result()], label="gemini")
        fallback = RecordingProvider(label="openrouter")
        out = DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["thanks"])
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(out.results[0]["directive_type"], "no_op")

    def test_valid_no_op_from_openrouter_accepted(self):
        primary = RecordingProvider(error=LLMError("fail"), label="gemini")
        fallback = RecordingProvider(results=[_no_op_result()], label="openrouter")
        out = DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["thanks"])
        self.assertEqual(out.results[0]["directive_type"], "no_op")

    def test_equivalent_directives_normalized_identically(self):
        gemini_payload = [_solar_result()]
        openrouter_payload = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [12, 13, 14, 15], "factor": 0.7},
                "explanation": "Different wording.",
            }
        ]
        g = validate_interpretation_schema(1, gemini_payload)
        o = validate_interpretation_schema(1, openrouter_payload)
        self.assertEqual(
            {k: v for k, v in g[0].items() if k != "explanation"},
            {k: v for k, v in o[0].items() if k != "explanation"},
        )

    def test_note_ordering_preserved(self):
        raw = [
            _no_op_result(0),
            {
                "note_index": 1,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [1, 2]},
                "explanation": "No charge.",
            },
        ]
        raw[0]["note_index"] = 0
        ordered = validate_interpretation_schema(2, raw)
        self.assertEqual([e["note_index"] for e in ordered], [0, 1])

    def test_invalid_directives_coerced_before_optimizer(self):
        bad = [{"note_index": 0, "applies": True, "directive_type": "bogus", "structured_adjustment": {}}]
        with self.assertRaises(LLMError):
            validate_interpretation_schema(1, bad)
        coerced = validate_directives(
            [{"note_index": 0, "applies": True, "directive_type": "bogus",
              "structured_adjustment": {"hours": [1]}}],
            1,
            BATTERY,
        )
        self.assertEqual(coerced[0].directive_type, "no_op")

    def test_api_keys_not_logged(self):
        secret = "sk-or-v1-SECRETKEY1234567890"
        primary = RecordingProvider(error=LLMError(f"failed with {secret}"), label="gemini")
        fallback = RecordingProvider(error=LLMError("backup down"), label="openrouter")
        with self.assertLogs("gridwise.directive_interpreter", level="WARNING") as captured:
            logging.getLogger("gridwise.directive_interpreter").setLevel(logging.WARNING)
            with self.assertRaises(LLMError):
                DirectiveInterpreter(primary=primary, fallback=fallback).interpret(["x"])
        blob = " ".join(r.getMessage() for r in captured.records)
        self.assertNotIn(secret, blob)

    def test_interpret_notes_uses_injected_interpreter(self):
        primary = RecordingProvider(results=[_no_op_result()], label="gemini")
        fallback = RecordingProvider(label="openrouter")
        reset_interpreter_for_tests(DirectiveInterpreter(primary=primary, fallback=fallback))
        interpret_notes(["hello"])
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(get_last_provider_used(), "gemini")


class TestOpenAICompatibleProvider(unittest.TestCase):
    def _mock_client(self, content: str):
        response = mock.Mock()
        response.choices = [mock.Mock(message=mock.Mock(content=content))]

        client = mock.Mock()
        client.chat.completions.create.return_value = response
        return client

    def test_provider_parses_json_and_validates(self):
        payload = json.dumps({"results": [_solar_result()]})
        client = self._mock_client(payload)

        def factory(key, base_url, timeout):
            return client

        provider = GeminiProvider(
            name="gemini",
            model="gemini-test",
            api_keys=["AIzaSy_test"],
            base_url="https://example.com",
            client_factory=factory,
        )
        out = provider.interpret(["cut solar"], timeout=5.0)
        self.assertEqual(out[0]["directive_type"], "solar_reduction")
        client.chat.completions.create.assert_called_once()

    def test_schema_failure_raises_llm_error(self):
        payload = json.dumps({
            "results": [{
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [1], "factor": 2.5},
                "explanation": "bad factor",
            }]
        })
        provider = GeminiProvider(
            name="gemini",
            model="m",
            api_keys=["AIzaSy_x"],
            base_url="https://example.com",
            client_factory=lambda *a: self._mock_client(payload),
        )
        with self.assertRaises(LLMError):
            provider.interpret(["x"], timeout=1.0)


if __name__ == "__main__":
    unittest.main()
