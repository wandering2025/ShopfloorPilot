"""Template-driven scripted provider for labeled offline supervisor tests.

Each scripted turn names tool calls whose argument templates are filled from
the digest the supervisor actually sent (run ID, pinned state version,
candidate ID, evidence IDs). This proves orchestration mechanics only; it is
not evidence of DeepSeek interpretation quality or live readiness.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from shopfloor_pilot.providers.errors import ProviderError
from shopfloor_pilot.providers.fake import FAKE_PROVIDER_LABEL
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
)

DETERMINISTIC_BASELINE_LABEL = (
    "DETERMINISTIC RULE BASELINE — no LLM. Fixed observable-input policy routed "
    "through the same supervisor tools, solver, validator and approval service."
)


@dataclass(frozen=True, slots=True)
class ScriptedCall:
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    raw_arguments: str | None = None  # deliberately malformed JSON, if set


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    calls: tuple[ScriptedCall, ...] = ()
    text: str | None = None
    failure: ProviderError | None = None
    before: Callable[[], None] | None = None  # deterministic test barrier
    reasoning: str | None = None  # private thinking-mode text; must never be logged


class TemplateScriptedProvider:
    """Deterministic Provider transport; replays turns, never touches the network."""

    provider_name = "scripted-fake"
    label = FAKE_PROVIDER_LABEL

    def __init__(
        self, turns: Sequence[ScriptedTurn], *, model: str = "scripted-fake-model"
    ) -> None:
        self._turns = list(turns)
        self._model = model
        self.requests: list[ProviderRequest] = []
        self.calls = 0

    @property
    def remaining_turns(self) -> int:
        return len(self._turns)

    def complete(self, request: ProviderRequest, *, timeout_seconds: float) -> ProviderResponse:
        del timeout_seconds
        self.requests.append(request)
        self.calls += 1
        if not self._turns:
            raise AssertionError("TemplateScriptedProvider script exhausted")
        turn = self._turns.pop(0)
        if turn.before is not None:
            turn.before()
        if turn.failure is not None:
            raise turn.failure
        digest = _digest(request)
        calls = tuple(
            ProviderToolCall(
                id=f"call-{self.calls}-{index}",
                name=item.tool,
                arguments_json=(
                    item.raw_arguments
                    if item.raw_arguments is not None
                    else json.dumps(_resolve(item.arguments, digest), sort_keys=True)
                ),
            )
            for index, item in enumerate(turn.calls, start=1)
        )
        message = ProviderMessage(
            role=MessageRole.ASSISTANT,
            content=turn.text,
            tool_calls=calls,
            reasoning=turn.reasoning,
        )
        prompt_tokens = max(len(request.estimated_payload_text().encode("utf-8")) // 4, 1)
        completion_tokens = 16 + sum(len(call.arguments_json) // 4 for call in calls)
        return ProviderResponse(
            message=message,
            model=self._model,
            provider=self.provider_name,
            finish_reason="tool_calls" if calls else "stop",
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )


class DeterministicPolicyProvider(TemplateScriptedProvider):
    """Fixed no-LLM response policy for fair S13 comparisons.

    The policy's tool program is assembled from frozen structured order input or
    observable alarm text before a run. Template resolution may bind only values
    emitted by the supervisor digest (run/version/candidate/evidence identities).
    It deliberately uses the same provider-validation and supervisor execution path
    as the scripted and live arms while retaining a distinct identity and label.
    """

    provider_name = "deterministic-baseline"
    label = DETERMINISTIC_BASELINE_LABEL

    def __init__(self, turns: Sequence[ScriptedTurn]) -> None:
        super().__init__(turns, model="deterministic-policy-v1")


def _digest(request: ProviderRequest) -> dict[str, Any]:
    for message in request.messages:
        if message.role is MessageRole.USER and message.content:
            try:
                value = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "workflow" in value:
                return value
    return {}


def _lookup(token: str, digest: Mapping[str, Any]) -> Any:
    workflow = digest.get("workflow", {})
    if token == "$run_id":
        return workflow.get("run_id")
    if token == "$state_version":
        return (digest.get("observed") or {}).get("state_version")
    if token == "$candidate_id":
        return (digest.get("candidate") or {}).get("candidate_id")
    if token == "$focus_machine_ids":
        return list(digest.get("focus_machine_ids", []))
    if token.startswith("$evidence:"):
        kind = token.removeprefix("$evidence:")
        return [item["id"] for item in digest.get("evidence", []) if item["kind"] == kind]
    if token.startswith("$evidence_first:"):
        kind = token.removeprefix("$evidence_first:")
        matches = [item["id"] for item in digest.get("evidence", []) if item["kind"] == kind]
        return matches[0] if matches else None
    return token


def _resolve(value: Any, digest: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return _lookup(value, digest)
    if isinstance(value, Mapping):
        return {key: _resolve(item, digest) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        resolved: list[Any] = []
        for item in value:
            result = _resolve(item, digest)
            expands = isinstance(item, str) and (
                item.startswith("$evidence:") or item == "$focus_machine_ids"
            )
            if expands and isinstance(result, list):
                resolved.extend(result)
            else:
                resolved.append(result)
        return resolved
    return value
