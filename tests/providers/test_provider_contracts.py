"""B0 provider contract tests; all upstream responses are local fixtures."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
if SDK_ROOT.is_dir():
    sys.path.insert(0, str(SDK_ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "provider-anthropic"))
sys.path.insert(0, str(ROOT / "plugins" / "provider-gemini"))

from provider_anthropic import provider as anthropic  # noqa: E402
from provider_anthropic.mock import MemoryHost as AnthropicHost  # noqa: E402
from provider_anthropic.mock import ScriptedTransport as AnthropicTransport  # noqa: E402
from provider_gemini import provider as gemini  # noqa: E402
from provider_gemini.mock import MemoryHost as GeminiHost  # noqa: E402
from provider_gemini.mock import ScriptedTransport as GeminiTransport  # noqa: E402

# Keep every contract assertion on the public SDK verifier module.  Some SDK
# releases intentionally do not re-export every verifier at package root, so
# importing these symbols from ``plotpilot_plugin_sdk`` would skip the
# strongest checks in this suite.
from plotpilot_plugin_sdk.verifier import (  # type: ignore[import-not-found]
    verify_capability_descriptor,
    verify_checkpoint,
    verify_manifest,
    verify_provenance_receipt,
    verify_result_bundle,
    verify_stream_prefix,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _request(module: Any, *, invocation_id: str = "invoke-1") -> dict[str, Any]:
    return {
        "schema": module.INPUT_SCHEMA,
        "invocation_id": invocation_id,
        "worker_run_id": f"worker-{invocation_id}",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "run_snapshot_hash": "a" * 64,
        "model": "fixture-model-v1",
        "messages": [
            {"role": "system", "content": "你是一个确定性 fixture。"},
            {"role": "user", "content": "输出一句问候。"},
        ],
        "max_tokens": 128,
        "temperature": 0.0,
        "output_role": "assistant.text",
        "target": {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-1"},
        "created_at": "2026-08-27T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("module", "manifest_name", "descriptor_name"),
    [
        (anthropic, "provider-anthropic", "provider-anthropic"),
        (gemini, "provider-gemini", "provider-gemini"),
    ],
)
def test_descriptor_and_manifest_are_frozen(module: Any, manifest_name: str, descriptor_name: str) -> None:
    descriptor = module.capability_descriptor()
    descriptor_file = json.loads((ROOT / "plugins" / descriptor_name / "descriptor.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "plugins" / manifest_name / "plugin.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "catalog" / "plugin-catalog-v1.json").read_text(encoding="utf-8"))
    catalog_entry = next(item for item in catalog["code_plugins"] if item["plugin_id"] == module.PLUGIN_ID)

    assert descriptor == descriptor_file == module.DESCRIPTOR
    assert set(descriptor) == {
        "schema", "capability_id", "provider", "input_schema", "output_schema",
        "result_contract", "supports", "deterministic", "accepted_data_formats",
    }
    assert descriptor["result_contract"] == "artifact-bundle/v1"
    assert descriptor["supports"] == ["run", "cancel"]
    assert descriptor["accepted_data_formats"] == []
    assert descriptor["deterministic"] is False
    assert manifest["plugin_id"] == module.PLUGIN_ID
    assert manifest["capabilities"] == [{
        "capability_id": descriptor["capability_id"],
        "operations": ["run", "cancel"],
        "result_contract": "artifact-bundle/v1",
    }]
    assert manifest["needs"] == list(module.NEEDS)
    assert catalog_entry["project"] == "NAP-00"
    assert catalog_entry["source_write_set"] == [f"plugins/{descriptor_name}/**"]
    assert catalog_entry["needs"] == list(module.NEEDS)
    assert catalog_entry["capabilities"][0]["headless"] is True
    assert catalog_entry["capabilities"][0]["ui_contributions"] == []
    if verify_capability_descriptor is not None:
        verify_capability_descriptor(
            descriptor,
            expected_capability_id=descriptor["capability_id"],
            expected_provider=descriptor["provider"],
        )
        verify_manifest(manifest)


@pytest.mark.parametrize(
    ("module", "provider_type", "host_type", "fixture_name"),
    [
        (anthropic, anthropic.AnthropicProvider, AnthropicHost, "anthropic-success.json"),
        (gemini, gemini.GeminiProvider, GeminiHost, "gemini-success.json"),
    ],
)
def test_success_result_stream_checkpoint_receipt_and_no_secret(
    module: Any, provider_type: Any, host_type: Any, fixture_name: str
) -> None:
    request = _request(module)
    host = host_type()
    request_asset_id = host.seed_json_asset(request)
    transport = (AnthropicTransport if module is anthropic else GeminiTransport)(_fixture(fixture_name))
    outcome = provider_type(transport).run({"request_asset_id": request_asset_id}, host)

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    assert outcome.conditional_result is None
    bundle = outcome.result
    assert bundle["schema"] == "result-bundle/v1"
    assert bundle["contract_id"] == "artifact-bundle/v1"
    assert bundle["bundle_type"] == "artifact"
    assert bundle["partial"] is False
    assert bundle["items"][0]["schema"] == "artifact-item/v1"
    assert bundle["items"][0]["status"] == "complete"
    assert outcome.output_text == "你好，世界" if module is anthropic else "你好，Gemini"
    assert [chunk.delta for chunk in outcome.chunks] == (["你好", "，世界"] if module is anthropic else ["你好", "，Gemini"])
    assert [chunk.seq for chunk in outcome.chunks] == [1, 2]
    assert [chunk.prefix for chunk in outcome.chunks] == (["你好", "你好，世界"] if module is anthropic else ["你好", "你好，Gemini"])
    assert len(host.stream_prefixes) == 2
    assert len(host.checkpoints) == 2
    for index, chunk in enumerate(outcome.chunks):
        assert chunk.stream_prefix is not None
        assert chunk.stream_prefix["schema"] == "stream-prefix/v1"
        assert chunk.prefix_asset_id == host.stream_prefixes[index]["asset_id"]
        assert chunk.stream_prefix["prefix_asset_id"] == chunk.prefix_asset_id
        assert chunk.stream_prefix["prefix_hash"] == chunk.prefix_hash
        if verify_stream_prefix is not None:
            previous = outcome.chunks[index - 1].stream_prefix if index else None
            verify_stream_prefix(chunk.stream_prefix, previous=previous, content=chunk.prefix.encode("utf-8"))
    assert host.completions[-1]["outcome"] == "succeeded"
    assert {method for method, _ in host.calls} >= set(module.NEEDS)

    wire = transport.requests[0]
    assert "api_key" not in wire and "access_token" not in wire
    assert "x-api-key" not in wire["headers"]
    assert "x-goog-api-key" not in wire["headers"]
    assert "api_key" not in json.dumps(wire)
    if module is anthropic:
        assert wire["url"].endswith("/v1/messages")
        assert wire["body"]["system"] == "你是一个确定性 fixture。"
    else:
        assert ":streamGenerateContent?alt=sse" in wire["url"]
        assert wire["body"]["systemInstruction"]["parts"][0]["text"] == "你是一个确定性 fixture。"

    if verify_result_bundle is not None:
        verify_result_bundle(bundle, snapshot_hash_value=request["run_snapshot_hash"])
        for checkpoint in host.checkpoints:
            verify_checkpoint(checkpoint, expected_snapshot_hash=request["run_snapshot_hash"])
        verify_provenance_receipt(outcome.receipt)


@pytest.mark.parametrize(
    ("module", "provider_type", "host_type", "transport_type", "fixture_name"),
    [
        (anthropic, anthropic.AnthropicProvider, AnthropicHost, AnthropicTransport, "anthropic-failure.json"),
        (gemini, gemini.GeminiProvider, GeminiHost, GeminiTransport, "gemini-failure.json"),
    ],
)
def test_failure_uses_conditional_diagnostic_result_not_artifact_failure(
    module: Any, provider_type: Any, host_type: Any, transport_type: Any, fixture_name: str
) -> None:
    host = host_type()
    outcome = provider_type(transport_type(_fixture(fixture_name))).run(_request(module), host)

    assert outcome.status == "failed"
    assert outcome.result is None
    assert outcome.conditional_result is not None
    diagnostic = outcome.conditional_result
    assert diagnostic["contract_id"] == "diagnostic-bundle/v1"
    assert diagnostic["bundle_type"] == "diagnostic"
    assert diagnostic["partial"] is True
    assert diagnostic["items"][0]["schema"] == "diagnostic-item/v1"
    assert diagnostic["items"][0]["status"] == "failed"
    assert outcome.receipt["bundle_id"] == diagnostic["bundle_id"]
    assert outcome.error is not None
    assert host.completions[-1]["outcome"] == "failed"
    if verify_result_bundle is not None:
        verify_result_bundle(diagnostic, snapshot_hash_value="a" * 64)
        verify_provenance_receipt(outcome.receipt)


@pytest.mark.parametrize(
    ("module", "provider_type", "host_type", "transport_type", "fixture_name"),
    [
        (anthropic, anthropic.AnthropicProvider, AnthropicHost, AnthropicTransport, "anthropic-success.json"),
        (gemini, gemini.GeminiProvider, GeminiHost, GeminiTransport, "gemini-success.json"),
    ],
)
def test_cancel_is_control_operation_and_does_not_add_result_contract(
    module: Any, provider_type: Any, host_type: Any, transport_type: Any, fixture_name: str
) -> None:
    host = host_type()
    provider = provider_type(transport_type(_fixture(fixture_name)))
    request = _request(module, invocation_id="invoke-cancel")
    assert provider.cancel(request["worker_run_id"]) is True
    assert provider.cancel(request["worker_run_id"]) is False
    outcome = provider.run(request, host)

    assert outcome.status == "cancelled"
    assert outcome.result is None
    assert outcome.conditional_result is None
    assert outcome.error == {"code": "CANCELLED", "message": "provider run cancelled", "retryable": False}
    assert not provider.transport.requests
    assert host.completions[-1]["outcome"] == "cancelled"


@pytest.mark.parametrize(
    ("module", "provider_type", "host_type", "transport_type", "fixture_name"),
    [
        (anthropic, anthropic.AnthropicProvider, AnthropicHost, AnthropicTransport, "anthropic-success.json"),
        (gemini, gemini.GeminiProvider, GeminiHost, GeminiTransport, "gemini-success.json"),
    ],
)
def test_provider_requires_injected_transport_and_rejects_credentials_in_input(
    module: Any, provider_type: Any, host_type: Any, transport_type: Any, fixture_name: str
) -> None:
    with pytest.raises(ValueError, match="Transport is required|transport is required"):
        provider_type().run(_request(module), host_type())
    bad = _request(module)
    bad["api_key"] = "not-a-secret-fixture"
    with pytest.raises(module.ProviderError, match="credentials"):
        provider_type(transport_type(_fixture(fixture_name))).run(bad, host_type())
