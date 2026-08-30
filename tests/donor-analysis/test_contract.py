from __future__ import annotations

import base64
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
PLUGIN_ROOT = ROOT / "plugins" / "donor-analysis"
for path in (SDK_ROOT, PLUGIN_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from donor_analysis import runtime as donor_runtime
from donor_analysis.contract import (
    DonorContractError,
    EvidenceSpanError,
    bind_current_accepted_atoms,
    build_atom_provenance,
    build_evidence_span,
    canonical_json_bytes,
    hash_json,
    sha256_text,
    validate_atom_payload,
    validate_claim_authority,
    validate_claim_input,
    validate_claim_payload,
    validate_evidence_span,
    validate_nodes,
    validate_rereview_diagnostics,
    validate_rereview_records,
)

TEXT = "A😀e\u0301Z。动作骤停。"
TEXT_HASH = sha256_text(TEXT)
NODES = [
    {"node_id": "node-all", "start_codepoint": 0, "end_codepoint": len(TEXT)},
    {"node_id": "node-evidence", "start_codepoint": 1, "end_codepoint": 5},
]


def evidence(*, node_id: str = "node-evidence") -> dict:
    return build_evidence_span(
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        node_id=node_id,
        start_codepoint=1,
        end_codepoint=4,
        canonical_text=TEXT,
    )


def provenance(*, manual: bool = False, method: dict | None = None) -> dict:
    analysis_method = method or {
        "name": "book-atom-manual" if manual else "book-atom-extract",
        "version": "1",
    }
    return build_atom_provenance(
        mode="manual" if manual else "model",
        analysis_method=analysis_method,
        plugin_id="com.plotpilot.novelagent.donor-analysis",
        package_hash="9" * 64,
        release_id="8" * 64,
        capability_id=(
            "analysis.book.atom.manual/v1"
            if manual else "analysis.book.atom.extract/v1"
        ),
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        worker_run_id="worker-1",
        lease_epoch=1,
        provenance_receipt_id="receipt-1",
        run_snapshot_hash="7" * 64,
        workspace_id="ws-1",
        document_id="doc-1",
        source_revision_id="rev-1",
        canonical_text_hash=TEXT_HASH,
        created_at="2026-08-30T00:00:00Z",
        model_profile_revision_id=None if manual else "model-profile-1",
        model_receipt_id=None if manual else "model-receipt-1",
        actor_id="user-1" if manual else None,
        annotation_asset_id="asset-annotation" if manual else None,
        annotation_asset_hash="a" * 64 if manual else None,
    )


def atom(*, manual: bool = False) -> dict:
    method = {
        "name": "book-atom-manual" if manual else "book-atom-extract",
        "version": "1",
    }
    return {
        "schema": "book-atom/v1",
        "atom_kind": "action",
        "title": "骤停动作",
        "observation": "动作突然停止",
        "interpretation": "制造节奏顿挫",
        "applicability": "章节转折",
        "limitations": "依赖上下文",
        "source_category": "user_annotation" if manual else "original_observation",
        "observation_confidence": "confirmed",
        "interpretation_confidence": "inferred",
        "evidence_spans": [evidence()],
        "tags": ["动作", "节奏"],
        "analysis_method": method,
        "provenance": provenance(manual=manual, method=method),
        "prompt_eligible": False,
        "promotion_authorized": False,
        "authority": "candidate_only",
    }


def claim_input() -> dict:
    span = evidence()
    second = deepcopy(span)
    second.update(
        {
            "node_id": "node-all",
            "start_codepoint": 5,
            "end_codepoint": len(TEXT),
            "quote": TEXT[5:],
            "quote_hash": hashlib.sha256(TEXT[5:].encode("utf-8")).hexdigest(),
        }
    )
    return {
        "schema": "claim-input/v1",
        "source_revision_id": "rev-1",
        "ordered_atoms": [
            {
                "ordinal": 0,
                "atom_id": "atom-1",
                "payload_hash": "1" * 64,
                "acceptance_ordinal": 7,
                "evidence_spans": [span],
            },
            {
                "ordinal": 1,
                "atom_id": "atom-2",
                "payload_hash": "2" * 64,
                "acceptance_ordinal": 8,
                "evidence_spans": [second],
            },
        ],
    }


def accepted_atoms(value: dict) -> list[dict]:
    return [
        {
            "atom_id": item["atom_id"],
            "source_revision_id": value["source_revision_id"],
            "status": "accepted",
            "is_current": True,
            "acceptance_ordinal": item["acceptance_ordinal"],
            "payload_asset_id": f"asset-{item['atom_id']}",
            "payload_hash": item["payload_hash"],
            "evidence_spans": deepcopy(item["evidence_spans"]),
        }
        for item in value["ordered_atoms"]
    ]


def claim_authority(value: dict | None = None) -> dict:
    frozen = freeze_claim_input() if value is None else value
    parameters_hash = "a" * 64
    return {
        "parameters_asset_id": "asset-claim-input",
        "parameters_asset_hash": parameters_hash,
        "snapshot_parameters_asset_id": "asset-claim-input",
        "snapshot_asset_hashes": [
            {"asset_id": "asset-canonical", "sha256": "b" * 64},
            {"asset_id": "asset-claim-input", "sha256": parameters_hash},
        ],
        "accepted_atoms": accepted_atoms(frozen),
    }


def freeze_claim_input(value: dict | None = None) -> dict:
    return validate_claim_input(
        claim_input() if value is None else value,
        canonical_text=TEXT,
        nodes=NODES,
        workspace_id="ws-1",
        document_id="doc-1",
        source_revision_id="rev-1",
        canonical_text_hash=TEXT_HASH,
    )


def claim(value: dict) -> dict:
    return {
        "schema": "book-claim/v1",
        "category": "rhythm",
        "title": "顿挫节奏",
        "claim": "作者用骤停形成章节顿挫",
        "scope": "chapter",
        "applicability": "转折处",
        "counterexamples": [],
        "conflicts": [],
        "interpretation_confidence": "inferred",
        "ordered_atom_ids": [item["atom_id"] for item in value["ordered_atoms"]],
        "evidence_spans": [
            deepcopy(span)
            for item in value["ordered_atoms"]
            for span in item["evidence_spans"]
        ],
        "analysis_method": {"name": "book-claim-generate", "version": "1"},
        "prompt_eligible": False,
        "promotion_authorized": False,
        "authority": "candidate_only",
    }


class BundleAuthorityHost:
    def __init__(self, assets: dict[str, bytes]) -> None:
        self.assets = assets

    def call(self, method: str, params: dict) -> dict:
        assert method == "host.asset.read/v1"
        assert set(params) == {"asset_id", "offset", "length"}
        data = self.assets[params["asset_id"]]
        offset = params["offset"]
        page = data[offset : offset + params["length"]]
        return {
            "base64_chunk": base64.b64encode(page).decode("ascii"),
            "next_offset": (
                None if offset + len(page) >= len(data) else offset + len(page)
            ),
            "content_hash": hashlib.sha256(page).hexdigest(),
        }


def bundle_authority_fixture(
    *,
    title: str = "骤停动作",
    model_receipt_id: str = "model-receipt-1",
) -> tuple[BundleAuthorityHost, dict, object, dict]:
    request = {
        "schema": "analysis.book.atom.extract-request/v1",
        "capability_id": donor_runtime.CAPABILITY_ATOM_EXTRACT,
        "operation_key": donor_runtime.CAPABILITY_ATOM_EXTRACT,
        "operation": "run",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "worker_run_id": "worker-1",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-1"],
        "provenance_receipt_id": "receipt-1",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "7" * 64,
        "workspace_id": "ws-1",
        "document_id": "doc-1",
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "taxonomy_asset_id": "asset-taxonomy",
        "taxonomy_asset_hash": "6" * 64,
        "model_profile_revision_id": "model-profile-1",
    }
    request, context = donor_runtime._context(
        request, donor_runtime.CAPABILITY_ATOM_EXTRACT
    )
    payload = atom()
    payload["title"] = title
    payload["provenance"]["model"]["receipt_id"] = model_receipt_id
    payload_asset_id = "asset-output-atom"
    payload_bytes = donor_runtime._json_bytes(payload)
    host = BundleAuthorityHost(
        {
            "asset-canonical": TEXT.encode("utf-8"),
            payload_asset_id: payload_bytes,
        }
    )
    item = donor_runtime._candidate_item(
        request,
        context,
        payload,
        payload_asset_id,
        "book-atom/v1",
        0,
    )
    bundle = donor_runtime._bundle(
        request,
        donor_runtime.CAPABILITY_ATOM_EXTRACT,
        [item],
        "8" * 64,
    )
    return host, request, context, bundle


def derive_bundle_authority(
    fixture: tuple[BundleAuthorityHost, dict, object, dict],
) -> tuple[str, list[str]]:
    host, request, context, bundle = fixture
    return donor_runtime._derive_bundle_authority(
        host,
        request,
        donor_runtime.CAPABILITY_ATOM_EXTRACT,
        context,
        bundle,
        "9" * 64,
        "8" * 64,
        code="TEST_CONTRACT_INVALID",
    )


def reverse_mapping_order(value):
    if isinstance(value, dict):
        return {
            key: reverse_mapping_order(value[key])
            for key in reversed(tuple(value))
        }
    if isinstance(value, list):
        return [reverse_mapping_order(item) for item in value]
    return value


def test_unicode_scalar_half_open_span_and_combining_character_are_exact() -> None:
    # Python indexes Unicode scalar values, so emoji is one position and the
    # combining accent remains a separate scalar.  The quote is exact; no NFC.
    assert len("😀") == 1
    assert len("e\u0301") == 2
    span = evidence()
    assert span["start_codepoint"] == 1
    assert span["end_codepoint"] == 4
    assert span["quote"] == "😀e\u0301"
    assert span["quote_hash"] == hashlib.sha256("😀e\u0301".encode("utf-8")).hexdigest()
    assert span["canonical_text_hash"] == TEXT_HASH
    assert validate_nodes(NODES, len(TEXT)) == NODES
    assert validate_evidence_span(
        span,
        TEXT,
        NODES,
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        canonical_text_hash=TEXT_HASH,
    ) == span


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("wrong revision", lambda s: s.update(revision_id="rev-2")),
        ("wrong quote", lambda s: s.update(quote="😀é")),
        ("wrong quote hash", lambda s: s.update(quote_hash="0" * 64)),
        ("wrong canonical hash", lambda s: s.update(canonical_text_hash="0" * 64)),
        ("out of bounds", lambda s: s.update(end_codepoint=len(TEXT) + 1)),
        ("node escape", lambda s: s.update(node_id="node-evidence", end_codepoint=6, quote=TEXT[1:6], quote_hash=sha256_text(TEXT[1:6]))),
        ("unknown field", lambda s: s.update(utf16_offset=1)),
    ],
)
def test_evidence_span_tampering_fails_closed(label: str, mutate) -> None:
    span = evidence()
    mutate(span)
    with pytest.raises(EvidenceSpanError, match="."):
        validate_evidence_span(
            span,
            TEXT,
            NODES,
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            canonical_text_hash=TEXT_HASH,
        )


