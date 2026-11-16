import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { closeSync, ftruncateSync, mkdirSync, mkdtempSync, openSync, readFileSync, readdirSync, rmSync, symlinkSync, utimesSync, writeFileSync, writeSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { inspectPackageTree, validateBackendBuildVersion, validateModelManifest, validatePackageCommands, validatePackageScope, validateWindowsAgent } from './windows-package-checks.mjs'
import { checkFreshness, probeSsh, sshProbeArgs } from './windows-release-readiness.mjs'

const here = dirname(fileURLToPath(import.meta.url))

test('backend source build version must match the desktop release without ambiguity', () => {
  const source = "BUILD_VERSION = '0.1.4'\n"
  const project = '[project]\nversion = "0.1.4"\n'
  assert.deepEqual(validateBackendBuildVersion('0.1.4', source, project), [])
  assert.notDeepEqual(validateBackendBuildVersion('0.1.4', "BUILD_VERSION = '0.1.3'\n", project), [])
  assert.notDeepEqual(validateBackendBuildVersion('0.1.4', source + "BUILD_VERSION = '0.1.4'\n", project), [])
  assert.notDeepEqual(validateBackendBuildVersion('0.1.4', source, '[project]\nversion = "0.1.3"\n'), [])
})
const manifest = () => ({ model_dir: 'model', files: { 'encoder.onnx': { minimum_bytes: 1, sha256: 'a'.repeat(64) } } })

test('installer smoke performs read-only installation guard before any cleanup or installer', () => {
  const smoke=readFileSync(join(here,'smoke-windows-installer.ps1'),'utf8')
  const guard=readFileSync(join(here,'windows-installation-guard.ps1'),'utf8')
  const index=smoke.indexOf('Assert-HscIsolatedInstallSafe -ReadRegistry')
  const recheck=smoke.indexOf('Assert-HscTargetRegistryAbsent $registryReader')
  assert.ok(index>0)
  assert.ok(recheck>index && recheck<smoke.indexOf('Start-Process -FilePath $installer.FullName'))
  for (const action of ['Stop-OwnedInstallProcesses $installDir','Start-Process -FilePath $installer.FullName']) {
    assert.ok(smoke.indexOf(action)>index,action)
  }
  assert.equal(smoke.includes('Remove-Item $installDir'),false)
  assert.ok(smoke.includes("[Guid]::NewGuid().ToString('N')"))
  for (const required of ['RegistryHive]::CurrentUser','RegistryHive]::LocalMachine','RegistryView]::Registry64','RegistryView]::Registry32','SMOKE_INSTALL_EXISTING_OR_UNCONFIRMED']) {
    assert.ok(guard.includes(required),required)
  }
})
const build = () => ({ files: ['out/**/*', 'package.json'], extraResources: [
  { from: 'dist/home-study-local-agent.exe', to: 'local-agent/home-study-local-agent.exe' },
  { from: 'dist/hsc-smtp-worker', to: 'local-agent/smtp-worker' },
  { from: 'services/local_agent/models', to: 'local-models' },
  { from: 'services/local_agent/model-manifest.json', to: 'local-models/model-manifest.json' },
  { from: 'dist/knowledge/atomic-knowledge.db', to: 'knowledge/atomic-knowledge.db' },
  { from: 'dist/knowledge/atomic-knowledge-manifest.json', to: 'knowledge/atomic-knowledge-manifest.json' },
] })
function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'hsc-package-test-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  return root
}
function write(root, path, content = 'fixture') {
  mkdirSync(dirname(join(root, path)), { recursive: true })
  writeFileSync(join(root, path), content)
}
function exe(root, { machine = 0x8664, magic = 0x20b, characteristics = 2, signature = 0x4550, offset = 128,
  sections = 1, optionalSize = 112, rawStart = 512, rawSize = 512 } = {}) {
  const path = join(root, 'agent.exe')
  const header = Buffer.alloc(512)
  header.write('MZ')
  header.writeUInt32LE(offset, 60)
  header.writeUInt32LE(signature, 128)
  header.writeUInt16LE(machine, 132)
  header.writeUInt16LE(sections, 134)
  header.writeUInt16LE(optionalSize, 148)
  header.writeUInt16LE(characteristics, 150)
  header.writeUInt16LE(magic, 152)
  header.writeUInt32LE(rawSize, 280)
  header.writeUInt32LE(rawStart, 284)
  const fd = openSync(path, 'w')
  try { writeSync(fd, header); ftruncateSync(fd, 5_000_000) } finally { closeSync(fd) }
  return path
}

