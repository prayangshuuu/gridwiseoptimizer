"""Mandatory LLM interpretation layer (OpenRouter only).

``interpret_notes`` turns free-text operator notes into structured directives.
Configuration is read exclusively from ``gridwise.llm_env`` (``OPENROUTER_API_KEY``,
optional ``LLM_MODEL`` / ``LLM_BASE_URL`` / timeout vars). Validated JSON is
returned for guardrails; never trust it for scheduling directly.
"""
from __future__ import annotations

from .directive_interpreter import (
    DirectiveInterpreter,
    InterpretationResult,
    clear_cache,
    get_last_provider_used,
)
from .llm_env import load_dotenv
from .llm_contract import (
    DIRECTIVE_TYPES,
    FEWSHOT_ASSISTANT,
    FEWSHOT_USER,
    SYSTEM_PROMPT,
    build_chat_messages,
    validate_interpretation_schema,
)
from .llm_errors import LLMError

_default_interpreter: DirectiveInterpreter | None = None


def _interpreter() -> DirectiveInterpreter:
    global _default_interpreter
    if _default_interpreter is None:
        load_dotenv()
        _default_interpreter = DirectiveInterpreter.from_env()
    return _default_interpreter


def reset_interpreter_for_tests(interpreter: DirectiveInterpreter | None = None) -> None:
    """Replace the process-wide interpreter (tests only)."""
    global _default_interpreter
    _default_interpreter = interpreter or DirectiveInterpreter.from_env()


def interpret_notes(operator_notes, battery=None):
    """Interpret operator notes via OpenRouter (DeepSeek v4.1 flash)."""
    result = _interpreter().interpret(list(operator_notes), battery=battery)
    return result.results


__all__ = [
    "DIRECTIVE_TYPES",
    "FEWSHOT_ASSISTANT",
    "FEWSHOT_USER",
    "SYSTEM_PROMPT",
    "InterpretationResult",
    "LLMError",
    "build_chat_messages",
    "clear_cache",
    "get_last_provider_used",
    "interpret_notes",
    "load_dotenv",
    "reset_interpreter_for_tests",
    "validate_interpretation_schema",
]