def test_surrogate_input_is_rejected_instead_of_becoming_a_fake_scalar() -> None:
    with pytest.raises(DonorContractError, match="surrogate"):
        sha256_text("\ud800")


def test_automatic_and_manual_atoms_require_exact_evidence_and_provenance() -> None:
    automatic = validate_atom_payload(
        atom(),
        canonical_text=TEXT,
        nodes=NODES,
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        canonical_text_hash=TEXT_HASH,
        expected_mode="model",
    )
    manual = validate_atom_payload(
        atom(manual=True),
        canonical_text=TEXT,
        nodes=NODES,
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        canonical_text_hash=TEXT_HASH,
        expected_mode="manual",
    )
    assert automatic["authority"] == manual["authority"] == "candidate_only"
    assert automatic["provenance"]["model"]["receipt_id"] == "model-receipt-1"
    assert automatic["provenance"]["skill"]["skill_id"] == "com.plotpilot.skill.donor.atomic-breakdown"
    assert manual["provenance"]["model"] is None
    assert manual["provenance"]["manual_annotation"]["asset_id"] == "asset-annotation"
    assert not automatic["prompt_eligible"] and not manual["promotion_authorized"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["model"].update(receipt_id="model-receipt-other"),
        lambda p: p["plugin"].update(package_hash="6" * 64),
        lambda p: p["run"].update(attempt_id="attempt-other"),
        lambda p: p.update(run_snapshot_hash="5" * 64),
        lambda p: p["source_revision"].update(revision_id="rev-old"),
    ],
)
def test_f001_automatic_atom_provenance_must_equal_trusted_host_binding(mutation) -> None:
    value = atom()
    expected = deepcopy(value["provenance"])
    mutation(value["provenance"])
    with pytest.raises(DonorContractError, match="provenance"):
        validate_atom_payload(
            value,
            canonical_text=TEXT,
            nodes=NODES,
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            canonical_text_hash=TEXT_HASH,
            expected_mode="model",
            expected_provenance=expected,
        )


