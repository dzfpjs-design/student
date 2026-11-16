import { createHmac } from 'node:crypto'
import { nativeWritingFields, validNativeWriting, type NativeWritingInput, type NativeWritingOriginal } from '../shared/nativeWriting'

export function signNativeWriting(key: string | undefined, input: NativeWritingInput, issuedAt: string): string {
  if (!key || !/^[a-f0-9]{64}$/.test(key) || !validNativeWriting(input)) throw Error('原稿采集授权暂不可用，未提交作答。')
  return createHmac('sha256', Buffer.from(key, 'hex')).update(JSON.stringify([
    'native_writing_capture_v1', ...nativeWritingFields.map(k => input[k]), issuedAt,
  ]), 'utf8').digest('hex')
}

export async function captureNativeWriting(input: NativeWritingInput, runtime: {
  status: string; localApiUrl: string; sessionToken: string
}, sign: (input: NativeWritingInput, issuedAt: string) => string): Promise<NativeWritingOriginal> {
  if (!validNativeWriting(input) || runtime.status !== 'ready') throw Error('本题原稿尚未确认，未提交作答。')
  const url = new URL(runtime.localApiUrl)
  if (url.protocol !== 'http:' || !['127.0.0.1','localhost','[::1]'].includes(url.hostname)
    || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('原稿只能保存在本机。')
  const issuedAt = new Date().toISOString()
  const response = await fetch(`${runtime.localApiUrl}/v1/sessions/${encodeURIComponent(input.session_id)}/native-writing-original`, {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15000),
    headers: {'Content-Type':'application/json','X-HSC-Session':runtime.sessionToken,
      'X-HSC-Native-Writing':sign(input,issuedAt),'X-HSC-Native-Writing-Time':issuedAt}, body: JSON.stringify(input),
  }).catch(() => { throw Error('原稿采集回执未确认，原文保留；请说“核对原稿提交”，不会自动重新采集。') })
  if (!response.ok) {
    if(response.status===409)throw Error('原稿与当前孩子、题目或稿件版本不一致；原文保留，未提交其他稿件。')
    throw Error('原稿采集回执未确认，原文保留；请说“核对原稿提交”，不会自动重新采集。')
  }
  return await response.json().catch(() => { throw Error('原稿采集回执未确认，原文保留；请说“核对原稿提交”，不会自动重新采集。') }) as NativeWritingOriginal
}
