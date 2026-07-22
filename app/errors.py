"""
Domain-level exceptions raised by the service layer (llm, embeddings, …).

Each carries a user-facing Arabic `message` and optional structured `details`.
They subclass RuntimeError so existing `except RuntimeError` sites and tests
keep working unchanged. Nothing here imports FastAPI — services stay
transport-agnostic; the API layer (app.api.errors) maps these to HTTP
responses and the unified error envelope.
"""


class ChatbotError(RuntimeError):
    """Base for expected, message-carrying application errors."""

    code = "INTERNAL_ERROR"

    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.message = message
        self.details = details


class UpstreamServiceError(ChatbotError):
    """An external dependency (chat LLM / embeddings API) failed or returned an
    unusable response. Maps to HTTP 502 Bad Gateway."""

    code = "UPSTREAM_ERROR"


class ConfigurationError(ChatbotError):
    """The server is missing required configuration (an API key / URL). Maps to
    HTTP 503 Service Unavailable — the service simply cannot run as set up."""

    code = "CONFIGURATION_ERROR"


class ServiceNotReadyError(ChatbotError):
    """The local knowledge indexes are still building or failed validation.

    This is deliberately distinct from an empty retrieval result: callers may
    retry it, and the LLM must never turn it into a factual "not found" claim.
    """

    code = "SERVICE_NOT_READY"
