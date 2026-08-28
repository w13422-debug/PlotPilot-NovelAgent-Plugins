from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TS_DIR = ROOT / "sdk" / "typescript"


def _node(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=check,
        env={**os.environ, "NODE_NO_WARNINGS": "1"},
    )


def test_typescript_sources_parse_with_the_host_runtime() -> None:
    for source in sorted(TS_DIR.glob("*.ts")):
        result = _node("--experimental-strip-types", "--check", str(source))
        assert result.stdout == ""
        assert result.stderr == ""


def test_typescript_sdk_matches_python_golden_contracts() -> None:
    script = r'''
const { readFileSync } = await import('node:fs')
const sdk = await import('./sdk/typescript/index.ts')
const readJson = path => JSON.parse(readFileSync(path, 'utf8'))
const bytes = path => new Uint8Array(readFileSync(path))
const snapshot = readJson('contracts/golden/run-snapshot/snapshot.json')
const bundle = readJson('contracts/examples/result-bundle.json')
const data = readJson('contracts/examples/fixtures/plugin-data-bundle.json')
const manifest = readJson('contracts/examples/fixtures/plugin-manifest-code.json')
const catalog = readJson('catalog/plugin-catalog-v1.json')
const plan = readJson('contracts/examples/fixtures/plugin-plan.json')
const generation = readJson('contracts/examples/fixtures/plugin-generation.json')
const lifecycle = readJson('contracts/examples/fixtures/plugin-lifecycle-transition.json')
const retirement = readJson('contracts/examples/fixtures/release-retirement.json')
const pin = readJson('contracts/examples/fixtures/release-pin.json')
const heartbeat = readJson('contracts/examples/fixtures/rpc-notification.json')
const packageFiles = {
  'data/rules.json': bytes('contracts/golden/package/data/rules.json'),
  'plugin.json': bytes('contracts/golden/package/plugin.json'),
}
const skillFiles = {
  'prompt.txt': bytes('contracts/golden/skill/prompt.txt'),
  'skill.json': bytes('contracts/golden/skill/skill.json'),
}
const rejected = async action => {
  try { await action(); return false } catch (_) { return true }
}
await sdk.verifySnapshot(snapshot)
await sdk.verifyDataBundle(data)
sdk.verifyResultProfile(bundle, snapshot.workspace_id)
await sdk.verifyManifest(manifest)
sdk.verifyCatalog(catalog)
sdk.verifyPlan(plan)
sdk.verifyDataInterpreterBinding('style-pack/v1', 'com.plotpilot.novelagent.chapter-workflow', 'writing.chapter.draft/v1', catalog)
sdk.verifyPlan({ ...plan, data_bindings: [{ data_binding_id: 'data-binding-1', data_plugin_id: 'com.plotpilot.data.style', release_requirement: '1.0.0', format_id: 'style-pack/v1', interpreter_binding_id: 'interpreter-1', order: 10, enabled: true, parameters_asset_id: null }] }, { 'interpreter-1': { plugin_id: 'com.plotpilot.novelagent.chapter-workflow', capability_id: 'writing.chapter.draft/v1' } }, catalog)
sdk.verifyGeneration(generation)
sdk.verifyGenerationUnchanged(generation, generation)
sdk.verifyLifecycleTransition(lifecycle)
sdk.verifyReleaseRetirement(retirement)
sdk.verifyReleasePin(pin, retirement)
sdk.validateRpcRequest(heartbeat, 1)
const packageDigest = await sdk.packageDigest(packageFiles, 'com.plotpilot.golden.echo', '1.0.0')
const skillDigest = await sdk.skillPackageDigest(skillFiles, 'com.plotpilot.skill.golden', '1.0.0')
const quoteHash = await sdk.sha256Hex(sdk.utf8('bc'))
await sdk.verifyEvidenceSpan({
    schema: 'evidence-span/v1', workspace_id: 'ws-1', document_id: 'doc-1', revision_id: 'rev-1', node_id: 'node-1',
  start_codepoint: 1, end_codepoint: 3, quote: 'bc', quote_hash: quoteHash,
  canonical_text_hash: await sdk.sha256Hex(sdk.utf8('abc')),
  }, 'abc', { start_codepoint: 0, end_codepoint: 3 })
const claimSpan = {
  schema: 'evidence-span/v1', workspace_id: 'ws-1', document_id: 'doc-1', revision_id: 'rev-1', node_id: 'node-1',
  start_codepoint: 1, end_codepoint: 2, quote: '😀', quote_hash: await sdk.sha256Hex(sdk.utf8('😀')),
  canonical_text_hash: await sdk.sha256Hex(sdk.utf8('A😀B')),
}
const claim = { schema: 'claim-input/v1', source_revision_id: 'rev-1', ordered_atoms: [{ ordinal: 0, atom_id: 'atom-1', payload_hash: '1'.repeat(64), acceptance_ordinal: 1, evidence_spans: [claimSpan] }] }
await sdk.verifyClaimInput(claim, { canonicalText: 'A😀B', acceptedAtoms: { 'atom-1': { atom_id: 'atom-1', current: true, accepted: true, revision_id: 'rev-1', payload_hash: '1'.repeat(64), acceptance_ordinal: 1 } } })
const observed = {
  package_hash: packageDigest.packageHash,
  package_release_id: packageDigest.releaseId,
  skill_package_hash: skillDigest.skillPackageHash,
  skill_release_id: skillDigest.skillReleaseId,
  request_key: await sdk.requestKey(snapshot),
  snapshot_hash: snapshot.snapshot_hash,
  nfc_casefold_equal: sdk.unicodeNfcCasefold('cafe\u0301.txt') === sdk.unicodeNfcCasefold('caf\u00e9.txt'),
  sharp_s_casefold_equal: sdk.unicodeNfcCasefold('straße.txt') === sdk.unicodeNfcCasefold('strasse.txt'),
  data_tamper_rejected: await rejected(() => sdk.verifyDataBundle({ ...data, bundle_hash: '0'.repeat(64) })),
  workspace_mismatch_rejected: await rejected(() => sdk.verifyResultProfile(bundle, 'workspace-other')),
  collision_rejected: await rejected(() => sdk.buildFilesSha256({ 'Straße.txt': new Uint8Array([1]), 'strasse.txt': new Uint8Array([2]) })),
}
if (!observed.nfc_casefold_equal || !observed.sharp_s_casefold_equal || !observed.data_tamper_rejected || !observed.workspace_mismatch_rejected || !observed.collision_rejected) throw new Error(JSON.stringify(observed))
console.log(JSON.stringify(observed))
'''
    result = _node("--experimental-strip-types", "--input-type=module", "--eval", script)
    observed = json.loads(result.stdout)
    assert observed == {
        "package_hash": "987e80013fe0cddd463eb8976fd75b62dbabef4f8e0e321ae6ad82a54f09b068",
        "package_release_id": "00d365d816a45108cac08b5dc26eae015a44401c04aed0e9d512ed8fb24d88dd",
        "skill_package_hash": "5cf3df6acea3c792ee36fa3c0c5757f87c8219aba88cfc568bd4a67738983b7f",
        "skill_release_id": "abe35d45644a2ebac3b3c914876c613342b8d5a3e2421334241a06ec887436b7",
        "request_key": "5e346b6254626cb314a0d4040a64e8a9797c0a890537ce07e9920a911a525e61",
        "snapshot_hash": "5a7e60677a5ced4803c96f7b7db657409051ba8f3f5b18aa8ac28978e2b4a1d2",
        "nfc_casefold_equal": True,
        "sharp_s_casefold_equal": True,
        "data_tamper_rejected": True,
        "workspace_mismatch_rejected": True,
        "collision_rejected": True,
    }


