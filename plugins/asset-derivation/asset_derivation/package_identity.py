"""Fail-closed identity verifier for Asset Derivation."""
from __future__ import annotations
import hashlib,json,re,sys
from pathlib import Path
_sdk=next((p/'sdk' for p in Path(__file__).resolve().parents if (p/'sdk').exists()),None)
if _sdk and str(_sdk) not in sys.path:sys.path.insert(0,str(_sdk))
from .capability_spec import CAPABILITY_SPECS,DESCRIPTOR_PATHS,PLUGIN_ID,VERSION,capability_projection,plugin_manifest,ui_metadata
MODULE_NAME='asset_derivation';IDENTITY_SCHEMA='asset-derivation-package-identity/v1';HASH_RE=re.compile(r'^[0-9a-f]{64}$')
def package_root():return Path(__file__).resolve().parents[1]
def identity_path(root=None):return (root or package_root())/MODULE_NAME/'identity.json'
def _json(path):
 def dup(pairs):
  d={}
  for k,v in pairs:
   if k in d:raise ValueError('duplicate key')
   d[k]=v
  return d
 try:v=json.loads(path.read_text(encoding='utf8'),object_pairs_hook=dup,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
 except Exception as exc:raise RuntimeError(f'asset package JSON unavailable: {path}') from exc
 if not isinstance(v,dict):raise RuntimeError('asset package JSON must object')
 return v
def _identity(root=None):
 ident=_json(identity_path(root))
 if set(ident)!={'schema','plugin_id','version','package_hash','release_id'} or ident['schema']!=IDENTITY_SCHEMA or ident['plugin_id']!=PLUGIN_ID or ident['version']!=VERSION or any(not isinstance(ident[x],str) or HASH_RE.fullmatch(ident[x]) is None or ident[x]=='0'*64 for x in ('package_hash','release_id')):
  raise RuntimeError('asset identity schema/owner/hash invalid')
 return ident
def _manifest(root):
 raw=(root/'files.sha256').read_bytes();text=raw.decode('utf8')
 if raw.startswith(b'\xef\xbb\xbf') or b'\r' in raw or not text.endswith('\n') or text.endswith('\n\n'):raise RuntimeError('asset files.sha256 newline/BOM drift')
 names=[]
 for line in text.splitlines(keepends=True):
  m=re.fullmatch(r'([0-9a-f]{64})  (.+)\n',line)
  if not m or m.group(2) in names or m.group(2)=='files.sha256':raise RuntimeError('asset files.sha256 malformed')
  names.append(m.group(2))
 if names!=sorted(names,key=lambda x:x.encode()):raise RuntimeError('asset manifest order drift')
 return names,raw
def calculate_identity(root=None):
 root=root or package_root();ident=_identity(root)
 names,manifest=_manifest(root);files={n:(root/n).read_bytes() for n in names}
 from plotpilot_plugin_sdk.package import digest_package
 digest=digest_package(files,PLUGIN_ID,VERSION)
 if digest.files_sha256!=manifest or digest.package_hash!=ident['package_hash'] or digest.release_id!=ident['release_id']:raise RuntimeError('asset digest/identity mismatch')
 expected=_json(root/'expected.json');required={'schema','plugin_id','version','package_files','files_sha256','package_hash','release_id','provider_identity','descriptor_paths','descriptor_sha256','descriptor_provider_identity','schema_index_paths','schema_index_path_list','schema_index_sha256','schema_artifacts','schema_artifact_sha256','wheel_path','wheel_import','plugin_path','identity_path','capability_projection','capability_projection_sha256'}
 if set(expected)!=required or expected['schema']!='asset-derivation-package-expected/v1' or expected['package_files']!=names or expected['files_sha256']!=manifest.decode() or expected['package_hash']!=digest.package_hash or expected['release_id']!=digest.release_id or expected['provider_identity']!=ident:raise RuntimeError('asset expected identity mismatch')
 if _json(root/'plugin.json')!=plugin_manifest() or _json(root/'ui/metadata-only.json')!=ui_metadata():raise RuntimeError('asset manifest/UI drift')
 projection=capability_projection();ph=hashlib.sha256(json.dumps(projection,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 if expected['capability_projection']!=projection or expected['capability_projection_sha256']!=ph:raise RuntimeError('asset capability projection drift')
 for spec in CAPABILITY_SPECS:
  rel=spec.descriptor_path;d=_json(root/rel)
  if d!=spec.descriptor(digest.release_id) or expected['descriptor_provider_identity'].get(rel)!=d['provider'] or hashlib.sha256((root/rel).read_bytes()).hexdigest()!=expected['descriptor_sha256'].get(rel):raise RuntimeError('asset descriptor drift')
 for rel in expected['schema_index_path_list']:
  idx=_json(root/rel)
  if hashlib.sha256((root/rel).read_bytes()).hexdigest()!=expected['schema_index_sha256'].get(rel) or idx.get('schemas')!=expected['schema_artifacts'].get(rel):raise RuntimeError('asset schema index drift')
  for item in idx['schemas']:
   art=root/MODULE_NAME/item['path']
   if not art.is_file() or hashlib.sha256(art.read_bytes()).hexdigest()!=item['sha256'] or expected['schema_artifact_sha256'].get(art.relative_to(root).as_posix())!=item['sha256']:raise RuntimeError('asset schema artifact drift')
 if not (root/expected['wheel_path']).is_file():raise RuntimeError('asset wheel missing')
 return digest
def load_runtime_identity():
 # Source checkouts include the outer manifest/expected files and therefore
 # get full package verification.  Wheels deliberately omit those outer
 # files; their installer supplies the immutable identity sidecar, which is
 # the only runtime metadata available in an isolated wheel import.
 root=package_root();value=_identity(root)
 if (root/'files.sha256').is_file(): calculate_identity(root)
 return value
