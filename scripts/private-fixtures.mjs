import { createHash, randomBytes } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { copyFile, lstat, link, mkdir, mkdtemp, rm, unlink } from 'node:fs/promises'
import { constants } from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'

// These are checksums only; bytes remain in an authenticated private release.
export const FIXTURES = Object.freeze([
  { asset: 'pet-study-room.png', path: 'src/renderer/src/assets/pet-study-room.png', bytes: 2147269, sha256: 'f6158823b5b6adf9f2df056aa32e3ede0ea4a8d3b13e80fcf412bb561b05e80e' },
  { asset: 'marked-answer-auto-capture.png', path: 'services/local_agent/tests/fixtures/marked-answer-auto-capture.png', bytes: 74440, sha256: '8fa690537ddb33facbf8233853cae3531c658486c2a3cb9cd46c0db9405e2466' },
  { asset: 'writingPaperFrame.json.gz', path: 'src/renderer/src/test/writingPaperFrame.json.gz', bytes: 18507, sha256: 'bd3412498ed18f9abb18bcfa7d865a34c8feb90ee74b77652d53cd57ba847d62' },
  { asset: 'short-mandarin-dui.wav', path: 'services/local_agent/tests/fixtures/short-mandarin-dui.wav', bytes: 15266, sha256: '9e2f53b24869195f0032e243f159da4ad5dc6d65a50ae07a228434c07ba7d8b3' },
  { asset: 'short-mandarin-dui-trimmed.wav', path: 'services/local_agent/tests/fixtures/short-mandarin-dui-trimmed.wav', bytes: 6330, sha256: '14e7a6d5ccfd4f6de18d8c71e2a3afe5ddcec9773b6b60245bbcc87c85d082b5' },
])
export const MODELS = Object.freeze([
  { asset: 'sherpa-encoder.int8.onnx', path: 'services/local_agent/models/sherpa-onnx-zipformer-zh-en-2023-11-22/encoder-epoch-34-avg-19.int8.onnx', bytes: 69037476, sha256: '28780a73b0fc5a05ec579ad4dbbc7d8fcd09fed5e6c431f84dcaab8f8170d9bf' },
  { asset: 'sherpa-decoder.onnx', path: 'services/local_agent/models/sherpa-onnx-zipformer-zh-en-2023-11-22/decoder-epoch-34-avg-19.onnx', bytes: 5165101, sha256: '110dc19143f0731ce0f64513e524d7d8f729e37107e1392da790f66d6c936a0b' },
  { asset: 'sherpa-joiner.int8.onnx', path: 'services/local_agent/models/sherpa-onnx-zipformer-zh-en-2023-11-22/joiner-epoch-34-avg-19.int8.onnx', bytes: 1033479, sha256: '498861ff0963ecbbff101a940ed533a715e5d68ee96431a3b092d0ca00c3ba38' },
  { asset: 'sherpa-tokens.txt', path: 'services/local_agent/models/sherpa-onnx-zipformer-zh-en-2023-11-22/tokens.txt', bytes: 25645, sha256: '2d5d6245197582cea396b23fa29d592705be31bac9a97bb74a8d1bca3ca6a0e0' },
  { asset: 'bge-small-model_optimized.onnx', path: 'services/local_agent/models/bge-small-zh-v1.5/model_optimized.onnx', bytes: 94781076, sha256: '1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38' },
  { asset: 'bge-small-tokenizer.json', path: 'services/local_agent/models/bge-small-zh-v1.5/tokenizer.json', bytes: 439125, sha256: '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26' },
])
export const PROTOTYPES = Object.freeze([
  { asset: 'capture-sheet.jpg', path: 'docs/prototypes/mistake-book-v1/assets/capture-sheet.jpg', bytes: 165779, sha256: 'c3a117ef148f665f9333e29d4fe87fc3e8010d0bb8a84d923bdc3fd135aa9871' },
  { asset: 'english-tense-1.jpg', path: 'docs/prototypes/mistake-book-v1/assets/english-tense-1.jpg', bytes: 38070, sha256: '7104c939043ee08ef6f9410871a6c179307ebfb1e8bd832cf8cd6b11bf5efd49' },
  { asset: 'english-tense-2.jpg', path: 'docs/prototypes/mistake-book-v1/assets/english-tense-2.jpg', bytes: 40729, sha256: 'e78e95c4d33cec733aafb1c2254637c68bcc8d5040aef8221fca3cd8949a34f0' },
  { asset: 'math-fractions-1.jpg', path: 'docs/prototypes/mistake-book-v1/assets/math-fractions-1.jpg', bytes: 35360, sha256: 'ca2f5e599a4979e05957ffce706de5bd81f1e835491ba54d6399ebb0e8a9c7e6' },
  { asset: 'math-fractions-2.jpg', path: 'docs/prototypes/mistake-book-v1/assets/math-fractions-2.jpg', bytes: 33500, sha256: 'e8320bc9f6dcfa1afaee4e400eb0ba941b6c8d663b3c3a724f2b940f15e99d6b' },
  { asset: 'math-fractions-3.jpg', path: 'docs/prototypes/mistake-book-v1/assets/math-fractions-3.jpg', bytes: 39880, sha256: '20d9d8598c159751f5749f3fc346cfa00ece96e662650fdeb47402e8dcbec0c9' },
  { asset: 'math-fractions-4.jpg', path: 'docs/prototypes/mistake-book-v1/assets/math-fractions-4.jpg', bytes: 30988, sha256: '0a720b37cfc3cd76382a8efd506a31d25fd2856e02f9dc582a20624db4626e7b' },
  { asset: 'physics-heat-1.jpg', path: 'docs/prototypes/mistake-book-v1/assets/physics-heat-1.jpg', bytes: 42371, sha256: '628f761712dceacf79eb4264de7d2c23ea49f2456c3c2db5f3c0d404cab18c98' },
  { asset: 'physics-heat-2.jpg', path: 'docs/prototypes/mistake-book-v1/assets/physics-heat-2.jpg', bytes: 45591, sha256: '8006eaa96e5c2fe9a174e8d8ccbfc550960aeae3c618282ecfe6ff394f3f19e9' },
])
// This private executable contains no signing key; it remains platform-specific.
export const MAINTAINER = Object.freeze([
  { asset: 'publisher-macos-arm64', path: 'scripts/pet-catalog-maintainer/publisher-macos-arm64', bytes: 116216, sha256: 'bfb94d7c6dd8f3fcf4f26fe48c7dec4066d25c874b50754a451ab9d51075c8eb' },
])
export function group(name) {
  if (name === 'build') return FIXTURES.slice(0, 1)
  if (name === 'test') return FIXTURES
  if (name === 'backend') return [...FIXTURES, ...MODELS]
  if (name === 'models') return MODELS
  if (name === 'prototypes') return PROTOTYPES
  if (name === 'maintainer') return MAINTAINER
  if (name === 'all') return [...FIXTURES, ...MODELS, ...PROTOTYPES]
  throw Error('PRIVATE_RESOURCE_GROUP_INVALID')
}