def test_f001_manual_atom_provenance_binds_annotation_without_fake_model() -> None:
    value = atom(manual=True)
    expected = deepcopy(value["provenance"])
    value["provenance"]["manual_annotation"]["asset_id"] = "asset-other"
    with pytest.raises(DonorContractError, match="provenance"):
        validate_atom_payload(
            value,
            canonical_text=TEXT,
            nodes=NODES,
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            canonical_text_hash=TEXT_HASH,
            expected_mode="manual",
            expected_provenance=expected,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: a.update(accepted=True),
        lambda a: a.update(authority="published"),
        lambda a: a.update(evidence_spans=[]),
        lambda a: a.update(prompt_eligible=True),
        lambda a: a.update(atom_kind="unknown-kind"),
        lambda a: a.update(source_category="user_annotation"),
        lambda a: a.update(provenance=None),
        lambda a: a["provenance"]["run"].pop("attempt_id"),
        lambda a: a["provenance"]["skill"].update(release_id="0" * 64),
        lambda a: a["provenance"]["source_revision"].update(revision_id="rev-old"),
    ],
)
def test_automatic_atom_closed_and_authority_negative_cases(mutation) -> None:
    value = atom()
    mutation(value)
    with pytest.raises(DonorContractError, match="."):
        validate_atom_payload(
            value,
            canonical_text=TEXT,
            nodes=NODES,
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            canonical_text_hash=TEXT_HASH,
            expected_mode="model",
        )


def test_manual_atom_cannot_bypass_exact_revision_or_provenance() -> None:
    for mutation in (
        lambda a: a.update(provenance=None),
        lambda a: a["provenance"]["manual_annotation"].update(asset_hash="A" * 64),
        lambda a: a["provenance"].update(model={"profile_revision_id": "profile", "receipt_id": "receipt"}),
        lambda a: a["evidence_spans"][0].update(revision_id="rev-old"),
    ):
        value = atom(manual=True)
        mutation(value)
        with pytest.raises(DonorContractError, match="."):
            validate_atom_payload(
                value,
                canonical_text=TEXT,
                nodes=NODES,
                workspace_id="ws-1",
                document_id="doc-1",
                revision_id="rev-1",
                canonical_text_hash=TEXT_HASH,
                expected_mode="manual",
            )


def test_claim_binds_exact_ordered_current_accepted_atom_snapshot() -> None:
    frozen = freeze_claim_input()
    authoritative = bind_current_accepted_atoms(frozen, accepted_atoms(frozen))
    assert [row["acceptance_ordinal"] for row in authoritative] == [7, 8]
    validated = validate_claim_payload(claim(frozen), claim_input=frozen)
    assert validated["ordered_atom_ids"] == ["atom-1", "atom-2"]
    assert validated["authority"] == "candidate_only"


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing", lambda v: v["ordered_atoms"].clear()),
        ("duplicate atom", lambda v: v["ordered_atoms"][1].update(atom_id="atom-1")),
        ("duplicate acceptance", lambda v: v["ordered_atoms"][1].update(acceptance_ordinal=7)),
        ("ordinal gap", lambda v: v["ordered_atoms"][1].update(ordinal=2)),
        ("cross revision", lambda v: v.update(source_revision_id="rev-2")),
        ("hash drift", lambda v: v["ordered_atoms"][0].update(payload_hash="A" * 64)),
        ("unknown field", lambda v: v.update(raw_text="forbidden")),
    ],
)
def test_claim_input_missing_duplicate_order_revision_and_hash_cases_fail_closed(label: str, mutate) -> None:
    value = claim_input()
    mutate(value)
    with pytest.raises(DonorContractError, match="."):
        freeze_claim_input(value)


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("not current", lambda rows: rows[0].update(is_current=False)),
        ("not accepted", lambda rows: rows[0].update(status="superseded")),
        ("stale revision", lambda rows: rows[0].update(source_revision_id="rev-old")),
        ("payload drift", lambda rows: rows[0].update(payload_hash="3" * 64)),
        ("ordinal forgery", lambda rows: rows[0].update(acceptance_ordinal=999)),
        ("evidence drift", lambda rows: rows[0].update(evidence_spans=[])),
        ("extra authority", lambda rows: rows[0].update(publication_id="pub-1")),
    ],
)
def test_claim_authoritative_atom_forgery_and_supersession_fail_closed(label: str, mutate) -> None:
    frozen = freeze_claim_input()
    rows = accepted_atoms(frozen)
    mutate(rows)
    with pytest.raises(DonorContractError, match="."):
        bind_current_accepted_atoms(frozen, rows)


