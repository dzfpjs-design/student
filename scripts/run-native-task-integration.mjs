import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const python = process.env.HSC_TEST_PYTHON ?? join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
if (!existsSync(python)) {
  console.error('原生任务集成测试未运行：请准备项目 Python 虚拟环境，或设置 HSC_TEST_PYTHON 指向已安装本地服务依赖的解释器。')
  process.exit(1)
}
console.info('运行合成 native task + loopback/SQLite 集成测试；此测试不使用真实麦克风或云模型。')
const result = spawnSync(process.execPath, [join(root, 'node_modules/vitest/vitest.mjs'), 'run',
  'src/renderer/src/NativeTaskVertical.test.tsx', '--reporter=dot'], {
  cwd: root, env: {...process.env, HSC_RUN_NATIVE_TASK_INTEGRATION: '1', HSC_TEST_PYTHON: python}, stdio: 'inherit',
})
if (result.error) {
  console.error('原生任务集成测试启动失败：', result.error.message)
  process.exit(1)
}
process.exit(result.status ?? 1)
