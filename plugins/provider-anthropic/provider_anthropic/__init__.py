"""Headless Anthropic provider plugin for PlotPilot B0."""

from .provider import (
    ANTHROPIC_CAPABILITY_ID,
    DESCRIPTOR,
    NEEDS,
    PACKAGE_HASH,
    PLUGIN_ID,
    RELEASE_ID,
    AnthropicHTTPTransport,
    AnthropicProvider,
    InvocationOutcome,
    StreamChunk,
    capability_descriptor,
)

__all__ = [
    "ANTHROPIC_CAPABILITY_ID",
    "DESCRIPTOR",
    "NEEDS",
    "PACKAGE_HASH",
    "PLUGIN_ID",
    "RELEASE_ID",
    "AnthropicHTTPTransport",
    "AnthropicProvider",
    "InvocationOutcome",
    "StreamChunk",
    "capability_descriptor",
]
