"""Typed, validation-only DTO surface for the additive Core API families.

This module contains no Core database handle and no callable that can perform
Publication.  It is shared by P1 adapter tests and P4 client fixtures; plugin
workers remain limited to the frozen framed RPC surface.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, TypedDict

from .canonical import canonical_bytes, parse_json_bytes, sha256_hex
from .errors import ContractValidationError
from ._workspace import WORKSPACE_ROOT
from .verifier import assert_valid, verify_snapshot


ROOT = WORKSPACE_ROOT
CORE_API_MATRIX = json.loads(
    (ROOT / "contracts" / "json-schema" / "core-api-method-matrix.v1.json").read_text(encoding="utf-8")
)


class WorkspaceDto(TypedDict):
    schema: Literal["core-workspace/v1"]
    workspace_id: str
    workspace_kind: str
    title: str
    status: str
    current_plan_revision_id: str | None
    created_at: str
    updated_at: str
    revision: int


class DocumentDto(TypedDict):
    schema: Literal["core-document/v1"]
    document_id: str
    workspace_id: str
    document_type: str
    title: str
    current_revision_id: str | None
    created_at: str
    updated_at: str
    revision: int


class NodeDto(TypedDict):
    schema: Literal["core-node/v1"]
    node_id: str
    workspace_id: str
    document_id: str | None
    node_type: str
    title: str
    parent_node_id: str | None
    position: int
    current_revision_id: str | None
    created_at: str
    updated_at: str
    revision: int


class RelationDto(TypedDict):
    schema: Literal["core-relation/v1"]
    relation_id: str
    workspace_id: str
    relation_type: str
    source_id: str
    target_id: str
    revision_id: str | None
    created_at: str


class RevisionDto(TypedDict):
    schema: Literal["core-revision/v1"]
    revision_id: str
    workspace_id: str
    document_id: str | None
    node_id: str | None
    parent_revision_id: str | None
    content_hash: str
    created_by: str
    source_candidate_id: str | None
    created_at: str
    revision_number: int
    payload_schema: str | None


class PublicationCommand(TypedDict):
    schema: Literal["publication-command/v1"]
    publication_operation_key: str
    workspace_id: str
    candidate_id: str
    accepted_by: str


class PublicationRevisionRef(TypedDict):
    revision_id: str
    workspace_id: str
    entity_kind: Literal["document", "node_structure", "relation_set"]
    entity_id: str
    content_hash: str
    revision_number: int


class PublicationResult(TypedDict):
    schema: Literal["publication-result/v1"]
    publication_id: str
    candidate_id: str
    workspace_id: str
    entity_kind: Literal["document", "node_structure", "relation_set"]
    entity_id: str
    resulting_revision: PublicationRevisionRef
    idempotent: bool


class AssetMetadataDto(TypedDict):
    schema: Literal["asset-metadata/v1"]
    asset_id: str
    sha256: str
    mime: str
    size: int
    logical_role: str
    provenance: str
    rebuildable: bool


class AssetReadRangeDto(TypedDict):
    schema: Literal["asset-read-range/v1"]
    asset_id: str
    offset: int
    length: int
    total_size: int
    base64_chunk: str
    next_offset: int | None
    content_hash: str


class CoreHttpErrorDto(TypedDict):
    schema: Literal["core-http-error/v1"]
    error_code: Literal["unknown_reference", "cross_workspace", "stale_cas", "operation_key_reuse", "incomplete_publication"]
    message: str
    retryable: bool


class WorkspaceGetQuery(TypedDict):
    schema: Literal["core-workspace-get-query/v1"]
    workspace_id: str


class DocumentGetQuery(TypedDict):
    schema: Literal["core-document-get-query/v1"]
    workspace_id: str
    document_id: str


class NodeGetQuery(TypedDict):
    schema: Literal["core-node-get-query/v1"]
    workspace_id: str
    node_id: str


class DocumentRevisionQuery(TypedDict):
    schema: Literal["core-document-revision-query/v1"]
    workspace_id: str
    document_id: str
    revision_id: str | None
    offset: int
    limit: int


class NodeRevisionQuery(TypedDict):
    schema: Literal["core-node-revision-query/v1"]
    workspace_id: str
    node_id: str
    revision_id: str | None
    offset: int
    limit: int


class RevisionGetQuery(TypedDict):
    schema: Literal["core-revision-get-query/v1"]
    workspace_id: str
    revision_id: str


class ExportRevisionItem(TypedDict):
    ordinal: int
    document_id: str
    document_type: str
    title: str
    revision_id: str
    content_asset_id: str
    content_hash: str
    mime: Literal["text/plain"]
    encoding: Literal["utf-8"]


class ExportCurrentRevisions(TypedDict):
    schema: Literal["export-current-revisions/v1"]
    workspace_id: str
    core_snapshot_revision: int
    ordered_revisions: list[ExportRevisionItem]
    generated_at: str


_AUTHORITY_WORKSPACE_PAGES = {
    "core-workspace-page/v1",
    "core-document-page/v1",
    "core-node-page/v1",
    "core-relation-page/v1",
    "core-revision-page/v1",
}


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _page_semantics(value: Mapping[str, Any]) -> None:
    items = value["items"]
    offset = value["offset"]
    total = value["total"]
    expected_next = offset + len(items) if offset + len(items) < total else None
    if value["next_offset"] != expected_next:
        _fail("Core page next_offset does not match offset/items/total")


def parse_core_authority(
    value: Mapping[str, Any],
    *,
    expected_workspace_id: str | None = None,
) -> dict[str, Any]:
    assert_valid("core-authority-command-query/v1", value)
    schema = value["schema"]
    if schema == "core-workspace-update-command/v1" and value["title"] is None and value["status"] is None:
        _fail("workspace update must change title or status")
    if schema == "core-revision/v1" and ((value["document_id"] is None) == (value["node_id"] is None)):
        _fail("Core Revision must target exactly one document or node")
    if schema in _AUTHORITY_WORKSPACE_PAGES:
        _page_semantics(value)
    if schema == "core-revision-content-page/v1":
        text_length = len(value["text"])
        if value["length"] != text_length or value["offset"] + text_length > value["total_length"]:
            _fail("revision content page length/range mismatch")
        expected_next = value["offset"] + text_length if value["offset"] + text_length < value["total_length"] else None
        if value["next_offset"] != expected_next:
            _fail("revision content next_offset mismatch")
    if expected_workspace_id is not None:
        workspace_values: list[str] = []
        if "workspace_id" in value and value["workspace_id"] is not None:
            workspace_values.append(value["workspace_id"])
        for item in value.get("items", []):
            if isinstance(item, Mapping) and item.get("workspace_id") is not None:
                workspace_values.append(item["workspace_id"])
        if any(item != expected_workspace_id for item in workspace_values):
            _fail("Core authority payload crosses workspace identity")
    return copy.deepcopy(dict(value))


def parse_publication(
    value: Mapping[str, Any],
    *,
    expected_workspace_id: str | None = None,
    command: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    assert_valid("publication-command-result/v1", value)
    workspace_id = value["workspace_id"]
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        _fail("Publication crosses workspace identity")
    if value["schema"] == "publication-result/v1":
        revision = value["resulting_revision"]
        if revision["workspace_id"] != workspace_id or revision["entity_kind"] != value["entity_kind"] or revision["entity_id"] != value["entity_id"]:
            _fail("Publication result is not bound to its resulting Revision")
        if command is not None:
            parse_publication(command)
            if command["workspace_id"] != workspace_id or command["candidate_id"] != value["candidate_id"]:
                _fail("Publication result does not match the command")
    return copy.deepcopy(dict(value))


def parse_asset_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    assert_valid("asset-metadata/v1", value)
    if value["schema"] == "asset-read-range/v1":
        try:
            decoded = base64.b64decode(value["base64_chunk"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ContractValidationError("Asset range base64 is invalid") from exc
        if len(decoded) != value["length"]:
            _fail("Asset range decoded length mismatch")
        if hashlib.sha256(decoded).hexdigest() != value["content_hash"]:
            _fail("Asset range content_hash mismatch")
        end = value["offset"] + value["length"]
        if end > value["total_size"]:
            _fail("Asset range exceeds total_size")
        expected_next = end if end < value["total_size"] else None
        if value["next_offset"] != expected_next:
            _fail("Asset range next_offset mismatch")
    return copy.deepcopy(dict(value))


def verify_asset_metadata_range_pair(metadata: Mapping[str, Any], range_value: Mapping[str, Any]) -> None:
    """Bind metadata and a complete bounded range for one immutable Asset."""

    parsed_metadata = parse_asset_contract(metadata)
    parsed_range = parse_asset_contract(range_value)
    if parsed_metadata["schema"] != "asset-metadata/v1" or parsed_range["schema"] != "asset-read-range/v1":
        _fail("Asset pair requires metadata and read-range variants")
    if parsed_metadata["asset_id"] != parsed_range["asset_id"] or parsed_metadata["size"] != parsed_range["total_size"]:
        _fail("Asset metadata/range identity or size mismatch")
    if parsed_range["offset"] == 0 and parsed_range["length"] == parsed_range["total_size"]:
        if parsed_metadata["sha256"] != parsed_range["content_hash"]:
            _fail("complete Asset range hash does not match metadata")


def parse_export_current_revisions(
    value: Mapping[str, Any],
    *,
    expected_workspace_id: str | None = None,
    run_snapshot: Mapping[str, Any] | None = None,
    asset_id: str | None = None,
    asset_sha256: str | None = None,
) -> dict[str, Any]:
    if asset_id is not None or asset_sha256 is not None:
        _fail("object-only Export parsing cannot bind caller-supplied Asset hashes; use verify_export_current_revisions_asset")
    assert_valid("export-current-revisions/v1", value)
    if expected_workspace_id is not None and value["workspace_id"] != expected_workspace_id:
        _fail("Export input crosses workspace identity")
    items = value["ordered_revisions"]
    if [item["ordinal"] for item in items] != list(range(len(items))):
        _fail("Export revision ordinals must be contiguous from zero")
    for field_name in ("document_id", "revision_id", "content_asset_id"):
        values = [item[field_name] for item in items]
        if len(values) != len(set(values)):
            _fail(f"Export revisions contain duplicate {field_name}")
    if run_snapshot is not None:
        verify_snapshot(run_snapshot)
        if run_snapshot["workspace_id"] != value["workspace_id"]:
            _fail("Export input does not belong to the RunSnapshot workspace")
        input_revisions = {
            (item["document_id"], item["revision_id"], item["content_hash"])
            for item in run_snapshot["input_revisions"]
        }
        if len(input_revisions) != len(run_snapshot["input_revisions"]) or len(items) != len(input_revisions):
            _fail("Export revisions are not an exact projection of RunSnapshot input_revisions")
        if any((item["document_id"], item["revision_id"], item["content_hash"]) not in input_revisions for item in items):
            _fail("Export item is not frozen in RunSnapshot input_revisions")
    return copy.deepcopy(dict(value))


def verify_export_current_revisions_asset(
    raw_asset_bytes: bytes | bytearray | memoryview,
    run_snapshot: Mapping[str, Any],
    *,
    asset_id: str | None = None,
) -> dict[str, Any]:
    """Verify an export from the immutable Asset bytes, never from a caller hash.

    The RunSnapshot is verified first, then its ``parameters_asset_id`` and
    ``asset_hashes`` provide the only accepted Asset binding.  The bytes must
    be strict UTF-8 RFC 8785 JSON: BOMs, duplicate keys, whitespace and other
    non-canonical encodings are rejected before the closed export semantics
    are evaluated.
    """

    if not isinstance(raw_asset_bytes, (bytes, bytearray, memoryview)):
        _fail("Export Asset must be raw bytes")
    raw = bytes(raw_asset_bytes)
    from .verifier import verify_snapshot as _verify_snapshot

    _verify_snapshot(run_snapshot)
    expected_asset_id = run_snapshot.get("parameters_asset_id")
    if not isinstance(expected_asset_id, str):
        _fail("RunSnapshot has no export parameters Asset")
    if asset_id is not None and asset_id != expected_asset_id:
        _fail("Export Asset is not RunSnapshot parameters_asset_id")
    assets = {
        item["asset_id"]: item["sha256"]
        for item in run_snapshot.get("asset_hashes", [])
        if isinstance(item, Mapping) and isinstance(item.get("asset_id"), str)
    }
    expected_hash = assets.get(expected_asset_id)
    if not isinstance(expected_hash, str):
        _fail("Export Asset hash is absent from RunSnapshot asset_hashes")
    actual_hash = sha256_hex(raw)
    if actual_hash != expected_hash:
        _fail("Export Asset bytes do not match the RunSnapshot hash")
    parsed = parse_json_bytes(raw)
    if not isinstance(parsed, Mapping):
        _fail("Export Asset must contain a JSON object")
    if canonical_bytes(parsed) != raw:
        _fail("Export Asset JSON must be exact RFC 8785 JCS bytes")
    return parse_export_current_revisions(
        parsed,
        expected_workspace_id=run_snapshot.get("workspace_id"),
        run_snapshot=run_snapshot,
    )


def _parse_by_schema(value: Mapping[str, Any]) -> dict[str, Any]:
    schema = value.get("schema")
    if isinstance(schema, str) and schema.startswith("core-"):
        return parse_core_authority(value)
    if schema in {"publication-command/v1", "publication-result/v1"}:
        return parse_publication(value)
    if schema in {"asset-query/v1", "asset-read-range-query/v1", "asset-metadata/v1", "asset-read-range/v1"}:
        return parse_asset_contract(value)
    _fail(f"unsupported Core HTTP fixture schema: {schema}")


@dataclass
class CoreHttpContractFixture:
    """Typed P1/P4 fake keyed by the P0-owned HTTP method matrix."""

    exchanges: dict[str, list[tuple[bytes, int, dict[str, Any]]]] = field(default_factory=dict)

    @staticmethod
    def _route(route_id: str) -> dict[str, Any]:
        route = next((item for item in CORE_API_MATRIX["routes"] if item["route_id"] == route_id), None)
        if route is None:
            _fail(f"unknown Core HTTP route fixture: {route_id}")
        return route

    @classmethod
    def _parse_request(cls, route_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        route = cls._route(route_id)
        placeholders = tuple(re.findall(r"\{([^{}]+)\}", route["path_template"]))
        if tuple(route.get("path_identity", [])) != placeholders:
            _fail(f"Core HTTP route path identity is not bound to its path template: {route_id}")
        if request.get("schema") != route["request_schema"]:
            _fail("Core HTTP fixture is not bound to the route request schema")
        for field_name in route.get("path_identity", []):
            value = request.get(field_name)
            if not isinstance(value, str) or not value:
                _fail(f"Core HTTP path identity {field_name} must be non-empty")
        return _parse_by_schema(request)

    @staticmethod
    def _assert_entity_kind(route: Mapping[str, Any], value: Mapping[str, Any]) -> None:
        expected = route.get("result_entity_kind")
        if expected is None:
            return
        schema = value.get("schema")
        schema_kind = {
            "core-workspace/v1": "workspace",
            "core-document/v1": "document",
            "core-node/v1": "node",
            "core-relation/v1": "relation",
            "core-revision/v1": "revision",
            "core-workspace-page/v1": "workspace",
            "core-document-page/v1": "document",
            "core-node-page/v1": "node",
            "core-relation-page/v1": "relation",
            "core-revision-page/v1": "revision",
            "core-revision-content-page/v1": "revision",
            "asset-metadata/v1": "asset",
            "asset-read-range/v1": "asset",
            "core-delete-result/v1": value.get("entity_kind"),
        }.get(schema)
        if schema_kind != expected:
            _fail(f"Core HTTP route entity kind does not match its result schema: {route['route_id']}")

    @classmethod
    def _parse_response(cls, route_id: str, status: int, response: Mapping[str, Any]) -> dict[str, Any]:
        route = cls._route(route_id)
        if status in route["success_statuses"]:
            if response.get("schema") != route["result_schema"]:
                _fail("Core HTTP fixture is not bound to the route result schema")
            parsed = _parse_by_schema(response)
            cls._assert_entity_kind(route, parsed)
            return parsed
        failures = {
            int(item["status"]): set(item["error_codes"])
            for item in route.get("failure_statuses", [])
        }
        allowed_codes = failures.get(status)
        if route.get("error_schema") != "core-http-error/v1" or allowed_codes is None:
            _fail("Core HTTP fixture status is not declared by the route")
        if response.get("schema") != route["error_schema"]:
            _fail("Core HTTP failure is not bound to the typed error schema")
        parsed = _parse_by_schema(response)
        if parsed.get("error_code") not in allowed_codes:
            _fail("Core HTTP error code is not allowed for this route/status")
        return parsed

    def add(self, route_id: str, request: Mapping[str, Any], status: int, response: Mapping[str, Any]) -> None:
        parsed_request = self._parse_request(route_id, request)
        parsed_response = self._parse_response(route_id, status, response)
        request_bytes = canonical_bytes(parsed_request)
        entries = self.exchanges.setdefault(route_id, [])
        if any(existing[0] == request_bytes for existing in entries):
            _fail("Core HTTP fixture cannot register the same request twice")
        entries.append((request_bytes, status, parsed_response))

    def request(self, route_id: str, request: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if route_id not in self.exchanges:
            _fail(f"Core HTTP route has no fixture response: {route_id}")
        parsed = self._parse_request(route_id, request)
        request_bytes = canonical_bytes(parsed)
        for expected, status, response in self.exchanges[route_id]:
            if request_bytes == expected:
                return status, copy.deepcopy(response)
        _fail("Core HTTP fixture request does not match any frozen exchange")


__all__ = [
    "AssetMetadataDto",
    "AssetReadRangeDto",
    "CoreHttpErrorDto",
    "CoreHttpContractFixture",
    "DocumentGetQuery",
    "DocumentRevisionQuery",
    "DocumentDto",
    "ExportCurrentRevisions",
    "NodeDto",
    "NodeGetQuery",
    "NodeRevisionQuery",
    "PublicationCommand",
    "PublicationResult",
    "RelationDto",
    "RevisionDto",
    "RevisionGetQuery",
    "WorkspaceDto",
    "WorkspaceGetQuery",
    "parse_asset_contract",
    "parse_core_authority",
    "parse_export_current_revisions",
    "verify_export_current_revisions_asset",
    "parse_publication",
    "verify_asset_metadata_range_pair",
]
