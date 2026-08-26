from __future__ import annotations


class LLMError(RuntimeError):
    """Base error for an LLM invocation."""


class LLMProviderError(LLMError):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class LLMResponseError(LLMError):
    """The provider returned a response that did not satisfy the decision schema."""


class LLMBudgetExceeded(LLMError):
    """An invocation was refused by a configured spending limit."""
