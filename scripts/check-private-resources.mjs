import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { checkFixtures, group } from './private-fixtures.mjs'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
try {
  const name = process.argv[2] || 'all'
  const states = await checkFixtures(root, group(name))
  const failed = states.filter(item => item.state !== 'valid')
  if (failed.length) {
    console.error('项目需要的私有资源缺失或哈希不符：')
    for (const item of failed) console.error(`  ${item.path}: ${item.state}`)
    console.error('请先登录 gh，再按文档运行 node scripts/restore-private-resources.mjs <私有仓库 owner/name>；已有不同文件不会被覆盖。')
    process.exitCode = 1
  } else console.log(`私有资源校验通过：${name} (${states.length} 项)`)
} catch (error) { console.error(`私有资源检查失败：${error.message}`); process.exitCode = 1 }
