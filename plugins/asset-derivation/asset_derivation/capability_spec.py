"""Canonical release-neutral specification for Asset Derivation."""
from __future__ import annotations
from dataclasses import dataclass
PLUGIN_ID='com.plotpilot.novelagent.asset-derivation'; VERSION='0.1.0'
NEEDS=('host.asset.read/v1','host.asset.create/v1','host.asset.upload.status/v1','host.job.event/v1','host.job.complete/v1','host.checkpoint.commit/v1','host.candidate.stage/v1')
@dataclass(frozen=True)
class CapabilitySpec:
 capability_id:str; descriptor_path:str; schema_directory:str; input_schema:str; output_schema:str; result_contract:str; supports:tuple[str,...]; deterministic:bool; accepted_data_formats:tuple[str,...]; request_fields:tuple[str,...]; ui_contributions:tuple[tuple[str,str],...]
 @property
 def schema_index_path(self):return f'asset_derivation/schemas/{self.schema_directory}/index.json'
 def manifest_capability(self):return {'capability_id':self.capability_id,'operations':list(self.supports),'result_contract':self.result_contract}
 def descriptor(self,release_id):return {'schema':'capability-provider/v1','capability_id':self.capability_id,'provider':{'plugin_id':PLUGIN_ID,'release_id':release_id},'input_schema':self.input_schema,'output_schema':self.output_schema,'result_contract':self.result_contract,'supports':list(self.supports),'deterministic':self.deterministic,'accepted_data_formats':list(self.accepted_data_formats)}
 def projection(self):return {'capability_id':self.capability_id,'descriptor_path':self.descriptor_path,'schema_index_path':self.schema_index_path,'input_schema':self.input_schema,'output_schema':self.output_schema,'result_contract':self.result_contract,'bundle_type':'artifact' if self.result_contract=='artifact-bundle/v1' else 'candidate_batch','supports':list(self.supports),'deterministic':self.deterministic,'request_fields_group':list(self.request_fields),'accepted_data_formats':list(self.accepted_data_formats),'ui_contributions':[{'contribution_id':a,'slot':b} for a,b in self.ui_contributions]}
CAPABILITY_SPECS=(
 CapabilitySpec('asset.template.derive/v1','descriptor-template-derive.json','template-derive','asset.template.derive-request/v1','asset.template.derive-result/v1','candidate-batch/v1',('run','resume','cancel'),False,('character-archetype/v1','world-rule-template/v1','plot-structure-template/v1'),('source','target'),(('nap-ui-asset-template-derive-v1-donors-asset-derivation','donors.asset.derivation'),('nap-ui-asset-template-derive-v1-workbench-reference-panel','workbench.reference.panel'))),
 CapabilitySpec('asset.data-plugin.package/v1','descriptor-data-package.json','data-package','asset.data-plugin.package-request/v1','asset.data-plugin.package-result/v1','artifact-bundle/v1',('run','validate'),True,(),('package',),(('nap-ui-asset-data-plugin-package-v1-donors-asset-derivation','donors.asset.derivation'),)),
)
SPEC_BY_CAPABILITY={x.capability_id:x for x in CAPABILITY_SPECS}; CAPABILITIES=tuple(SPEC_BY_CAPABILITY); DESCRIPTOR_PATHS=tuple(x.descriptor_path for x in CAPABILITY_SPECS); INDEX_BY_CAPABILITY={x.capability_id:x.schema_index_path for x in CAPABILITY_SPECS}
def capability_projection():return [x.projection() for x in CAPABILITY_SPECS]
def descriptors(release_id):return {x.capability_id:x.descriptor(release_id) for x in CAPABILITY_SPECS}
def plugin_manifest():
 contributions=[{'contribution_id':cid,'slot':slot,'capability_id':s.capability_id} for s in CAPABILITY_SPECS for cid,slot in s.ui_contributions]
 return {'schema':'plotpilot-plugin/v1','plugin_id':PLUGIN_ID,'version':VERSION,'display_name':'Novel-Agent Asset Derivation','compatibility':{'core_api':'>=1.0 <2.0','plugin_rpc':'1','ui_host':'1','python':'3.12.*'},'capabilities':[s.manifest_capability() for s in CAPABILITY_SPECS],'settings':None,'needs':list(NEEDS),'kind':'code','backend':{'entrypoint':'asset_derivation.runtime:main','wheel':'backend/plotpilot_asset_derivation-0.1.0-py3-none-any.whl','requirements_lock':'backend/requirements.lock','wheelhouse':'backend/wheels','max_concurrency':1},'storage':{'schema_version':1,'migration_policy':'transactional-shadow','migration_manifest':'migrations/manifest.json'},'ui':{'entry':'ui/metadata-only.json','runtime':'worker-ui/v1','contributions':contributions},'data':None}
def ui_metadata():
 slots=[]
 for s in CAPABILITY_SPECS:
  for _,slot in s.ui_contributions:
   if slot not in slots:slots.append(slot)
 return {'schema':'plugin-ui-metadata/v1','plugin_id':PLUGIN_ID,'implementation':'host-projected-metadata-only','slots':slots}

def descriptor_files(release_id): return {x.descriptor_path:x.descriptor(release_id) for x in CAPABILITY_SPECS}
