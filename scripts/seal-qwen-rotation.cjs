'use strict'

// The secret is accepted only over a private, non-terminal stdin stream. It is
// never accepted as an argument/environment variable or printed. The encrypted
// payload and random wrapping key are intentionally packaged together: an
// installer recipient can extract both, so this is not recipient secrecy.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto')
let outputDirectory, payloadCreated = false, keyCreated = false, failed = false, input = Buffer.alloc(0), wrappingKey, plain
function fail() {
  if (failed) return
  failed = true
  try { if (payloadCreated) fs.unlinkSync(path.join(outputDirectory,'qwen-rotation.bin')) } catch {}
  try { if (keyCreated) fs.unlinkSync(path.join(outputDirectory,'qwen-rotation.key')) } catch {}
  input.fill(0); wrappingKey?.fill(0); plain?.fill(0)
  try { process.stderr.write('ROTATION_PREPARE_FAILED\n') } catch {}
  process.exitCode = 1
}
try {
  if (process.argv.length !== 3 || process.stdin.isTTY) throw Error()
  outputDirectory = path.resolve(process.argv[2])
  const dir = fs.lstatSync(outputDirectory)
  if (!dir.isDirectory() || dir.isSymbolicLink()) throw Error()
  process.stdin.on('data', chunk => {
    if (input.length + chunk.length > 514) { process.stdin.destroy(); fail(); return }
    const next = Buffer.concat([input,chunk]); input.fill(0); input = next
  })
  process.stdin.on('error', fail)
  process.stdin.on('end', () => {
    if (failed) return
    try {
      const apiKey = input.toString('utf8').replace(/\r?\n$/, '')
      if (apiKey.length < 16 || apiKey.length > 512 || apiKey.trim() !== apiKey || /[\x00-\x20\x7f]/.test(apiKey)) throw Error()
      wrappingKey = crypto.randomBytes(32)
      const iv = crypto.randomBytes(12)
      plain = Buffer.from(JSON.stringify({version:1,generation:crypto.randomUUID(),order:Date.now(),apiKey}),'utf8')
      const cipher = crypto.createCipheriv('aes-256-gcm',wrappingKey,iv)
      const ciphertext = Buffer.concat([Buffer.from('HSCROT01'),iv,cipher.update(plain),cipher.final(),cipher.getAuthTag()])
      let fd = fs.openSync(path.join(outputDirectory,'qwen-rotation.key'),'wx',0o600);keyCreated=true
      try { fs.writeFileSync(fd,wrappingKey);fs.fsyncSync(fd) } finally { fs.closeSync(fd) }
      fd = fs.openSync(path.join(outputDirectory,'qwen-rotation.bin'),'wx',0o600);payloadCreated=true
      try { fs.writeFileSync(fd,ciphertext);fs.fsyncSync(fd) } finally { fs.closeSync(fd) }
      input.fill(0);wrappingKey.fill(0);plain.fill(0)
      process.stdout.write('ROTATION_PAYLOAD_PREPARED\n')
    } catch { fail() }
  })
} catch { fail() }
