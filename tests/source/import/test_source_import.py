"""Targeted NAP-B1-IMPORT-001 tests."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins" / "source-import"
FIXTURES = ROOT / "data" / "source-cleaning" / "fixtures" / "import"
import sys
sys.path[:0] = [str(PLUGIN), str(ROOT / "sdk")]

from source_import.adapters.txt import TxtDecodeError, decode_txt
from source_import.contract import verify_self_hashed
from source_import.importer.epub import EpubError, parse_epub
from source_import.pipeline import parse_source
from source_import.structure import build_structure_evidence
from source_import.worker import ImportCancelled, SourceImportPlugin, WorkerError, read_core_asset
from source_import.package_identity import calculate_identity


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def child_pythonpath(*roots: str | Path) -> str:
    parts = [str(root) for root in roots]
    inherited = os.environ.get("PYTHONPATH")
    if inherited:
        parts.append(inherited)
    return os.pathsep.join(parts)


def test_package_identity_and_closed_descriptors() -> None:
    digest = calculate_identity()
    expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
    assert digest.package_hash == expected["package_hash"]
    assert digest.release_id == expected["release_id"]
    assert expected["package_files"] == sorted(expected["package_files"], key=lambda value: value.encode("utf-8"))
    assert "descriptor.json" not in expected["files_sha256"]
    assert "descriptor-parse.json" not in expected["files_sha256"]
    for name, capability in (("descriptor.json", "source.import.inspect/v1"), ("descriptor-parse.json", "source.import.parse/v1")):
        descriptor = json.loads((PLUGIN / name).read_text(encoding="utf-8"))
        assert set(descriptor) == {"schema", "capability_id", "provider", "input_schema", "output_schema", "result_contract", "supports", "deterministic", "accepted_data_formats"}
        assert descriptor["capability_id"] == capability
        assert descriptor["provider"]["release_id"] == expected["release_id"]
        assert descriptor["deterministic"] is True
        assert descriptor["accepted_data_formats"] == []
        assert descriptor["provider"]["plugin_id"] == expected["plugin_id"]
        descriptor_hash_key = "descriptor_sha256" if name == "descriptor.json" else "descriptor_parse_sha256"
        assert sha((PLUGIN / name).read_bytes()) == expected[descriptor_hash_key]

    schema_index_paths = {
        "source.import.inspect/v1": "source_import/schemas/inspect-index.json",
        "source.import.parse/v1": "source_import/schemas/parse-index.json",
    }
    assert expected["schema_index_paths"] == schema_index_paths
    assert "source_import/schemas/index.json" not in expected["package_files"]
    schema_artifacts = {
        "source.import.inspect/v1": [
            {"schema_id": "source.import.inspect-request/v1", "path": "schemas/inspect-input.schema.json", "sha256": sha((PLUGIN / "source_import/schemas/inspect-input.schema.json").read_bytes())},
            {"schema_id": "source.import.inspect-result/v1", "path": "schemas/inspect-output.schema.json", "sha256": sha((PLUGIN / "source_import/schemas/inspect-output.schema.json").read_bytes())},
        ],
        "source.import.parse/v1": [
            {"schema_id": "source.import.parse-request/v1", "path": "schemas/parse-input.schema.json", "sha256": sha((PLUGIN / "source_import/schemas/parse-input.schema.json").read_bytes())},
            {"schema_id": "source.import.parse-result/v1", "path": "schemas/parse-output.schema.json", "sha256": sha((PLUGIN / "source_import/schemas/parse-output.schema.json").read_bytes())},
        ],
    }
    for capability, index_path in schema_index_paths.items():
        index = json.loads((PLUGIN / index_path).read_text(encoding="utf-8"))
        assert set(index) == {"schema", "plugin_id", "capability_id", "schemas"}
        assert index["schema"] == "provider-schema-index/v1"
        assert index["plugin_id"] == expected["plugin_id"]
        assert index["capability_id"] == capability
        assert index["schemas"] == schema_artifacts[capability]
        assert all(f"{expected['wheel_import']}/{item['path']}" in expected["package_files"] for item in index["schemas"])
    wheel = PLUGIN / expected["wheel_path"]
    with zipfile.ZipFile(wheel) as archive:
        assert archive.testzip() is None
        assert "source_import/worker.py" in archive.namelist()
        assert "source_import/identity.json" not in archive.namelist()
        assert "source_import/schemas/inspect-index.json" in archive.namelist()
        assert "source_import/schemas/parse-index.json" in archive.namelist()


def test_pep517_hook_matches_canonical_rebuild() -> None:
    from source_import.build_backend import build_wheel
    expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as wheel_dir:
        wheel_name = build_wheel(wheel_dir)
        built = Path(wheel_dir) / wheel_name
        checked_in = PLUGIN / expected["wheel_path"]
        assert built.read_bytes() == checked_in.read_bytes()
        with zipfile.ZipFile(built) as archive:
            assert "source_import/identity.json" not in archive.namelist()


def test_wheel_install_identity() -> None:
    expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
    outer = json.loads((PLUGIN / "source_import" / "identity.json").read_text(encoding="utf-8"))
    wheel = PLUGIN / expected["wheel_path"]
    with zipfile.ZipFile(wheel) as archive:
        assert "source_import/identity.json" not in archive.namelist()
        with tempfile.TemporaryDirectory() as install_dir:
            archive.extractall(install_dir)
            shutil.copyfile(PLUGIN / "source_import" / "identity.json", Path(install_dir) / "source_import" / "identity.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = child_pythonpath(install_dir, ROOT / "sdk")
            code = r"""
