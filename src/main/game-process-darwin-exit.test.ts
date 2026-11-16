// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'

vi.mock('node:child_process', () => ({ execFile: vi.fn() }))
vi.mock('node:fs/promises', () => ({ realpath: vi.fn(), stat: vi.fn(), readFile: vi.fn(), readlink: vi.fn() }))
import { execFile } from 'node:child_process'
import { realpath, stat } from 'node:fs/promises'
import { queryProcessIdentity } from './game-process-identity'

afterEach(() => { vi.restoreAllMocks(); vi.resetAllMocks() })
function replies(outputs: string[]) {
  vi.spyOn(process, 'platform', 'get').mockReturnValue('darwin')
  vi.mocked(execFile).mockImplementation((...args: unknown[]) => {
    const callback = args[args.length - 1] as (error: unknown, out: { stdout: string; stderr: string }) => void
    callback(null, { stdout: outputs.shift() ?? '', stderr: '' })
    return {} as never
  })
}
it('does not resolve or signal a zombie executable name', async () => {
  replies(['Z Thu Sep 10 21:00:00 2026 (node)'])
  expect(await queryProcessIdentity(123)).toBeNull()
  expect(realpath).not.toHaveBeenCalled()
})
it('recognizes exit between ps and executable lookup, not all missing files as exit', async () => {
  replies(['S Thu Sep 10 21:00:00 2026 /node', '{ sec = 1 }', 'Z Thu Sep 10 21:00:00 2026 (node)'])
  vi.mocked(realpath).mockRejectedValue(Object.assign(new Error('gone'), { code: 'ENOENT' }))
  expect(await queryProcessIdentity(123)).toBeNull()
})
it('retains identity failure when an executable disappears but the process is still live', async () => {
  replies(['S Thu Sep 10 21:00:00 2026 /node', '{ sec = 1 }', 'S Thu Sep 10 21:00:00 2026 /node'])
  vi.mocked(realpath).mockRejectedValue(Object.assign(new Error('gone'), { code: 'ENOENT' }))
  await expect(queryProcessIdentity(123)).rejects.toThrow('gone')
  expect(stat).not.toHaveBeenCalled()
})
