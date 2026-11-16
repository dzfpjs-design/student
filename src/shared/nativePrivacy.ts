export type NativePrivacyMetadata={nonce:string;request_id:string;expected_generation:string;
  kind:'delete'|'restore'|'portable_restore';child_id:string|null;scope:string|null;
  command_sha256:string;cleanup_manifest_sha256:string}
export type NativePrivacyCommand={path:string;body:Record<string,unknown>}
export type NativePrivacyResponse={status:number;body:unknown}
export function validNativePrivacy(value:unknown):value is NativePrivacyMetadata{
  if(!value||typeof value!=='object'||Array.isArray(value))return false
  const v=value as Record<string,unknown>
  if(Object.keys(v).sort().join(',')!==['nonce','request_id','expected_generation','kind','child_id','scope','command_sha256','cleanup_manifest_sha256'].sort().join(','))return false
  return ['nonce','request_id','expected_generation'].every(k=>typeof v[k]==='string'&&v[k].length>0&&v[k].length<=128)
    &&['command_sha256','cleanup_manifest_sha256'].every(k=>typeof v[k]==='string'&&/^[a-f0-9]{64}$/.test(v[k]))
    &&['delete','restore','portable_restore'].includes(v.kind as string)
    &&['child_id','scope'].every(k=>v[k]===null||(typeof v[k]==='string'&&v[k].length>0&&v[k].length<=128))
}