def test_claim_cannot_skip_layer_a_or_reorder_atoms() -> None:
    frozen = freeze_claim_input()
    for mutation in (
        lambda c: c.update(ordered_atom_ids=[]),
        lambda c: c.update(ordered_atom_ids=["atom-2", "atom-1"]),
        lambda c: c.update(evidence_spans=[]),
        lambda c: c.update(raw_quote=TEXT),
    ):
        value = claim(frozen)
        mutation(value)
        with pytest.raises(DonorContractError, match="."):
            validate_claim_payload(value, claim_input=frozen)


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("empty evidence", lambda v: v["ordered_atoms"][0].update(evidence_spans=[])),
        ("wrong quote", lambda v: v["ordered_atoms"][0]["evidence_spans"][0].update(quote="ZZ")),
        ("wrong offset", lambda v: v["ordered_atoms"][0]["evidence_spans"][0].update(start_codepoint=0)),
        ("wrong node", lambda v: v["ordered_atoms"][0]["evidence_spans"][0].update(node_id="node-missing")),
        ("wrong revision", lambda v: v["ordered_atoms"][0]["evidence_spans"][0].update(revision_id="rev-old")),
        ("wrong canonical hash", lambda v: v["ordered_atoms"][0]["evidence_spans"][0].update(canonical_text_hash="f" * 64)),
    ],
)
def test_f002_claim_input_revalidates_each_canonical_evidence_span(label: str, mutate) -> None:
    value = claim_input()
    mutate(value)
    with pytest.raises(DonorContractError, match="."):
        freeze_claim_input(value)


def test_rereview_records_preserve_one_to_one_known_lineage_without_current_mutation() -> None:
    records = [
        {
            "candidate_id": "successor-atom-1",
            "candidate_kind": "book_atom",
            "status": "pending",
            "is_current": False,
            "payload_hash": "4" * 64,
            "payload_asset_id": "asset-successor",
            "source_revision_id": "rev-1",
            "parent_candidate_id": "atom-1",
            "successor_candidate_id": "successor-atom-1",
        }
    ]
    assert validate_rereview_records(
        records, source_revision_id="rev-1", known_parent_ids={"atom-1"}
    ) == records
    for mutation in (
        lambda r: r[0].update(source_revision_id="rev-2"),
        lambda r: r[0].update(parent_candidate_id="unknown"),
        lambda r: r[0].update(parent_candidate_id=None),
        lambda r: r[0].update(current_revision_id="rev-1"),
    ):
        value = deepcopy(records)
        mutation(value)
        with pytest.raises(DonorContractError, match="."):
            validate_rereview_records(
                value, source_revision_id="rev-1", known_parent_ids={"atom-1"}
            )


def rereview_records() -> list[dict]:
    return [
        {
            "candidate_id": f"successor-{index}",
            "candidate_kind": "book_atom",
            "status": "pending",
            "is_current": False,
            "payload_hash": str(index) * 64,
            "payload_asset_id": f"asset-successor-{index}",
            "source_revision_id": "rev-1",
            "parent_candidate_id": f"parent-{index}",
            "successor_candidate_id": f"successor-{index}",
        }
        for index in (1, 2)
    ]


def rereview_diagnostics() -> list[dict]:
    return [
        {
            "candidate_id": f"successor-{index}",
            "severity": "info",
            "code": "OK",
            "message": "",
            "recommendation": "keep",
            "successor_candidate_id": f"successor-{index}",
        }
        for index in (1, 2)
    ]


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing", lambda rows: rows.pop()),
        ("duplicate", lambda rows: rows.__setitem__(1, deepcopy(rows[0]))),
        ("unknown", lambda rows: rows[1].update(candidate_id="unknown")),
        ("reordered", lambda rows: rows.reverse()),
        ("extra", lambda rows: rows.append(deepcopy(rows[0]))),
    ],
)
def test_f003_rereview_diagnostics_are_complete_ordered_one_to_one(label: str, mutate) -> None:
    records = rereview_records()
    diagnostics = rereview_diagnostics()
    mutate(diagnostics)
    with pytest.raises(DonorContractError, match="."):
        validate_rereview_diagnostics(
            diagnostics, candidate_records=records, allow_successor=True
        )


def test_f003_rereview_diagnostics_valid_complete_mapping_passes() -> None:
    records = rereview_records()
    diagnostics = rereview_diagnostics()
    assert validate_rereview_diagnostics(
        diagnostics, candidate_records=records, allow_successor=True
    ) == diagnostics


def test_f001_f002_persisted_schemas_require_closed_provenance_and_nonempty_evidence() -> None:
    schema_root = PLUGIN_ROOT / "donor_analysis" / "schemas"
    atom_validator = Draft202012Validator(
        json.loads((schema_root / "book-atom.schema.json").read_text(encoding="utf-8"))
    )
    claim_input_validator = Draft202012Validator(
        json.loads((schema_root / "claim-input.schema.json").read_text(encoding="utf-8"))
    )
    atom_validator.validate(atom())
    atom_validator.validate(atom(manual=True))
    claim_input_validator.validate(claim_input())

    empty_atom = atom()
    empty_atom["evidence_spans"] = []
    assert list(atom_validator.iter_errors(empty_atom))
    open_provenance = atom()
    open_provenance["provenance"]["untrusted"] = True
    assert list(atom_validator.iter_errors(open_provenance))
    empty_claim_evidence = claim_input()
    empty_claim_evidence["ordered_atoms"][0]["evidence_spans"] = []
    assert list(claim_input_validator.iter_errors(empty_claim_evidence))


