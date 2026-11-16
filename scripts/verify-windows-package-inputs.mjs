import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream, existsSync, lstatSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import process from 'node:process'
import { inspectPackageTree, validateBackendBuildVersion, validateModelManifest, validatePackageCommands, validatePackageScope, validateWindowsAgent } from './windows-package-checks.mjs'
import { validateSmtpWorkerManifest } from './smtp-worker-manifest.mjs'

if (process.argv.slice(2).some((arg) => arg !== '--static-only')) {
  console.error('Usage: node scripts/verify-windows-package-inputs.mjs [--static-only]')
  process.exit(2)
}

const root = process.cwd()
const staticOnly = process.argv.includes('--static-only')
const packageJsonPath = resolve(root, 'package.json')
let packageJson
try {
  if (!lstatSync(packageJsonPath).isFile()) throw new Error('Not a regular file')
  packageJson = JSON.parse(readFileSync(packageJsonPath, 'utf8'))
  if (!packageJson || typeof packageJson !== 'object' || Array.isArray(packageJson)) throw new Error('Not an object')
} catch {
  console.error('Windows package gate: package.json must be a regular file containing a valid JSON object')
  process.exit(1)
}
const failures = []
try {
  failures.push(...validateBackendBuildVersion(packageJson.version,
    readFileSync(resolve(root, 'services/local_agent/src/home_study_agent/build_info.py'), 'utf8'),
    readFileSync(resolve(root, 'services/local_agent/pyproject.toml'), 'utf8')))
} catch { failures.push('Backend build version source is missing or unreadable') }

