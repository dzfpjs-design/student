'use strict'

// This file is outside app.asar: its hashes therefore cannot hash themselves.
// It is generated after extraResources are copied and before NSIS is built.
const { createHash } = require('node:crypto')
const { createReadStream } = require('node:fs')
const fs = require('node:fs/promises')
const path = require('node:path')
const copyPrivateResources = require('./copy-private-resources.cjs')

async function hashArtifact(file) {
  const before = await fs.lstat(file)
  if (!before.isFile() || before.isSymbolicLink()) throw new Error('RELEASE_ARTIFACT_INVALID')
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(file)) digest.update(chunk)
  const after = await fs.lstat(file)
  if (!after.isFile() || after.isSymbolicLink() || before.dev !== after.dev || before.ino !== after.ino
      || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) {
    throw new Error('RELEASE_ARTIFACT_CHANGED')
  }
  return digest.digest('hex')
}

module.exports = async function writeReleaseInfo(context) {
  if (context.electronPlatformName !== 'win32') return
  const version = context.packager?.appInfo?.version
  if (typeof version !== 'string' || !/^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(version)) throw new Error('RELEASE_VERSION_INVALID')
  const resources = path.join(context.appOutDir, 'resources')
  const privateFiles = await copyPrivateResources(context, resources)
  try {
  const [agent, frontend] = await Promise.all([
    hashArtifact(path.join(resources, 'local-agent', 'home-study-local-agent.exe')),
    hashArtifact(path.join(resources, 'app.asar')),
  ])
  const identity = createHash('sha256').update(`${agent}\n${frontend}`, 'ascii').digest('hex').slice(0, 20)
  const info = { schema: 1, release_id: `hsc-${version}-${identity}`, agent_sha256: agent, app_asar_sha256: frontend }
  await fs.writeFile(path.join(resources, 'release-info.json'), JSON.stringify(info) + '\n', { encoding: 'utf8', flag: 'wx' })
  } catch (error) {
    for (const file of privateFiles) await fs.unlink(file).catch(() => {})
    throw error
  }
}
