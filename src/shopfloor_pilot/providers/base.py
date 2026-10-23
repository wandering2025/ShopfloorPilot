"""Provider transport protocol shared by the live adapter and scripted fake."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ProviderRequest, ProviderResponse


@runtime_checkable
class Provider(Protocol):
    """One non-retrying completion transport."""

    @property
    def provider_name(self) -> str: ...

    def complete(self, request: ProviderRequest, *, timeout_seconds: float) -> ProviderResponse: ...
