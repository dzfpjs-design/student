import type { PetCatalogTariff } from './petAudioCatalog'
// Reviewed public list prices, not an account entitlement or a quality certificate.
export const PET_AUDIO_TARIFF = Object.freeze({
  id: 'aliyun-cn-qwen35-omni-plus-20260315-20261024-v1',
  model: 'qwen3.5-omni-plus-2026-03-15', region: 'cn-beijing', currency: 'CNY',
  verifiedAt: '2026-10-24T05:04:30Z', reviewBefore: '2026-11-23T05:04:30Z',
  textInputCnyPerMillion: '7', audioInputCnyPerMillion: '53', textOutputWithAudioCnyPerMillion: '40',
  audioTokensPerSecond: '7', inputSecondMicros: 371, inputTextTokenMicros: 7, outputTokenMicros: 40,
  priceSource: 'https://help.aliyun.com/zh/model-studio/qwen3-5-omni-plus',
  meteringSource: 'https://help.aliyun.com/zh/model-studio/qwen-omni'
})

export function currentPetAudioTariff(id: string | undefined, baseUrl: string, model: string, now = Date.now(), selected: PetCatalogTariff = PET_AUDIO_TARIFF): PetCatalogTariff | null {
  if (id !== selected.id || model !== selected.model
    || !/^https:\/\/(?:dashscope\.aliyuncs\.com|[a-z0-9][a-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com)\/compatible-mode\/v1$/.test(baseUrl)
    || !Number.isFinite(now) || now < Date.parse(selected.verifiedAt) || now >= Date.parse(selected.reviewBefore)) return null
  return selected
}