test('manifest accepts valid hashes and refuses missing, empty and malformed declarations', () => {
  assert.deepEqual(validateModelManifest(manifest()), [])
  for (const value of [null, [], {}, { model_dir: 'model', files: {} }, { model_dir: 'model', files: [] }]) {
    assert.ok(validateModelManifest(value).length)
  }
  for (const entry of [null, {}, { minimum_bytes: 0, sha256: 'a'.repeat(64) }, { minimum_bytes: 1, sha256: 'bad' }]) {
    assert.ok(validateModelManifest({ ...manifest(), files: { 'model.onnx': entry } }).length)
  }
})

test('manifest rejects traversal, absolute paths, Windows alternate streams and separators', () => {
  for (const path of ['../private', '/private', 'C:/private', 'C:\\private', 'a/../b', 'a\\b', 'a//b', 'a:stream', '.',
    'model/.. /private', 'a\u0000b', 'a\nb', 'CON', 'nul.onnx', 'model.', 'model ', 'LPT1.txt']) {
    assert.ok(validateModelManifest({ ...manifest(), model_dir: path }).length, path)
    assert.ok(validateModelManifest({ ...manifest(), files: { [path]: manifest().files['encoder.onnx'] } }).length, path)
  }
})

test('package scope rejects broadened globs, duplicate and extra resources, and platform overrides', () => {
  assert.deepEqual(validatePackageScope(build()), [])
  for (const value of [null, {}, { ...build(), files: ['**/*'] },
    { ...build(), extraResources: [...build().extraResources, { from: 'family-data', to: 'family-data' }] },
    { ...build(), extraResources: [build().extraResources[0], build().extraResources[0], ...build().extraResources.slice(2)] },
    { ...build(), extraFiles: ['.env'] }, { ...build(), win: { extraResources: ['family-data'] } },
    { ...build(), extraResources: build().extraResources.map((entry) => ({ ...entry, filter: ['**/*'] })) }]) {
    assert.ok(validatePackageScope(value).length)
  }
})

test('payload scan permits public model fixtures and compiled assets', (t) => {
  const root = fixture(t)
  write(root, 'out/main/index.js')
  write(root, 'models/model/test_wavs/0.wav')
  assert.deepEqual(inspectPackageTree(root, 'out'), [])
  assert.deepEqual(inspectPackageTree(root, 'models'), [])
})

for (const path of ['family-data/child.txt', '.env.local', 'home-study.db-wal', 'backup.sqlite3', 'key.dpapi', 'runtime.jsonl', 'secret.pem']) {
  test(`payload scan rejects ${path} without returning sensitive contents`, (t) => {
    const root = fixture(t)
    write(root, `out/${path}`, 'DO_NOT_DISCLOSE_THIS_FIXTURE')
    const failures = inspectPackageTree(root, 'out')
    assert.ok(failures.length)
    assert.ok(!JSON.stringify(failures).includes('DO_NOT_DISCLOSE'))
  })
}

test('payload scan rejects symlinked directories without following their targets', (t) => {
  const root = fixture(t)
  write(root, 'private/sensitive.txt')
  mkdirSync(join(root, 'out'))
  symlinkSync(join(root, 'private'), join(root, 'out/linked'), process.platform === 'win32' ? 'junction' : 'dir')
  assert.deepEqual(inspectPackageTree(root, 'out'), ['Package payload contains a symbolic link'])
})

