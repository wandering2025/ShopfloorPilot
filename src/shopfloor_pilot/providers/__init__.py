"""Backend provider boundary (models, validation, fake, and costs).

The live adapter, configuration loader, bounded wrapper, and readiness command
are imported from their explicit modules so importing the package stays free of
credentials, network access, and ledger side effects.
"""

from .base import Provider
from .clock import Clock, FakeClock, SystemClock
from .costs import TokenRates, micros_to_string, to_micros
from .errors import (
    BudgetExceededError,
    ConfigurationError,
    LedgerError,
    ProviderError,
    ProviderErrorCode,
    ProviderResponseValidationError,
    ToolCallValidationError,
    WorkflowStateError,
)
from .fake import (
    FAKE_PROVIDER_LABEL,
    FakeFailure,
    FakeReply,
    ScriptedFakeProvider,
    scripted_text,
    scripted_tool_call,
)
from .models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderTurn,
    TokenUsage,
    ToolChoice,
    ToolSchema,
    ValidatedToolCall,
)
from .redaction import REDACTED, SecretRedactor, redact_text
from .retry import RetryPolicy
from .validation import (
    validate_request,
    validate_response_turn,
    validate_tool_result_content,
)

__all__ = [
    "FAKE_PROVIDER_LABEL",
    "REDACTED",
    "BudgetExceededError",
    "Clock",
    "ConfigurationError",
    "FakeClock",
    "FakeFailure",
    "FakeReply",
    "LedgerError",
    "MessageRole",
    "Provider",
    "ProviderError",
    "ProviderErrorCode",
    "ProviderMessage",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderResponseValidationError",
    "ProviderToolCall",
    "ProviderTurn",
    "RetryPolicy",
    "ScriptedFakeProvider",
    "SecretRedactor",
    "SystemClock",
    "TokenRates",
    "TokenUsage",
    "ToolCallValidationError",
    "ToolChoice",
    "ToolSchema",
    "ValidatedToolCall",
    "WorkflowStateError",
    "micros_to_string",
    "redact_text",
    "scripted_text",
    "scripted_tool_call",
    "to_micros",
    "validate_request",
    "validate_response_turn",
    "validate_tool_result_content",
]
