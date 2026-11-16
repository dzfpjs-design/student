import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it } from 'vitest'
import { validateControlledGameLaunch } from './controlled-game'

const roots: string[] = []
afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))))

describe('controlled game launch validation', () => {
  it('accepts only an existing executable and clamps the time budget', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-game-')); roots.push(root)
    const executable = join(root, 'math-game.exe')
    await writeFile(executable, 'fixture')
    await expect(validateControlledGameLaunch({
      path: executable, title: '数学挑战', maxSeconds: 99_999, gameSessionId: 'game-session-1'
    })).resolves.toMatchObject({ path: executable, maxSeconds: 3600, title: '数学挑战' })
  })

  it('rejects scripts and missing executables', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-game-')); roots.push(root)
    const script = join(root, 'game.cmd')
    await writeFile(script, 'exit 0')
    await expect(validateControlledGameLaunch({
      path: script, title: '脚本', maxSeconds: 60, gameSessionId: 'game-session-2'
    })).rejects.toThrow('Windows .exe')
    await expect(validateControlledGameLaunch({
      path: join(root, 'missing.exe'), title: '缺失', maxSeconds: 60, gameSessionId: 'game-session-3'
    })).rejects.toThrow()
  })
})
