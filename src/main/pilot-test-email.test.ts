import { describe, expect, it, vi } from 'vitest'
import { PilotTestEmailCoordinator } from './pilot-test-email'

const runtime = () => ({ localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'session-token', status: 'ready' })

describe('PilotTestEmailCoordinator', () => {
  it('claims, sends through an injected SMTP transport and records accepted result once', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = []
    const fetchImpl = vi.fn(async (input: URL | RequestInfo, init?: RequestInit) => {
      calls.push({ url: String(input), init })
      if (calls.length === 1) return new Response(JSON.stringify({
        due: true, delivery_date: '2026-09-25', recipient: 'guardian@example.test', subject: 'daily', body: 'body', digest_sha256: 'a'.repeat(64), claim_token: 'f'.repeat(32),
      }), { status: 200 })
      return new Response(JSON.stringify({ status: 'sent' }), { status: 200 })
    }) as typeof fetch
    const send = vi.fn(async () => ({ accepted: true as const, transport: 'smtp', client: 'configured_smtp' }))
    const coordinator = new PilotTestEmailCoordinator(
      { enabled: true, recipient: 'guardian@example.test', sendHour: 22 },
      { runtime, fetchImpl, send },
    )
    coordinator.start()
    await coordinator.checkNow()
    coordinator.stop()
    expect(send).toHaveBeenCalledWith({ recipient: 'guardian@example.test', subject: 'daily', body: 'body' })
    expect(JSON.parse(String(calls[1].init?.body))).toMatchObject({ status: 'sent', transport: 'smtp' })
  })

  it('records an SMTP delivery timeout as unknown so restart cannot duplicate it', async () => {
    let request = 0
    const fetchImpl = vi.fn(async (_input: URL | RequestInfo, init?: RequestInit) => {
      request += 1
      if (request === 1) return new Response(JSON.stringify({
        due: true, delivery_date: '2026-09-25', recipient: 'guardian@example.test', subject: 'daily', body: 'body', digest_sha256: 'd'.repeat(64), claim_token: 'f'.repeat(32),
      }), { status: 200 })
      expect(JSON.parse(String(init?.body))).toMatchObject({ status: 'unknown', error_code: 'SMTP_DELIVERY_TIMEOUT' })
      return new Response('{}', { status: 200 })
    }) as typeof fetch
    const coordinator = new PilotTestEmailCoordinator(
      { enabled: true, recipient: 'guardian@example.test', sendHour: 22 },
      { runtime, fetchImpl, send: async () => { throw new Error('SMTP_DELIVERY_TIMEOUT') } },
    )
    coordinator.start()
    await coordinator.checkNow()
    coordinator.stop()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it('uses the configured runtime SMTP path', async () => {
    let request = 0
    const resultBodies: string[] = []
    const fetchSpy = vi.fn(async (_input: URL | RequestInfo, init?: RequestInit) => {
      request += 1
      if (request === 1) return new Response(JSON.stringify({
        due: true, delivery_date: '2026-09-25', recipient: 'guardian@example.test', subject: 'daily', body: 'body', digest_sha256: 'a'.repeat(64), claim_token: 'f'.repeat(32),
      }), { status: 200 })
      resultBodies.push(String(init?.body))
      return new Response('{}', { status: 200 })
    })
    const fetchImpl = fetchSpy as typeof fetch
    const smtpSubmit = vi.fn(async () => ({ accepted: true as const, transport: 'smtp', client: 'configured_smtp' }))
    const coordinator = new PilotTestEmailCoordinator(
      { enabled: true, recipient: 'guardian@example.test', sendHour: 22 },
      { runtime, fetchImpl, smtpSubmit },
    )
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(smtpSubmit).toHaveBeenCalledOnce()
    expect(smtpSubmit).toHaveBeenCalledWith(expect.objectContaining({ claim_token: 'f'.repeat(32) }), expect.anything(), expect.anything())
    expect(JSON.parse(resultBodies[0])).toMatchObject({ status: 'sent', transport: 'smtp' })
  })

  it('logs only a known local authorization error code from a 422 response', async () => {
    const diagnostic = vi.fn()
    const fetchImpl = vi.fn(async () => Response.json({detail:{code:'PILOT_RECIPIENT_NOT_AUTHORIZED',private:'never log'}},{status:422})) as typeof fetch
    const coordinator = new PilotTestEmailCoordinator({enabled:true,recipient:'guardian@example.test',sendHour:22},{runtime,fetchImpl,diagnostic})
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(diagnostic).toHaveBeenCalledWith('pilot_email_claim_failed',{error_code:'PILOT_RECIPIENT_NOT_AUTHORIZED'})
  })

  it('records a missing SMTP configuration without using an alternate desktop mail client', async () => {
    let request = 0
    const resultBodies: string[] = []
    const fetchSpy = vi.fn(async (_input: URL | RequestInfo, init?: RequestInit) => {
      request += 1
      if (request === 1) return new Response(JSON.stringify({
        due: true, delivery_date: '2026-09-25', recipient: 'guardian@example.test', subject: 'daily', body: 'body', digest_sha256: 'b'.repeat(64), claim_token: 'f'.repeat(32),
      }), { status: 200 })
      resultBodies.push(String(init?.body))
      return new Response('{}', { status: 200 })
    })
    const fetchImpl = fetchSpy as typeof fetch
    const coordinator = new PilotTestEmailCoordinator(
      { enabled: true, recipient: 'guardian@example.test', sendHour: 22 },
      { runtime, fetchImpl, smtpSubmit: async () => { throw new Error('CHANNEL_NOT_CONFIGURED') } },
    )
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(JSON.parse(resultBodies[0])).toMatchObject({ status: 'failed', error_code: 'CHANNEL_NOT_CONFIGURED' })
  })

  it('preserves unknown SMTP acceptance without automatic resubmission', async () => {
    let request = 0
    const fetchImpl = vi.fn(async (_input: URL | RequestInfo, init?: RequestInit) => {
      request += 1
      if (request === 1) return new Response(JSON.stringify({
        due: true, delivery_date: '2026-09-25', recipient: 'guardian@example.test', subject: 'daily', body: 'body', digest_sha256: 'c'.repeat(64), claim_token: 'f'.repeat(32),
      }), { status: 200 })
      expect(JSON.parse(String(init?.body))).toMatchObject({ status: 'unknown', error_code: 'SMTP_DELIVERY_UNKNOWN' })
      return new Response('{}', { status: 200 })
    }) as typeof fetch
    const coordinator = new PilotTestEmailCoordinator(
      { enabled: true, recipient: 'guardian@example.test', sendHour: 22 },
      { runtime, fetchImpl, smtpSubmit: async () => { throw new Error('SMTP_DELIVERY_UNKNOWN') } },
    )
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it('does nothing while the local service is not ready', async () => {
    const fetchImpl = vi.fn()
    const coordinator = new PilotTestEmailCoordinator(
      { enabled: true, recipient: 'guardian@example.test', sendHour: 22 },
      { runtime: () => ({ ...runtime(), status: 'starting' }), fetchImpl: fetchImpl as typeof fetch },
    )
    coordinator.start()
    await coordinator.checkNow()
    coordinator.stop()
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

describe('authoritative email result diagnostics', () => {
  it.each([
    ['processing', 'pilot_email_result_unknown', 'running'],
    ['sent', 'pilot_email_sent', 'sent'],
    ['failed', 'pilot_email_delivery_failed', 'failed'],
    ['unknown', 'pilot_email_result_unknown', 'unknown'],
  ])('reports backend %s after local SMTP timeout', async (serverStatus, expectedEvent, expectedStatus) => {
    const diagnostic = vi.fn()
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(Response.json({due:true,delivery_date:'2026-11-03',recipient:'guardian@example.test',subject:'public',body:'synthetic',digest_sha256:'a'.repeat(64),claim_token:'f'.repeat(32)}))
      .mockResolvedValueOnce(Response.json({status:serverStatus,transport:'smtp',error_code:'CHANNEL_NOT_CONFIGURED',private:'never log'})) as typeof fetch
    const coordinator = new PilotTestEmailCoordinator({enabled:true,recipient:'guardian@example.test',sendHour:22},{runtime,fetchImpl,diagnostic,smtpSubmit:async()=>{throw new Error('SMTP_DELIVERY_UNKNOWN')}})
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(diagnostic).toHaveBeenCalledTimes(1)
    expect(diagnostic).toHaveBeenCalledWith(expectedEvent, expect.objectContaining({status:expectedStatus}))
    if (serverStatus === 'sent' || serverStatus === 'processing') {
      expect(JSON.stringify(diagnostic.mock.calls)).not.toContain('SMTP_DELIVERY_UNKNOWN')
      expect(diagnostic).not.toHaveBeenCalledWith('pilot_email_delivery_failed',expect.anything())
    }
    expect(JSON.stringify(diagnostic.mock.calls)).not.toContain('never log')
  })
})

describe('persisted diagnostic recipient', () => {
  it('sends only to a unique persisted recipient using the normal claim flow', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(Response.json({ recipient: 'guardian@example.test' }))
      .mockResolvedValueOnce(Response.json({ due: true, delivery_date: '2026-11-03', recipient: 'guardian@example.test', subject: 'x', body: 'x', digest_sha256: 'a'.repeat(64), claim_token: 'f'.repeat(32) }))
      .mockResolvedValueOnce(Response.json({ status: 'sent' })) as typeof fetch
    const smtpSubmit = vi.fn(async () => ({ accepted: true as const, transport: 'smtp', client: 'configured_smtp' }))
    const coordinator = new PilotTestEmailCoordinator({ enabled: true, recipient: '', sendHour: 22 }, { runtime, fetchImpl, smtpSubmit })
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(smtpSubmit).toHaveBeenCalledWith(expect.objectContaining({ recipient: 'guardian@example.test' }), expect.anything(), expect.anything())
  })

  it('does not claim or send when the server has no unique authorized recipient', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(Response.json({ recipient: '' })) as typeof fetch
    const smtpSubmit = vi.fn()
    const coordinator = new PilotTestEmailCoordinator({ enabled: true, recipient: '', sendHour: 22 }, { runtime, fetchImpl, smtpSubmit })
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(smtpSubmit).not.toHaveBeenCalled()
  })

  it('uses the unique persisted recipient and refuses a mismatched claim', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(Response.json({ recipient: 'guardian@example.test' }))
      .mockResolvedValueOnce(Response.json({ due: true, delivery_date: '2026-11-03', recipient: 'other@example.test', subject: 'x', body: 'x', digest_sha256: 'a'.repeat(64), claim_token: 'f'.repeat(32) })) as typeof fetch
    const smtpSubmit = vi.fn()
    const coordinator = new PilotTestEmailCoordinator({ enabled: true, recipient: '', sendHour: 22 }, { runtime, fetchImpl, smtpSubmit })
    coordinator.start(); await coordinator.checkNow(); coordinator.stop()
    expect(new URL(String(vi.mocked(fetchImpl).mock.calls[1][0])).searchParams.get('recipient')).toBe('guardian@example.test')
    expect(smtpSubmit).not.toHaveBeenCalled()
  })
})
