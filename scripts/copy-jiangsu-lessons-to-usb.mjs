import { execFileSync } from 'node:child_process'
import { createReadStream, createWriteStream } from 'node:fs'
import { copyFile, mkdir, opendir, readFile, stat, writeFile } from 'node:fs/promises'
import { basename, join, relative, resolve } from 'node:path'
import { homedir } from 'node:os'
import process from 'node:process'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) args.set(process.argv[index], process.argv[index + 1])
const mode = args.get('--mode') ?? 'plan'
const destinationRoot = resolve(args.get('--destination') ?? '/Volumes/KINGSTON')
const sourceRoot = resolve(args.get('--source') ?? join(homedir(), 'seyk_kb'))
const videoRoot = join(sourceRoot, 'videos')
const transcriptRoot = join(sourceRoot, 'transcripts')
const targetRoot = join(destinationRoot, '家庭学习伙伴-江苏讲解视频')
const checkpointPath = join(targetRoot, 'copy-checkpoint.json')

async function filesUnder(root) {
  const result = []
  async function walk(dir) {
    const handle = await opendir(dir)
    for await (const entry of handle) {
      const path = join(dir, entry.name)
      if (entry.isDirectory()) await walk(path)
      else if (entry.isFile()) {
        const info = await stat(path)
        result.push({ path, relative_path: relative(root, path), bytes: info.size })
      }
    }
  }
  await walk(root)
  return result.sort((left, right) => left.relative_path.localeCompare(right.relative_path, 'zh-CN'))
}

function freeBytes(path) {
  const output = execFileSync('df', ['-Pk', path], { encoding: 'utf8' }).trim().split('\n').at(-1)
  const fields = output.trim().split(/\s+/)
  return Number(fields[3]) * 1024
}

async function copyVerified(source, target, bytes) {
  await mkdir(resolve(target, '..'), { recursive: true })
  await new Promise((done, fail) => {
    const input = createReadStream(source)
    const output = createWriteStream(target)
    input.on('error', fail); output.on('error', fail); output.on('finish', done); input.pipe(output)
  })
  const copied = await stat(target)
  if (copied.size !== bytes) throw new Error(`文件大小校验失败：${basename(source)}`)
}

const destinationInfo = await stat(destinationRoot).catch(() => null)
if (!destinationInfo?.isDirectory()) throw new Error(`找不到目标 U 盘：${destinationRoot}`)
const videos = await filesUnder(videoRoot)
const transcripts = await filesUnder(transcriptRoot)
const videoBytes = videos.reduce((sum, item) => sum + item.bytes, 0)
const transcriptBytes = transcripts.reduce((sum, item) => sum + item.bytes, 0)
const databaseBytes = (await stat(join(sourceRoot, 'seyk_kb.db'))).size
const totalBytes = videoBytes + transcriptBytes + databaseBytes
const availableBytes = freeBytes(destinationRoot)
await mkdir(targetRoot, { recursive: true })
const manifest = {
  schema: 'hsc.jiangsu-lessons-usb.v1',
  source_label: 'seyk_kb',
  video_count: videos.length,
  video_bytes: videoBytes,
  transcript_count: transcripts.length,
  transcript_bytes: transcriptBytes,
  index_database_bytes: databaseBytes,
  total_bytes: totalBytes,
  destination_available_bytes: availableBytes,
  complete_copy_possible: availableBytes >= totalBytes + 5_000_000_000,
  video_files: videos.map(({ relative_path, bytes }) => ({ relative_path, bytes })),
  transcript_files: transcripts.map(({ relative_path, bytes }) => ({ relative_path, bytes })),
}
await writeFile(join(targetRoot, 'video-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')
await writeFile(join(targetRoot, '请先阅读.txt'), [
  `江苏讲解视频共 ${videos.length} 个，配套转录文本 ${transcripts.length} 份，连同索引库合计 ${(totalBytes / 1024 ** 3).toFixed(1)} GiB。`,
  `当前 U 盘可用 ${(availableBytes / 1024 ** 3).toFixed(1)} GiB。`,
  manifest.complete_copy_possible ? '容量足够，可执行完整断点拷贝。' : '容量不足，未拷贝部分视频，避免把残缺资料误认为完整知识库。请更换至少 256GB、建议 512GB 的 U 盘。',
  'video-manifest.json 是视频、转录和索引库的完整清单；copy-checkpoint.json 记录断点和逐文件大小校验。',
].join('\r\n'), 'utf8')

if (mode === 'plan') {
  console.log(JSON.stringify({ mode, video_count: videos.length, transcript_count: transcripts.length, total_bytes: totalBytes, available_bytes: availableBytes, copy_started: false }))
  process.exit(0)
}
if (mode !== 'copy') throw new Error('mode must be plan or copy')
if (!manifest.complete_copy_possible) throw new Error('U 盘容量不足，未开始不完整视频拷贝。')

const prior = await readFile(checkpointPath, 'utf8').then(JSON.parse).catch(() => ({ completed: [] }))
const completed = new Set(Array.isArray(prior.completed) ? prior.completed : [])
const transcriptCompleted = new Set(Array.isArray(prior.transcript_completed) ? prior.transcript_completed : [])
const saveCheckpoint = async () => writeFile(checkpointPath, `${JSON.stringify({ completed: [...completed], transcript_completed: [...transcriptCompleted], total: videos.length, transcript_total: transcripts.length }, null, 2)}\n`, 'utf8')
for (const item of videos) {
  if (completed.has(item.relative_path)) continue
  await copyVerified(item.path, join(targetRoot, 'videos', item.relative_path), item.bytes)
  completed.add(item.relative_path)
  await saveCheckpoint()
}
for (const item of transcripts) {
  if (transcriptCompleted.has(item.relative_path)) continue
  await copyVerified(item.path, join(targetRoot, 'transcripts', item.relative_path), item.bytes)
  transcriptCompleted.add(item.relative_path)
  await saveCheckpoint()
}
await copyFile(join(sourceRoot, 'seyk_kb.db'), join(targetRoot, 'seyk_kb.db'))
console.log(JSON.stringify({ mode, completed: completed.size, total: videos.length, transcript_completed: transcriptCompleted.size, transcript_total: transcripts.length, verified_by_size: true }))
