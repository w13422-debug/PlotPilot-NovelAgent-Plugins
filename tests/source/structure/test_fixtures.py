from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sdk"))
sys.path.insert(0, str(ROOT / "plugins" / "source-structure"))

from source_structure.contract import build_evidence_span, sha256_text  # noqa: E402


class StructureFixtureTests(unittest.TestCase):
    def test_golden_fixture_is_deterministic_and_contract_bound(self) -> None:
        fixture = json.loads((ROOT / "data" / "source-cleaning" / "fixtures" / "structure" / "golden_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema"], "source-structure-fixtures/v1")
        unicode_case = fixture["cases"][0]
        node = unicode_case["node"]
        actual = build_evidence_span(
            workspace_id=unicode_case["workspace_id"],
            document_id=unicode_case["document_id"],
            revision_id=unicode_case["revision_id"],
            node_id=node["node_id"],
            canonical_text=unicode_case["canonical_text"],
            start_codepoint=unicode_case["span"]["start_codepoint"],
            end_codepoint=unicode_case["span"]["end_codepoint"],
            node_range={"start_codepoint": node["start_codepoint"], "end_codepoint": node["end_codepoint"]},
        )
        self.assertEqual(actual, unicode_case["span"])
        self.assertEqual(sha256_text(unicode_case["canonical_text"]), unicode_case["canonical_text_hash"])
        self.assertEqual(fixture["cases"][1]["expected_classifications"], ["unchanged", "rebound", "needs_rerun", "orphaned"])


if __name__ == "__main__":
    unittest.main()