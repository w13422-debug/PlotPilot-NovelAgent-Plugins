"""Deterministically rebuild the immutable style-pack/v1 Data package."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
import re
from typing import Any

ROOT=Path(__file__).resolve().parent/"v1"
PLUGIN_ID="com.plotpilot.novelagent.style-pack.core-default"
CODE_PLUGIN_ID="com.plotpilot.novelagent.style-manufacturing"
FORMAT_ID="style-pack/v1";VERSION="1.0.0"
SEMVER_RE=re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
ID_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
# The Data namespace is checked against the frozen catalog projection rather
# than only the two Style packages.  This prevents a newly introduced Data
# package from colliding with any Code/Skill identity when built in isolation.
RESERVED_IDS={
    "com.plotpilot.novelagent.source-import","com.plotpilot.novelagent.source-cleaning-runtime","com.plotpilot.novelagent.source-structure",
    "com.plotpilot.novelagent.donor-analysis","com.plotpilot.novelagent.narrative-analysis","com.plotpilot.novelagent.character-distillation",
    "com.plotpilot.novelagent.asset-derivation","com.plotpilot.novelagent.style-manufacturing","com.plotpilot.novelagent.style-runtime",
    "com.plotpilot.novelagent.consistency-audit","com.plotpilot.novelagent.impact-repair","com.plotpilot.novelagent.branch-canon",
    "com.plotpilot.novelagent.story-state","com.plotpilot.novelagent.writing-context","com.plotpilot.novelagent.chapter-workflow",
    "com.plotpilot.novelagent.skill-recommender","com.plotpilot.novelagent.analysis-io","com.plotpilot.novelagent.outline-projection",
    "com.plotpilot.novelagent.knowledge-projection","com.plotpilot.novelagent.provider-openai-compatible",
    "com.plotpilot.novelagent.provider-anthropic","com.plotpilot.novelagent.provider-gemini",
    "com.plotpilot.skill.donor.atomic-breakdown","com.plotpilot.skill.donor.claim-synthesis","com.plotpilot.skill.donor.narrative-unit",
    "com.plotpilot.skill.donor.character-line","com.plotpilot.skill.donor.foreshadow-line","com.plotpilot.skill.donor.style-analysis",
    "com.plotpilot.skill.outline.book","com.plotpilot.skill.outline.volume","com.plotpilot.skill.outline.chapter","com.plotpilot.skill.outline.plot-unit",
    "com.plotpilot.skill.writing.combat","com.plotpilot.skill.writing.psychology","com.plotpilot.skill.writing.dialogue","com.plotpilot.skill.writing.scenery",
    "com.plotpilot.skill.writing.hook","com.plotpilot.skill.writing.pacing","com.plotpilot.skill.writing.refine","com.plotpilot.skill.quality.consistency",
    "com.plotpilot.skill.style.apply","com.plotpilot.novelagent.style-pack.core-default","com.plotpilot.novelagent.lexicon.zh-core",
}

def validate_identity(plugin_id:str,version:str)->None:
    if (not isinstance(plugin_id,str) or not ID_RE.fullmatch(plugin_id)
            or plugin_id in RESERVED_IDS - {PLUGIN_ID}
            or not isinstance(version,str) or not SEMVER_RE.fullmatch(version)):
        raise ValueError("Data identity must be collision-free and SemVer")

def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
def pretty(v:Any)->bytes:return (json.dumps(v,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
def sha(v:bytes)->str:return hashlib.sha256(v).hexdigest()
def hj(prefix:str,v:Any)->str:return sha(prefix.encode("ascii")+b"\n"+canonical(v))
def release(package_hash:str)->str:return sha(f"plotpilot-release/v1\n{PLUGIN_ID}\n{VERSION}\n{package_hash}\n".encode("ascii"))

def documents()->dict[str,bytes]:
    validate_identity(PLUGIN_ID, VERSION)
    pack={"schema":"style-pack/v1","style_pack_id":"com.plotpilot.style.core-default","version":"1.0.0","style_release_id":"","payload_hash":"",
      "source_cas":{"asset_id":"asset-style-source-core-default","sha256":sha("风声掠过长街。".encode()),"revision_id":"revision-style-source-core-default"},
      "target_cas":{"workspace_id":"workspace-style-library","entity_id":"style-core-default","base_revision_id":"revision-style-base-1","base_content_hash":sha(b"style-base-v1")},
      "manufacture_snapshot":{"run_snapshot_hash":sha(b"run-snapshot-style-core-default"),"route_id":"route-style-manufacturer","prompt_hash":sha(b"prompt-style-manufacturer-v1"),"output_schema_hash":sha(b"style-pack-schema-v1"),"chunk_plan_hash":sha(b"style-chunk-plan-v1"),"provider_attempt_ids":["provider-attempt-manufacturer-1"],"checkpoint_ids":["checkpoint-style-manufacturer-1"],"usage":{"input_tokens":128,"output_tokens":64,"total_tokens":192}},
      "features":{"voice":["克制","清晰"],"rhythm":["短长句交错"],"syntax":["主动句优先"],"imagery":["自然意象"],"taboos":["空泛堆砌"]},
      "lexicon_bindings":[],"constraints":["不复制范文句子","不改变事实"],"exemplar_hashes":[sha("风声掠过长街。".encode())]}
    projection=dict(pack);projection["payload_hash"]="";projection["style_release_id"]="";pack["payload_hash"]=hj("style-pack/v1",projection)
    pack["style_release_id"]=sha(f"style-release/v1\n{pack['style_pack_id']}\n{pack['version']}\n{pack['payload_hash']}\n".encode())
    cases=[]
    for i in range(1,4):cases.append({"case_id":f"blind-case-{i}","writer_output_hash":sha(f"writer-{i}".encode()),"review_output_hash":sha(f"review-{i}".encode()),"score_0_100":90-i,"passed":True})
    receipt={"schema":"author-style-qualification-receipt/v1","receipt_id":"style-qualification-core-default","style_pack_id":pack["style_pack_id"],"style_release_id":pack["style_release_id"],"style_payload_hash":pack["payload_hash"],"qualification_run_snapshot_hash":sha(b"qualification-run-style-core-default"),"rubric_hash":sha(b"quality-rubric-v1"),"manufacturing_route_id":"route-style-manufacturer","writer_route_id":"route-style-writer-blind","reviewer_route_id":"route-style-reviewer-blind","manufacturing_attempt_ids":["provider-attempt-manufacturer-1"],"writer_attempt_ids":["provider-attempt-writer-1"],"reviewer_attempt_ids":["provider-attempt-reviewer-1"],"case_results":cases,"decision":"pass","automatic_eligible":True,"created_at":"2026-08-30T09:00:00Z","receipt_hash":""}
    receipt["receipt_hash"]=hj("author-style-qualification-receipt/v1",{k:v for k,v in receipt.items() if k!="receipt_hash"})
    manifest={"schema":"plotpilot-plugin/v1","plugin_id":PLUGIN_ID,"version":VERSION,"display_name":"Novel-Agent 默认合格文风包","compatibility":{"core_api":">=1.0 <2.0","plugin_rpc":"1","ui_host":"1","python":"3.12.*"},"capabilities":[],"settings":None,"needs":[],"kind":"data","data":{"format":FORMAT_ID,"root":"data/style-pack.json"}}
    schema={"$id":"https://plotpilot.local/data/style-pack-v1","$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":False,"required":list(pack),"properties":{"schema":{"const":FORMAT_ID},"style_pack_id":{"type":"string"},"version":{"type":"string","pattern":SEMVER_RE.pattern},"style_release_id":{"type":"string","pattern":"^[0-9a-f]{64}$"},"payload_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"source_cas":{"type":"object"},"target_cas":{"type":"object"},"manufacture_snapshot":{"type":"object"},"features":{"type":"object"},"lexicon_bindings":{"type":"array"},"constraints":{"type":"array"},"exemplar_hashes":{"type":"array"}}}
    qual_schema={"$id":"https://plotpilot.local/data/author-style-qualification-receipt-v1","$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":False,"required":list(receipt),"properties":{"schema":{"const":"author-style-qualification-receipt/v1"},"receipt_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"}}}
    id_schema={"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"};hash_schema={"type":"string","pattern":"^[0-9a-f]{64}$"}
    def closed(required,properties):return {"type":"object","additionalProperties":False,"required":required,"properties":properties}
    schema["properties"].update({
      "source_cas":closed(["asset_id","sha256","revision_id"],{"asset_id":id_schema,"sha256":hash_schema,"revision_id":id_schema}),
      "target_cas":closed(["workspace_id","entity_id","base_revision_id","base_content_hash"],{"workspace_id":id_schema,"entity_id":id_schema,"base_revision_id":id_schema,"base_content_hash":hash_schema}),
      "manufacture_snapshot":closed(["run_snapshot_hash","route_id","prompt_hash","output_schema_hash","chunk_plan_hash","provider_attempt_ids","checkpoint_ids","usage"],{"run_snapshot_hash":hash_schema,"route_id":id_schema,"prompt_hash":hash_schema,"output_schema_hash":hash_schema,"chunk_plan_hash":hash_schema,"provider_attempt_ids":{"type":"array","items":id_schema},"checkpoint_ids":{"type":"array","items":id_schema},"usage":closed(["input_tokens","output_tokens","total_tokens"],{k:{"type":"integer","minimum":0} for k in ("input_tokens","output_tokens","total_tokens")})}),
      "features":closed(["voice","rhythm","syntax","imagery","taboos"],{k:{"type":"array","items":{"type":"string"}} for k in ("voice","rhythm","syntax","imagery","taboos")}),
      "lexicon_bindings":{"type":"array","items":closed(["data_plugin_id","data_release_id","bundle_hash","order"],{"data_plugin_id":id_schema,"data_release_id":hash_schema,"bundle_hash":hash_schema,"order":{"type":"integer","minimum":1}})},
    })
    case_schema=closed(["case_id","writer_output_hash","review_output_hash","score_0_100","passed"],{"case_id":id_schema,"writer_output_hash":hash_schema,"review_output_hash":hash_schema,"score_0_100":{"type":"integer","minimum":0,"maximum":100},"passed":{"type":"boolean"}})
    qual_schema["properties"]={"schema":{"const":"author-style-qualification-receipt/v1"},"receipt_id":id_schema,"style_pack_id":id_schema,"style_release_id":hash_schema,"style_payload_hash":hash_schema,"qualification_run_snapshot_hash":hash_schema,"rubric_hash":hash_schema,"manufacturing_route_id":id_schema,"writer_route_id":id_schema,"reviewer_route_id":id_schema,"manufacturing_attempt_ids":{"type":"array","items":id_schema},"writer_attempt_ids":{"type":"array","items":id_schema},"reviewer_attempt_ids":{"type":"array","items":id_schema},"case_results":{"type":"array","minItems":3,"maxItems":3,"items":case_schema},"decision":{"enum":["pass","fail"]},"automatic_eligible":{"type":"boolean"},"created_at":{"type":"string"},"receipt_hash":hash_schema}
    # Runtime qualification receipts bind the exact immutable Assets used for
    # the blind writer/reviewer path.  Keep the historical base receipt shape
    # valid for frozen fixtures, but require the complete extension whenever
    # any Asset binding is present (no partial/ambiguous receipt can validate).
    extension_required=["style_pack_asset_id","style_pack_asset_hash","sealed_case_asset_id","sealed_case_asset_hash","rubric_asset_id","rubric_asset_hash","writer_output_asset_ids","writer_output_asset_hashes","review_output_asset_id","review_output_asset_hash"]
    qual_schema["properties"].update({
      "style_pack_asset_id":id_schema,"style_pack_asset_hash":hash_schema,
      "sealed_case_asset_id":id_schema,"sealed_case_asset_hash":hash_schema,
      "rubric_asset_id":id_schema,"rubric_asset_hash":hash_schema,
      "review_output_asset_id":id_schema,"review_output_asset_hash":hash_schema,
      "writer_output_asset_ids":{"type":"array","minItems":3,"maxItems":3,"uniqueItems":True,"items":id_schema},
      "writer_output_asset_hashes":{"type":"array","minItems":3,"maxItems":3,"uniqueItems":True,"items":hash_schema},
    })
    qual_schema["allOf"]=[{"oneOf":[
      {"not":{"anyOf":[{"required":[field]} for field in extension_required]}},
      {"required":extension_required},
    ]}]
    fixture={"schema":"style-pack-qualification-fixture/v1","style_pack":pack,"qualification_receipt":receipt,"exact_release_eligible":True,"identity_domains":{"style":"style-release/v1","data_or_code":"plotpilot-release/v1","skill":"plotpilot-skill-release/v1","code_plugin_id":CODE_PLUGIN_ID}}
    return {"data/style-pack.json":canonical(pack),"fixtures/exact-qualified-release.json":pretty(fixture),"plugin.json":pretty(manifest),"schemas/style-pack-v1.schema.json":pretty(schema),"schemas/qualification-receipt-v1.schema.json":pretty(qual_schema)}

def generated()->dict[str,bytes]:
    files=documents();manifest=b"".join(f"{sha(files[p])}  {p}\n".encode() for p in sorted(files,key=lambda x:x.encode()))
    package_hash=sha(b"plotpilot-package/v1\n"+manifest);release_id=release(package_hash);root=files["data/style-pack.json"]
    bundle0={"schema":"plugin-data-bundle/v1","bundle_id":"style-pack-core-default-v1-bundle","data_plugin_id":PLUGIN_ID,"data_release_id":release_id,"package_hash":package_hash,"format_id":FORMAT_ID,"root_path":"data/style-pack.json","files":[{"path":"data/style-pack.json","asset_id":"asset-style-pack-core-default-v1","sha256":sha(root),"mime":"application/json","size":len(root)}]}
    bundle={**bundle0,"bundle_hash":hj("plugin-data-bundle/v1",bundle0)}
    identity={"schema":"style-data-package-identity/v1","package_kind":"data","plugin_id":PLUGIN_ID,"format_id":FORMAT_ID,"version":VERSION,"package_hash":package_hash,"release_id":release_id,"separate_from_code_plugin_id":CODE_PLUGIN_ID}
    expected={"schema":"style-data-package-expected/v1","package_kind":"data","plugin_id":PLUGIN_ID,"format_id":FORMAT_ID,"version":VERSION,"manifest_path":"plugin.json","root_path":"data/style-pack.json","package_files":sorted(files,key=lambda x:x.encode()),"files_sha256":manifest.decode(),"fixture_hashes":{p:sha(v) for p,v in files.items()},"package_hash":package_hash,"release_id":release_id,"bundle_hash":bundle["bundle_hash"],"identity_path":"identity.json","identity_domains":{"style":"style-release/v1","data_or_code":"plotpilot-release/v1","skill":"plotpilot-skill-release/v1","code_plugin_id":CODE_PLUGIN_ID}}
    return {**files,"files.sha256":manifest,"identity.json":pretty(identity),"expected.json":pretty(expected),"fixtures/plugin-data-bundle.json":pretty(bundle)}

def main()->int:
    p=argparse.ArgumentParser();p.add_argument("--rebuild",action="store_true");args=p.parse_args();items=generated()
    if args.rebuild:
        for name,data in items.items():path=ROOT/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    for name,data in items.items():
        if not (ROOT/name).is_file() or (ROOT/name).read_bytes()!=data:raise SystemExit(f"DRIFT {name}")
    e=json.loads(items["expected.json"]);print(json.dumps({"format_id":FORMAT_ID,"package_hash":e["package_hash"],"release_id":e["release_id"],"status":"PASS"},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