import json
from source_import.contract import build_provenance_receipt, hash_jcs
from source_import.package_identity import load_runtime_identity
from source_import.worker import SourceImportPlugin, _bundle, capability_descriptor
plugin = SourceImportPlugin()
request = {
    "worker_run_id": "run-1", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1",
    "lease_epoch": 1, "run_snapshot_hash": "a" * 64, "provenance_receipt_id": "receipt-1",
    "created_at": "2026-08-28T00:00:00Z",
}
bundle = _bundle(request, "source.import.inspect/v1", plugin.release_id, "artifact-bundle/v1", "artifact", "bundle-1", [{
    "schema": "artifact-item/v1", "item_id": "item-1", "artifact_kind": "source-canonical-text",
    "payload_asset_id": "asset-1", "payload_hash": "b" * 64, "mime": "text/plain; charset=utf-8",
    "source_refs": [{"workspace_id": None, "source_type": "asset", "source_id": "asset-source", "revision_or_hash": "c" * 64}],
    "status": "complete",
}])
receipt = build_provenance_receipt(
    request, capability_id="source.import.inspect/v1", release_id=plugin.release_id,
    package_hash=plugin.package_hash, bundle_id="bundle-1", bundle_hash=hash_jcs("result-bundle/v1", bundle),
)
print(json.dumps({
    "identity": load_runtime_identity(), "descriptor_inspect": capability_descriptor("source.import.inspect/v1"),
    "descriptor_parse": capability_descriptor("source.import.parse/v1"), "producer": bundle["producer"],
    "receipt": receipt,
}, sort_keys=True))
"""
            result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, check=True)
    installed = json.loads(result.stdout)
    assert installed["identity"] == outer
    assert installed["identity"]["package_hash"] == expected["package_hash"]
    assert installed["identity"]["release_id"] == expected["release_id"]
    assert installed["descriptor_inspect"]["provider"] == {"plugin_id": expected["plugin_id"], "release_id": expected["release_id"]}
    assert installed["descriptor_parse"]["provider"] == {"plugin_id": expected["plugin_id"], "release_id": expected["release_id"]}
    assert installed["producer"]["release_id"] == expected["release_id"]
    assert installed["receipt"]["release_id"] == expected["release_id"]
    assert installed["receipt"]["package_hash"] == expected["package_hash"]


def test_installed_wheel_identity_tamper_and_missing_fail_closed() -> None:
    expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
    wheel = PLUGIN / expected["wheel_path"]
    code = "from source_import.package_identity import load_runtime_identity; load_runtime_identity()"
    with zipfile.ZipFile(wheel) as archive:
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing"
            archive.extractall(missing)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(missing)
            result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True)
            assert result.returncode != 0
            assert "package identity is unavailable" in result.stderr
            tampered = Path(root) / "tampered"
            archive.extractall(tampered)
            identity_path = tampered / "source_import" / "identity.json"
            identity_path.write_text(json.dumps({
                "schema": "source-import-package-identity/v1", "plugin_id": "com.example.tampered",
                "version": "0.1.0", "package_hash": expected["package_hash"], "release_id": expected["release_id"],
            }), encoding="utf-8")
            env["PYTHONPATH"] = str(tampered)
            result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True)
            assert result.returncode != 0
            assert "package identity" in result.stderr
            bundle_copy = Path(root) / "bundle"
            shutil.copytree(PLUGIN, bundle_copy)
            bundle_identity = bundle_copy / "source_import" / "identity.json"
            invalid = json.loads(bundle_identity.read_text(encoding="utf-8"))
            invalid["release_id"] = "0" * 64
            bundle_identity.write_text(json.dumps(invalid), encoding="utf-8")
            with pytest.raises(RuntimeError, match="does not match its canonical payload"):
                calculate_identity(bundle_copy)


def test_installed_wheel_fails_closed_without_sdk() -> None:
    expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
    wheel = PLUGIN / expected["wheel_path"]
    with zipfile.ZipFile(wheel) as archive:
        with tempfile.TemporaryDirectory() as install_dir:
            archive.extractall(install_dir)
            shutil.copyfile(PLUGIN / "source_import" / "identity.json", Path(install_dir) / "source_import" / "identity.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = install_dir
            code = "from source_import.worker import SourceImportPlugin; SourceImportPlugin().run({}, None)"
            result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "SDK_UNAVAILABLE" in result.stderr


def test_txt_lossless_profiles_and_unicode_offsets() -> None:
    utf8 = decode_txt((FIXTURES / "txt_utf8_bom_crlf.txt").read_bytes())
    assert utf8.encoding == "utf-8"
    assert utf8.had_bom is True
    assert utf8.replaced is False
    assert "\r\n" in utf8.text
    assert "emoji 😀" in utf8.text
    assert "e\u0301" in utf8.text
    gb = decode_txt((FIXTURES / "txt_gb18030.txt").read_bytes())
    assert gb.encoding == "gb18030"
    assert "中文正文" in gb.text
    paste = decode_txt((FIXTURES / "paste_utf8.txt").read_bytes(), source_kind="paste")
    assert paste.text.endswith("e\u0301\n")
    with pytest.raises(TxtDecodeError):
        decode_txt((FIXTURES / "binary.txt").read_bytes())
    with pytest.raises(TxtDecodeError):
        decode_txt((FIXTURES / "invalid_utf8.txt").read_bytes())
    with pytest.raises(TxtDecodeError):
        decode_txt((FIXTURES / "zip_masquerade.txt").read_bytes())
    with pytest.raises(TxtDecodeError):
        decode_txt("中文".encode("gb18030"), source_kind="paste")
    text = "😀e\u0301\r\n第二章\n"
    evidence = build_structure_evidence(text, source_asset_id="asset-source", source_asset_hash="a" * 64)
    assert evidence["coordinate_system"] == "python-unicode-codepoint/v1"
    assert evidence["canonical_text_hash"] == sha(text.encode("utf-8"))
    for span in evidence["ordered_spans"]:
        assert text[span["start_codepoint"]:span["end_codepoint"]] == span["quote"]
        assert span["quote_hash"] == sha(span["quote"].encode("utf-8"))
    assert len(text) == 9


def test_epub_order_decode_nav_and_no_repack() -> None:
    raw = (FIXTURES / "ordered.epub").read_bytes()
    original_hash = sha(raw)
    parsed = parse_epub(raw)
    assert sha(raw) == original_hash
    assert parsed.encoding_evidence == "utf-8@default,utf-8@xml"
    assert "第1卷 卷一·起风" in parsed.text
    assert "第1章　开端" in parsed.text
    assert "第2章　转折" in parsed.text
    assert "第2卷 卷二·落雨" in parsed.text
    assert "第1章　尾声" in parsed.text
    assert "目录" not in parsed.text
    assert "　少年推开门" in parsed.text
    assert "\ufffd" not in parsed.text
    declared = parse_epub((FIXTURES / "declared_gb18030.epub").read_bytes())
    assert declared.encoding_evidence == "gb18030@xml"
    assert "中文正文没有经过替换" in declared.text
    nav = parse_epub((FIXTURES / "nav_relative.epub").read_bytes())
    assert "第1章　导航给出的章名" in nav.text
    assert "　标准 EPUB3 导航正文" in nav.text


def test_epub_normal_directory_entries_are_skipped_safely() -> None:
    parsed = parse_epub((FIXTURES / "epub_directories.epub").read_bytes())
    assert "目录项安全" in parsed.text
    assert "显式目录项不应阻止 EPUB 导入" in parsed.text


@pytest.mark.parametrize("name", ["epub_traversal.epub", "epub_bad_xml.epub", "epub_invalid_bytes.epub", "epub_replacement.epub", "epub_bomb.epub"])
def test_epub_malicious_and_bounded_inputs(name: str) -> None:
    with pytest.raises((EpubError, ValueError)):
        parse_epub((FIXTURES / name).read_bytes())


def test_epub_resource_limit_is_deterministic() -> None:
    from source_import.limits import ImportLimits, ImportLimitError
    with pytest.raises((ImportLimitError, EpubError)):
        parse_epub((FIXTURES / "ordered.epub").read_bytes(), limits=ImportLimits(max_zip_compression_ratio=1.0))


def test_epub_text_limit_rejects_during_decode_and_format() -> None:
    from source_import.limits import ImportLimits, ImportLimitError
    with pytest.raises(ImportLimitError):
        parse_epub((FIXTURES / "ordered.epub").read_bytes(), limits=ImportLimits(max_text_characters=64))


class FakeHost:
    def __init__(self, assets: dict[str, bytes], *, page_limit: int = 7) -> None:
        self.assets = dict(assets)
        self.page_limit = page_limit
        self.uploads: dict[str, bytearray] = {}
        self.upload_meta: dict[str, tuple[str, int, str]] = {}
        self.asset_ids: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.read_calls = 0
        self.next_asset = 0
        self.last_checkpoint_asset: str | None = None
        self.last_stage: dict[str, object] | None = None
        self.last_stage_response: dict[str, object] | None = None
        self.complete_calls: list[dict[str, object]] = []
        self.last_complete: dict[str, object] | None = None

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        if method == "host.asset.read/v1":
            asset_id = str(params["asset_id"])
            raw = self.assets[asset_id]
            offset = int(params["offset"])
            length = min(int(params["length"]), self.page_limit)
            chunk = raw[offset:offset + length]
            self.read_calls += 1
            next_offset = None if offset + len(chunk) == len(raw) else offset + len(chunk)
            return {"base64_chunk": base64.b64encode(chunk).decode("ascii"), "next_offset": next_offset, "content_hash": sha(chunk)}
        if method == "host.asset.create/v1":
            upload_id = str(params["upload_id"])
            offset = int(params["offset"])
            data = base64.b64decode(str(params["base64_chunk"]), validate=True)
            assert sha(data) == params["chunk_hash"]
            buf = self.uploads.setdefault(upload_id, bytearray())
            assert offset == len(buf)
            buf.extend(data)
            final = bool(params["final"])
            if final:
                expected = str(params["expected_hash"])
                assert sha(bytes(buf)) == expected
                asset_id = f"core-asset-{sha(upload_id.encode('utf-8'))[:16]}"
                self.assets[asset_id] = bytes(buf)
                self.asset_ids[upload_id] = asset_id
                return {"upload_id": upload_id, "accepted_bytes": len(buf), "completed": True, "asset_id": asset_id}
            return {"upload_id": upload_id, "accepted_bytes": len(buf), "completed": False, "asset_id": None}
        if method == "host.asset.upload.status/v1":
            upload_id = str(params["upload_id"])
            asset_id = self.asset_ids[upload_id]
            return {"accepted_bytes": len(self.uploads[upload_id]), "completed": True, "asset_id": asset_id}
        if method == "host.job.event/v1":
            return {"accepted": True, "job_event_seq": len([item for item in self.calls if item[0] == method])}
        if method == "host.checkpoint.commit/v1":
            self.last_checkpoint_asset = str(params["checkpoint_asset_id"])
            return {"accepted": True, "checkpoint_id": "checkpoint-1", "completed_units": 0, "total_units": None, "job_event_seq": 1}
        if method == "host.candidate.stage/v1":
            self.last_stage = dict(params)
            bundle = json.loads(self.assets[str(params["result_bundle_asset_id"])].decode("utf-8"))
            rows = [
                {
                    "item_id": item["item_id"], "candidate_id": f"candidate-{index}",
                    "stage_status": "created", "publication_eligibility": "review_only",
                }
                for index, item in enumerate(bundle["items"])
            ]
            self.last_stage_response = {"accepted": True, "staged_items": rows, "job_event_seq": 2}
            return dict(self.last_stage_response)
        if method == "host.job.complete/v1":
            self.last_complete = dict(params)
            self.complete_calls.append(dict(params))
            return {
                "accepted": True, "attempt_state": params["outcome"], "step_state": params["outcome"],
                "job_state": params["outcome"], "provenance_receipt_id": "receipt-1",
                "job_event_seq": 3, "core_event_high_water": 3,
            }
        raise AssertionError(f"unexpected RPC: {method}")


def test_asset_pages_are_contiguous_and_hash_bound() -> None:
    raw = b"0123456789abcdef"
    host = FakeHost({"asset-source": raw}, page_limit=4)
    result = read_core_asset(host, "asset-source", sha(raw), page_size=4)
    assert result.data == raw
    assert result.next_offset is None
    assert len(result.page_hashes) == 4
    bad = FakeHost({"asset-source": raw}, page_limit=4)
    original = bad.call
    def tamper(method: str, params: dict[str, object]) -> dict[str, object]:
        result = original(method, params)
        if method == "host.asset.read/v1" and bad.read_calls == 1:
            result["content_hash"] = "0" * 64
        return result
    bad.call = tamper  # type: ignore[method-assign]
    with pytest.raises(Exception, match="content_hash"):
        read_core_asset(bad, "asset-source", sha(raw), page_size=4)


def test_rpc_result_is_frozen_and_sdk_checked() -> None:
    raw = b"rpc-boundary"
    host = FakeHost({"asset-source": raw}, page_limit=4)
    original = host.call
    def extra_field(method: str, params: dict[str, object]) -> dict[str, object]:
        result = original(method, params)
        if method == "host.asset.read/v1":
            result["offset"] = params["offset"]
        return result
    host.call = extra_field  # type: ignore[method-assign]
    with pytest.raises(WorkerError, match="public RPC schema"):
        read_core_asset(host, "asset-source", sha(raw), page_size=4)


def _request(capability: str, source: bytes, *, kind: str = "paste", **extra: object) -> dict[str, object]:
    capability = capability if capability.endswith("/v1") else capability + "/v1"
    request: dict[str, object] = {
        "schema": {
            "source.import.inspect/v1": "source.import.inspect-request/v1",
            "source.import.parse/v1": "source.import.parse-request/v1",
        }[capability], "capability_id": capability,
        "worker_run_id": "run-1", "job_id": "job-1", "step_id": "step-1",
        "attempt_id": "attempt-1", "lease_epoch": 1, "run_snapshot_hash": "a" * 64,
        "source_asset_id": "asset-source", "source_asset_hash": sha(source),
        "source_kind": kind, "source_name": "fixture.txt",
        "provenance_receipt_id": "receipt-1", "created_at": "2026-08-28T00:00:00Z",
        "page_size": 4,
    }
    request.update(extra)
    return request


@pytest.mark.parametrize(("capability", "bad_schema"), [
    ("source.import.inspect/v1", "source.import.parse-request/v1"),
    ("source.import.parse/v1", "source.import.inspect-request/v1"),
    ("source.import.inspect/v1", "source.import.inspect/v1-request/v1"),
    ("source.import.parse/v1", "source.import.parse/v1-request/v1"),
])
def test_request_schema_mapping_and_invalid_invocation_fails_closed(capability: str, bad_schema: str) -> None:
    source = b"schema-negative"
    host = FakeHost({"asset-source": source}, page_limit=4)
    request = _request(capability, source)
    request["schema"] = bad_schema
    result = SourceImportPlugin().run(request, host)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INPUT_INVALID"
    assert host.read_calls == 0
    assert host.last_complete is not None
    assert host.last_complete["outcome"] == "failed"


def test_inspect_parse_candidate_and_receipts() -> None:
    source = "第一章\n正文 😀\n".encode("utf-8")
    host = FakeHost({"asset-source": source}, page_limit=4)
    plugin = SourceImportPlugin()
    inspected = plugin.inspect(_request("source.import.inspect", source), host)
    assert inspected["status"] == "succeeded"
    assert inspected["result"]["contract_id"] == "artifact-bundle/v1"
    assert len(inspected["result"]["items"]) == 2
    for receipt in inspected["result"]["items"]:
        assert receipt["payload_asset_id"].startswith("core-asset-")
    for receipt in (inspected["receipt"],):
        verify_self_hashed(receipt, field="receipt_hash", prefix="provenance-receipt/v1")
    parsed = plugin.parse(_request(
        "source.import.parse", source, target={"workspace_id": "workspace-1", "entity_kind": "document", "entity_id": "document-1"},
        base={"revision_id": "revision-1", "content_hash": "b" * 64},
        structure_target={"workspace_id": "workspace-1", "entity_kind": "node_structure", "entity_id": "structure-1"},
        structure_base={"revision_id": "revision-2", "content_hash": "c" * 64},
    ), host)
    assert parsed["status"] == "succeeded"
    bundle = parsed["result"]
    assert bundle["contract_id"] == "candidate-batch/v1"
    assert len(bundle["items"]) == 2
    assert bundle["items"][0]["item_kind"] == "document"
    assert bundle["items"][1]["item_kind"] == "node_structure"
    assert bundle["items"][1]["parent_candidate_ids"] == [bundle["items"][0]["item_id"]]
    assert all(item["payload_asset_id"].startswith("core-asset-") for item in bundle["items"])
    assert all("revision_id" in item["base"] for item in bundle["items"])
    verify_self_hashed(parsed["receipt"], field="receipt_hash", prefix="provenance-receipt/v1")
    assert parsed["receipt"]["staged_items"] == [item["item_id"] for item in bundle["items"]]
    assert not any("publication" in str(value).casefold() for value in bundle.values())
    assert host.last_stage is not None
    assert host.last_stage_response is not None
    assert [row["item_id"] for row in host.last_stage_response["staged_items"]] == [item["item_id"] for item in bundle["items"]]
    assert plugin.last_stage_response == host.last_stage_response
    from plotpilot_plugin_sdk.verifier import verify_provenance_receipt, verify_result_bundle
    verify_result_bundle(bundle, snapshot_workspace_id="workspace-1", snapshot_hash_value="a" * 64)
    verify_provenance_receipt(parsed["receipt"])
    assert any(method == "host.job.event/v1" for method, _ in host.calls)
    assert any(method == "host.job.complete/v1" for method, _ in host.calls)
    assert any(method == "host.candidate.stage/v1" for method, _ in host.calls)
    assert any(method == "host.asset.upload.status/v1" for method, _ in host.calls)


def test_cancel_checkpoint_and_resume_state() -> None:
    source = ("第一章\n" + ("正文\n" * 10)).encode("utf-8")

    class BlockingHost(FakeHost):
        def __init__(self, assets: dict[str, bytes], *, page_limit: int = 4) -> None:
            super().__init__(assets, page_limit=page_limit)
            self.entered = threading.Event()
            self.release = threading.Event()
            self.blocked = False

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "host.asset.read/v1" and not self.blocked:
                self.blocked = True
                self.entered.set()
                assert self.release.wait(5)
            return super().call(method, params)

    host = BlockingHost({"asset-source": source})
    request = _request("source.import.inspect", source, checkpoint_id="checkpoint-1", checkpoint_ids=["checkpoint-1"])
    plugin = SourceImportPlugin()
    holder: dict[str, object] = {}

    def run_active() -> None:
        holder["result"] = plugin.inspect(request, host)

    thread = threading.Thread(target=run_active)
    thread.start()
    assert host.entered.wait(5)
    assert SourceImportPlugin().cancel("run-1")["accepted"] is True
    host.release.set()
    thread.join(5)
    assert not thread.is_alive()
    cancelled = holder["result"]
    assert cancelled["status"] == "cancelled"
    checkpoint = cancelled["checkpoint"]
    assert checkpoint is not None
    assert checkpoint["checkpoint"]["state_asset_id"].startswith("core-asset-")
    assert checkpoint["checkpoint_asset_hash"] == sha(host.assets[checkpoint["checkpoint_asset_id"]])
    assert host.read_calls == 1
    assert host.last_complete is not None and host.last_complete["outcome"] == "cancelled"
    assert any(method == "host.checkpoint.commit/v1" for method, _ in host.calls)

    resumed_request = _request(
        "source.import.inspect", source, checkpoint_id="checkpoint-1", checkpoint_ids=["checkpoint-1"],
        resume_checkpoint_asset_id=checkpoint["checkpoint_asset_id"],
        resume_checkpoint_asset_hash=checkpoint["checkpoint_asset_hash"],
        resume_state_asset_id=checkpoint["state_asset_id"],
        resume_state_asset_hash=checkpoint["state_asset_hash"],
    )
    resumed = SourceImportPlugin().inspect(resumed_request, host)
    assert resumed["status"] == "succeeded"
    assert resumed["read"]["next_offset"] is None
    assert host.last_complete is not None and host.last_complete["outcome"] == "succeeded"

    baseline_host = FakeHost({"asset-source": source})
    baseline = SourceImportPlugin().inspect(request, baseline_host)
    assert baseline["status"] == "succeeded"
    assert resumed["result"] == baseline["result"]
    result_upload = next(params["upload_id"] for method, params in host.calls if method == "host.asset.create/v1" and str(params["upload_id"]).endswith("-result"))
    baseline_upload = next(params["upload_id"] for method, params in baseline_host.calls if method == "host.asset.create/v1" and str(params["upload_id"]).endswith("-result"))
    assert host.assets[host.asset_ids[str(result_upload)]] == baseline_host.assets[baseline_host.asset_ids[str(baseline_upload)]]

    tampered_checkpoint_host = FakeHost(host.assets)
    tampered_checkpoint_host.assets[checkpoint["checkpoint_asset_id"]] = b"{}"
    rejected_checkpoint = SourceImportPlugin().inspect(resumed_request, tampered_checkpoint_host)
    assert rejected_checkpoint["status"] == "failed"
    assert rejected_checkpoint["error"]["code"] in {"ASSET_READ_ERROR", "CHECKPOINT_INVALID"}
    assert tampered_checkpoint_host.last_complete is not None
    assert tampered_checkpoint_host.last_complete["outcome"] == "failed"

    tampered_state_host = FakeHost(host.assets)
    tampered_state_host.assets[checkpoint["state_asset_id"]] = b"{}"
    state_only_request = _request(
        "source.import.inspect", source, checkpoint_id="checkpoint-1", checkpoint_ids=["checkpoint-1"],
        resume_state_asset_id=checkpoint["state_asset_id"], resume_state_asset_hash=checkpoint["state_asset_hash"],
    )
    rejected_state = SourceImportPlugin().inspect(state_only_request, tampered_state_host)
    assert rejected_state["status"] == "failed"
    assert rejected_state["error"]["code"] in {"ASSET_READ_ERROR", "CHECKPOINT_INVALID"}
    assert tampered_state_host.last_complete is not None
    assert tampered_state_host.last_complete["outcome"] == "failed"


@pytest.mark.parametrize(("capability", "kind", "source_name", "cancel_module", "gate_call"), [
    ("source.import.inspect", "txt", "parse-loop.txt", "structure", 3),
    ("source.import.parse", "txt", "parse-loop.txt", "structure", 3),
    ("source.import.parse", "epub", "parse-loop.epub", "epub", 2),
])
def test_parse_loop_cancel_checkpoint_resume_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    kind: str,
    source_name: str,
    cancel_module: str,
    gate_call: int,
) -> None:
    source = (
        ("第一章\n" + "正文 😀\n" * 40).encode("utf-8")
        if kind == "txt"
        else (FIXTURES / "ordered.epub").read_bytes()
    )

    class ParseLoopHost(FakeHost):
        def __init__(self, assets: dict[str, bytes]) -> None:
            super().__init__(assets, page_limit=128)
            self.source_eof_returned = threading.Event()

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            response = super().call(method, params)
            if (
                method == "host.asset.read/v1"
                and params["asset_id"] == "asset-source"
                and response["next_offset"] is None
            ):
                self.source_eof_returned.set()
            return response

    host = ParseLoopHost({"asset-source": source})
    parse_loop_entered = threading.Event()
    release_parse_loop = threading.Event()
    gate_state = {"calls": 0, "used": False}
    if cancel_module == "epub":
        from source_import.importer import epub as parser_module
    else:
        from source_import import structure as parser_module
    original_check = parser_module._check_cancel

    def gated_check(cancel_check: object) -> None:
        gate_state["calls"] += 1
        if (
            not gate_state["used"]
            and gate_state["calls"] >= gate_call
            and host.source_eof_returned.is_set()
        ):
            gate_state["used"] = True
            parse_loop_entered.set()
            assert release_parse_loop.wait(5)
        original_check(cancel_check)

    monkeypatch.setattr(parser_module, "_check_cancel", gated_check)
    request_extra: dict[str, object] = {
        "checkpoint_id": "checkpoint-1",
        "checkpoint_ids": ["checkpoint-1"],
        "source_name": source_name,
        "page_size": 128,
    }
    if capability == "source.import.parse":
        request_extra.update({
            "target": {"workspace_id": "workspace-1", "entity_kind": "document", "entity_id": "document-1"},
            "base": {"revision_id": "revision-1", "content_hash": "b" * 64},
        })
    request = _request(capability, source, kind=kind, **request_extra)
    plugin = SourceImportPlugin()
    holder: dict[str, object] = {}

    def run_active() -> None:
        try:
            method = plugin.parse if capability == "source.import.parse" else plugin.inspect
            holder["result"] = method(request, host)
        except BaseException as exc:  # pragma: no cover - surfaced below with its original repr
            holder["error"] = exc

    thread = threading.Thread(target=run_active)
    thread.start()
    assert host.source_eof_returned.wait(5)
    assert parse_loop_entered.wait(5)
    assert SourceImportPlugin().cancel("run-1")["accepted"] is True
    release_parse_loop.set()
    thread.join(10)
    assert not thread.is_alive()
    assert "error" not in holder, repr(holder.get("error"))
    cancelled = holder["result"]
    assert cancelled["status"] == "cancelled"
    checkpoint = cancelled["checkpoint"]
    assert checkpoint is not None
    state = json.loads(host.assets[checkpoint["state_asset_id"]].decode("utf-8"))
    expected_pages = max(1, (len(source) + 127) // 128)
    assert state["phase"] == "source_parse"
    assert state["asset_eof"] is True
    assert state["capability_id"] == request["capability_id"]
    assert state["run_snapshot_hash"] == request["run_snapshot_hash"]
    assert state["source_asset_id"] == "asset-source"
    assert state["source_asset_hash"] == sha(source)
    assert state["next_offset"] == state["prefix_size"] == len(source)
    assert state["page_index"] == expected_pages
    assert host.assets[state["prefix_asset_id"]] == source
    assert host.last_complete is not None and host.last_complete["outcome"] == "cancelled"
    assert host.last_checkpoint_asset == checkpoint["checkpoint_asset_id"]
    assert cancelled["read"]["next_offset"] == len(source)

    source_reads_before_resume = len([
        1 for method, params in host.calls
        if method == "host.asset.read/v1" and params["asset_id"] == "asset-source"
    ])
    resumed_request = _request(
        capability, source, kind=kind, **request_extra,
        resume_checkpoint_asset_id=checkpoint["checkpoint_asset_id"],
        resume_checkpoint_asset_hash=checkpoint["checkpoint_asset_hash"],
        resume_state_asset_id=checkpoint["state_asset_id"],
        resume_state_asset_hash=checkpoint["state_asset_hash"],
    )
    method = SourceImportPlugin().parse if capability == "source.import.parse" else SourceImportPlugin().inspect
    resumed = method(resumed_request, host)
    assert resumed["status"] == "succeeded"
    source_reads_after_resume = len([
        1 for called_method, params in host.calls
        if called_method == "host.asset.read/v1" and params["asset_id"] == "asset-source"
    ])
    assert source_reads_after_resume == source_reads_before_resume

    baseline_host = FakeHost({"asset-source": source}, page_limit=128)
    baseline_plugin = SourceImportPlugin()
    baseline_method = baseline_plugin.parse if capability == "source.import.parse" else baseline_plugin.inspect
    baseline = baseline_method(request, baseline_host)
    assert baseline["status"] == "succeeded"
    assert resumed["result"] == baseline["result"]
    assert resumed["receipt"] == baseline["receipt"]
    result_upload = next(
        str(params["upload_id"]) for called_method, params in host.calls
        if called_method == "host.asset.create/v1" and str(params["upload_id"]).endswith("-result")
    )
    baseline_upload = next(
        str(params["upload_id"]) for called_method, params in baseline_host.calls
        if called_method == "host.asset.create/v1" and str(params["upload_id"]).endswith("-result")
    )
    assert host.assets[host.asset_ids[result_upload]] == baseline_host.assets[baseline_host.asset_ids[baseline_upload]]
    if capability == "source.import.parse":
        assert resumed["receipt"]["staged_items"] == [item["item_id"] for item in resumed["result"]["items"]]


def test_stage_rows_tamper_is_rejected() -> None:
    source = b"Chapter\nbody\n"

    class TamperedStageHost(FakeHost):
        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            response = super().call(method, params)
            if method == "host.candidate.stage/v1":
                response["staged_items"][0]["item_id"] = "candidate-not-emitted"
            return response

    host = TamperedStageHost({"asset-source": source}, page_limit=4)
    request = _request(
        "source.import.parse", source,
        target={"workspace_id": "workspace-1", "entity_kind": "document", "entity_id": "document-1"},
        base={"revision_id": "revision-1", "content_hash": "b" * 64},
    )
    result = SourceImportPlugin().parse(request, host)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "CANDIDATE_STAGE_ERROR"
    assert host.last_complete is not None and host.last_complete["outcome"] == "failed"
