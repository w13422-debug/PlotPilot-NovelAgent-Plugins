from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sdk"))
sys.path.insert(0, str(ROOT / "plugins" / "source-structure"))

from source_structure.contract import (  # noqa: E402
    EvidenceSpanError,
    StructureContractError,
    build_evidence_span,
    build_rebind_report,
    find_text_matches,
    rebind_evidence,
    sha256_text,
    validate_rebind_report,
)
from source_structure.runtime import (  # noqa: E402
    CAPABILITY_REBIND_INSPECT,
    CAPABILITY_REBIND_PROPOSE,
    StructurePlugin,
)
from tests.source.structure.test_structure import (  # noqa: E402
    MemoryHost,
    node,
    rebind_request,
)


def evidence_fixture(text: str = "甲目标乙") -> tuple[list[dict], dict]:
    nodes = [node("n-1", 0, len(text))]
    span = build_evidence_span(
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        node_id="n-1",
        canonical_text=text,
        start_codepoint=1,
        end_codepoint=3,
        node_range={"start_codepoint": 0, "end_codepoint": len(text)},
    )
    item = {
        "schema": "source-evidence-item/v1",
        "evidence_id": "ev-1",
        "span": span,
        "parent_candidate_id": None,
    }
    return nodes, item


class F002ContractClosureTests(unittest.TestCase):
    def test_same_revision_exact_noop_preserves_unicode_and_casefold_contracts(
        self,
    ) -> None:
        text = "A😀ß目标"
        nodes = [node("n-unicode", 0, len(text))]
        span = build_evidence_span(
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-unicode",
            node_id="n-unicode",
            canonical_text=text,
            start_codepoint=1,
            end_codepoint=2,
            node_range={
                "start_codepoint": 0,
                "end_codepoint": len(text),
            },
        )
        item = {
            "schema": "source-evidence-item/v1",
            "evidence_id": "ev-unicode",
            "span": span,
            "parent_candidate_id": None,
        }
        unchanged = rebind_evidence(
            item,
            text,
            text,
            nodes,
            deepcopy(nodes),
            source_canonical_text_hash=sha256_text(text),
            target_canonical_text_hash=sha256_text(text),
            target_revision_id="rev-unicode",
            expected_workspace_id="ws-1",
            expected_document_id="doc-1",
            expected_source_revision_id="rev-unicode",
        )
        self.assertEqual(unchanged["classification"], "unchanged")
        self.assertFalse(unchanged["candidate_eligible"])
        self.assertEqual(unchanged["source_span"], unchanged["target_span"])
        self.assertEqual(unchanged["target_span"]["quote"], "😀")
        self.assertEqual(
            unchanged["target_span"]["quote_hash"],
            hashlib.sha256("😀".encode("utf-8")).hexdigest(),
        )
        report = build_rebind_report(
            workspace_id="ws-1",
            document_id="doc-1",
            source_revision_id="rev-unicode",
            source_canonical_text_hash=sha256_text(text),
            target_revision_id="rev-unicode",
            target_canonical_text_hash=sha256_text(text),
            items=[unchanged],
        )
        self.assertEqual(validate_rebind_report(report), report)

        matches = find_text_matches(
            canonical_text="ß",
            query="s",
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-fold",
            nodes=[node("n-fold", 0, 1)],
            case_sensitive=False,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            (
                matches[0]["span"]["start_codepoint"],
                matches[0]["span"]["end_codepoint"],
            ),
            (0, 1),
        )
        self.assertEqual(matches[0]["span"]["quote"], "ß")

    def test_same_revision_hash_text_or_node_drift_fails_closed(
        self,
    ) -> None:
        source = "甲目标乙"
        source_nodes, item = evidence_fixture(source)
        cases = {
            "hash": (
                source,
                deepcopy(source_nodes),
                "f" * 64,
            ),
            "sol-changed-content": (
                "甲目标丙",
                deepcopy(source_nodes),
                sha256_text("甲目标丙"),
            ),
            "node-binding": (
                source,
                [node("n-target", 0, len(source))],
                sha256_text(source),
            ),
        }
        for label, (target, target_nodes, target_hash) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(EvidenceSpanError):
                    rebind_evidence(
                        item,
                        source,
                        target,
                        source_nodes,
                        target_nodes,
                        source_canonical_text_hash=sha256_text(source),
                        target_canonical_text_hash=target_hash,
                        target_revision_id="rev-1",
                        expected_workspace_id="ws-1",
                        expected_document_id="doc-1",
                        expected_source_revision_id="rev-1",
                    )

    def test_three_rebind_classes_are_cross_revision_only(
        self,
    ) -> None:
        source = "甲目标乙"
        source_nodes, item = evidence_fixture(source)
        vectors = (
            (
                "rev-2",
                "前甲目标乙后",
                [node("n-2", 0, len("前甲目标乙后"))],
                "rebound",
            ),
            (
                "rev-3",
                "目标中目标",
                [node("n-3", 0, len("目标中目标"))],
                "needs_rerun",
            ),
            (
                "rev-4",
                "完全不同",
                [node("n-4", 0, len("完全不同"))],
                "orphaned",
            ),
        )
        classified: list[tuple[str, str, dict]] = []
        for target_revision, target, target_nodes, expected_class in vectors:
            result = rebind_evidence(
                item,
                source,
                target,
                source_nodes,
                target_nodes,
                source_canonical_text_hash=sha256_text(source),
                target_canonical_text_hash=sha256_text(target),
                target_revision_id=target_revision,
                expected_workspace_id="ws-1",
                expected_document_id="doc-1",
                expected_source_revision_id="rev-1",
            )
            self.assertEqual(result["classification"], expected_class)
            build_rebind_report(
                workspace_id="ws-1",
                document_id="doc-1",
                source_revision_id="rev-1",
                source_canonical_text_hash=sha256_text(source),
                target_revision_id=target_revision,
                target_canonical_text_hash=sha256_text(target),
                items=[result],
            )
            classified.append((target_revision, target, result))

        for _target_revision, _target, result in classified:
            invalid = deepcopy(result)
            if invalid["target_span"] is not None:
                invalid["target_span"] = deepcopy(invalid["source_span"])
            with self.subTest(same_revision_class=result["classification"]):
                with self.assertRaises(StructureContractError):
                    build_rebind_report(
                        workspace_id="ws-1",
                        document_id="doc-1",
                        source_revision_id="rev-1",
                        source_canonical_text_hash=sha256_text(source),
                        target_revision_id="rev-1",
                        target_canonical_text_hash=sha256_text(source),
                        items=[invalid],
                    )

        cross_revision_unchanged = deepcopy(classified[0][2])
        cross_revision_unchanged["classification"] = "unchanged"
        cross_revision_unchanged["candidate_eligible"] = False
        with self.assertRaises(StructureContractError):
            build_rebind_report(
                workspace_id="ws-1",
                document_id="doc-1",
                source_revision_id="rev-1",
                source_canonical_text_hash=sha256_text(source),
                target_revision_id="rev-2",
                target_canonical_text_hash=sha256_text("前甲目标乙后"),
                items=[cross_revision_unchanged],
            )


