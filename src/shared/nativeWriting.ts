export type NativeWritingScope = {
  child_id: string; session_id: string; prompt_id: string; item_version: string; diagnostic_item_id: string
}
export type NativeWritingInput = NativeWritingScope & { text: string; client_action_id: string }
export type NativeWritingOriginal = NativeWritingScope & {
  artifact_id: string; original_revision: string; text_sha256: string; status: 'completed';
  local_only: true; cloud_transmission_allowed: false
}
export const nativeWritingFields = ['child_id','session_id','prompt_id','item_version','diagnostic_item_id','text','client_action_id'] as const
export function validNativeWriting(value: unknown): value is NativeWritingInput {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  return Object.keys(row).length === nativeWritingFields.length && nativeWritingFields.every(k =>
    typeof row[k] === 'string' && Boolean((row[k] as string).trim()) && (row[k] as string).length <= (k === 'text' ? 4000 : 200))
}
