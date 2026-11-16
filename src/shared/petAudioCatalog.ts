/** Data only. A parsed payload is not trusted until its dedicated signature is verified. */
export const PET_CATALOG_SCHEMA = 'pet_audio_catalog_signed_v1' as const
export const PET_CATALOG_PROTOCOL = 'qwen_omni_audio_json_sse_v1' as const
export const PET_CATALOG_MAX_BYTES = 16384
export const PET_CATALOG_TRUST_KEYS: Readonly<Record<string, { issuer: string; publicKeyPem: string }>> = Object.freeze({"hsc-maintainer-20261024-v1":{"issuer":"家庭学习伙伴项目维护者","publicKeyPem":"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEACaMR8RCdhJQxU01vZiB4I0ZP0DxspA/fzSctIGtPwm0=\n-----END PUBLIC KEY-----\n"}})
export type PetCatalogEnvelope = { schema: typeof PET_CATALOG_SCHEMA; key_id: string; payload_base64: string; signature_base64: string }
export type PetCatalogPayload = {
  schema: 'pet_audio_catalog_v1'; revision: number; id: string; verified_at: string; review_before: string
  region: 'cn-beijing'; currency: 'CNY'; endpoint_family: 'aliyun-compatible-cn-beijing-v1'
  protocol: typeof PET_CATALOG_PROTOCOL; model: string; text_input_cny_per_million: string
  audio_input_cny_per_million: string; text_output_with_audio_cny_per_million: string
  audio_tokens_per_second: number; source_urls: string[]; evidence_sha256: string
}
export type PetCatalogTariff = {
  id: string; model: string; region: string; currency: string; verifiedAt: string; reviewBefore: string
  textInputCnyPerMillion: string; audioInputCnyPerMillion: string; textOutputWithAudioCnyPerMillion: string
  audioTokensPerSecond: string; inputSecondMicros: number; inputTextTokenMicros: number; outputTokenMicros: number
  priceSource: string; meteringSource: string
}
export type PetCatalogSelection = { file: string; sha256: string; revision: number }
export type PetCatalogPreview = { token: string; sha256: string; revision: number; issuer: string; tariff: PetCatalogTariff; sourceUrls: string[] }
const fields = ['schema','revision','id','verified_at','review_before','region','currency','endpoint_family','protocol','model',
  'text_input_cny_per_million','audio_input_cny_per_million','text_output_with_audio_cny_per_million','audio_tokens_per_second','source_urls','evidence_sha256']
const need = (v: unknown, code: string): void => { if (!v) throw Error(code) }
export function canonicalPetCatalog(value: unknown): string {
  const sort = (x: unknown): unknown => Array.isArray(x) ? x.map(sort) : x && typeof x === 'object'
    ? Object.fromEntries(Object.keys(x).sort().map(k => [k, sort((x as Record<string, unknown>)[k])])) : x
  return JSON.stringify(sort(value))
}
export function petCatalogTariff(payload: PetCatalogPayload, now = Date.now()): PetCatalogTariff {
  need(payload && typeof payload === 'object' && !Array.isArray(payload) && Object.keys(payload).sort().join('|') === [...fields].sort().join('|'), 'PET_CATALOG_FIELDS')
  need(payload.schema === 'pet_audio_catalog_v1' && Number.isSafeInteger(payload.revision) && payload.revision > 0 && payload.revision <= 2147483647
    && typeof payload.id === 'string' && /^[A-Za-z0-9-]{8,100}$/.test(payload.id), 'PET_CATALOG_ID')
  need(payload.region === 'cn-beijing' && payload.currency === 'CNY' && payload.endpoint_family === 'aliyun-compatible-cn-beijing-v1'
    && payload.protocol === PET_CATALOG_PROTOCOL, 'PET_CATALOG_PROTOCOL')
  need(typeof payload.model === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{1,110}-20\d\d-\d\d-\d\d$/.test(payload.model), 'PET_CATALOG_FIXED_MODEL')
  const stamp = (x: string): number => {
    need(typeof x === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(x), 'PET_CATALOG_TIME')
    const n = Date.parse(x); need(Number.isFinite(n) && new Date(n).toISOString().replace('.000Z','Z') === x, 'PET_CATALOG_TIME'); return n
  }
  const start = stamp(payload.verified_at), end = stamp(payload.review_before)
  need(Number.isFinite(now) && start <= now && now < end && end > start && end - start <= 30 * 86400000, 'PET_CATALOG_EXPIRED')
  need(Array.isArray(payload.source_urls) && payload.source_urls.length >= 1 && payload.source_urls.length <= 4
    && new Set(payload.source_urls).size === payload.source_urls.length && payload.source_urls.every(x => typeof x === 'string'
      && /^https:\/\/help\.aliyun\.com\/zh\/model-studio\/[A-Za-z0-9/_-]{1,180}$/.test(x))
    && typeof payload.evidence_sha256 === 'string' && /^[a-f0-9]{64}$/.test(payload.evidence_sha256), 'PET_CATALOG_SOURCES')
  need(Number.isSafeInteger(payload.audio_tokens_per_second) && payload.audio_tokens_per_second > 0 && payload.audio_tokens_per_second <= 1000, 'PET_CATALOG_METERING')
  const micros = (x: string): bigint => {
    need(typeof x === 'string' && /^(?:0|[1-9]\d{0,5})(?:\.\d{1,6})?$/.test(x), 'PET_CATALOG_PRICE')
    const [whole, fraction = ''] = x.split('.'), n = BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6,'0'))
    need(n > 0n, 'PET_CATALOG_PRICE'); return n
  }
  const ceiling = (n: bigint): number => { const v = (n + 999999n) / 1000000n; need(v > 0n && v <= 1000000n, 'PET_CATALOG_PRICE_BOUND'); return Number(v) }
  return { id: payload.id, model: payload.model, region: payload.region, currency: payload.currency, verifiedAt: payload.verified_at, reviewBefore: payload.review_before,
    textInputCnyPerMillion: payload.text_input_cny_per_million, audioInputCnyPerMillion: payload.audio_input_cny_per_million,
    textOutputWithAudioCnyPerMillion: payload.text_output_with_audio_cny_per_million, audioTokensPerSecond: String(payload.audio_tokens_per_second),
    inputSecondMicros: ceiling(micros(payload.audio_input_cny_per_million) * BigInt(payload.audio_tokens_per_second)),
    inputTextTokenMicros: ceiling(micros(payload.text_input_cny_per_million)), outputTokenMicros: ceiling(micros(payload.text_output_with_audio_cny_per_million)),
    priceSource: payload.source_urls[0], meteringSource: payload.source_urls[1] ?? payload.source_urls[0] }
}