test('PE check accepts the header of an x64 executable (not a runtime smoke)', (t) => {
  assert.deepEqual(validateWindowsAgent(exe(fixture(t))), [])
})

for (const [name, options] of Object.entries({ x86: { machine: 0x14c }, arm64: { machine: 0xaa64 },
  dll: { characteristics: 0x2002 }, nonExecutable: { characteristics: 0 },
  pe32: { magic: 0x10b }, signature: { signature: 0 }, invalidOffset: { offset: 0xffffffff } })) {
  test(`PE check rejects ${name}`, (t) => assert.ok(validateWindowsAgent(exe(fixture(t), options)).length))
}

for (const [name, options] of Object.entries({ noSections: { sections: 0 }, tooManySections: { sections: 97 },
  noOptionalHeader: { optionalSize: 0 }, sectionBeyondEof: { rawStart: 4_999_999 },
  sectionInsideHeader: { rawStart: 1 }, sectionSizeOverflow: { rawSize: 0xffffffff } })) {
  test(`PE check rejects malformed section layout: ${name}`, (t) => assert.ok(validateWindowsAgent(exe(fixture(t), options)).length))
}

test('PE check rejects a missing, truncated or non-PE file and directories', (t) => {
  const root = fixture(t)
  assert.ok(validateWindowsAgent(join(root, 'missing')).length)
  assert.ok(validateWindowsAgent(root).length)
  write(root, 'small.exe', 'MZ')
  assert.ok(validateWindowsAgent(join(root, 'small.exe')).length)
  const path = exe(root)
  const fd = openSync(path, 'r+')
  try { writeSync(fd, Buffer.from('NO'), 0, 2, 0) } finally { closeSync(fd) }
  assert.ok(validateWindowsAgent(path).length)
})

test('freshness rejects stale or missing outputs, but not Python cache files', (t) => {
  const root = fixture(t)
  write(root, 'src/main.py')
  write(root, 'src/__pycache__/main.pyc')
  write(root, 'out/app.js')
  utimesSync(join(root, 'src/main.py'), 200, 200)
  utimesSync(join(root, 'out/app.js'), 100, 100)
  assert.equal(checkFreshness(root, ['out/app.js'], ['src']), false)
  utimesSync(join(root, 'out/app.js'), 300, 300)
  assert.equal(checkFreshness(root, ['out/app.js'], ['src']), true)
  assert.equal(checkFreshness(root, ['missing'], ['src']), false)
})

const sshOptions = { target: 'hscdeploy@192.0.2.1', identity: '/fixture/identity', remoteRoot: 'C:\\HSC\\source' }
test('SSH probe fixes strict authentication and read-only commands, escaping Windows paths', () => {
  const args = sshProbeArgs({ ...sshOptions, remoteRoot: "C:\\HSC\\it's a source" })
  for (const value of ['none', 'BatchMode=yes', 'StrictHostKeyChecking=yes', 'UpdateHostKeys=no',
    'PermitLocalCommand=no', 'ClearAllForwardings=yes', '-NoProfile', '-NonInteractive']) assert.ok(args.includes(value))
  const command = Buffer.from(args.at(-1), 'base64').toString('utf16le')
  assert.ok(command.includes("it''s a source"))
  assert.ok(!/Get-Content|Set-Content|Start-Process|Remove-Item|HSC_DATABASE|Invoke-WebRequest|\$env:/i.test(command))
  for (const target of ['-oProxyCommand=bad', 'user@host;bad', 'host']) assert.throws(() => sshProbeArgs({ ...sshOptions, target }))
})