async function digest(path) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(path)) hash.update(chunk)
  return hash.digest('hex')
}
function checkedEntry(root, item) {
  if (!/^[A-Za-z0-9._-]+$/.test(item.asset) || !/^[0-9a-f]{64}$/.test(item.sha256)
      || isAbsolute(item.path) || item.path.split('/').some(part => !part || part === '..' || part === '.')) throw Error('FIXTURE_MANIFEST_INVALID')
  const absolute = resolve(root, item.path), rel = relative(resolve(root), absolute)
  if (!rel || rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) throw Error('FIXTURE_PATH_OUTSIDE_REPOSITORY')
  return absolute
}
async function safeDirectories(root, target, create = true) {
  const r = resolve(root), rel = relative(r, dirname(target))
  let current = r
  const rootStat = await lstat(r)
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) throw Error('FIXTURE_REPOSITORY_UNSAFE')
  for (const part of rel.split(sep).filter(Boolean)) {
    current = join(current, part)
    if (create) { try { await mkdir(current) } catch (e) { if (e.code !== 'EEXIST') throw e } }
    else { try { await lstat(current) } catch (e) { if (e.code === 'ENOENT') return false; throw e } }
    const info = await lstat(current)
    if (!info.isDirectory() || info.isSymbolicLink()) throw Error('FIXTURE_DIRECTORY_UNSAFE')
  }
  return true
}
async function currentState(target, expected) {
  try {
    const st = await lstat(target)
    if (!st.isFile() || st.isSymbolicLink()) return 'conflict'
    return await digest(target) === expected ? 'valid' : 'conflict'
  } catch (error) { if (error.code === 'ENOENT') return 'missing'; throw error }
}
export async function checkFixtures(root, entries = FIXTURES) {
  const results = []
  for (const e of entries) {
    const path = checkedEntry(root, e)
    const present = await safeDirectories(root, path, false)
    results.push({ path: e.path, state: present ? await currentState(path, e.sha256) : 'missing' })
  }
  return results
}
/** fetch(asset, outputDir) must write exactly outputDir/asset. */
export async function restoreFixtures(root, fetch, entries = FIXTURES) {
  const names = new Set(), paths = new Set()
  for (const e of entries) {
    checkedEntry(root, e)
    if (names.has(e.asset) || paths.has(e.path)) throw Error('FIXTURE_MANIFEST_DUPLICATE')
    names.add(e.asset); paths.add(e.path)
  }
  const before = await checkFixtures(root, entries)
  if (before.some(r => r.state === 'conflict')) throw Error('FIXTURE_CONFLICT_EXISTING_FILE')
  const pending = entries.filter((_, i) => before[i].state === 'missing')
  if (!pending.length) return { restored: 0, alreadyPresent: entries.length }
  const scratch = await mkdtemp(join(tmpdir(), 'hsc-private-fixtures-'))
  try {
    // Validate all downloaded bytes before changing any repository path.
    for (const entry of pending) {
      await fetch(entry.asset, scratch, entry)
      const downloaded = join(scratch, entry.asset), info = await lstat(downloaded)
      if (!info.isFile() || info.isSymbolicLink() || (entry.bytes !== undefined && info.size !== entry.bytes) || await digest(downloaded) !== entry.sha256) throw Error(`FIXTURE_HASH_MISMATCH: ${entry.asset}`)
    }
    for (const entry of pending) {
      const target = checkedEntry(root, entry)
      await safeDirectories(root, target)
      const stage = join(dirname(target), `.hsc-fixture-${randomBytes(12).toString('hex')}.tmp`)
      try {
        await copyFile(join(scratch, entry.asset), stage, constants.COPYFILE_EXCL)
        if (await digest(stage) !== entry.sha256) throw Error('FIXTURE_STAGE_HASH_MISMATCH')
        try { await link(stage, target) } catch (error) {
          if (error.code !== 'EEXIST' || await currentState(target, entry.sha256) !== 'valid') throw error
        }
      } finally { await unlink(stage).catch(() => {}) }
    }
    const after = await checkFixtures(root, entries)
    if (after.some(r => r.state !== 'valid')) throw Error('FIXTURE_RESTORE_NOT_VERIFIED')
    return { restored: pending.length, alreadyPresent: entries.length - pending.length }
  } finally { await rm(scratch, { recursive: true, force: true }) }
}
export function githubFetcher(repo, tag, run = spawnSync) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) || !/^[A-Za-z0-9_.-]+$/.test(tag)) throw Error('PRIVATE_RELEASE_COORDINATES_INVALID')
  let assets
  return async (asset, dir, expected) => {
    if (!assets) {
      const visibility = run('gh', ['repo', 'view', repo, '--json', 'visibility', '--jq', '.visibility'], { stdio: 'pipe', encoding: 'utf8' })
      if (visibility.error || visibility.status !== 0 || visibility.stdout.trim() !== 'PRIVATE') throw Error('PRIVATE_REPOSITORY_REQUIRED: 无法确认资源仓库为私有且可访问')
      const view = run('gh', ['release', 'view', tag, '--repo', repo, '--json', 'assets'], { stdio: 'pipe', encoding: 'utf8' })
      if (view.error || view.status !== 0) throw Error('PRIVATE_RELEASE_UNAVAILABLE: 确认 gh 已登录并可访问私有资源仓库')
      try { assets = JSON.parse(view.stdout).assets } catch { throw Error('PRIVATE_RELEASE_METADATA_INVALID') }
      if (!Array.isArray(assets)) throw Error('PRIVATE_RELEASE_METADATA_INVALID')
    }
    const matching = assets.filter(item => item.name === asset)
    if (matching.length !== 1 || matching[0].size !== expected.bytes || (matching[0].digest && matching[0].digest !== `sha256:${expected.sha256}`)) throw Error(`PRIVATE_RELEASE_ASSET_MISMATCH: ${asset}`)
    const result = run('gh', ['release', 'download', tag, '--repo', repo, '--pattern', asset, '--dir', dir], { stdio: 'pipe', encoding: 'utf8' })
    if (result.error || result.status !== 0) throw Error('PRIVATE_RELEASE_DOWNLOAD_FAILED: 确认 gh 已登录并可访问私有资源仓库')
  }
}
