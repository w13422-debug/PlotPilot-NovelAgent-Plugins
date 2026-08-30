from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
import pytest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins" / "donor-analysis"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from donor_analysis.contract import (  # noqa: E402
    DonorContractError,
    EvidenceSpanError,
    bind_current_accepted_atoms,
    build_atom_provenance,
    build_evidence_span,
    sha256_text,
    validate_atom_payload,
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
