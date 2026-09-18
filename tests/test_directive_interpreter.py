"""Tests for the OpenRouter directive interpreter."""
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
from gridwise.llm_contract import (  # noqa: E402
    align_minimum_reserves_from_notes,
    validate_interpretation_schema,
)
from gridwise.llm_errors import LLMError as LLMErrorAlias  # noqa: E402
from gridwise.llm_providers import OpenRouterProvider  # noqa: E402
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
    name = "openrouter"

    def __init__(self, results=None, error=None):
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

    def test_provider_success(self):
        provider = RecordingProvider(results=[_solar_result()])
        interp = DirectiveInterpreter(provider=provider)
        out = interp.interpret(["cut solar noon to 4pm"])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(out.provider_used, "openrouter")
        self.assertEqual(out.results[0]["directive_type"], "solar_reduction")

    def test_provider_failure_raises(self):
        provider = RecordingProvider(error=LLMError("LLM call failed: APITimeoutError"))
        with self.assertRaises(LLMError):
            DirectiveInterpreter(provider=provider).interpret(["x"])

    def test_cache_hit_skips_second_call(self):
        provider = RecordingProvider(results=[_no_op_result()])
        interp = DirectiveInterpreter(provider=provider)
        interp.interpret(["thanks"])
        interp.interpret(["thanks"])
        self.assertEqual(provider.calls, 1)

    def test_equivalent_directives_normalized_identically(self):
        payload = [_solar_result()]
        a = validate_interpretation_schema(1, payload)
        b = validate_interpretation_schema(1, [{
            **payload[0],
            "explanation": "Different wording.",
        }])
        self.assertEqual(
            {k: v for k, v in a[0].items() if k != "explanation"},
            {k: v for k, v in b[0].items() if k != "explanation"},
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

    def test_align_minimum_reserves_from_percentage_note(self):
        note = (
            "Keep at least 50% of the battery capacity stored from 6 PM until 9 PM."
        )
        battery = {"capacity_kwh": 200}
        raw = [{
            "note_index": 0,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 0.0},
            "explanation": "x",
        }]
        fixed = align_minimum_reserves_from_notes(raw, [note], battery)
        self.assertEqual(fixed[0]["structured_adjustment"]["minimum_energy_kwh"], 100.0)

    def test_interpret_notes_uses_injected_interpreter(self):
        provider = RecordingProvider(results=[_no_op_result()])
        reset_interpreter_for_tests(DirectiveInterpreter(provider=provider))
        interpret_notes(["hello"])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(get_last_provider_used(), "openrouter")


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

        provider = OpenRouterProvider(
            name="openrouter",
            model="deepseek-test",
            api_keys=["sk-or-test"],
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
        provider = OpenRouterProvider(
            name="openrouter",
            model="m",
            api_keys=["sk-or-x"],
            base_url="https://example.com",
            client_factory=lambda *a: self._mock_client(payload),
        )
        with self.assertRaises(LLMError):
            provider.interpret(["x"], timeout=1.0)


if __name__ == "__main__":
    unittest.main()