test('SSH authentication and timeout errors are bounded and never print raw output', () => {
  const auth = probeSsh(sshOptions, () => ({ status: 255, stderr: 'Permission denied synthetic-0ceda85f1d800bf2470c5042', stdout: 'synthetic-0ceda85f1d800bf2470c5042' }))
  assert.deepEqual(auth, { status: 'blocked', connection: 'unverified', reason: 'authentication_failed' })
  assert.equal(probeSsh(sshOptions, () => ({ error: { code: 'ETIMEDOUT' } })).reason, 'timeout')
})

test('SSH success requires structured Windows facts and strips unknown data', () => {
  const facts = { platform: 'Win32NT', x64: true, source_exists: true, node_available: true, npm_available: true,
    python_available: true, pyinstaller_available: true, secret: 'synthetic-0ceda85f1d800bf2470c5042' }
  const run = (value) => () => ({ status: 0, stdout: JSON.stringify(value) })
  const result = probeSsh(sshOptions, run(facts))
  assert.equal(result.status, 'passed')
  assert.equal(result.connection, 'passed')
  assert.ok(!JSON.stringify(result).includes('synthetic-0ceda85f1d800bf2470c5042'))
  assert.equal(probeSsh(sshOptions, run({ ...facts, pyinstaller_available: false })).status, 'blocked')
  assert.equal(probeSsh(sshOptions, run({ ...facts, source_exists: false })).connection, 'passed')
  assert.equal(probeSsh(sshOptions, run({ ...facts, platform: 'Unix' })).status, 'blocked')
  assert.equal(probeSsh(sshOptions, run({})).reason, 'unexpected_probe_response')
})

test('gate and readiness CLIs fail closed on unknown options', () => {
  for (const script of ['verify-windows-package-inputs.mjs', 'windows-release-readiness.mjs']) {
    const result = spawnSync(process.execPath, [join(here, script), '--static-onyl'], { encoding: 'utf8' })
    assert.equal(result.status, 2)
    assert.match(result.stderr, /Usage:/)
  }
})

test('readiness CLI checks a missing workspace without creating artifacts', (t) => {
  const root = fixture(t)
  const result = spawnSync(process.execPath, [join(here, 'windows-release-readiness.mjs'), '--root', root, '--static-only'], { encoding: 'utf8' })
  assert.equal(result.status, 1)
  assert.equal(JSON.parse(result.stdout).checks.package_inputs, 'blocked')
  assert.deepEqual(readdirSync(root), [])
})

function gateFixture(t) {
  const root = fixture(t)
  const repo = dirname(here)
  for (const path of ['package.json', 'build/installer.nsh', 'build/pyinstaller-hooks/hook-webrtcvad.py',
    '.github/workflows/windows-build.yml', 'scripts/smoke-windows-agent.ps1', 'scripts/smoke-windows-installer.ps1', 'scripts/windows-installation-guard.ps1',
    'scripts/verify-parent-lockout.py', 'docs/development/windows-release-runbook.md',
    'src/main/hardware-inventory.test.ts', 'src/main/index.ts', 'src/main/local-service.ts', 'src/main/local-service.test.ts',
    'services/local_agent/pyproject.toml', 'services/local_agent/src/home_study_agent/build_info.py', 'services/local_agent/src/home_study_agent/database.py', 'services/local_agent/tests/test_database.py']) {
    write(root, path, readFileSync(join(repo, path)))
  }
  for (const path of ['out/main/index.js', 'out/preload/index.js', 'out/renderer/index.html']) write(root, path)
  const knowledgeDb = join(root, 'dist/knowledge/atomic-knowledge.db')
  mkdirSync(dirname(knowledgeDb), { recursive: true })
  const knowledgeFd = openSync(knowledgeDb, 'w')
  try { ftruncateSync(knowledgeFd, 100_000_000) } finally { closeSync(knowledgeFd) }
  write(root, 'dist/knowledge/atomic-knowledge-manifest.json', JSON.stringify({ schema: 'hsc.atomic-knowledge.package.v1', packaged_name: 'atomic-knowledge.db', sha256: 'a993f8c574e0fea8c1cdcbcd9408d9e2e107ee6e4d120edcfa11decd53fa0cae', bytes: 100_000_000 }))
  const model = manifest()
  model.files['encoder.onnx'].sha256 = createHash('sha256').update('fixture').digest('hex')
  write(root, 'services/local_agent/model-manifest.json', JSON.stringify(model))
  write(root, 'services/local_agent/models/model/encoder.onnx')
  const semantic = { ...model, model_dir: 'bge-small-zh-v1.5' }
  write(root, 'services/local_agent/semantic-model-manifest.json', JSON.stringify(semantic))
  write(root, 'services/local_agent/models/bge-small-zh-v1.5/encoder.onnx')
  return root
}
function gate(root, staticOnly = true) {
  return spawnSync(process.execPath, [join(here, 'verify-windows-package-inputs.mjs'), ...(staticOnly ? ['--static-only'] : [])],
    { cwd: root, encoding: 'utf8', timeout: 15000, maxBuffer: 32768 })
}

