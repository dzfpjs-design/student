import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { FIXTURES, MODELS, PROTOTYPES, MAINTAINER, githubFetcher, restoreFixtures } from './private-fixtures.mjs'
import { constants } from 'node:fs'
import { open } from 'node:fs/promises'
import { createHash } from 'node:crypto'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repo = process.argv[2]
const choice = process.argv[3] || 'all'
try {
  if (!repo || !['all', 'fixtures', 'models', 'prototypes', 'maintainer'].includes(choice)) throw Error('用法：node scripts/restore-private-resources.mjs owner/private-repo [all|fixtures|models|prototypes|maintainer]')
  if (choice === 'all' || choice === 'fixtures') {
    const out = await restoreFixtures(root, githubFetcher(repo, 'hsc-private-dev-fixtures-v1'), FIXTURES)
    console.log(`界面与测试资源：恢复 ${out.restored}，已存在 ${out.alreadyPresent}`)
  }
  if (choice === 'all' || choice === 'models') {
    const out = await restoreFixtures(root, githubFetcher(repo, 'hsc-private-models-v1'), MODELS)
    console.log(`离线模型：恢复 ${out.restored}，已存在 ${out.alreadyPresent}`)
  }
  if (choice === 'all' || choice === 'prototypes') {
    const out = await restoreFixtures(root, githubFetcher(repo, 'hsc-private-prototype-assets-v1'), PROTOTYPES)
    console.log(`原型图片：恢复 ${out.restored}，已存在 ${out.alreadyPresent}`)
  }
  if (choice === 'maintainer') {
    if (process.platform !== 'darwin' || process.arch !== 'arm64') throw Error('维护者工具仅支持 macOS arm64；签名密钥须由本机 Keychain 单独提供。')
    const out = await restoreFixtures(root, githubFetcher(repo, 'hsc-pet-catalog-maintainer-v1'), MAINTAINER)
    const file = await open(resolve(root, MAINTAINER[0].path), constants.O_RDONLY | constants.O_NOFOLLOW)
    try {
      const info = await file.stat()
      if (!info.isFile() || info.size !== MAINTAINER[0].bytes || createHash('sha256').update(await file.readFile()).digest('hex') !== MAINTAINER[0].sha256) throw Error('MAINTAINER_RESOURCE_INVALID')
      await file.chmod(0o700)
    } finally { await file.close() }
    console.log(`维护者工具：恢复 ${out.restored}，已存在 ${out.alreadyPresent}；签名密钥未包含，请使用原维护机 Keychain。`)
  }
  console.log('资源恢复并逐项校验完成。')
} catch (error) { console.error(`资源未完全恢复：${error.message}`); process.exitCode = 1 }
