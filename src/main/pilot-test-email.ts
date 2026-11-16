export type PilotEmailMessage = {
  recipient: string
  subject: string
  body: string
}

export type PilotTestEmailConfig = {
  enabled: boolean
  recipient: string
  sendHour: number
  pollIntervalMs?: number
}

export type PilotRuntime = {
  localApiUrl: string
  sessionToken: string
  status: string
}

type DueDigest = {
  due: boolean
  delivery_date?: string
  recipient?: string
  subject?: string
  body?: string
  digest_sha256?: string
  claim_token?: string
}

type Dependencies = {
  runtime: () => PilotRuntime
  fetchImpl?: typeof fetch
  send?: (message: PilotEmailMessage) => Promise<{ accepted: true; transport: string; client: string }>
  smtpSubmit?: (claim: Required<Pick<DueDigest, 'delivery_date' | 'recipient' | 'digest_sha256' | 'claim_token'>>, runtime: PilotRuntime, fetchImpl: typeof fetch) => Promise<{ accepted: true; transport: string; client: string }>
  diagnostic?: (event: string, details?: Record<string, unknown>) => void | Promise<void>
}

export class PilotTestEmailCoordinator {
  private timer: ReturnType<typeof setInterval> | null = null
  private active: Promise<void> | null = null
  private stopped = true

  constructor(private readonly config: PilotTestEmailConfig, private readonly dependencies: Dependencies) {}

  start(): void {
    if (!this.config.enabled || this.timer) return
    this.stopped = false
    void this.checkNow()
    this.timer = setInterval(() => void this.checkNow(), this.config.pollIntervalMs ?? 60_000)
    this.timer.unref?.()
  }

  stop(): void {
    this.stopped = true
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  async checkNow(): Promise<void> {
    if (this.stopped || this.active) return this.active ?? Promise.resolve()
    this.active = this.runOnce().finally(() => { this.active = null })
    return this.active
  }

  private async runOnce(): Promise<void> {
    const runtime = this.dependencies.runtime()
    if (runtime.status !== 'ready' || !runtime.localApiUrl || !runtime.sessionToken) return
    const fetchImpl = this.dependencies.fetchImpl ?? fetch
    let recipient = this.config.recipient.trim()
    if (!recipient) {
      try {
        const response = await fetchImpl(new URL('/v1/pilot-test-digest/config', runtime.localApiUrl), {
          headers: { 'X-HSC-Session': runtime.sessionToken }, signal: AbortSignal.timeout(15_000), cache: 'no-store',
        })
        if (!response.ok) return
        const config = await response.json() as { recipient?: unknown }
        recipient = typeof config.recipient === 'string' ? config.recipient.trim() : ''
      } catch { return }
      // No trusted or unambiguous configured address means no log email.
      if (!recipient || recipient.length > 320 || !/^[^\s@]+@[^\s@]+$/.test(recipient)) return
    }
    const url = new URL('/v1/pilot-test-digest/due', runtime.localApiUrl)
    url.searchParams.set('recipient', recipient)
    url.searchParams.set('send_hour', String(this.config.sendHour))
    let due: DueDigest
    try {
      const response = await fetchImpl(url, {
        headers: { 'X-HSC-Session': runtime.sessionToken },
        signal: AbortSignal.timeout(15_000),
        cache: 'no-store',
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({})) as { detail?: { code?: unknown } }
        const code = data.detail?.code
        const allowed = new Set(['PILOT_RECIPIENT_NOT_AUTHORIZED','PILOT_RECIPIENT_REVOKED','PILOT_RECIPIENT_INVALID','PILOT_SEND_HOUR_INVALID','PILOT_RECIPIENT_ALIAS_AMBIGUOUS','PILOT_LEGACY_ATTACHMENT_SNAPSHOT_MISSING','PILOT_ATTACHMENT_INVALID','PILOT_ATTACHMENT_WINDOW_INVALID','PILOT_ATTACHMENT_HASH_MISMATCH','PILOT_DIGEST_CLAIM_MISMATCH'])
        throw new Error(typeof code === 'string' && allowed.has(code) ? code : `PILOT_DIGEST_CLAIM_HTTP_${response.status}`)
      }
      due = await response.json() as DueDigest
    } catch (error) {
      await this.emit('pilot_email_claim_failed', { error_code: safeErrorCode(error) })
      return
    }
    if (!due.due) return
    if (!due.delivery_date || !due.recipient || due.recipient.trim().toLowerCase() !== recipient.toLowerCase() || !due.subject || !due.body || !due.digest_sha256 || !due.claim_token) {
      await this.emit('pilot_email_claim_invalid', { error_code: 'PILOT_DIGEST_INVALID' })
      return
    }
    let status: 'sent' | 'failed' | 'unknown' = 'sent'
    let transport: string | undefined
    let errorCode: string | undefined
    try {
      const receipt = this.dependencies.send
        ? await this.dependencies.send({ recipient: due.recipient, subject: due.subject, body: due.body })
        : await (this.dependencies.smtpSubmit ?? submitWithRuntimeSmtp)({
          delivery_date: due.delivery_date,
          recipient: due.recipient,
          digest_sha256: due.digest_sha256,
          claim_token: due.claim_token,
        }, runtime, fetchImpl)
      transport = receipt.transport
    } catch (error) {
      errorCode = safeErrorCode(error)
      status = isUnknownDelivery(errorCode) ? 'unknown' : 'failed'
    }
    const resultUrl = new URL(`/v1/pilot-test-digest/${encodeURIComponent(due.delivery_date)}/result`, runtime.localApiUrl)
    try {
      const response = await fetchImpl(resultUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken },
        body: JSON.stringify({ recipient: due.recipient, status, transport, error_code: errorCode, claim_token: due.claim_token }),
        signal: AbortSignal.timeout(15_000),
        cache: 'no-store',
      })
      if (!response.ok) throw new Error(`PILOT_DIGEST_RESULT_HTTP_${response.status}`)
      const result = await response.json().catch(() => null) as { status?: unknown; transport?: unknown; error_code?: unknown } | null
      if (result?.status === 'sent') {
        await this.emit('pilot_email_sent', { status: 'sent', transport: typeof result.transport === 'string' ? result.transport : undefined, delivery_date: due.delivery_date })
      } else if (result?.status === 'failed') {
        await this.emit('pilot_email_delivery_failed', { status: 'failed', error_code: typeof result.error_code === 'string' ? safeErrorCode(new Error(result.error_code)) : undefined, delivery_date: due.delivery_date })
      } else {
        // A local timeout is not the provider outcome. The service may still
        // own the SMTP submission, or its result may be unknown.
        await this.emit('pilot_email_result_unknown', { status: result?.status === 'processing' ? 'running' : 'unknown', delivery_date: due.delivery_date })
      }
    } catch (error) {
      await this.emit('pilot_email_result_unknown', { error_code: safeErrorCode(error) })
    }
  }

  private async emit(event: string, details?: Record<string, unknown>): Promise<void> {
    await this.dependencies.diagnostic?.(event, details)
  }

}