@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "manual"])
def test_g2_atom_has_exact_unicode_scalar_evidence_and_complete_trusted_provenance(
    manual: bool,
) -> None:
    value = atom(manual=manual)
    expected = deepcopy(value["provenance"])
    validated = validate_atom_payload(
        value,
        canonical_text=TEXT,
        nodes=NODES,
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        canonical_text_hash=TEXT_HASH,
        expected_mode="manual" if manual else "model",
        expected_provenance=expected,
    )

    span = validated["evidence_spans"][0]
    assert span["start_codepoint"] == 1
    assert span["end_codepoint"] == 4
    assert span["quote"] == "😀e\u0301"
    assert span["quote"] == TEXT[span["start_codepoint"] : span["end_codepoint"]]
    assert span["quote_hash"] == sha256_text(span["quote"])
    assert set(validated["provenance"]) == {
        "schema", "mode", "method", "model", "skill", "plugin", "run",
        "run_snapshot_hash", "source_revision", "manual_annotation", "created_at",
    }
    assert validated["provenance"] == expected


@pytest.mark.parametrize(
    ("manual", "path", "replacement"),
    [
        (False, ("method", "version"), "2"),
        (False, ("model", "profile_revision_id"), "profile-other"),
        (False, ("model", "receipt_id"), "receipt-other"),
        (False, ("skill", "skill_id"), "com.plotpilot.skill.other"),
        (False, ("skill", "package_hash"), "1" * 64),
        (False, ("skill", "release_id"), "2" * 64),
        (False, ("plugin", "plugin_id"), "com.plotpilot.other"),
        (False, ("plugin", "package_hash"), "3" * 64),
        (False, ("plugin", "release_id"), "4" * 64),
        (False, ("plugin", "capability_id"), "analysis.book.atom.manual/v1"),
        (False, ("run", "job_id"), "job-other"),
        (False, ("run", "step_id"), "step-other"),
        (False, ("run", "attempt_id"), "attempt-other"),
        (False, ("run", "worker_run_id"), "worker-other"),
        (False, ("run", "lease_epoch"), 2),
        (False, ("run", "provenance_receipt_id"), "receipt-other"),
        (False, ("run_snapshot_hash",), "5" * 64),
        (False, ("source_revision", "workspace_id"), "ws-other"),
        (False, ("source_revision", "document_id"), "doc-other"),
        (False, ("source_revision", "revision_id"), "rev-other"),
        (False, ("source_revision", "canonical_text_hash"), "6" * 64),
        (False, ("created_at",), "2026-08-31T00:00:00Z"),
        (True, ("manual_annotation", "actor_id"), "actor-other"),
        (True, ("manual_annotation", "asset_id"), "asset-other"),
        (True, ("manual_annotation", "asset_hash"), "7" * 64),
        (True, ("plugin", "package_hash"), "3" * 64),
        (True, ("run", "attempt_id"), "attempt-other"),
        (True, ("run_snapshot_hash",), "5" * 64),
        (True, ("source_revision", "revision_id"), "rev-other"),
        (True, ("created_at",), "2026-08-31T00:00:00Z"),
    ],
    ids=lambda value: str(value),
)
def test_g2_atom_rejects_any_trusted_provenance_component_drift(
    manual: bool,
    path: tuple[str, ...],
    replacement,
) -> None:
    value = atom(manual=manual)
    expected = deepcopy(value["provenance"])
    cursor = value["provenance"]
    for component in path[:-1]:
        cursor = cursor[component]
    cursor[path[-1]] = replacement

    with pytest.raises(DonorContractError, match="."):
        validate_atom_payload(
            value,
            canonical_text=TEXT,
            nodes=NODES,
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            canonical_text_hash=TEXT_HASH,
            expected_mode="manual" if manual else "model",
            expected_provenance=expected,
        )


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("empty", lambda span: span.clear()),
        ("scalar-start", lambda span: span.update(start_codepoint=2)),
        ("scalar-end", lambda span: span.update(end_codepoint=3)),
        ("quote", lambda span: span.update(quote="😀é")),
        ("quote-hash", lambda span: span.update(quote_hash="0" * 64)),
        ("node", lambda span: span.update(node_id="node-missing")),
        ("revision", lambda span: span.update(revision_id="rev-other")),
        ("canonical-hash", lambda span: span.update(canonical_text_hash="0" * 64)),
    ],
)
def test_g2_atom_rejects_empty_or_noncanonical_evidence_before_acceptance(
    label: str,
    mutate,
) -> None:
    for manual in (False, True):
        value = atom(manual=manual)
        if label == "empty":
            value["evidence_spans"] = []
        else:
            mutate(value["evidence_spans"][0])
        with pytest.raises(DonorContractError, match="."):
            validate_atom_payload(
                value,
                canonical_text=TEXT,
                nodes=NODES,
                workspace_id="ws-1",
                document_id="doc-1",
                revision_id="rev-1",
                canonical_text_hash=TEXT_HASH,
                expected_mode="manual" if manual else "model",
                expected_provenance=value["provenance"],
            )


def test_g2_claim_binds_canonical_slice_node_revision_and_current_accepted_atoms() -> None:
    frozen = freeze_claim_input()
    authoritative = accepted_atoms(frozen)
    assert bind_current_accepted_atoms(frozen, authoritative) == authoritative
    projected = validate_claim_payload(claim(frozen), claim_input=frozen)
    assert projected["ordered_atom_ids"] == ["atom-1", "atom-2"]
    assert projected["evidence_spans"] == [
        span
        for item in frozen["ordered_atoms"]
        for span in item["evidence_spans"]
    ]


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("null-successor", lambda rows: rows[0].update(successor_candidate_id=None)),
        ("missing", lambda rows: rows.pop()),
        ("duplicate", lambda rows: rows.__setitem__(1, deepcopy(rows[0]))),
        ("unknown", lambda rows: rows[1].update(candidate_id="candidate-unknown")),
        ("reordered", lambda rows: rows.reverse()),
    ],
)
def test_g2_rereview_diagnostics_exactly_preserve_order_and_existing_successor(
    label: str,
    mutate,
) -> None:
    records = rereview_records()
    diagnostics = rereview_diagnostics()
    mutate(diagnostics)
    with pytest.raises(DonorContractError, match="."):
        validate_rereview_diagnostics(
            diagnostics,
            candidate_records=records,
            allow_successor=True,
        )