function isInsideGitWorktree() {
  try {
    return execFileSync('git', ['rev-parse', '--is-inside-work-tree'], {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim() === 'true'
  } catch {
    return false
  }
}

function requireFile(relativePath, minimumBytes = 1) {
  const absolutePath = resolve(root, relativePath)
  if (!existsSync(absolutePath)) {
    failures.push(`缺少文件：${relativePath}`)
    return
  }
  const info = lstatSync(absolutePath)
  if (!info.isFile() || info.isSymbolicLink()) {
    failures.push(`必须是普通文件且不得是符号链接：${relativePath}`)
    return
  }
  const size = info.size
  if (size < minimumBytes) failures.push(`文件过小或为空：${relativePath} (${size} bytes)`)
}

function requireEqual(actual, expected, label) {
  if (actual !== expected) failures.push(`${label} 应为 ${JSON.stringify(expected)}，实际为 ${JSON.stringify(actual)}`)
}

function requireText(relativePath, expectedText, label) {
  const absolutePath = resolve(root, relativePath)
  requireFile(relativePath)
  if (existsSync(absolutePath) && lstatSync(absolutePath).isFile() && !readFileSync(absolutePath, 'utf8').includes(expectedText)) {
    failures.push(`${label}缺少要求文本：${JSON.stringify(expectedText)}`)
  }
}

for (const output of ['out/main/index.js', 'out/preload/index.js', 'out/renderer/index.html']) {
  requireFile(output)
}

const databaseSourcePath = resolve(root, 'services/local_agent/src/home_study_agent/database.py')
requireFile('services/local_agent/src/home_study_agent/database.py')
if (existsSync(databaseSourcePath)) {
  const databaseSource = readFileSync(databaseSourcePath, 'utf8')
  // The newest migration may bind its version as a SQL parameter. Require the
  // single literal authority, rather than inferring a lower version from SQL.
  const declarations = [...databaseSource.matchAll(/^LATEST_SCHEMA_VERSION\s*=/gm)]
  const literal = databaseSource.match(/^LATEST_SCHEMA_VERSION\s*=\s*(\d+)\s*(?:#.*)?$/m)
  const parsedVersion = literal ? Number(literal[1]) : NaN
  const currentSchemaVersion = declarations.length === 1 && Number.isSafeInteger(parsedVersion) && parsedVersion > 0
    ? parsedVersion : null
  if (currentSchemaVersion === null) {
    failures.push('无法从 database.py 提取唯一的 LATEST_SCHEMA_VERSION 整数声明')
  } else {
    requireText('scripts/smoke-windows-agent.ps1', `-ne "${currentSchemaVersion}"`, 'Windows agent冒烟脚本')
    requireText(
      'scripts/smoke-windows-installer.ps1',
      `if ($runtimeLines[1] -ne "${currentSchemaVersion}")`,
      'Windows安装器首次启动数据库版本断言',
    )
    requireText(
      'scripts/smoke-windows-installer.ps1',
      `if ($upgradeLines[0] -ne "${currentSchemaVersion}")`,
      'Windows安装器覆盖升级数据库版本断言',
    )
    requireText('scripts/smoke-windows-installer.ps1', `$upgradeMetadata.target_schema_version -ne ${currentSchemaVersion})`, 'Windows升级回执目标版本断言')
    requireText('docs/development/windows-release-runbook.md', `SQLite v${currentSchemaVersion}`, 'Windows发布手册')
    requireText('docs/development/windows-release-runbook.md', `数据库版本不是${currentSchemaVersion}`, 'Windows发布失败处理')
  }
}

const installerInclude = 'build/installer.nsh'
requireFile(installerInclude)
if (existsSync(resolve(root, installerInclude))) {
  const installerScript = readFileSync(resolve(root, installerInclude), 'utf8')
  if (!installerScript.includes('customUnInstall') || !installerScript.includes('--remove-auto-launch')) {
    failures.push('NSIS 卸载钩子未包含登录自启清理命令')
  }
  if (isInsideGitWorktree()) {
    try {
      execFileSync('git', ['check-ignore', '-q', installerInclude], { cwd: root })
      failures.push(`${installerInclude} 仍被 .gitignore 排除`)
    } catch (error) {
      if (error?.status !== 1) failures.push(`无法确认 ${installerInclude} 的 Git 忽略状态`)
    }
  }
}

requireText('build/pyinstaller-hooks/hook-webrtcvad.py', 'copy_metadata("webrtcvad-wheels")', 'WebRTC VAD PyInstaller兼容hook')
requireText('build/pyinstaller-hooks/hook-webrtcvad.py', 'hiddenimports = ["_webrtcvad"]', 'WebRTC VAD二进制扩展收集')
requireText('package.json', '--additional-hooks-dir build/pyinstaller-hooks', 'Windows agent构建脚本')
requireText('services/local_agent/pyproject.toml', '"tzdata==2026.3"', 'Windows tzdata时区依赖')
requireText('package.json', '--collect-all tzdata', 'Windows tzdata时区数据收集')
requireText('services/local_agent/pyproject.toml', '"tokenizers==0.21.4"', '本地语义分词器依赖')
requireText('package.json', '--collect-all tokenizers', 'Windows语义分词器二进制收集')

requireText('src/main/hardware-inventory.test.ts', "it.runIf(process.platform === 'win32')", 'Windows PNP盘点集成测试')
requireText('src/main/index.ts', '--hardware-inventory-smoke', 'Electron打包后硬件盘点诊断')
requireText('src/main/index.ts', '--model-settings-smoke', 'Electron打包后模型安全存储诊断')
requireText('src/main/index.ts', "'render-process-gone'", 'Electron渲染进程自动恢复')
requireText('src/main/local-service.ts', 'LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS', '本地学习服务有界自动恢复')
requireText('src/main/local-service.test.ts', 'coalesces concurrent startup requests', '本地学习服务单飞启动回归')
requireText('scripts/smoke-windows-installer.ps1', 'safe_storage_available', 'Windows安装后模型安全存储断言')
requireText('scripts/smoke-windows-installer.ps1', 'left synthetic credentials behind', 'Windows安装后合成凭据清理断言')
requireText('scripts/smoke-windows-agent.ps1', 'sensor_release_platform_eligible', 'Windows本地服务平台门禁冒烟')
requireFile('scripts/verify-parent-lockout.py')
requireText('scripts/smoke-windows-agent.ps1', '--phase lock', 'Windows家长PIN锁定冒烟')
requireText('scripts/smoke-windows-agent.ps1', '--phase assert-locked', 'Windows家长PIN重启持久性冒烟')
requireText('scripts/smoke-windows-agent.ps1', 'parent lockout persistence', 'Windows家长PIN锁定完成证据')
requireText('scripts/smoke-windows-installer.ps1', '--hardware-inventory-smoke', 'Windows安装后PNP盘点冒烟')
requireText('scripts/smoke-windows-installer.ps1', 'Assert-HscIsolatedInstallSafe', 'Windows 安装前注册产品保护')
requireText('scripts/smoke-windows-installer.ps1', 'Assert-HscTargetRegistryAbsent $registryReader', 'Windows 安装前注册产品复查')
requireText('scripts/windows-installation-guard.ps1', 'Get-HscInstallRegistrySnapshot', 'Windows 注册表安全检查')
requireText('scripts/smoke-windows-installer.ps1', 'windows_pnp_cim_v1', 'Windows安装后原生PNP路径断言')
requireText('scripts/smoke-windows-installer.ps1', 'upgrade_smoke_sentinel', 'Windows覆盖升级数据保留冒烟')
requireText('scripts/smoke-windows-installer.ps1', 'upgrade_smoke_homework_status', 'Windows覆盖升级作业状态修复冒烟')
requireText('scripts/smoke-windows-installer.ps1', 'pre-upgrade recovery metadata', 'Windows升级前恢复点冒烟')
requireText('services/local_agent/tests/test_database.py', 'test_initialize_restores_pre_upgrade_snapshot_after_committed_migration_failure', '数据库迁移失败自动恢复测试')
requireText('services/local_agent/tests/test_database.py', 'test_initialize_rejects_newer_database_without_modifying_it', '数据库降级保护测试')
requireText('services/local_agent/tests/test_database.py', 'test_initialize_upgrades_v47_deletion_job_before_creating_new_index', 'v47删除任务表升级顺序测试')
requireText('scripts/smoke-windows-installer.ps1', 'ALTER TABLE deletion_job RENAME TO deletion_job_v48', 'Windows覆盖升级真实v47删除表模拟')
requireText('scripts/smoke-windows-installer.ps1', 'DROP TABLE IF EXISTS child_safety_event', 'Windows覆盖升级真实v47安全事件表模拟')
requireText('scripts/smoke-windows-installer.ps1', "pragma_table_info('child_safety_event')", 'Windows覆盖升级安全事件表验证')
requireText('services/local_agent/src/home_study_agent/database.py', 'restored_after_failure', '数据库升级恢复状态')

const modelManifestPath = 'services/local_agent/model-manifest.json'
requireFile(modelManifestPath)
let modelManifest
try {
  if (lstatSync(resolve(root, modelManifestPath)).isFile()) {
    modelManifest = JSON.parse(readFileSync(resolve(root, modelManifestPath), 'utf8'))
  }
} catch {
  failures.push('模型清单缺失或不是有效 JSON')
}
const manifestFailures = validateModelManifest(modelManifest)
failures.push(...manifestFailures)
if (manifestFailures.length) modelManifest = { model_dir: '', files: {} }
const modelTreeFailures = inspectPackageTree(root, 'services/local_agent/models')
failures.push(...modelTreeFailures)
const asrRoot = `services/local_agent/models/${modelManifest.model_dir}`
for (const [modelFile, expected] of Object.entries(modelTreeFailures.length ? {} : modelManifest.files)) {
  const relativePath = `${asrRoot}/${modelFile}`
  requireFile(relativePath, expected.minimum_bytes)
  if (existsSync(resolve(root, relativePath)) && lstatSync(resolve(root, relativePath)).isFile()) {
    const actualHash = createHash('sha256').update(readFileSync(resolve(root, relativePath))).digest('hex')
    if (actualHash !== expected.sha256) failures.push(`模型校验和不匹配：${modelFile}`)
  }
}

const semanticManifestPath = 'services/local_agent/semantic-model-manifest.json'
requireFile(semanticManifestPath)
try {
  if (!lstatSync(resolve(root, semanticManifestPath)).isFile()) throw new Error('Not a regular manifest')
  const semantic = JSON.parse(readFileSync(resolve(root, semanticManifestPath), 'utf8'))
  const invalid = validateModelManifest(semantic)
  failures.push(...invalid.map((message) => `Semantic ${message}`))
  if (!invalid.length && !modelTreeFailures.length) {
    for (const [name, expected] of Object.entries(semantic.files)) {
      const path = `services/local_agent/models/${semantic.model_dir}/${name}`
      requireFile(path, expected.minimum_bytes)
      if (existsSync(resolve(root, path)) && lstatSync(resolve(root, path)).isFile()) {
        const hash = createHash('sha256').update(readFileSync(resolve(root, path))).digest('hex')
        if (hash !== expected.sha256) failures.push(`语义模型校验和不匹配：${name}`)
      }
    }
  }
} catch {
  failures.push('语义模型清单缺失或不是有效JSON')
}

failures.push(...validatePackageScope(packageJson.build))
failures.push(...validatePackageCommands(packageJson.scripts))
failures.push(...inspectPackageTree(root, 'out'))
const extraResources = Array.isArray(packageJson.build?.extraResources) ? packageJson.build.extraResources : []
const agentResource = extraResources.find((entry) => entry?.to === 'local-agent/home-study-local-agent.exe')
const smtpWorkerResource = extraResources.find((entry) => entry?.to === 'local-agent/smtp-worker')
const modelResource = extraResources.find((entry) => entry?.to === 'local-models')
const modelManifestResource = extraResources.find((entry) => entry?.to === 'local-models/model-manifest.json')
const atomicGraphResource = extraResources.find((entry) => entry?.to === 'knowledge/atomic-knowledge.db')
const atomicGraphManifestResource = extraResources.find((entry) => entry?.to === 'knowledge/atomic-knowledge-manifest.json')
requireEqual(agentResource?.from, 'dist/home-study-local-agent.exe', '本地服务打包来源')
requireEqual(smtpWorkerResource?.from, 'dist/hsc-smtp-worker', 'SMTP worker 打包来源')
requireEqual(modelResource?.from, 'services/local_agent/models', '本地模型打包来源')
requireEqual(modelManifestResource?.from, modelManifestPath, '模型清单打包来源')
requireEqual(atomicGraphResource?.from, 'dist/knowledge/atomic-knowledge.db', '原子知识库打包来源')
requireEqual(atomicGraphManifestResource?.from, 'dist/knowledge/atomic-knowledge-manifest.json', '原子知识库清单打包来源')
requireFile('dist/knowledge/atomic-knowledge.db', 100_000_000)
requireFile('dist/knowledge/atomic-knowledge-manifest.json', 100)
// Presence and size alone cannot prove that the packaged graph matches its
// manifest. Stream the digest: the graph commonly exceeds a gigabyte.
try {
  const dbPath = resolve(root, 'dist/knowledge/atomic-knowledge.db')
  const manifestPath = resolve(root, 'dist/knowledge/atomic-knowledge-manifest.json')
  const before = lstatSync(dbPath)
  const manifestStat = lstatSync(manifestPath)
  if (!before.isFile() || before.isSymbolicLink() || !manifestStat.isFile() || manifestStat.isSymbolicLink() || manifestStat.size > 65536) {
    throw new Error('invalid file')
  }
  const manifestBytes = readFileSync(manifestPath)
  const manifest = JSON.parse(manifestBytes.toString('utf8'))
  if (manifest?.schema !== 'hsc.atomic-knowledge.package.v1' || manifest.packaged_name !== 'atomic-knowledge.db'
    || !Number.isSafeInteger(manifest.bytes) || manifest.bytes !== before.size || !/^[0-9a-f]{64}$/.test(manifest.sha256)) {
    throw new Error('invalid manifest')
  }
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(dbPath)) digest.update(chunk)
  const after = lstatSync(dbPath)
  const manifestAfter = lstatSync(manifestPath)
  if (!after.isFile() || after.isSymbolicLink() || before.dev !== after.dev || before.ino !== after.ino
    || before.size !== after.size || before.mtimeMs !== after.mtimeMs || digest.digest('hex') !== manifest.sha256
    || !manifestAfter.isFile() || manifestAfter.isSymbolicLink() || manifestStat.dev !== manifestAfter.dev
    || manifestStat.ino !== manifestAfter.ino || manifestStat.size !== manifestAfter.size
    || manifestStat.mtimeMs !== manifestAfter.mtimeMs || !manifestBytes.equals(readFileSync(manifestPath))) {
    throw new Error('checksum mismatch or changed')
  }
} catch {
  failures.push('原子知识库与清单校验和不匹配，或文件无效/校验期间发生变化')
}
requireEqual(packageJson.build?.nsis?.include, installerInclude, 'NSIS include')
requireEqual(packageJson.build?.appId, 'cn.family.homestudycompanion', 'Windows App ID')

const workflowPath = resolve(root, '.github/workflows/windows-build.yml')
requireFile('.github/workflows/windows-build.yml')
if (existsSync(workflowPath)) {
  const workflow = readFileSync(workflowPath, 'utf8')
  const packIndex = workflow.indexOf('npm run pack:win')
  const installerSmokeIndex = workflow.indexOf('./scripts/smoke-windows-installer.ps1')
  const testIndex = workflow.indexOf('npm test -- --run')
  if (packIndex < 0 || installerSmokeIndex < packIndex) {
    failures.push('Windows CI 必须先打包，再执行安装器冒烟')
  }
  if (testIndex < 0 || testIndex > packIndex) {
    failures.push('Windows CI 必须在打包前执行包含PNP盘点的Vitest')
  }
  if (!workflow.includes('Get-FileHash release/*.exe -Algorithm SHA256')) {
    failures.push('Windows CI 未生成安装包 SHA-256 清单')
  }
}

if (!staticOnly) {
  failures.push(...validateWindowsAgent(resolve(root, 'dist/home-study-local-agent.exe')))
  failures.push(...validateWindowsAgent(resolve(root, 'dist/hsc-smtp-worker/hsc-smtp-worker.exe'),100_000))
  failures.push(...inspectPackageTree(root, 'dist/hsc-smtp-worker'))
  failures.push(...validateSmtpWorkerManifest(resolve(root, 'dist/hsc-smtp-worker')))
}

if (failures.length) {
  console.error(`Windows 打包输入门禁失败（${failures.length} 项）：`)
  for (const failure of failures) console.error(`- ${failure}`)
  process.exit(1)
}

console.log(`Windows 打包输入门禁通过（${staticOnly ? '静态预检，未验证 Windows agent exe' : '完整模式'}）。`)
