"""Streamlit wrapper for the display-only factory-twin component.

The frontend is served by the local Streamlit server from the package
directory (no CDN, no build step). It renders a read-only timeline and never
returns a value, so it cannot trigger an operational action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

FRONTEND_DIRECTORY = Path(__file__).resolve().parent / "factory_twin_frontend"

_component = components.declare_component("factory_twin", path=str(FRONTEND_DIRECTORY))


def render_factory_twin(
    payload: dict[str, Any],
    *,
    key: str,
    renderer: str = "auto",
    speed: int = 120,
    hold_seconds: float = 1.6,
    autoplay: bool = True,
) -> None:
    """Render one timeline payload; options only seed the viewer's first choices."""

    _component(
        payload=payload,
        options={
            "renderer": renderer,
            "speed": speed,
            "hold_seconds": hold_seconds,
            "autoplay": autoplay,
        },
        key=key,
        default=None,
    )


__all__ = ["FRONTEND_DIRECTORY", "render_factory_twin"]