def test_g2_f001_f002_bundle_anchor_uses_one_closed_canonical_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = bundle_authority_fixture()
    projections: list[dict] = []

    def recording_hash(schema: str, value) -> str:
        if schema == "donor-analysis-bundle-provenance/v2":
            projections.append(deepcopy(value))
        return hash_json(schema, value)

    monkeypatch.setattr(donor_runtime, "hash_json", recording_hash)
    digest, receipts = derive_bundle_authority(fixture)

    assert receipts == ["model-receipt-1"]
    assert len(projections) == 1
    projection = projections[0]
    assert set(projection) == {
        "schema",
        "binding_hash",
        "bundle",
        "payloads",
        "model_receipt_ids",
        "skill_chain_result_refs",
    }
    assert projection["schema"] == "donor-analysis-bundle-provenance/v2"
    assert projection["bundle"] == fixture[3]
    assert projection["model_receipt_ids"] == receipts
    assert projection["skill_chain_result_refs"] == []
    assert len(projection["payloads"]) == 1
    assert set(projection["payloads"][0]) == {"asset_id", "content_hash", "payload"}
    assert digest == hash_json("donor-analysis-bundle-provenance/v2", projection)

    reordered = reverse_mapping_order(projection)
    assert canonical_json_bytes(reordered) == canonical_json_bytes(projection)
    assert hash_json("donor-analysis-bundle-provenance/v2", reordered) == digest


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing-bundle-field", lambda bundle: bundle.pop("producer")),
        ("open-bundle", lambda bundle: bundle.update(trusted=True)),
        ("missing-item-field", lambda bundle: bundle["items"][0].pop("target")),
        ("open-item", lambda bundle: bundle["items"][0].update(trusted=True)),
        (
            "open-source-ref",
            lambda bundle: bundle["items"][0]["source_refs"][0].update(trusted=True),
        ),
    ],
)
def test_g2_f001_f002_bundle_anchor_rejects_missing_or_open_envelope(
    label: str,
    mutate,
) -> None:
    fixture = bundle_authority_fixture()
    mutate(fixture[3])
    with pytest.raises(donor_runtime.DonorAnalysisWorkerError, match="."):
        derive_bundle_authority(fixture)


def test_g2_f001_bundle_anchor_accepts_only_executed_empty_skill_chain_control() -> None:
    fixture = bundle_authority_fixture()
    _digest, receipts = derive_bundle_authority(fixture)
    assert receipts == ["model-receipt-1"]
    assert fixture[3]["skill_chain_result_refs"] == []

    fixture[3]["skill_chain_result_refs"] = [
        {
            "schema": "skill-chain-ref/v1",
            "chain_result_id": "chain-unexecuted",
            "asset_id": None,
            "asset_hash": None,
            "result_bundle_id": fixture[3]["bundle_id"],
            "result_item_id": fixture[3]["items"][0]["item_id"],
            "stream_id": None,
            "acked_prefix_hash": None,
        }
    ]
    with pytest.raises(
        donor_runtime.DonorAnalysisWorkerError,
        match="unexecuted Skill-chain",
    ):
        derive_bundle_authority(fixture)


@pytest.mark.parametrize(
    "changed_fixture",
    [
        pytest.param(
            lambda: bundle_authority_fixture(title="另一合法标题"),
            id="result-semantics",
        ),
        pytest.param(
            lambda: bundle_authority_fixture(model_receipt_id="model-receipt-2"),
            id="model-provenance",
        ),
    ],
)
def test_g2_f001_f002_bundle_anchor_hash_changes_for_any_legal_semantic_change(
    changed_fixture,
) -> None:
    baseline_hash, _baseline_receipts = derive_bundle_authority(
        bundle_authority_fixture()
    )
    changed_hash, _changed_receipts = derive_bundle_authority(changed_fixture())
    assert changed_hash != baseline_hash


