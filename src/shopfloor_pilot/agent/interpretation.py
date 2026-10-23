"""Deterministic validation of model-drafted order interpretations.

The supervisor model never supplies authoritative numbers. It drafts *verbatim
spans* copied from the request text; this module checks that each span really
occurs in the request, parses it locally, rescans the whole request for
conflicting or unsupported language, and then resolves product, route, and
quantity through the S01 alias boundary. Relative due phrases resolve against
the decision boundary, and ``today`` resolves only to the documented scenario
shift boundary. The host date is never consulted.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.aliases import PRODUCT_ALIASES, resolve_order_request
from shopfloor_pilot.domain.base import (
    Identifier,
    PositiveQuantity,
    PriorityWeight,
    Seconds,
    Sha256,
    StrictModel,
)
from shopfloor_pilot.domain.fixtures import ScenarioClock
from shopfloor_pilot.domain.models import (
    Capability,
    DomainCatalog,
    FailureCode,
    Order,
    OrderDelta,
    OrderDeltaKind,
    OrderStatus,
    ResultStatus,
)

MAX_REQUEST_CHARACTERS = 500
MAX_SPAN_CHARACTERS = 80

# Documented S10 priority policy. The weights are explicit configuration, not
# model output: ``urgent`` matches the S01 urgent-insertion fixture weight.
PRIORITY_WEIGHTS: dict[str, int] = {"normal": 2, "urgent": 10}

_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_ENGLISH_NUMBERS = "|".join(word for word in _NUMBER_WORDS if word.isascii())
_CHINESE_NUMBERS = "".join(word for word in _NUMBER_WORDS if not word.isascii())
_NUMBER = (
    rf"((?<!\d)\d{{1,5}}(?!\d)|(?<![a-z])(?:{_ENGLISH_NUMBERS})(?![a-z])|[{_CHINESE_NUMBERS}])"
)
_UNIT_SECONDS = (
    (r"个小时|小时|hours?|hrs?|h\b", 3600),
    (r"分钟|minutes?|mins?", 60),
)
_RELATIVE_DUE = re.compile(
    _NUMBER + r"\s*-?\s*(" + "|".join(pattern for pattern, _ in _UNIT_SECONDS) + ")"
)
_TODAY = re.compile(r"\btoday\b|end of (?:the )?shift|今天|今日|本班|当班|下班前")
_QUANTITY_RANGE = re.compile(_NUMBER + r"\s*(?:or|to|-|~|或|到|至)\s*" + _NUMBER)
_NUMBER_TOKEN = re.compile(_NUMBER)

_VAGUE_QUANTITY = (
    "a few",
    "few ",
    "some ",
    "several",
    "a couple",
    "a bunch",
    "many ",
    "几件",
    "几个",
    "几块",
    "一些",
    "若干",
    "一批",
)
_URGENCY_TERMS = (
    "urgent",
    "rush",
    "asap",
    "expedite",
    "high priority",
    "加急",
    "紧急",
    "急单",
    "插单",
    "优先",
)
_UNSUPPORTED_PROCESS_TERMS = (
    "weld",
    "焊",
    "laser",
    "激光",
    "anodiz",
    "阳极氧化",
    "paint",
    "喷漆",
    "heat treat",
    "热处理",
    "grind",
    "磨削",
    "polish",
    "抛光",
)


class OrderPriority(StrEnum):
    NORMAL = "normal"
    URGENT = "urgent"


class InterpretationIssueCode(StrEnum):
    """Stable vocabulary; the first group requests clarification, the rest reject."""

    MISSING_PRODUCT = "missing_product"
    MISSING_QUANTITY = "missing_quantity"
    AMBIGUOUS_QUANTITY = "ambiguous_quantity"
    MISSING_DUE_TIME = "missing_due_time"
    CONFLICTING_DUE_TIME = "conflicting_due_time"
    UNPARSEABLE_DUE_TIME = "unparseable_due_time"
    TODAY_WITHOUT_CLOCK = "today_without_clock"
    PRIORITY_CONFLICT = "priority_conflict"
    UNSUPPORTED_REQUEST = "unsupported_request"
    UNSUPPORTED_PRODUCT = "unsupported_product"
    MULTIPLE_PRODUCTS = "multiple_products"
    INVALID_QUANTITY = "invalid_quantity"
    DUE_BEFORE_RELEASE = "due_before_release"
    ROUTE_MISMATCH = "route_mismatch"
    UNKNOWN_CAPABILITY = "unknown_capability"
    CAPABILITY_NOT_ON_ROUTE = "capability_not_on_route"
    UNSUPPORTED_PROCESS = "unsupported_process"
    MACHINE_PINNING_UNSUPPORTED = "machine_pinning_unsupported"
    SPAN_NOT_IN_REQUEST = "span_not_in_request"


CLARIFICATION_CODES = frozenset(
    {
        InterpretationIssueCode.MISSING_PRODUCT,
        InterpretationIssueCode.MISSING_QUANTITY,
        InterpretationIssueCode.AMBIGUOUS_QUANTITY,
        InterpretationIssueCode.MISSING_DUE_TIME,
        InterpretationIssueCode.CONFLICTING_DUE_TIME,
        InterpretationIssueCode.UNPARSEABLE_DUE_TIME,
        InterpretationIssueCode.TODAY_WITHOUT_CLOCK,
        InterpretationIssueCode.PRIORITY_CONFLICT,
    }
)

_S01_FAILURE_CODES: dict[FailureCode, InterpretationIssueCode] = {
    FailureCode.MISSING_PRODUCT: InterpretationIssueCode.MISSING_PRODUCT,
    FailureCode.UNSUPPORTED_PRODUCT: InterpretationIssueCode.UNSUPPORTED_PRODUCT,
    FailureCode.MISSING_QUANTITY: InterpretationIssueCode.MISSING_QUANTITY,
    FailureCode.INVALID_QUANTITY: InterpretationIssueCode.INVALID_QUANTITY,
    FailureCode.UNKNOWN_ROUTE: InterpretationIssueCode.ROUTE_MISMATCH,
    FailureCode.AMBIGUOUS_TIME: InterpretationIssueCode.MISSING_DUE_TIME,
}


class OrderInterpretationDraft(StrictModel):
    """Model-drafted spans; parsed from validated tool arguments only."""

    run_id: Identifier
    request_type: Literal["new_order", "unsupported"]
    product_text: str | None = Field(default=None, max_length=MAX_SPAN_CHARACTERS)
    quantity_text: str | None = Field(default=None, max_length=MAX_SPAN_CHARACTERS)
    due_texts: tuple[str, ...] = Field(default=(), max_length=3)
    priority: OrderPriority
    priority_text: str | None = Field(default=None, max_length=MAX_SPAN_CHARACTERS)
    requested_route_id: Identifier | None = None
    requested_capabilities: tuple[str, ...] = Field(default=(), max_length=5)
    requested_machine_ids: tuple[str, ...] = Field(default=(), max_length=4)


class InterpretationIssue(StrictModel):
    code: InterpretationIssueCode
    field: str = Field(min_length=1, max_length=40)
    message: str = Field(min_length=1, max_length=300)


class RouteStep(StrictModel):
    operation_template_id: Identifier
    required_capability: Capability
    duration_seconds: int = Field(strict=True, gt=0)
    capable_machine_ids: tuple[Identifier, ...]


class ApplicableConstraints(StrictModel):
    route_id: Identifier
    operation_sequence: tuple[RouteStep, ...] = Field(min_length=1)
    release_time_seconds: Seconds
    hard_horizon_seconds: int = Field(strict=True, gt=0)
    due_time_is_soft: Literal[True] = True
    precedence: Literal["route_sequence_per_unit"] = "route_sequence_per_unit"
    approval_required: Literal[True] = True


class OrderInterpretation(StrictModel):
    """Typed, deterministic outcome of one drafted order interpretation."""

    schema_version: Literal[1] = 1
    status: ResultStatus
    request_sha256: Sha256
    order_id: Identifier | None = None
    product_id: Identifier | None = None
    canonical_product_name: str | None = Field(default=None, max_length=100)
    route_id: Identifier | None = None
    quantity: PositiveQuantity | None = None
    due_time_seconds: Seconds | None = None
    due_basis: Literal["scenario_shift_boundary", "decision_boundary_offset"] | None = None
    release_time_seconds: Seconds | None = None
    priority: OrderPriority | None = None
    priority_weight: PriorityWeight | None = None
    constraints: ApplicableConstraints | None = None
    issues: tuple[InterpretationIssue, ...] = ()
    unresolved_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        allowed = {
            ResultStatus.SUCCESS,
            ResultStatus.CLARIFICATION_REQUIRED,
            ResultStatus.REJECTED,
        }
        if self.status not in allowed:
            raise ValueError("interpretation status must be success, clarification, or rejected")
        fields = (
            self.order_id,
            self.product_id,
            self.route_id,
            self.quantity,
            self.due_time_seconds,
            self.release_time_seconds,
            self.priority,
            self.priority_weight,
            self.constraints,
        )
        if self.status is ResultStatus.SUCCESS:
            if self.issues or self.unresolved_fields or None in fields:
                raise ValueError("a successful interpretation requires every field and no issue")
        else:
            if not self.issues or any(item is not None for item in fields):
                raise ValueError("an unsuccessful interpretation carries issues and no order")
        return self

    def order_delta(self) -> OrderDelta:
        if self.status is not ResultStatus.SUCCESS:
            raise ValueError("only a successful interpretation has an order draft")
        assert self.order_id and self.product_id and self.quantity and self.priority_weight
        assert self.due_time_seconds is not None and self.release_time_seconds is not None
        return OrderDelta(
            kind=OrderDeltaKind.ADD,
            order=Order(
                order_id=self.order_id,
                product_id=self.product_id,
                quantity=self.quantity,
                release_time_seconds=self.release_time_seconds,
                due_time_seconds=self.due_time_seconds,
                priority_weight=self.priority_weight,
                status=OrderStatus.PENDING,
            ),
        )


def normalize_text(value: str) -> str:
    """NFKC + casefold + collapsed whitespace; the only text normalization used."""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def request_sha256(request_text: str) -> str:
    return hashlib.sha256(request_text.encode("utf-8")).hexdigest()


def _number(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _due_values(text: str, *, boundary: int, clock: ScenarioClock | None) -> set[tuple[str, int]]:
    """All due expressions in ``text`` as ``(basis, simulated_second)`` pairs.

    ``today`` without a scenario clock yields ``("today_unresolved", -1)``.
    """

    values: set[tuple[str, int]] = set()
    for match in _RELATIVE_DUE.finditer(text):
        unit = match.group(2)
        multiplier = next(
            seconds for pattern, seconds in _UNIT_SECONDS if re.fullmatch(pattern, unit)
        )
        values.add(("decision_boundary_offset", boundary + _number(match.group(1)) * multiplier))
    if _TODAY.search(text):
        if clock is None:
            values.add(("today_unresolved", -1))
        else:
            values.add(("scenario_shift_boundary", clock.shift_boundary_seconds))
    return values


def _parse_quantity(span: str) -> int | None:
    tokens = _NUMBER_TOKEN.findall(span)
    if len(tokens) != 1:
        return None
    return _number(tokens[0])


def _capable_machines(catalog: DomainCatalog, capability: Capability) -> tuple[str, ...]:
    return tuple(
        sorted(item.machine_id for item in catalog.machines if capability in item.capabilities)
    )


def interpret_order_draft(
    draft: OrderInterpretationDraft,
    *,
    request_text: str,
    catalog: DomainCatalog,
    scenario_clock: ScenarioClock | None,
    decision_boundary_seconds: int,
    hard_horizon_seconds: int,
    order_id: str,
) -> OrderInterpretation:
    """Validate one draft against the request text, the catalog, and the clock."""

    text = normalize_text(request_text)
    issues: list[InterpretationIssue] = []

    def issue(code: InterpretationIssueCode, field: str, message: str) -> None:
        if not any(item.code is code and item.field == field for item in issues):
            issues.append(InterpretationIssue(code=code, field=field, message=message))

    def span(value: str | None, field: str) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = normalize_text(value)
        if normalized not in text:
            issue(
                InterpretationIssueCode.SPAN_NOT_IN_REQUEST,
                field,
                "drafted span does not occur verbatim in the request text",
            )
            return None
        return normalized

    if draft.request_type == "unsupported":
        issue(
            InterpretationIssueCode.UNSUPPORTED_REQUEST,
            "request_type",
            "the request is not a supported new-order request",
        )

    product_span = span(draft.product_text, "product")
    quantity_span = span(draft.quantity_text, "quantity")
    due_spans = [span(item, "due_time") for item in draft.due_texts]
    priority_span = span(draft.priority_text, "priority")

    # Whole-request scans are independent of what the model chose to report.
    mentioned_products = {
        product_id for alias, (product_id, _name) in PRODUCT_ALIASES.items() if alias in text
    }
    if len(mentioned_products) > 1:
        issue(
            InterpretationIssueCode.MULTIPLE_PRODUCTS,
            "product",
            "one request may register only one supported product",
        )
    for term in _UNSUPPORTED_PROCESS_TERMS:
        if term in text:
            issue(
                InterpretationIssueCode.UNSUPPORTED_PROCESS,
                "capabilities",
                f"requested process language {term!r} has no supported capability",
            )
            break
    machine_ids = {item.machine_id for item in catalog.machines}
    if draft.requested_machine_ids or any(item.casefold() in text for item in machine_ids):
        issue(
            InterpretationIssueCode.MACHINE_PINNING_UNSUPPORTED,
            "machine",
            "machine assignment is decided by the deterministic scheduler, not the request",
        )

    quantity: int | None = None
    if any(term in f"{text} " for term in _VAGUE_QUANTITY) or _QUANTITY_RANGE.search(text):
        issue(
            InterpretationIssueCode.AMBIGUOUS_QUANTITY,
            "quantity",
            "the request states an approximate or ranged quantity",
        )
    elif quantity_span is not None:
        quantity = _parse_quantity(quantity_span)
        if quantity is None:
            issue(
                InterpretationIssueCode.AMBIGUOUS_QUANTITY,
                "quantity",
                "the drafted quantity span does not contain exactly one number",
            )

    due_values = _due_values(text, boundary=decision_boundary_seconds, clock=scenario_clock)
    for item in due_spans:
        if item is None:
            continue
        parsed = _due_values(item, boundary=decision_boundary_seconds, clock=scenario_clock)
        if not parsed:
            issue(
                InterpretationIssueCode.UNPARSEABLE_DUE_TIME,
                "due_time",
                "the drafted due span is not a supported due-time expression",
            )
        due_values |= parsed
    due_seconds: int | None = None
    due_basis: str | None = None
    if ("today_unresolved", -1) in due_values:
        issue(
            InterpretationIssueCode.TODAY_WITHOUT_CLOCK,
            "due_time",
            "today requires an explicit scenario clock and shift boundary",
        )
    elif not due_values:
        issue(
            InterpretationIssueCode.MISSING_DUE_TIME,
            "due_time",
            "the request has no supported due-time expression",
        )
    elif len({value for _basis, value in due_values}) > 1:
        issue(
            InterpretationIssueCode.CONFLICTING_DUE_TIME,
            "due_time",
            "the request contains due-time expressions that resolve to different seconds",
        )
    else:
        due_basis, due_seconds = sorted(due_values)[0]
        if due_seconds < decision_boundary_seconds:
            issue(
                InterpretationIssueCode.DUE_BEFORE_RELEASE,
                "due_time",
                "the resolved due time precedes the decision boundary",
            )
            due_seconds = None

    urgent_language = any(term in text for term in _URGENCY_TERMS)
    priority = draft.priority
    if priority is OrderPriority.URGENT:
        if priority_span is None or not any(term in priority_span for term in _URGENCY_TERMS):
            issue(
                InterpretationIssueCode.PRIORITY_CONFLICT,
                "priority",
                "urgent priority requires a quoted urgency phrase from the request",
            )
    elif urgent_language:
        issue(
            InterpretationIssueCode.PRIORITY_CONFLICT,
            "priority",
            "the request contains urgency language but the draft says normal",
        )

    # A verbatim span may wrap a supported alias ("2 aluminum mounting plates");
    # resolve to the longest alias it contains, never to text outside the span.
    product_alias = product_span
    if product_span is not None and product_span not in PRODUCT_ALIASES:
        contained = [alias for alias in PRODUCT_ALIASES if alias in product_span]
        if len({PRODUCT_ALIASES[alias][0] for alias in contained}) == 1:
            product_alias = max(contained, key=len)
    resolution = resolve_order_request(
        product=product_alias,
        quantity=quantity if quantity is not None else None,
        due=due_seconds if due_seconds is not None else 0,
        scenario_clock=scenario_clock,
        requested_route_id=draft.requested_route_id,
    )
    for failure in resolution.failures:
        code = _S01_FAILURE_CODES.get(failure.code)
        if code is None:  # pragma: no cover - S01 vocabulary is closed
            code = InterpretationIssueCode.UNSUPPORTED_REQUEST
        if code is InterpretationIssueCode.MISSING_QUANTITY and any(
            item.field == "quantity" for item in issues
        ):
            continue
        issue(code, failure.field or "request", failure.message)

    route_steps: tuple[RouteStep, ...] = ()
    if resolution.status is ResultStatus.SUCCESS:
        product = next(
            item for item in catalog.products if item.product_id == resolution.product_id
        )
        route_capabilities = {item.required_capability.value for item in product.operations}
        for requested in draft.requested_capabilities:
            name = normalize_text(requested)
            if name not in {item.value for item in Capability}:
                issue(
                    InterpretationIssueCode.UNKNOWN_CAPABILITY,
                    "capabilities",
                    "requested capability is not part of the supported catalog",
                )
            elif name not in route_capabilities:
                issue(
                    InterpretationIssueCode.CAPABILITY_NOT_ON_ROUTE,
                    "capabilities",
                    "requested capability is not part of the predefined product route",
                )
        route_steps = tuple(
            RouteStep(
                operation_template_id=item.operation_template_id,
                required_capability=item.required_capability,
                duration_seconds=item.duration_seconds,
                capable_machine_ids=_capable_machines(catalog, item.required_capability),
            )
            for item in product.operations
        )
    elif draft.requested_capabilities:
        for requested in draft.requested_capabilities:
            if normalize_text(requested) not in {item.value for item in Capability}:
                issue(
                    InterpretationIssueCode.UNKNOWN_CAPABILITY,
                    "capabilities",
                    "requested capability is not part of the supported catalog",
                )

    digest = request_sha256(request_text)
    if issues:
        status = (
            ResultStatus.CLARIFICATION_REQUIRED
            if all(item.code in CLARIFICATION_CODES for item in issues)
            else ResultStatus.REJECTED
        )
        return OrderInterpretation(
            status=status,
            request_sha256=digest,
            issues=tuple(issues),
            unresolved_fields=tuple(dict.fromkeys(item.field for item in issues)),
        )

    assert due_seconds is not None and due_basis is not None and resolution.route_id
    return OrderInterpretation(
        status=ResultStatus.SUCCESS,
        request_sha256=digest,
        order_id=order_id,
        product_id=resolution.product_id,
        canonical_product_name=resolution.canonical_product_name,
        route_id=resolution.route_id,
        quantity=resolution.quantity,
        due_time_seconds=due_seconds,
        due_basis=due_basis,  # type: ignore[arg-type]
        release_time_seconds=decision_boundary_seconds,
        priority=priority,
        priority_weight=PRIORITY_WEIGHTS[priority.value],
        constraints=ApplicableConstraints(
            route_id=resolution.route_id,
            operation_sequence=route_steps,
            release_time_seconds=decision_boundary_seconds,
            hard_horizon_seconds=hard_horizon_seconds,
        ),
    )
