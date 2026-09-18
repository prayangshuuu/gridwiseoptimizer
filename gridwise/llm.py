"""Mandatory LLM interpretation layer (Gemini primary, OpenRouter fallback).

``interpret_notes`` turns free-text operator notes into structured directives.
Provider selection, models, and API keys come from environment variables only.
Validated JSON is returned for guardrails; never trust it for scheduling directly.
"""
from __future__ import annotations

from .directive_interpreter import (
    DirectiveInterpreter,
    InterpretationResult,
    clear_cache,
    get_last_provider_used,
)
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
        _default_interpreter = DirectiveInterpreter.from_env()
    return _default_interpreter


def reset_interpreter_for_tests(interpreter: DirectiveInterpreter | None = None) -> None:
    """Replace the process-wide interpreter (tests only)."""
    global _default_interpreter
    _default_interpreter = interpreter or DirectiveInterpreter.from_env()


def interpret_notes(operator_notes):
    """Interpret operator notes via Gemini, then OpenRouter on provider failure."""
    result = _interpreter().interpret(list(operator_notes))
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
    "reset_interpreter_for_tests",
    "validate_interpretation_schema",
]
