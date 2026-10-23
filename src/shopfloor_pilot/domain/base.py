"""Strict shared primitives for the ShopfloorPilot domain contract."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
WALL_CLOCK_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"

Identifier = Annotated[str, Field(pattern=ID_PATTERN, min_length=1, max_length=64)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
WallClockTimestamp = Annotated[str, Field(pattern=WALL_CLOCK_PATTERN)]
Seconds = Annotated[int, Field(strict=True, ge=0)]
PositiveSeconds = Annotated[int, Field(strict=True, gt=0)]
PositiveQuantity = Annotated[int, Field(strict=True, ge=1, le=10)]
PriorityWeight = Annotated[int, Field(strict=True, ge=1, le=100)]


class StrictModel(BaseModel):
    """Immutable, extra-forbidding model with strict Python-input validation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )
