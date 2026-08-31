from asset_derivation.build_backend import rebuild_package

if __name__=="__main__":
 import json
 v=rebuild_package();print(json.dumps({k:v[k] for k in ("plugin_id","package_hash","release_id","wheel_path")},sort_keys=True))