def test_g2_f002_bundle_anchor_is_stable_under_mapping_key_reordering() -> None:
    fixture = bundle_authority_fixture()
    baseline_hash, baseline_receipts = derive_bundle_authority(fixture)
    host, request, context, bundle = fixture
    reordered_fixture = (
        host,
        reverse_mapping_order(request),
        context,
        reverse_mapping_order(bundle),
    )
    reordered_hash, reordered_receipts = derive_bundle_authority(reordered_fixture)
    assert reordered_hash == baseline_hash
    assert reordered_receipts == baseline_receipts


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing-title", lambda value: value.pop("title")),
        ("unknown-field", lambda value: value.update(trusted=False)),
        ("wrong-schema", lambda value: value.update(schema="book-claim/v2")),
        ("wrong-authority", lambda value: value.update(authority="published")),
        ("empty-title", lambda value: value.update(title="")),
        ("empty-claim", lambda value: value.update(claim="")),
        ("invalid-category", lambda value: value.update(category=1)),
        ("invalid-scope", lambda value: value.update(scope=None)),
        ("invalid-applicability", lambda value: value.update(applicability=None)),
        ("invalid-counterexample", lambda value: value.update(counterexamples=[1])),
        ("invalid-conflict", lambda value: value.update(conflicts=[False])),
        ("invalid-confidence", lambda value: value.update(interpretation_confidence="certain")),
        ("unknown-atom", lambda value: value.update(ordered_atom_ids=["atom-unknown", "atom-2"])),
        ("reordered-atoms", lambda value: value["ordered_atom_ids"].reverse()),
        ("empty-evidence", lambda value: value.update(evidence_spans=[])),
        ("drifted-evidence", lambda value: value["evidence_spans"][0].update(quote_hash="0" * 64)),
        ("invalid-method-name", lambda value: value["analysis_method"].update(name="other")),
        ("invalid-method-version", lambda value: value["analysis_method"].update(version="2")),
        ("open-method", lambda value: value["analysis_method"].update(extra=True)),
        ("prompt-enabled", lambda value: value.update(prompt_eligible=True)),
        ("promotion-enabled", lambda value: value.update(promotion_authorized=True)),
    ],
)
def test_g2_f003_full_claim_validator_rejects_every_authority_or_payload_drift(
    label: str,
    mutate,
) -> None:
    frozen = freeze_claim_input()
    value = claim(frozen)
    mutate(value)
    with pytest.raises(DonorContractError, match="."):
        validate_claim_payload(value, claim_input=frozen)


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("missing-title", lambda value: value.pop("title")),
        ("unknown-field", lambda value: value.update(trusted=False)),
        ("wrong-schema", lambda value: value.update(schema="book-claim/v2")),
        ("empty-title", lambda value: value.update(title="")),
        ("empty-claim", lambda value: value.update(claim="")),
        ("invalid-category", lambda value: value.update(category=1)),
        ("invalid-scope", lambda value: value.update(scope=None)),
        ("invalid-applicability", lambda value: value.update(applicability=None)),
        ("invalid-counterexample", lambda value: value.update(counterexamples=[1])),
        ("invalid-conflict", lambda value: value.update(conflicts=[False])),
        ("invalid-confidence", lambda value: value.update(interpretation_confidence="certain")),
        ("empty-atom-list", lambda value: value.update(ordered_atom_ids=[])),
        ("duplicate-atom", lambda value: value.update(ordered_atom_ids=["atom-1", "atom-1"])),
        ("empty-evidence", lambda value: value.update(evidence_spans=[])),
        ("invalid-method-name", lambda value: value["analysis_method"].update(name="other")),
        ("invalid-method-version", lambda value: value["analysis_method"].update(version="2")),
        ("open-method", lambda value: value["analysis_method"].update(extra=True)),
        ("prompt-enabled", lambda value: value.update(prompt_eligible=True)),
        ("promotion-enabled", lambda value: value.update(promotion_authorized=True)),
        ("wrong-authority", lambda value: value.update(authority="published")),
    ],
)
def test_g2_f003_book_claim_schema_is_required_closed_and_candidate_only(
    label: str,
    mutate,
) -> None:
    frozen = freeze_claim_input()
    value = claim(frozen)
    schema = json.loads(
        (PLUGIN_ROOT / "donor_analysis" / "schemas" / "book-claim.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(value)
    mutate(value)
    assert list(validator.iter_errors(value)), label


def test_g2_f003_claim_authority_accepts_only_exact_run_snapshot_and_current_atoms() -> None:
    frozen = freeze_claim_input()
    authority = claim_authority(frozen)
    validated = validate_claim_authority(authority)
    assert validated == authority
    assert validated is not authority
    assert validated["snapshot_asset_hashes"] is not authority["snapshot_asset_hashes"]
    assert validated["accepted_atoms"] is not authority["accepted_atoms"]
    assert bind_current_accepted_atoms(frozen, validated["accepted_atoms"]) == accepted_atoms(frozen)


@pytest.mark.parametrize(
    "field",
    [
        "parameters_asset_id",
        "parameters_asset_hash",
        "snapshot_parameters_asset_id",
        "snapshot_asset_hashes",
        "accepted_atoms",
    ],
)
def test_g2_f003_claim_authority_requires_every_closed_top_level_field(field: str) -> None:
    authority = claim_authority()
    authority.pop(field)
    with pytest.raises(DonorContractError, match="closed"):
        validate_claim_authority(authority)


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("unknown-top-level", lambda value: value.update(trusted=True)),
        ("parameters-id-mismatch", lambda value: value.update(snapshot_parameters_asset_id="asset-other")),
        ("parameters-hash-invalid", lambda value: value.update(parameters_asset_hash="0")),
        ("parameters-binding-missing", lambda value: value["snapshot_asset_hashes"].pop()),
        ("empty-snapshot-bindings", lambda value: value.update(snapshot_asset_hashes=[])),
        ("duplicate-snapshot-binding", lambda value: value["snapshot_asset_hashes"].append(deepcopy(value["snapshot_asset_hashes"][0]))),
        ("open-snapshot-binding", lambda value: value["snapshot_asset_hashes"][0].update(size=1)),
        ("invalid-snapshot-hash", lambda value: value["snapshot_asset_hashes"][0].update(sha256="A" * 64)),
        ("empty-accepted-atoms", lambda value: value.update(accepted_atoms=[])),
        ("not-current", lambda value: value["accepted_atoms"][0].update(is_current=False)),
        ("not-accepted", lambda value: value["accepted_atoms"][0].update(status="superseded")),
        ("invalid-atom-id", lambda value: value["accepted_atoms"][0].update(atom_id="?")),
        ("invalid-revision-id", lambda value: value["accepted_atoms"][0].update(source_revision_id="?")),
        ("invalid-payload-asset-id", lambda value: value["accepted_atoms"][0].update(payload_asset_id="?")),
        ("invalid-payload-hash", lambda value: value["accepted_atoms"][0].update(payload_hash="A" * 64)),
        ("invalid-acceptance-ordinal", lambda value: value["accepted_atoms"][0].update(acceptance_ordinal=0)),
        ("empty-accepted-evidence", lambda value: value["accepted_atoms"][0].update(evidence_spans=[])),
        ("open-accepted-atom", lambda value: value["accepted_atoms"][0].update(publication_id="pub-1")),
        ("duplicate-atom-id", lambda value: value["accepted_atoms"][1].update(atom_id="atom-1")),
        ("duplicate-acceptance-ordinal", lambda value: value["accepted_atoms"][1].update(acceptance_ordinal=7)),
    ],
)
def test_g2_f003_claim_authority_rejects_snapshot_or_current_atom_drift(
    label: str,
    mutate,
) -> None:
    authority = claim_authority()
    mutate(authority)
    with pytest.raises(DonorContractError, match="."):
        validate_claim_authority(authority)


