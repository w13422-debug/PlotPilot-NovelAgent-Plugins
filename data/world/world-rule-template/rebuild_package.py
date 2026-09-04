"""Deterministic builder for the world-rule-template/v1 Data package."""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent/'v1'
PLUGIN_ID='com.plotpilot.novelagent.world-rule-template'; FORMAT_ID='world-rule-template/v1'; VERSION='1.0.0'
PAYLOAD=('data/world-rules.json','fixtures/interpreter-mappings.json','fixtures/interpreter-roundtrip.json','plugin.json','schemas/interpreter-roundtrip-v1.schema.json','schemas/world-rule-template-v1.schema.json')
CAPABILITIES=("asset.template.derive/v1","quality.consistency.audit/v1","story.chapter.settle/v1","story.state.derive/v1","writing.context.assemble/v1","writing.chapter.draft/v1")
PLUGINS=("com.plotpilot.novelagent.asset-derivation","com.plotpilot.novelagent.consistency-audit","com.plotpilot.novelagent.story-state","com.plotpilot.novelagent.story-state","com.plotpilot.novelagent.writing-context","com.plotpilot.novelagent.chapter-workflow")
def _sha(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def _json(v:object)->bytes:return (json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode('utf8')
def _canonical(v:object)->bytes:
 sdk=next((parent/'sdk' for parent in ROOT.parents if (parent/'sdk').exists()),None)
 if sdk is not None and str(sdk) not in sys.path:sys.path.insert(0,str(sdk))
 try:
  from plotpilot_plugin_sdk.canonical import canonical_bytes
  return bytes(canonical_bytes(v))
 except Exception:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')
def _load(rel):
 v=json.loads((ROOT/rel).read_text(encoding='utf8'))
 if not isinstance(v,dict):raise ValueError(rel+' must be object')
 return v
def _exact(v,keys,label):
 if set(v)!=set(keys):raise ValueError(f'{label} fields differ: expected={sorted(keys)} actual={sorted(v)}')
def _validate():
 value=_load('data/world-rules.json'); required={'schema','format_id','template_id','version','rules','invariants','constraint_kinds','interpreter_mappings'}; _exact(value,required,'world rules')
 if value['schema']!=FORMAT_ID or value['format_id']!=FORMAT_ID or value['version']!=VERSION:raise ValueError('world identity drift')
 if not isinstance(value['rules'],list) or not value['rules'] or not isinstance(value['invariants'],list) or not value['invariants']:raise ValueError('rules/invariants required')
 expected={(plugin,cap,direction) for plugin,cap in zip(PLUGINS,CAPABILITIES) for direction in ('format_to_capability','capability_to_format')}
 actual={(item.get('plugin_id'),item.get('capability_id'),item.get('direction')) for item in value['interpreter_mappings']}
 if actual!=expected or len(value['interpreter_mappings'])!=12:raise ValueError('interpreter mapping is not exact bidirectional set')
 rt=_load('fixtures/interpreter-roundtrip.json'); _exact(rt,{'schema','format_id','capabilities','forward_direction','reverse_direction'},'roundtrip')
 if tuple(rt['capabilities'])!=CAPABILITIES:raise ValueError('roundtrip capability order drift')
 mapping=_load('fixtures/interpreter-mappings.json'); _exact(mapping,{'schema','format_id','mappings'},'mapping fixture')
 if mapping['mappings']!=value['interpreter_mappings']:raise ValueError('mapping fixture differs')
 try:
  from jsonschema import Draft202012Validator
  schema=_load('schemas/world-rule-template-v1.schema.json'); Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(value)
 except ImportError:pass
def _generated():
 sdk=next((parent/'sdk' for parent in ROOT.parents if (parent/'sdk').exists()),None)
 if sdk is not None and str(sdk) not in sys.path:sys.path.insert(0,str(sdk))
 _validate(); files={p:(ROOT/p).read_bytes() for p in PAYLOAD}
 try:
  from plotpilot_plugin_sdk.package import digest_package
  digest=digest_package(files,PLUGIN_ID,VERSION); package_hash,release_id,manifest=digest.package_hash,digest.release_id,digest.files_sha256
 except Exception:
  manifest=''.join(f'{_sha(files[p])}  {p}\n' for p in sorted(files,key=lambda x:x.encode())).encode(); package_hash=_sha(b'plotpilot-package/v1\n'+manifest); release_id=_sha(f'plotpilot-release/v1\n{PLUGIN_ID}\n{VERSION}\n{package_hash}\n'.encode('ascii'))
 root_bytes=(ROOT/'data/world-rules.json').read_bytes(); base={'schema':'plugin-data-bundle/v1','bundle_id':'world-rule-template-v1-bundle','data_plugin_id':PLUGIN_ID,'data_release_id':release_id,'package_hash':package_hash,'format_id':FORMAT_ID,'root_path':'data/world-rules.json','files':[{'path':'data/world-rules.json','asset_id':'asset-world-rule-template-v1','sha256':_sha(root_bytes),'mime':'application/json','size':len(root_bytes)}]}; bundle={**base,'bundle_hash':_sha(b'plugin-data-bundle/v1\n'+_canonical(base))}
 identity={'schema':'world-rule-template-package-identity/v1','package_kind':'data','plugin_id':PLUGIN_ID,'format_id':FORMAT_ID,'version':VERSION,'package_hash':package_hash,'release_id':release_id,'separate_from_code_plugin_id':'com.plotpilot.novelagent.asset-derivation'}
 expected={'schema':'world-rule-template-expected/v1','package_kind':'data','plugin_id':PLUGIN_ID,'format_id':FORMAT_ID,'version':VERSION,'manifest_path':'plugin.json','root_path':'data/world-rules.json','package_files':list(PAYLOAD),'files_sha256':manifest.decode('utf8'),'fixture_hashes':{p:_sha((ROOT/p).read_bytes()) for p in PAYLOAD if p.startswith(('fixtures/','schemas/'))},'package_hash':package_hash,'release_id':release_id,'bundle_hash':bundle['bundle_hash'],'identity_path':'identity.json','identity_domains':{'data_or_code':'plotpilot-release/v1','skill':'plotpilot-skill-release/v1','code_plugin_id':'com.plotpilot.novelagent.asset-derivation'}}
 return {'files.sha256':manifest,'identity.json':_json(identity),'expected.json':_json(expected),'fixtures/plugin-data-bundle.json':_json(bundle)}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--rebuild',action='store_true');args=ap.parse_args();generated=_generated()
 if args.rebuild:
  for n,b in generated.items():(ROOT/n).write_bytes(b)
 for n,b in generated.items():
  if not (ROOT/n).is_file() or (ROOT/n).read_bytes()!=b:raise SystemExit('DRIFT '+n)
 e=json.loads(generated['expected.json']);print(json.dumps({'format_id':FORMAT_ID,'package_hash':e['package_hash'],'release_id':e['release_id'],'status':'PASS'},sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())
