"""Deterministically rebuild lexicon/v1 with an executable conflict fixture."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
import re
from typing import Any

ROOT=Path(__file__).resolve().parent/"v1"
PLUGIN_ID="com.plotpilot.novelagent.lexicon.zh-core";CODE_PLUGIN_ID="com.plotpilot.novelagent.style-manufacturing"
FORMAT_ID="lexicon/v1";VERSION="1.0.0";POLICY="highest-priority-then-plugin-id-byte-order";NORMALIZATION="unicode-nfc-casefold-v1"
SEMVER_RE=re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
ID_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
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
def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def pretty(v:Any)->bytes:return (json.dumps(v,ensure_ascii=False,indent=2)+"\n").encode()
def sha(v:bytes)->str:return hashlib.sha256(v).hexdigest()
def hj(prefix:str,v:Any)->str:return sha(prefix.encode("ascii")+b"\n"+canonical(v))
def release(h:str)->str:return sha(f"plotpilot-release/v1\n{PLUGIN_ID}\n{VERSION}\n{h}\n".encode())
def lexicon(lid:str,entries:list[dict[str,Any]])->dict[str,Any]:return {"schema":FORMAT_ID,"lexicon_id":lid,"version":VERSION,"normalization":NORMALIZATION,"conflict_policy":POLICY,"entries":sorted(entries,key=lambda x:x["normalized_term"].encode())}
def entry(term:str,preferred:str,priority:int,source:str,*,aliases:list[str]|None=None,forbidden:bool=False,note:str="")->dict[str,Any]:return {"term":term,"normalized_term":term.casefold(),"preferred":preferred,"aliases":sorted(set(aliases or []),key=lambda x:x.encode()),"forbidden":forbidden,"note":note,"priority":priority,"source_plugin_id":source}
def merge(values:list[dict[str,Any]])->dict[str,Any]:
    groups={}
    for value in values:
        for row in value["entries"]:groups.setdefault(row["normalized_term"],[]).append(dict(row))
    merged=[]
    for _key,rows in groups.items():merged.append(sorted(rows,key=lambda x:(-x["priority"],x["source_plugin_id"].encode(),canonical(x)))[0])
    projection=[{"lexicon_id":x["lexicon_id"],"version":x["version"],"entries":x["entries"]} for x in sorted(values,key=lambda x:x["lexicon_id"].encode())]
    return lexicon("merged:"+hj("lexicon-merge/v1",projection)[:48],merged)

def documents()->dict[str,bytes]:
    validate_identity(PLUGIN_ID, VERSION)
    a=lexicon("lexicon-foundation-a",[entry("凝视","注视",10,"com.plotpilot.lexicon.a",aliases=["凝望"]),entry("非常","避免使用",20,"com.plotpilot.lexicon.a",forbidden=True,note="避免空泛程度副词"),entry("归途","归途",5,"com.plotpilot.lexicon.a")])
    b=lexicon("lexicon-project-b",[entry("凝视","端详",30,"com.plotpilot.lexicon.b",aliases=["打量"],note="项目偏好，高优先级胜出"),entry("归途","归程",5,"com.plotpilot.lexicon.b",note="同优先级时 plugin_id 字节序 a 胜出"),entry("月色","月光",10,"com.plotpilot.lexicon.b")])
    result=merge([a,b])
    fixture={"schema":"lexicon-merge-fixture/v1","inputs":[a,b],"expected":result,"conflict_rules":["higher priority wins","equal priority uses source_plugin_id UTF-8 byte order","exact tie uses canonical row bytes","output uses normalized_term UTF-8 byte order"]}
    manifest={"schema":"plotpilot-plugin/v1","plugin_id":PLUGIN_ID,"version":VERSION,"display_name":"Novel-Agent 中文核心词库","compatibility":{"core_api":">=1.0 <2.0","plugin_rpc":"1","ui_host":"1","python":"3.12.*"},"capabilities":[],"settings":None,"needs":[],"kind":"data","data":{"format":FORMAT_ID,"root":"data/lexicon.json"}}
    schema={"$id":"https://plotpilot.local/data/lexicon-v1","$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":False,"required":["schema","lexicon_id","version","normalization","conflict_policy","entries"],"properties":{"schema":{"const":FORMAT_ID},"lexicon_id":{"type":"string"},"version":{"type":"string","pattern":SEMVER_RE.pattern},"normalization":{"const":NORMALIZATION},"conflict_policy":{"const":POLICY},"entries":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["term","normalized_term","preferred","aliases","forbidden","note","priority","source_plugin_id"],"properties":{"term":{"type":"string"},"normalized_term":{"type":"string"},"preferred":{"type":"string"},"aliases":{"type":"array","items":{"type":"string"}},"forbidden":{"type":"boolean"},"note":{"type":"string"},"priority":{"type":"integer"},"source_plugin_id":{"type":"string"}}}}}}
    fixture_schema={"$id":"https://plotpilot.local/data/lexicon-merge-fixture-v1","$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":False,"required":["schema","inputs","expected","conflict_rules"],"properties":{"schema":{"const":"lexicon-merge-fixture/v1"},"inputs":{"type":"array","minItems":2,"items":{"$ref":"lexicon-v1.schema.json"}},"expected":{"$ref":"lexicon-v1.schema.json"},"conflict_rules":{"type":"array","minItems":4,"items":{"type":"string"}}}}
    return {"data/lexicon.json":canonical(result),"fixtures/merge-conflicts.json":pretty(fixture),"plugin.json":pretty(manifest),"schemas/lexicon-v1.schema.json":pretty(schema),"schemas/lexicon-merge-fixture-v1.schema.json":pretty(fixture_schema)}

def generated()->dict[str,bytes]:
    files=documents();manifest=b"".join(f"{sha(files[p])}  {p}\n".encode() for p in sorted(files,key=lambda x:x.encode()));package_hash=sha(b"plotpilot-package/v1\n"+manifest);release_id=release(package_hash);root=files["data/lexicon.json"]
    bundle0={"schema":"plugin-data-bundle/v1","bundle_id":"lexicon-zh-core-v1-bundle","data_plugin_id":PLUGIN_ID,"data_release_id":release_id,"package_hash":package_hash,"format_id":FORMAT_ID,"root_path":"data/lexicon.json","files":[{"path":"data/lexicon.json","asset_id":"asset-lexicon-zh-core-v1","sha256":sha(root),"mime":"application/json","size":len(root)}]};bundle={**bundle0,"bundle_hash":hj("plugin-data-bundle/v1",bundle0)}
    identity={"schema":"lexicon-data-package-identity/v1","package_kind":"data","plugin_id":PLUGIN_ID,"format_id":FORMAT_ID,"version":VERSION,"package_hash":package_hash,"release_id":release_id,"separate_from_code_plugin_id":CODE_PLUGIN_ID}
    expected={"schema":"lexicon-data-package-expected/v1","package_kind":"data","plugin_id":PLUGIN_ID,"format_id":FORMAT_ID,"version":VERSION,"manifest_path":"plugin.json","root_path":"data/lexicon.json","package_files":sorted(files,key=lambda x:x.encode()),"files_sha256":manifest.decode(),"fixture_hashes":{p:sha(v) for p,v in files.items()},"package_hash":package_hash,"release_id":release_id,"bundle_hash":bundle["bundle_hash"],"identity_path":"identity.json","merge_policy":POLICY,"identity_domains":{"style":"style-release/v1","data_or_code":"plotpilot-release/v1","skill":"plotpilot-skill-release/v1","code_plugin_id":CODE_PLUGIN_ID}}
    return {**files,"files.sha256":manifest,"identity.json":pretty(identity),"expected.json":pretty(expected),"fixtures/plugin-data-bundle.json":pretty(bundle)}
def main()->int:
    p=argparse.ArgumentParser();p.add_argument("--rebuild",action="store_true");args=p.parse_args();items=generated()
    if args.rebuild:
        for name,data in items.items():path=ROOT/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    for name,data in items.items():
        if not (ROOT/name).is_file() or (ROOT/name).read_bytes()!=data:raise SystemExit(f"DRIFT {name}")
    e=json.loads(items["expected.json"]);print(json.dumps({"format_id":FORMAT_ID,"package_hash":e["package_hash"],"release_id":e["release_id"],"status":"PASS"},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
