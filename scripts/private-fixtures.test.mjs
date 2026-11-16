import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { checkFixtures, githubFetcher, restoreFixtures, group, MAINTAINER, PROTOTYPES } from './private-fixtures.mjs'

const sha = value => createHash('sha256').update(value).digest('hex')
const entries = [
  { asset: 'photo.png', path: 'src/assets/photo.png', sha256: sha('synthetic photo') },
  { asset: 'model.onnx', path: 'services/models/model.onnx', sha256: sha('synthetic model') },
]
async function harness(fn) {
  const root = await mkdtemp(join(tmpdir(), 'hsc-fixtures-test-'))
  try { await fn(root) } finally { await rm(root, { recursive: true, force: true }) }
}
const fetchGood = async (name, destination) => writeFile(join(destination, name), name === 'photo.png' ? 'synthetic photo' : 'synthetic model')

test('maintainer signer is a separate pinned optional resource and is not in normal runtime groups', () => {
  assert.equal(group('maintainer'), MAINTAINER)
  assert.equal(MAINTAINER.length, 1)
  assert.equal(MAINTAINER[0].bytes, 116216)
  assert.match(MAINTAINER[0].sha256, /^[a-f0-9]{64}$/)
  assert.equal(group('all').some(item => item.path === MAINTAINER[0].path), false)
})

test('missing files are reported; restoration is exact and repeated invocation is safe', () => harness(async root => {
  assert.deepEqual((await checkFixtures(root, entries)).map(x => x.state), ['missing', 'missing'])
  assert.deepEqual(await restoreFixtures(root, fetchGood, entries), { restored: 2, alreadyPresent: 0 })
  assert.equal(await readFile(join(root, entries[0].path), 'utf8'), 'synthetic photo')
  assert.deepEqual((await checkFixtures(root, entries)).map(x => x.state), ['valid', 'valid'])
  assert.deepEqual(await restoreFixtures(root, () => { throw Error('must not fetch') }, entries), { restored: 0, alreadyPresent: 2 })
}))
test('hash mismatch aborts before installing any downloaded resource', () => harness(async root => {
  await assert.rejects(restoreFixtures(root, async (name, destination) => writeFile(join(destination, name), name === 'photo.png' ? 'synthetic photo' : 'bad'), entries), /FIXTURE_HASH_MISMATCH/)
  assert.deepEqual((await checkFixtures(root, entries)).map(x => x.state), ['missing', 'missing'])
}))
test('different existing user file is never overwritten', () => harness(async root => {
  await mkdir(join(root, 'src/assets'), { recursive: true })
  await writeFile(join(root, entries[0].path), 'user file')
  await assert.rejects(restoreFixtures(root, fetchGood, entries), /FIXTURE_CONFLICT_EXISTING_FILE/)
  assert.equal(await readFile(join(root, entries[0].path), 'utf8'), 'user file')
}))
test('duplicate, traversal and unsafe target directory are refused', () => harness(async root => {
  await assert.rejects(restoreFixtures(root, fetchGood, [entries[0], entries[0]]), /FIXTURE_MANIFEST_DUPLICATE/)
  await assert.rejects(restoreFixtures(root, fetchGood, [{ ...entries[0], path: '../secret' }]), /FIXTURE_MANIFEST_INVALID/)
  const external = await mkdtemp(join(tmpdir(), 'hsc-fixture-external-'))
  try {
    await symlink(external, join(root, 'src'))
    await assert.rejects(restoreFixtures(root, fetchGood, entries), /FIXTURE_DIRECTORY_UNSAFE/)
  } finally { await rm(external, { recursive: true, force: true }) }
}))
test('release coordinates reject arguments or wildcard injection', () => {
  assert.throws(() => githubFetcher('owner/repo --confirm', 'tag'), /PRIVATE_RELEASE_COORDINATES_INVALID/)
  assert.throws(() => githubFetcher('owner/repo', '../tag'), /PRIVATE_RELEASE_COORDINATES_INVALID/)
})
test('remote fetch refuses public repos, size or digest mismatch without downloading', async () => {
  const entry = { ...entries[0], bytes: 15 }
  const calls = []
  const run = (_cmd, args) => {
    calls.push(args)
    if (args[0] === 'repo') return { status: 0, stdout: 'PRIVATE\n' }
    if (args[1] === 'view') return { status: 0, stdout: JSON.stringify({ assets: [{ name: entry.asset, size: 99, digest: `sha256:${entry.sha256}` }] }) }
    throw Error('must not download')
  }
  await assert.rejects(githubFetcher('owner/repo', 'v1', run)(entry.asset, '/tmp/none', entry), /PRIVATE_RELEASE_ASSET_MISMATCH/)
  assert.equal(calls.length, 2)
  await assert.rejects(githubFetcher('owner/repo', 'v1', () => ({ status: 0, stdout: 'PUBLIC\n' }))(entry.asset, '/tmp/none', entry), /PRIVATE_REPOSITORY_REQUIRED/)
  const wrongDigest = (_cmd, args) => args[0] === 'repo'
    ? { status: 0, stdout: 'PRIVATE\n' }
    : { status: 0, stdout: JSON.stringify({ assets: [{ name: entry.asset, size: entry.bytes, digest: `sha256:${'0'.repeat(64)}` }] }) }
  await assert.rejects(githubFetcher('owner/repo', 'v1', wrongDigest)(entry.asset, '/tmp/none', entry), /PRIVATE_RELEASE_ASSET_MISMATCH/)
})
test('prototype paths and names are unique and packaged under the expected directory', () => {
  assert.equal(PROTOTYPES.length, 9)
  assert.equal(new Set(PROTOTYPES.map(x => x.path)).size, 9)
  assert.ok(PROTOTYPES.every(x => x.path === `docs/prototypes/mistake-book-v1/assets/${x.asset}`))
})
test('backend tests require both fixture and model groups', () => {
  assert.equal(group('backend').length, 11)
  assert.equal(group('all').length, 20)
})
