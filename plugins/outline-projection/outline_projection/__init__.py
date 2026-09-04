"""Deterministic, read-only outline projection plugin.

Keep runtime loading lazy so the deterministic package builder can regenerate
identity files after source edits before an installed sidecar is valid.
"""

__all__ = ["CAPABILITY_ID", "OutlineProjectionPlugin", "capability_descriptor", "main", "render_projection"]


def __getattr__(name: str):
    if name in __all__:
        from . import runtime
        return getattr(runtime, name)
    raise AttributeError(name)