def test_typescript_sdk_verifies_catalog_demo_packages() -> None:
    script = r'''
const { readFileSync } = await import('node:fs')
const sdk = await import('./sdk/typescript/index.ts')
const readJson = path => JSON.parse(readFileSync(path, 'utf8'))
const bytes = path => new Uint8Array(readFileSync(path))
const packageFiles = (dir, expected) => Object.fromEntries(expected.package_files.map(path => [path, bytes(`${dir}/${path}`)]))
const codeDir = 'catalog/demos/code'
const dataDir = 'catalog/demos/data'
const skillDir = 'catalog/demos/skill'
const codeManifest = readJson(`${codeDir}/plugin.json`)
const dataManifest = readJson(`${dataDir}/plugin.json`)
const skillManifest = readJson(`${skillDir}/skill.json`)
const codeExpected = readJson(`${codeDir}/expected.json`)
const dataExpected = readJson(`${dataDir}/expected.json`)
const skillExpected = readJson(`${skillDir}/expected.json`)
const codeResult = readJson(`${codeDir}/fixtures/result-bundle.json`)
const dataBundle = readJson(`${dataDir}/fixtures/plugin-data-bundle.json`)
await sdk.verifyManifest(codeManifest)
await sdk.verifyManifest(dataManifest)
if (skillManifest.schema !== 'plotpilot-skill/v1') throw new Error('Skill schema mismatch')
await sdk.verifyPackageIdentity(packageFiles(codeDir, codeExpected), codeManifest.plugin_id, codeManifest.version, codeExpected.package_hash, codeExpected.release_id, bytes(`${codeDir}/files.sha256`))
await sdk.verifyPackageIdentity(packageFiles(dataDir, dataExpected), dataManifest.plugin_id, dataManifest.version, dataExpected.package_hash, dataExpected.release_id, bytes(`${dataDir}/files.sha256`))
await sdk.verifySkillIdentity(packageFiles(skillDir, skillExpected), skillManifest.skill_id, skillManifest.version, skillExpected.skill_package_hash, skillExpected.skill_release_id, bytes(`${skillDir}/files.sha256`))
await sdk.verifyDataBundle(dataBundle)
sdk.verifyResultProfile(codeResult, 'ws-1')
console.log(JSON.stringify({
  code_package_hash: codeExpected.package_hash,
  data_package_hash: dataExpected.package_hash,
  skill_package_hash: skillExpected.skill_package_hash,
  code_result_bundle: codeResult.bundle_id,
  data_bundle_hash: dataBundle.bundle_hash,
  skill_receipt_schema: readJson(`${skillDir}/fixtures/skill-run-receipt.json`).schema,
}))
'''
    result = _node("--experimental-strip-types", "--input-type=module", "--eval", script)
    assert json.loads(result.stdout) == {
        "code_package_hash": "8114aadd4763a975ed3070c715128c92d36345d2e3ae8a1b70c5e2c4b3e84c26",
        "data_package_hash": "28f55a1a08e23bfd650ffab1c1c072559ef8237300566cba1ee00f03a4edfe6d",
        "skill_package_hash": "69574c04055f92f3e94136b0ecbf3abe1e16f719e61c81a9a4af7e798f56394a",
        "code_result_bundle": "nap00-demo-code-bundle",
        "data_bundle_hash": "f1821127fb376e374550a278a154559c0937dc421102eb9dbf18d0a71b19fa40",
        "skill_receipt_schema": "skill-run-receipt/v1",
    }
