"""Shared base for every app/services/ exception.

Each concrete type — ConversationError, HarnessError, DiffError,
VoiceServiceError, DefinitionNotFound, LLMError — carries its own meaning
at its own module boundary. This gives them a common ancestor, so a caller
that wants "some service call failed" has one type to catch rather than
enumerating the list by hand. app/web/runtime.py's run_llm and run_agent
both do exactly that.

briefing_service.py is deliberately the one module with no exception type
of its own — it re-raises ConversationError as-is rather than wrapping it,
since a briefing failure and a conversation failure are the same failure
(the same connection, the same call) with nothing distinct to add.
"""

from __future__ import annotations


class ServiceError(RuntimeError):
    """Base class for every app/services/ exception. Catch this for "some
    service call failed" in general, or a concrete subclass
    (ConversationError, DiffError, ...) to handle one specifically."""