class F002RuntimeClosureTests(unittest.TestCase):
    @staticmethod
    def same_revision_request(
        host: MemoryHost,
        capability: str,
        drift: str | None,
    ) -> dict:
        request = rebind_request(
            host,
            capability,
            worker_run_id=(
                "run-f002-"
                + capability.rsplit(".", 1)[-1].replace("/", "-")
                + "-"
                + (drift or "exact")
            ),
        )
        source = host.assets["asset-source"].decode("utf-8")
        request["target_revision_id"] = request["source_revision_id"]
        if drift == "hash":
            host.seed_text("asset-target", source)
            request["target_canonical_text_hash"] = "f" * 64
            request["target_nodes"] = deepcopy(request["source_nodes"])
        elif drift == "text":
            changed = "甲目标丙"
            host.seed_text("asset-target", changed)
            request["target_canonical_text_hash"] = sha256_text(changed)
            request["target_nodes"] = deepcopy(request["source_nodes"])
        elif drift == "nodes":
            host.seed_text("asset-target", source)
            request["target_canonical_text_hash"] = sha256_text(source)
            request["target_nodes"] = [
                node("n-target", 0, len(source))
            ]
        else:
            host.seed_text("asset-target", source)
            request["target_canonical_text_hash"] = sha256_text(source)
            request["target_nodes"] = deepcopy(request["source_nodes"])
        return request

    def test_sol_same_revision_changed_content_probe_and_all_drifts_never_stage(
        self,
    ) -> None:
        for capability in (
            CAPABILITY_REBIND_INSPECT,
            CAPABILITY_REBIND_PROPOSE,
        ):
            for drift in ("hash", "text", "nodes"):
                with self.subTest(capability=capability, drift=drift):
                    plugin = StructurePlugin()
                    host = MemoryHost()
                    request = self.same_revision_request(
                        host, capability, drift
                    )
                    result = plugin.run(request, host)
                    self.assertEqual(
                        result["contract_id"],
                        "diagnostic-bundle/v1",
                    )
                    self.assertTrue(
                        all(
                            item["schema"] == "diagnostic-item/v1"
                            for item in result["items"]
                        )
                    )
                    self.assertEqual(host.stage_calls, [])
                    self.assertEqual(host.checkpoint_calls, [])
                    self.assertEqual(
                        host.completion_calls[-1]["outcome"],
                        "failed",
                    )

    def test_runtime_exact_noop_inspect_and_propose_emit_no_candidate(
        self,
    ) -> None:
        for capability in (
            CAPABILITY_REBIND_INSPECT,
            CAPABILITY_REBIND_PROPOSE,
        ):
            with self.subTest(capability=capability):
                plugin = StructurePlugin()
                host = MemoryHost()
                request = self.same_revision_request(
                    host, capability, None
                )
                request["selected_evidence_ids"] = []
                result = plugin.run(request, host)
                self.assertEqual(host.stage_calls, [])
                self.assertEqual(
                    host.completion_calls[-1]["outcome"],
                    "succeeded",
                )
                if capability == CAPABILITY_REBIND_INSPECT:
                    self.assertEqual(
                        result["contract_id"],
                        "diagnostic-bundle/v1",
                    )
                    details = json.loads(
                        host.assets[
                            result["items"][0]["details_asset_id"]
                        ].decode("utf-8")
                    )
                    self.assertEqual(
                        details["counts"],
                        {
                            "unchanged": 1,
                            "rebound": 0,
                            "needs_rerun": 0,
                            "orphaned": 0,
                        },
                    )
                else:
                    self.assertEqual(
                        result["contract_id"],
                        "candidate-batch/v1",
                    )
                    self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