test('actual package gate rejects a same-size tampered knowledge database', (t) => {
  const root = gateFixture(t)
  const file = join(root, 'dist/knowledge/atomic-knowledge.db')
  const fd = openSync(file, 'r+')
  try { writeSync(fd, Buffer.from([1]), 0, 1, 5000) } finally { closeSync(fd) }
  const result = gate(root)
  assert.equal(result.status, 1)
  assert.match(result.stderr, /知识库.*校验和/)
})

test('actual gate uses the declared schema even when the newest migration uses bound SQL parameters', (t) => {
  const root = gateFixture(t)
  const path = 'services/local_agent/src/home_study_agent/database.py'
  write(root, path, readFileSync(join(root, path), 'utf8').replace(/^LATEST_SCHEMA_VERSION\s*=\s*\d+$/m, 'LATEST_SCHEMA_VERSION = 999'))
  const result = gate(root)
  assert.equal(result.status, 1, 'Old literal migration SQL must not override the authoritative version')
  assert.match(result.stderr, /999/)
})

test('actual package gate rejects an old or ambiguous backend version source', (t) => {
  const root = gateFixture(t)
  const path = 'services/local_agent/src/home_study_agent/build_info.py'
  write(root, path, "BUILD_VERSION = '0.1.3'\n")
  const result = gate(root)
  assert.equal(result.status, 1)
  assert.match(result.stderr, /build versions must match/)
})

for (const declaration of ['', 'LATEST_SCHEMA_VERSION = computed_version()', 'LATEST_SCHEMA_VERSION = 70\nLATEST_SCHEMA_VERSION = 71']) {
  test(`schema gate fails closed for an ambiguous or unsupported declaration: ${JSON.stringify(declaration)}`, (t) => {
    const root = gateFixture(t)
    const path = 'services/local_agent/src/home_study_agent/database.py'
    write(root, path, readFileSync(join(root, path), 'utf8').replace(/^LATEST_SCHEMA_VERSION\s*=\s*\d+$/m, declaration))
    assert.equal(gate(root).status, 1)
  })
}

for (const [path, declaration] of [
  ['services/local_agent/pyproject.toml', '"tzdata==2026.3",'],
  ['package.json', '--collect-all tzdata'],
  ['services/local_agent/pyproject.toml', '"tokenizers==0.21.4",'],
  ['package.json', '--collect-all tokenizers'],
]) {
  test(`timezone data must be declared and explicitly collected: ${path}`, (t) => {
    const root = gateFixture(t)
    write(root, path, readFileSync(join(root, path), 'utf8').replace(declaration, ''))
    const result = gate(root)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /tzdata|分词器/)
  })
}

test('semantic model omission and corruption block the actual package gate', (t) => {
  const root = gateFixture(t)
  const file = 'services/local_agent/models/bge-small-zh-v1.5/encoder.onnx'
  rmSync(join(root, file))
  assert.equal(gate(root).status, 1)
  write(root, file, 'not the declared model')
  const corrupted = gate(root)
  assert.equal(corrupted.status, 1)
  assert.match(corrupted.stderr, /语义模型校验和/)
})

