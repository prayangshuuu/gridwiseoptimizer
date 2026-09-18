"""LLM layer exceptions."""


class LLMError(Exception):
    """Raised when an LLM provider or schema validation fails.

    ``transient`` and ``auth_failure`` are kept for backward compatibility.
    """

    transient: bool = False
    auth_failure: bool = False
