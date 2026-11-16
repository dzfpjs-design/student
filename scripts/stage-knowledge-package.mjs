import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { copyFile, mkdir, open, readFile, rename, stat, unlink, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { basename, join, resolve } from 'node:path'

const root = process.cwd()
const source = resolve(
  process.env.HSC_ATOMIC_GRAPH_SOURCE
    ?? join(homedir(), '.home-study-companion', 'knowledge-build', 'atomic-full-v6.db')
)
const outputDir = resolve(root, 'dist/knowledge')
const output = resolve(outputDir, 'atomic-knowledge.db')
const temporary = `${output}.staging`
const manifestPath = resolve(outputDir, 'atomic-knowledge-manifest.json')

async function sha256(path) {
  const hash = createHash('sha256')
  await new Promise((resolveStream, reject) => {
    const stream = createReadStream(path)
    stream.on('data', (chunk) => hash.update(chunk))
    stream.on('end', resolveStream)
    stream.on('error', reject)
  })
  return hash.digest('hex')
}

async function assertSqlite(path) {
  const handle = await open(path, 'r')
  try {
    const header = Buffer.alloc(16)
    await handle.read(header, 0, header.length, 0)
    if (header.toString('ascii') !== 'SQLite format 3\u0000') {
      throw new Error('知识库不是有效的 SQLite 3 文件。')
    }
  } finally {
    await handle.close()
  }
}

const sourceInfo = await stat(source).catch(() => null)
if (!sourceInfo?.isFile() || sourceInfo.size < 100_000_000) {
  throw new Error(`原子知识库源文件缺失或过小：${source}`)
}
await assertSqlite(source)
await mkdir(outputDir, { recursive: true })
const sourceHash = await sha256(source)

let staged = false
const existingManifest = await readFile(manifestPath, 'utf8')
  .then((value) => JSON.parse(value))
  .catch(() => null)
const outputInfo = await stat(output).catch(() => null)
// A previous manifest describes the bytes at staging time, not necessarily the
// current output. Same-size corruption must never be silently reused.
const outputMatches = existingManifest?.sha256 === sourceHash
  && outputInfo?.isFile() && outputInfo.size === sourceInfo.size
  && await sha256(output).catch(() => null) === sourceHash
if (!outputMatches) {
  await unlink(temporary).catch(() => undefined)
  await copyFile(source, temporary)
  await assertSqlite(temporary)
  const copiedHash = await sha256(temporary)
  if (copiedHash !== sourceHash) {
    await unlink(temporary).catch(() => undefined)
    throw new Error('知识库暂存副本校验失败。')
  }
  await rename(temporary, output)
  staged = true
}

const manifest = {
  schema: 'hsc.atomic-knowledge.package.v1',
  source_label: basename(source),
  packaged_name: 'atomic-knowledge.db',
  bytes: sourceInfo.size,
  sha256: sourceHash,
  read_only_runtime: true,
  atomic_extraction_resumed: false,
}
await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')
console.log(JSON.stringify({ staged, bytes: sourceInfo.size, sha256: sourceHash }))