test('actual gate CLI accepts a source archive without .git and rejects a missing agent in full mode', (t) => {
  const root = gateFixture(t)
  const result = gate(root)
  assert.equal(result.status, 0, result.stderr)
  const full = gate(root, false)
  assert.equal(full.status, 1)
  assert.match(full.stderr, /Windows agent is missing/)
})

test('actual gate CLI fails closed on malformed or empty model manifests', (t) => {
  const root = gateFixture(t)
  for (const body of ['{"sensitive_fixture": DO_NOT_PRINT}', '{}', '{"model_dir":"model","files":{}}']) {
    write(root, 'services/local_agent/model-manifest.json', body)
    const result = gate(root)
    assert.equal(result.status, 1)
    assert.ok(!result.stderr.includes('DO_NOT_PRINT'))
    assert.ok(!result.stderr.includes('SyntaxError'))
  }
})

test('actual gate CLI rejects corrupted model bytes and private payload names', (t) => {
  const root = gateFixture(t)
  write(root, 'services/local_agent/models/model/encoder.onnx', 'corrupted')
  assert.equal(gate(root).status, 1)
  write(root, 'services/local_agent/models/model/encoder.onnx')
  write(root, 'out/family-data/private.db', 'DO_NOT_PRINT')
  const result = gate(root)
  assert.equal(result.status, 1)
  assert.match(result.stderr, /forbidden data/)
  assert.ok(!result.stderr.includes('DO_NOT_PRINT'))
})

test('actual gate CLI rejects a symlinked model directory before hashing its contents', (t) => {
  const root = gateFixture(t)
  rmSync(join(root, 'services/local_agent/models/model'), { recursive: true })
  write(root, 'private/encoder.onnx', 'DO_NOT_PRINT')
  symlinkSync(join(root, 'private'), join(root, 'services/local_agent/models/model'), process.platform === 'win32' ? 'junction' : 'dir')
  const result = gate(root)
  assert.equal(result.status, 1)
  assert.match(result.stderr, /symbolic link/)
  assert.ok(!result.stderr.includes('DO_NOT_PRINT'))
})

test('actual gate rejects static substitution and command masking in the full packaging chain', (t) => {
  const root = gateFixture(t)
  const original = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'))
  assert.deepEqual(validatePackageCommands(original.scripts), [])
  for (const scripts of [
    { ...original.scripts, 'verify:package:win': 'node scripts/verify-windows-package-inputs.mjs --static-only' },
    { ...original.scripts, 'pack:win': 'npm run build && npm run verify:package:win:static && electron-builder --win nsis' },
    { ...original.scripts, 'pack:win': 'echo verify:package:win && electron-builder --win nsis' },
    { ...original.scripts, 'pack:win': false },
  ]) {
    write(root, 'package.json', JSON.stringify({ ...original, scripts }))
    const result = gate(root)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /Unaudited packaging command/)
    assert.ok(!result.stderr.includes('TypeError'))
  }
})

test('actual gate handles invalid package metadata without quoting its contents', (t) => {
  const root = fixture(t)
  for (const content of ['{"credential":"DO_NOT_PRINT",invalid}', 'null', '[]']) {
    write(root, 'package.json', content)
    const result = gate(root)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /valid JSON object/)
    assert.ok(!result.stderr.includes('DO_NOT_PRINT'))
  }
})

test('SSH-only CLI requires an explicit target and disallows local static mode', () => {
  for (const args of [['--ssh-only'], ['--ssh-only', '--static-only', '--ssh-target', sshOptions.target]]) {
    const result = spawnSync(process.execPath, [join(here, 'windows-release-readiness.mjs'), ...args], { encoding: 'utf8' })
    assert.equal(result.status, 2)
    assert.match(result.stderr, /--ssh-only requires/)
  }
})
