"""Directed provider remediation tests for NAP-B0-F-006..010 and F-012."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
if SDK_ROOT.is_dir():
    sys.path.insert(0, str(SDK_ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "provider-anthropic"))
sys.path.insert(0, str(ROOT / "plugins" / "provider-gemini"))

from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs  # noqa: E402
from plotpilot_plugin_sdk.package import build_files_sha256, digest_package  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_capability_descriptor,
    verify_checkpoint,
    verify_manifest,
    verify_package_manifest,
    verify_provenance_receipt,
    verify_result_bundle,
    verify_stream_prefix,
)
from provider_anthropic import provider as anthropic  # noqa: E402
from provider_anthropic import contract as anthropic_contract  # noqa: E402
from provider_anthropic.contract import ProviderSchemaError as AnthropicSchemaError  # noqa: E402
from provider_anthropic.mock import MemoryHost as AnthropicHost  # noqa: E402
from provider_anthropic.mock import ScriptedTransport as AnthropicTransport  # noqa: E402
from provider_gemini import provider as gemini  # noqa: E402
from provider_gemini import contract as gemini_contract  # noqa: E402
from provider_gemini.contract import ProviderSchemaError as GeminiSchemaError  # noqa: E402
from provider_gemini.mock import MemoryHost as GeminiHost  # noqa: E402
from provider_gemini.mock import ScriptedTransport as GeminiTransport  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"
CASES = (
    (anthropic, anthropic.AnthropicProvider, AnthropicHost, AnthropicTransport, AnthropicSchemaError, "anthropic"),
    (gemini, gemini.GeminiProvider, GeminiHost, GeminiTransport, GeminiSchemaError, "gemini"),
)
CONTRACTS = {"anthropic": anthropic_contract, "gemini": gemini_contract}


def _fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _core_ids(kind: str) -> tuple[str, str, tuple[str, ...]]:
    return (
        f"stream-core-{kind}",
        f"receipt-core-{kind}",
        tuple(f"checkpoint-core-{kind}-{index}" for index in range(1, 5)),
    )


def _request(module: Any, *, invocation_id: str = "invoke-1") -> dict[str, Any]:
    kind = "anthropic" if module is anthropic else "gemini"
    stream_id, receipt_id, checkpoint_ids = _core_ids(kind)
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
        "stream": True,
        "output_role": "assistant.text",
        "target": {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-1"},
        "created_at": "2026-08-27T00:00:00Z",
        "stream_id": stream_id,
        "checkpoint_ids": list(checkpoint_ids),
        "provenance_receipt_id": receipt_id,
    }


def _case_fixture(case: tuple[Any, ...]) -> tuple[Any, Any, Any, Any, Any, str]:
    return case


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_descriptor_and_closed_schema_artifacts_are_resolvable(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    descriptor = module.capability_descriptor()
    package_root = ROOT / "plugins" / f"provider-{kind}"
    descriptor_file = json.loads((package_root / "descriptor.json").read_text(encoding="utf-8"))
    assert descriptor == descriptor_file == module.DESCRIPTOR
    verify_capability_descriptor(descriptor, expected_capability_id=descriptor["capability_id"], expected_provider=descriptor["provider"])
    for schema_id, path_name in ((module.INPUT_SCHEMA, "input.schema.json"), (module.OUTPUT_SCHEMA, "output.schema.json")):
        path = package_root / f"provider_{kind}" / "schemas" / path_name
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$id"] == schema_id
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert path_name in {"input.schema.json", "output.schema.json"}
    assert module.NEEDS == tuple(json.loads((package_root / "plugin.json").read_text(encoding="utf-8"))["needs"])


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_f006_all_descriptor_schema_references_match_unique_hashed_artifacts(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    package_root = ROOT / "plugins" / f"provider-{kind}"
    contract = CONTRACTS[kind]
    descriptor = module.capability_descriptor()
    index = contract.load_schema_index()
    references = (descriptor["input_schema"], descriptor["output_schema"])
    assert len(references) == len(set(references)) == 2
    assert set(references) == {module.INPUT_SCHEMA, module.OUTPUT_SCHEMA}
    assert set(index) == set(references)
    resolved_paths = []
    for schema_id in references:
        relative_path = index[schema_id]["path"]
        artifact_path = (package_root / f"provider_{kind}" / relative_path).resolve()
        raw = artifact_path.read_bytes()
        assert artifact_path.is_file()
        assert index[schema_id]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert contract.schema_path(schema_id) == artifact_path
        assert contract.schema_bytes(schema_id) == raw
        assert contract.load_schema(schema_id)["$id"] == schema_id
        resolved_paths.append(str(artifact_path).casefold())
    assert len(resolved_paths) == len(set(resolved_paths))


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
@pytest.mark.parametrize("mutation", ["stale_digest", "artifact_byte", "missing_id", "duplicate_id", "unknown_field", "wrong_type"])
def test_f006_schema_index_integrity_mutations_fail_closed(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str, mutation: str) -> None:
    source_package = ROOT / "plugins" / f"provider-{kind}" / f"provider_{kind}"
    contract = CONTRACTS[kind]
    with tempfile.TemporaryDirectory() as temp:
        temp_package = Path(temp) / f"provider_{kind}"
        shutil.copytree(source_package, temp_package)
        index_path = temp_package / "schemas" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if mutation == "stale_digest":
            index["schemas"][0]["sha256"] = "0" * 64
        elif mutation == "artifact_byte":
            artifact = temp_package / index["schemas"][0]["path"]
            artifact.write_bytes(artifact.read_bytes() + b" ")
        elif mutation == "missing_id":
            index["schemas"][0].pop("schema_id")
        elif mutation == "duplicate_id":
            index["schemas"][1]["schema_id"] = index["schemas"][0]["schema_id"]
        elif mutation == "unknown_field":
            index["unexpected"] = True
        elif mutation == "wrong_type":
            index["schemas"][0]["sha256"] = 7
        else:  # pragma: no cover - guarded by parametrization
            raise AssertionError(mutation)
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        with pytest.raises(schema_error):
            contract.load_schema_index(index_path)


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_f006_input_and_invocation_output_are_closed_and_validated(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    request = _request(module)
    normalized = module._validate_request(copy.deepcopy(request))
    assert normalized["temperature"] == 0.0
    mutations = []
    missing = copy.deepcopy(request)
    missing.pop("job_id")
    mutations.append(missing)
    extra = copy.deepcopy(request)
    extra["unknown_field"] = "must reject"
    mutations.append(extra)
    wrong_type = copy.deepcopy(request)
    wrong_type["messages"][0]["content"] = 42
    mutations.append(wrong_type)
    nested_extra = copy.deepcopy(request)
    nested_extra["response_format"] = {"type": "json", "unknown": True}
    mutations.append(nested_extra)
    for mutated in mutations:
        with pytest.raises(module.ProviderError):
            module._validate_request(mutated)

    host = host_type()
    transport = transport_type(_fixture(f"{kind}-success.json"))
    outcome = provider_type(transport).run(request, host)
    envelope = outcome.as_dict()
    assert set(envelope) == {"status", "result", "conditional_result", "receipt", "error", "chunks", "output_text"}
    assert envelope["status"] == "succeeded"
    assert envelope["result"] is not None and envelope["conditional_result"] is None and envelope["error"] is None
    module._validate_provider_schema(module.OUTPUT_SCHEMA, envelope, code="test")
    mutated_output = copy.deepcopy(envelope)
    mutated_output["unexpected"] = True
    with pytest.raises(module.ProviderError):
        module._validate_provider_schema(module.OUTPUT_SCHEMA, mutated_output, code="test")


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_success_result_stream_checkpoint_receipt_and_provider_parity(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    request = _request(module)
    host = host_type()
    transport = transport_type(_fixture(f"{kind}-success.json"))
    outcome = provider_type(transport).run(request, host)

    assert outcome.status == "succeeded"
    assert outcome.result is not None and outcome.conditional_result is None
    bundle = outcome.result
    assert bundle["schema"] == "result-bundle/v1"
    assert bundle["contract_id"] == "artifact-bundle/v1"
    assert bundle["bundle_type"] == "artifact"
    assert bundle["partial"] is False
    assert outcome.output_text == ("你好，世界" if module is anthropic else "你好，Gemini")
    assert [chunk.delta for chunk in outcome.chunks] == (["你好", "，世界"] if module is anthropic else ["你好", "，Gemini"])
    assert [chunk.seq for chunk in outcome.chunks] == [1, 2]
    assert [chunk.prefix for chunk in outcome.chunks] == (["你好", "你好，世界"] if module is anthropic else ["你好", "你好，Gemini"])
    assert len(host.stream_prefixes) == 2 and len(host.checkpoints) == 2
    for index, chunk in enumerate(outcome.chunks):
        assert chunk.stream_prefix is not None
        assert chunk.stream_prefix["stream_id"] == request["stream_id"]
        assert chunk.stream_prefix["prefix_hash"] == chunk.prefix_hash
        assert chunk.prefix_asset_id == host.stream_prefixes[index]["asset_id"]
        verify_stream_prefix(chunk.stream_prefix, previous=outcome.chunks[index - 1].stream_prefix if index else None, content=chunk.prefix.encode("utf-8"))
    for index, checkpoint in enumerate(host.checkpoints):
        assert checkpoint["checkpoint_id"] == request["checkpoint_ids"][index]
        verify_checkpoint(checkpoint, expected_snapshot_hash=request["run_snapshot_hash"], previous_seq=index or None)
    verify_result_bundle(bundle, snapshot_hash_value=request["run_snapshot_hash"])
    verify_provenance_receipt(outcome.receipt)
    assert outcome.receipt["receipt_id"] == request["provenance_receipt_id"]
    assert outcome.receipt["package_hash"] == module.PACKAGE_HASH
    assert outcome.receipt["release_id"] == module.RELEASE_ID
    assert host.completions[-1]["outcome"] == "succeeded"
    assert host.completions[-1]["worker_run_id"] == request["worker_run_id"]
    # Inline requests do not need host.asset.read; the asset-backed request
    # path is exercised separately, while all other advertised Host methods
    # must be observed on a successful run.
    assert {method for method, _ in host.calls} >= (set(module.NEEDS) - {"host.asset.read/v1"})
    wire = transport.requests[0]
    assert "api_key" not in wire and "access_token" not in wire
    assert "x-api-key" not in wire["headers"] and "x-goog-api-key" not in wire["headers"]
    assert "api_key" not in json.dumps(wire)
    if module is anthropic:
        assert wire["url"] == "https://api.anthropic.com/v1/messages"
        assert wire["body"]["system"] == "你是一个确定性 fixture。"
    else:
        assert wire["url"] == "https://generativelanguage.googleapis.com/v1beta/models/fixture-model-v1:streamGenerateContent?alt=sse"
        assert wire["body"]["systemInstruction"]["parts"][0]["text"] == "你是一个确定性 fixture。"


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_f007_caller_endpoint_and_transport_are_rejected_before_credential_bearing_send(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    request = _request(module)
    for field in ("base_url", "transport", "transport_config", "endpoint"):
        bad = copy.deepcopy(request)
        bad[field] = "https://caller-controlled.invalid"
        with pytest.raises(module.ProviderError, match="forbidden"):
            module._validate_request(bad)
    expected_origin = module.CORE_ENDPOINT_ORIGIN
    transport_class = module.AnthropicHTTPTransport if module is anthropic else module.GeminiHTTPTransport
    with pytest.raises(ValueError, match="fixed Core-owned origin"):
        transport_class("https://caller-controlled.invalid", "CANARY-SECRET")
    bound = transport_class(expected_origin, "CANARY-SECRET")
    with pytest.raises(ValueError, match="fixed Core-owned origin"):
        list(bound.stream({"url": "https://caller-controlled.invalid/steal", "headers": {}, "body": {}}))

    provider = provider_type(transport_type(_fixture(f"{kind}-success.json")))
    wire = provider._wire_request(module._validate_request(request))
    assert wire["url"].startswith(expected_origin + "/")
    assert "caller-controlled" not in wire["url"]


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
@pytest.mark.parametrize(
    "method,overrides,drops,extras",
    [
        ("host.stream.commit/v1", {"stream_id": "stream-attacker"}, set(), {}),
        ("host.stream.commit/v1", {"accepted": False}, set(), {}),
        ("host.checkpoint.commit/v1", {"checkpoint_id": "checkpoint-attacker"}, set(), {}),
        ("host.checkpoint.commit/v1", {"accepted": False}, set(), {}),
        ("host.job.complete/v1", {"provenance_receipt_id": "receipt-attacker"}, set(), {}),
        ("host.job.complete/v1", {"attempt_state": "running", "step_state": "running", "job_state": "running"}, set(), {}),
        ("host.job.complete/v1", {"accepted": False}, set(), {}),
        ("host.stream.commit/v1", {}, {"acked_prefix_hash"}, {}),
        ("host.job.complete/v1", {}, set(), {"unexpected": True}),
    ],
)
def test_f008_every_mutated_host_result_fails_closed(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str, method: str, overrides: dict[str, object], drops: set[str], extras: dict[str, object]) -> None:
    request = _request(module)
    host = host_type(response_overrides={method: overrides}, response_drops={method: drops}, response_extras={method: extras})
    transport = transport_type(_fixture(f"{kind}-success.json"))
    try:
        outcome = provider_type(transport).run(request, host)
    except module.ProviderError:
        return
    assert outcome.status != "succeeded"
    assert outcome.error is not None
    assert not (host.completions and host.completions[-1]["outcome"] == "succeeded")


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_f009_complete_package_manifest_identity_wheel_and_mutations(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    package_root = ROOT / "plugins" / f"provider-{kind}"
    expected = json.loads((package_root / "expected.json").read_text(encoding="utf-8"))
    required = {"schema", "package_files", "files_sha256", "package_hash", "release_id", "wheel_path", "input_schema_path", "output_schema_path"}
    assert required <= set(expected)
    package_files = {path: (package_root / path).read_bytes() for path in expected["package_files"]}
    assert "files.sha256" in package_files
    for required_path in ("descriptor.json", "plugin.json", expected["input_schema_path"], expected["output_schema_path"], expected["wheel_path"], "backend/requirements.lock", "backend/wheels/README.txt", "migrations/manifest.json"):
        assert required_path in package_files and (package_root / required_path).is_file()
    assert any(path.endswith("provider.py") for path in package_files)
    assert expected["files_sha256"] == (package_root / "files.sha256").read_text(encoding="utf-8")
    manifest_files = {path: data for path, data in package_files.items() if path != "files.sha256"}
    verify_package_manifest(manifest_files, package_files["files.sha256"])
    identity_files = {path: (package_root / path).read_bytes() for path in expected["identity_files"]}
    digest = digest_package(identity_files, expected["plugin_id"], expected["version"])
    assert digest.package_hash == expected["package_hash"]
    assert digest.release_id == expected["release_id"]
    identity = json.loads((package_root / f"provider_{kind}" / "identity.json").read_text(encoding="utf-8"))
    assert identity["package_hash"] == expected["package_hash"] and identity["release_id"] == expected["release_id"]
    descriptor = json.loads((package_root / "descriptor.json").read_text(encoding="utf-8"))
    assert descriptor["provider"]["release_id"] == expected["release_id"]

    mutated = dict(identity_files)
    source_path = next(path for path in mutated if path.endswith("provider.py"))
    mutated[source_path] = mutated[source_path] + b"\n# byte mutation\n"
    mutated_digest = digest_package(mutated, expected["plugin_id"], expected["version"])
    assert mutated_digest.package_hash != expected["package_hash"]
    tampered_manifest = dict(manifest_files)
    tampered_manifest["descriptor.json"] = tampered_manifest["descriptor.json"].replace(b"capability-provider", b"capability-provideq", 1)
    with pytest.raises(Exception):
        verify_package_manifest(tampered_manifest, package_files["files.sha256"])


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_f009_wheel_is_pep427_importable_and_metadata_is_present(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    package_root = ROOT / "plugins" / f"provider-{kind}"
    expected = json.loads((package_root / "expected.json").read_text(encoding="utf-8"))
    wheel = package_root / expected["wheel_path"]
    assert zipfile.is_zipfile(wheel)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = [name for name in names if name.endswith(".dist-info/METADATA")]
        wheel_name = [name for name in names if name.endswith(".dist-info/WHEEL")]
        record_name = [name for name in names if name.endswith(".dist-info/RECORD")]
        assert len(metadata_name) == len(wheel_name) == len(record_name) == 1
        metadata = archive.read(metadata_name[0]).decode("utf-8")
        wheel_metadata = archive.read(wheel_name[0]).decode("utf-8")
        assert "Version: 0.1.0" in metadata
        assert "Root-Is-Purelib: true" in wheel_metadata and "Tag: py3-none-any" in wheel_metadata
        assert f"{expected['wheel_import']}/__init__.py" in names
        records = csv_records(archive.read(record_name[0]))
        for name, encoded_hash, size in records:
            if name == record_name[0]:
                assert encoded_hash == "" and size == ""
            else:
                data = archive.read(name)
                assert encoded_hash == "sha256=" + __import__("base64").urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
                assert size == str(len(data))
        with tempfile.TemporaryDirectory() as temp:
            archive.extractall(temp)
            code = "import importlib.metadata, sys; sys.path.insert(0, r'%s'); import %s; print(importlib.metadata.version('%s')); print(%s.main()['schema'])" % (temp, expected["wheel_import"], metadata.split("Name: ", 1)[1].splitlines()[0], expected["wheel_import"])
            result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, check=True)
            assert result.stdout.splitlines() == ["0.1.0", "capability-provider/v1"]


def csv_records(raw: bytes) -> list[tuple[str, str, str]]:
    import csv
    import io

    return [tuple(row) for row in csv.reader(io.StringIO(raw.decode("utf-8")))]


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_f010_jcs_numeric_unicode_vectors_and_no_approximate_fallback(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    vectors = [
        ({"zero": -0.0, "fraction": 1e-7, "small": 1e-6}, b'{"fraction":1e-7,"small":0.000001,"zero":0}'),
        ({"n": 333333333.33333329}, b'{"n":333333333.3333333}'),
        ({"n": 1e30}, b'{"n":1e+30}'),
        ({"n": 4.50}, b'{"n":4.5}'),
        ({"n": 2e-3}, b'{"n":0.002}'),
        ({"n": 1e-27}, b'{"n":1e-27}'),
        ({"n": 1e20}, b'{"n":100000000000000000000}'),
        ({"n": 1e21}, b'{"n":1e+21}'),
        ({"unicode": "café😀\u2028"}, "{\"unicode\":\"café😀\u2028\"}".encode("utf-8")),
        (
            {"€": "Euro Sign", "\r": "Carriage Return", "😀": "Emoji", "1": "One", "ö": "Latin Small Letter O With Diaeresis"},
            '{"\\r":"Carriage Return","1":"One","ö":"Latin Small Letter O With Diaeresis","€":"Euro Sign","😀":"Emoji"}'.encode("utf-8"),
        ),
        ({"controls": "\b\t\n\f\r\"\\"}, b'{"controls":"\\b\\t\\n\\f\\r\\"\\\\"}'),
    ]
    for value, expected in vectors:
        assert module._canonical_bytes(value) == expected == canonical_bytes(value)
        assert module._hash_jcs("jcs-vector/v1", value) == hash_jcs("jcs-vector/v1", value)
    for non_finite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(Exception):
            module._canonical_bytes({"n": non_finite})
    source = (ROOT / "plugins" / f"provider-{kind}" / f"provider_{kind}" / "provider.py").read_text(encoding="utf-8")
    # json.dumps is allowed for the provider wire body; it must not be used as
    # a canonical/hash implementation or as an SDK fallback.
    assert "json.dumps(value" not in source
    assert "json.dumps(dict(value)" not in source
    assert "except ImportError" in source and "SDK_UNAVAILABLE" in source


@pytest.mark.parametrize("kind,provider_dir,module_name", [("anthropic", "provider-anthropic", "provider_anthropic"), ("gemini", "provider-gemini", "provider_gemini")])
def test_f010_sdk_absence_fails_closed(kind: str, provider_dir: str, module_name: str) -> None:
    provider_path = ROOT / "plugins" / provider_dir
    code = "import sys; sys.path.insert(0, r'%s'); import %s.provider as p;\ntry:\n p._canonical_bytes({'zero': -0.0})\nexcept p.ProviderError as exc:\n print(exc.code)" % (provider_path, module_name)
    result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "SDK_UNAVAILABLE"


@pytest.mark.parametrize("module,provider_type,host_type,transport_type,schema_error,kind", CASES)
def test_f012_conditional_failure_and_gemini_output_text_regression(module: Any, provider_type: Any, host_type: Any, transport_type: Any, schema_error: Any, kind: str) -> None:
    host = host_type()
    outcome = provider_type(transport_type(_fixture(f"{kind}-failure.json"))).run(_request(module), host)
    assert outcome.status == "failed"
    assert outcome.result is None
    assert outcome.conditional_result is not None
    assert outcome.conditional_result["contract_id"] == "diagnostic-bundle/v1"
    assert outcome.conditional_result["items"][0]["status"] == "failed"
    assert outcome.receipt["bundle_id"] == outcome.conditional_result["bundle_id"]
    assert outcome.error is not None
    if module is gemini:
        assert outcome.error["code"] == "PROMPT_BLOCKED"