def claim_rereview_record() -> dict:
    record = deepcopy(rereview_records()[0])
    record.update(
        candidate_id="successor-claim-1",
        candidate_kind="book_claim",
        parent_candidate_id="parent-claim-1",
        successor_candidate_id="successor-claim-1",
        claim_authority=claim_authority(),
    )
    return record


def rereview_schema_request(operation: str, records: list[dict]) -> dict:
    value = {
        "schema": "analysis.book.rereview-request/v1",
        "capability_id": "analysis.book.rereview/v1",
        "operation_key": "analysis.book.rereview/v1",
        "operation": operation,
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "worker_run_id": "worker-1",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-1"],
        "provenance_receipt_id": "receipt-1",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "7" * 64,
        "workspace_id": "ws-1",
        "document_id": "doc-1",
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "candidate_records": deepcopy(records),
        "known_parent_candidate_ids": [
            record["parent_candidate_id"] for record in records
        ],
        "allow_successor": True,
        "model_profile_revision_id": "model-profile-1",
    }
    if operation == "resume":
        value.update(
            resume_checkpoint_asset_id="asset-checkpoint",
            resume_checkpoint_asset_hash="5" * 64,
            resume_state_asset_id="asset-state",
            resume_state_asset_hash="6" * 64,
        )
    return value


def rereview_schema_validator() -> Draft202012Validator:
    schema = json.loads(
        (
            PLUGIN_ROOT
            / "donor_analysis"
            / "schemas"
            / "rereview"
            / "input.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_g2_f003_mixed_rereview_records_require_claim_authority_only_for_claims() -> None:
    atom_record = rereview_records()[0]
    claim_record = claim_rereview_record()
    records = [atom_record, claim_record]
    validated = validate_rereview_records(
        records,
        source_revision_id="rev-1",
        known_parent_ids={"parent-1", "parent-claim-1"},
    )
    assert [item["candidate_kind"] for item in validated] == ["book_atom", "book_claim"]
    assert "claim_authority" not in validated[0]
    assert validated[1]["claim_authority"] == claim_record["claim_authority"]


@pytest.mark.parametrize("variant", ["claim-missing", "atom-injected", "claim-invalid"])
def test_g2_f003_rereview_claim_authority_is_closed_by_candidate_kind(variant: str) -> None:
    record = claim_rereview_record() if variant != "atom-injected" else rereview_records()[0]
    if variant == "claim-missing":
        record.pop("claim_authority")
    elif variant == "atom-injected":
        record["claim_authority"] = claim_authority()
    else:
        record["claim_authority"]["accepted_atoms"][0]["is_current"] = False
    with pytest.raises(DonorContractError, match="."):
        validate_rereview_records(
            [record],
            source_revision_id="rev-1",
            known_parent_ids={record["parent_candidate_id"]},
        )


@pytest.mark.parametrize("operation", ["run", "resume", "cancel"])
def test_g2_f003_rereview_schema_preserves_atom_only_backward_control(
    operation: str,
) -> None:
    rereview_schema_validator().validate(
        rereview_schema_request(operation, [rereview_records()[0]])
    )


@pytest.mark.parametrize("operation", ["run", "resume", "cancel"])
def test_g2_f003_rereview_schema_accepts_claim_authority_forward_control(
    operation: str,
) -> None:
    rereview_schema_validator().validate(
        rereview_schema_request(operation, [claim_rereview_record()])
    )


@pytest.mark.parametrize("operation", ["run", "resume", "cancel"])
@pytest.mark.parametrize("variant", ["claim-missing", "atom-injected"])
def test_g2_f003_rereview_schema_closes_authority_by_candidate_kind(
    operation: str,
    variant: str,
) -> None:
    record = claim_rereview_record() if variant == "claim-missing" else rereview_records()[0]
    if variant == "claim-missing":
        record.pop("claim_authority")
    else:
        record["claim_authority"] = claim_authority()
    value = rereview_schema_request(operation, [record])
    assert list(rereview_schema_validator().iter_errors(value)), (operation, variant)


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("open-authority", lambda value: value.update(trusted=True)),
        ("missing-parameters-id", lambda value: value.pop("parameters_asset_id")),
        ("invalid-parameters-hash", lambda value: value.update(parameters_asset_hash="A" * 64)),
        ("empty-snapshot-bindings", lambda value: value.update(snapshot_asset_hashes=[])),
        ("open-snapshot-binding", lambda value: value["snapshot_asset_hashes"][0].update(size=1)),
        ("invalid-snapshot-id", lambda value: value["snapshot_asset_hashes"][0].update(asset_id="?")),
        ("empty-accepted-atoms", lambda value: value.update(accepted_atoms=[])),
        ("open-accepted-atom", lambda value: value["accepted_atoms"][0].update(publication_id="pub-1")),
        ("not-current", lambda value: value["accepted_atoms"][0].update(is_current=False)),
        ("not-accepted", lambda value: value["accepted_atoms"][0].update(status="superseded")),
        ("invalid-acceptance-ordinal", lambda value: value["accepted_atoms"][0].update(acceptance_ordinal=0)),
        ("empty-accepted-evidence", lambda value: value["accepted_atoms"][0].update(evidence_spans=[])),
        ("open-accepted-evidence", lambda value: value["accepted_atoms"][0]["evidence_spans"][0].update(utf16_offset=1)),
    ],
)
def test_g2_f003_rereview_schema_rejects_claim_authority_shape_drift(
    label: str,
    mutate,
) -> None:
    record = claim_rereview_record()
    mutate(record["claim_authority"])
    value = rereview_schema_request("run", [record])
    assert list(rereview_schema_validator().iter_errors(value)), label
