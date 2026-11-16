export type TutorExplanationConfig = { apiKey: string; baseUrl?: string; model?: string }
export type TutorExplanationInput = { apiKey?: string; baseUrl: string; model: string }
export type TutorExplanationSummary = { configured: boolean; keyConfigured: boolean; requiresEndpoint: boolean; baseUrl: string; model: string }

export function validateTutorEndpoint(value: unknown): string {
  if (typeof value !== 'string' || value.length > 2048 || value !== value.trim() || /[^\x21-\x7e]|[\\%?#]/.test(value)) throw Error('TUTOR_EXPLANATION_URL_INVALID')
  const raw = /^https:\/\/([^/]+)(?:\/v1\/*|\/?)$/.exec(value)
  if (!raw || raw[1].endsWith(':')) throw Error('TUTOR_EXPLANATION_URL_INVALID')
  let url: URL
  try { url = new URL(value) } catch { throw Error('TUTOR_EXPLANATION_URL_INVALID') }
  if (url.protocol !== 'https:' || url.username || url.password || (url.port && Number(url.port) === 0)) throw Error('TUTOR_EXPLANATION_URL_INVALID')
  const host = url.hostname.toLowerCase(), labels = host.split('.')
  if (host.length > 253 || labels.length < 2 || !/^[a-z]{2,63}$/.test(labels[labels.length-1]) || host.endsWith('.local') || host.endsWith('.localhost')
      || labels.some(label => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) throw Error('TUTOR_EXPLANATION_URL_INVALID')
  return `https://${host}${url.port ? `:${url.port}` : ''}/v1`
}

export function validateTutorModel(value: unknown): string {
  if (typeof value !== 'string' || !/^deepseek-[A-Za-z0-9][A-Za-z0-9._-]{0,110}$/.test(value)) throw Error('TUTOR_EXPLANATION_MODEL_INVALID')
  return value
}

