"""Deterministic rebuild for immutable Character Skills."""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
SKILL='character-line'; SKILL_ID='com.plotpilot.skill.donor.character-line'; VERSION='1.0.0'
PAYLOAD=('method.json','method.schema.json','prompt.txt','skill.json')
def _sha(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def _json(v:object)->bytes:return (json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode('utf8')
def _canonical(v):
 sdk=next((p/'sdk' for p in Path(__file__).resolve().parents if (p/'sdk').exists()),None)
 if sdk and str(sdk) not in sys.path:sys.path.insert(0,str(sdk))
 try:
  from plotpilot_plugin_sdk.canonical import canonical_bytes; return bytes(canonical_bytes(v))
 except Exception:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')
def _generated(root):
 import json
 manifest=json.loads((root/'skill.json').read_text(encoding='utf8')); method=json.loads((root/'method.json').read_text(encoding='utf8'))
 if set(manifest)!={'schema','skill_id','version','display_name','stage','actions'} or manifest['skill_id']!=SKILL_ID or manifest['version']!=VERSION:raise ValueError('Skill manifest drift')
 expected_fields={'schema','skill_id','version','input_format_id','capability_ids','result_contract','authority','deterministic','evidence_required','source_binding'}
 if set(method)!=expected_fields or method['skill_id']!=SKILL_ID or method['capability_ids']!=['analysis.character.atom.extract/v1','analysis.character.card.generate/v1'] or method['authority']!='candidate_only':raise ValueError('Skill method drift')
 files={p:(root/p).read_bytes() for p in PAYLOAD}
 sdk=next((p/'sdk' for p in Path(__file__).resolve().parents if (p/'sdk').exists()),None)
 if sdk and str(sdk) not in sys.path:sys.path.insert(0,str(sdk))
 try:
  from plotpilot_plugin_sdk.package import skill_package_hash,skill_release_id,build_files_sha256
  manifest_bytes=build_files_sha256(files); digest=skill_package_hash(files); rel=skill_release_id(SKILL_ID,VERSION,digest)
 except Exception:
  manifest_bytes=''.join(f'{_sha(files[p])}  {p}\n' for p in sorted(files,key=lambda x:x.encode())).encode();digest=_sha(b'plotpilot-skill-package/v1\n'+manifest_bytes);rel=_sha(f'plotpilot-skill-release/v1\n{SKILL_ID}\n{VERSION}\n{digest}\n'.encode('ascii'))
 identity={'schema':'character-skill-package-identity/v1','package_kind':'skill','skill_id':SKILL_ID,'version':VERSION,'skill_package_hash':digest,'skill_release_id':rel,'separate_from_code_plugin_id':'com.plotpilot.novelagent.character-distillation','separate_from_data_plugin_id':'com.plotpilot.novelagent.character-archetype'}
 expected={'schema':'character-skill-expected/v1','package_kind':'skill','skill_id':SKILL_ID,'version':VERSION,'manifest_path':'skill.json','package_files':list(PAYLOAD),'files_sha256':manifest_bytes.decode('utf8'),'skill_package_hash':digest,'skill_release_id':rel,'identity_path':'identity.json','fixture_hashes':{'method.json':_sha((root/'method.json').read_bytes()),'method.schema.json':_sha((root/'method.schema.json').read_bytes())},'identity_domains':{'data_or_code':'plotpilot-release/v1','skill':'plotpilot-skill-release/v1','code_plugin_id':'com.plotpilot.novelagent.character-distillation','data_plugin_id':'com.plotpilot.novelagent.character-archetype'}}
 return {'files.sha256':manifest_bytes,'identity.json':_json(identity),'expected.json':_json(expected)}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--rebuild',action='store_true');args=ap.parse_args();root=ROOT/SKILL;generated=_generated(root)
 if args.rebuild:
  for n,b in generated.items():(root/n).write_bytes(b)
 for n,b in generated.items():
  if not (root/n).is_file() or (root/n).read_bytes()!=b:raise SystemExit('DRIFT '+n)
 e=json.loads(generated['expected.json']);print(json.dumps({'skill_id':SKILL_ID,'skill_package_hash':e['skill_package_hash'],'skill_release_id':e['skill_release_id'],'status':'PASS'},sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())
