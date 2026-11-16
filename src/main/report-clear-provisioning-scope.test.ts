// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { parse } from '@babel/parser'
import { transformSync } from 'esbuild'


function actualHandler() {
  const source = readFileSync(resolve('src/main/index.ts'), 'utf8')
  const tree = parse(source, { sourceType: 'module', plugins: ['typescript'] }); let statement = ''
  const visit = (node: any): void => {
    if (!node || typeof node !== 'object') return
    const call = node.type === 'ExpressionStatement' ? node.expression : null
    if (call?.type === 'CallExpression' && call.callee?.object?.name === 'ipcMain' && call.callee.property?.name === 'handle'
      && call.arguments[0]?.value === 'report-delivery:clear-secrets') statement = source.slice(node.start, node.end)
    for (const value of Object.values(node)) if (Array.isArray(value)) value.forEach(visit); else if (value && typeof value === 'object') visit(value)
  }
  visit(tree); if (!statement) throw Error('PUBLIC_REPORT_HANDLER_MISSING')
  let callback!: (event: unknown, names?: unknown) => Promise<unknown>
  const secrets: Record<string, string> = { smtpPassword: 'PUBLIC_SYNTHETIC_SMTP', wecomWebhook: 'PUBLIC_SYNTHETIC_WEBHOOK' }
  const slots: Record<string, string> = { model: 'pending', asr: 'pending', qwenRealtime: 'pending', smtpDefaults: 'pending' }
  const order: string[] = []
  const inspect = vi.fn(async () => ({ summary: { smtpPasswordConfigured: !!secrets.smtpPassword, wecomWebhookConfigured: !!secrets.wecomWebhook } }))
  const clear = vi.fn(async (names?: string[]) => { order.push('clear'); for (const name of names ?? ['smtpPassword', 'wecomWebhook']) delete secrets[name]; return { ...secrets } })
  const suppress = vi.fn(async (_root: string, names: string[]) => { order.push('suppress'); for (const name of names) slots[name] = 'suppressed' })
  const restart = vi.fn(async () => { order.push('restart') }), set = vi.fn()
  const env = { ipcMain: { handle: (_name: string, value: typeof callback) => { callback = value } }, app: { getPath: () => '/public/synthetic/not-opened' },
    reportDeliverySecrets: { inspect, clear }, suppressHouseholdProvisioningSlots: suppress, localService: { setReportDeliverySecrets: set, restart } }
  new Function(...Object.keys(env), transformSync(statement, { loader: 'ts', target: 'es2022' }).code)(...Object.values(env))
  return { callback, inspect, clear, suppress, restart, set, slots, order, secrets }
}

const illegal: Array<[string, unknown]> = [
  ['model slot', ['model']], ['ASR slot', ['asr']], ['Qwen slot', ['qwenRealtime']], ['defaults slot', ['smtpDefaults']],
  ['mixed report and model slots', ['smtpPassword', 'model']], ['unknown slot', ['other']], ['string', 'smtpPassword'],
  ['object', {}], ['array-like object', { 0: 'smtpPassword', length: 1 }], ['null', null], ['number', 1],
  ['undefined element', [undefined]], ['sparse array', Array(1)], ['nonstring element', ['smtpPassword', 1]],
]
it.each(illegal)('rejects %s before protected-store inspection or any mutation', async (_label, input) => {
  const f = actualHandler()
  await expect(f.callback({}, input)).rejects.toThrow('无效的送达通道凭据删除请求。')
  expect(f.inspect).not.toHaveBeenCalled(); expect(f.suppress).not.toHaveBeenCalled(); expect(f.clear).not.toHaveBeenCalled()
  expect(f.restart).not.toHaveBeenCalled(); expect(f.set).not.toHaveBeenCalled()
  expect(f.slots).toEqual({ model: 'pending', asr: 'pending', qwenRealtime: 'pending', smtpDefaults: 'pending' })
})

it.each([
  ['omitted', undefined, ['smtpPassword', 'wecomWebhook']], ['SMTP only', ['smtpPassword'], ['smtpPassword']],
  ['webhook only', ['wecomWebhook'], ['wecomWebhook']], ['both', ['smtpPassword', 'wecomWebhook'], ['smtpPassword', 'wecomWebhook']],
  ['empty original selection', [], []],
] as Array<[string, string[] | undefined, string[]]>)('retains %s with exactly the original report deletion range', async (_label, input, selected) => {
  const f = actualHandler(); await f.callback({}, input)
  expect(f.clear).toHaveBeenCalledExactlyOnceWith(input)
  expect(f.slots.model).toBe('pending'); expect(f.slots.asr).toBe('pending'); expect(f.slots.qwenRealtime).toBe('pending'); expect(f.slots.smtpDefaults).toBe('pending')
  if (selected.length) {
    expect(f.suppress).toHaveBeenCalledExactlyOnceWith('/public/synthetic/not-opened', selected)
    expect(f.order).toEqual(['suppress', 'clear', 'restart']); expect(f.restart).toHaveBeenCalledOnce()
  } else {
    expect(f.suppress).not.toHaveBeenCalled(); expect(f.restart).not.toHaveBeenCalled(); expect(f.order).toEqual(['clear'])
  }
})