async function submitWithRuntimeSmtp(
  claim: Required<Pick<DueDigest, 'delivery_date' | 'recipient' | 'digest_sha256' | 'claim_token'>>,
  runtime: PilotRuntime,
  fetchImpl: typeof fetch,
): Promise<{ accepted: true; transport: string; client: string }> {
  const url = new URL(`/v1/pilot-test-digest/${encodeURIComponent(claim.delivery_date)}/submit-email`, runtime.localApiUrl)
  let response: Response
  try {
    response = await fetchImpl(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken },
      body: JSON.stringify({ recipient: claim.recipient, digest_sha256: claim.digest_sha256, claim_token: claim.claim_token }),
      signal: AbortSignal.timeout(30_000),
      cache: 'no-store',
    })
  } catch {
    // The provider may have accepted DATA before the local HTTP response was lost.
    // Preserve the unknown state so an automatic retry cannot duplicate the email.
    throw new Error('SMTP_DELIVERY_UNKNOWN')
  }
  const body = await response.json().catch(() => ({})) as {
    accepted?: boolean
    transport?: string
    client?: string
    error_code?: string
    delivery_unknown?: boolean
    detail?: { code?: string; delivery_unknown?: boolean }
  }
  if (!response.ok || body.accepted !== true) {
    const code = body.error_code || body.detail?.code || `SMTP_SUBMIT_HTTP_${response.status}`
    if (body.delivery_unknown || body.detail?.delivery_unknown) throw new Error('SMTP_DELIVERY_UNKNOWN')
    throw new Error(code)
  }
  return { accepted: true, transport: body.transport || 'smtp', client: body.client || 'configured_smtp' }
}

function safeErrorCode(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error)
  const match = raw.toUpperCase().match(/[A-Z][A-Z0-9_]{2,119}/)
  return match?.[0] ?? 'PILOT_EMAIL_UNKNOWN_ERROR'
}

function isUnknownDelivery(errorCode: string): boolean {
  return errorCode.includes('TIMEOUT') || errorCode.includes('INVALID_RECEIPT') || errorCode.includes('RESULT_UNKNOWN')
    || errorCode.includes('DELIVERY_UNKNOWN')
}
