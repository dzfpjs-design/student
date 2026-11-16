import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'

const candidates = process.platform === 'win32'
  ? [resolve('.venv', 'Scripts', 'python.exe'), 'python']
  : [resolve('.venv', 'bin', 'python'), 'python3']
const executable = candidates.find((candidate, index) => index > 0 || existsSync(candidate))
if (!executable) throw new Error('找不到项目 Python 运行时。')
const result = spawnSync(executable, process.argv.slice(2), { stdio: 'inherit' })
if (result.error) throw result.error
process.exit(result.status ?? 1)
