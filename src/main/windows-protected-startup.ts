import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

// Electron's Windows OS-crypt provider is persisted independently of our blobs.
// Never publish a data key while its protection key exists only in memory.
export async function waitForWindowsProtectedStorage(
  sessionDataPath: string, timeoutMs = 30_000, pollMs = 100,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  do {
    try {
      const state = JSON.parse(await readFile(join(sessionDataPath, 'Local State'), 'utf8'))
      const encoded: unknown = state?.os_crypt?.encrypted_key
      if (typeof encoded === 'string') {
        const protectedKey = Buffer.from(encoded, 'base64')
        if (protectedKey.length > 5 && protectedKey.subarray(0, 5).toString('ascii') === 'DPAPI'
            && protectedKey.toString('base64') === encoded) return
      }
    } catch (error) {
      if (!(error instanceof SyntaxError) && !(error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')) {
        throw new Error('系统保护配置无法核对；未创建或更换本地密钥。')
      }
    }
    if (Date.now() >= deadline) break
    await new Promise(resolve => setTimeout(resolve, Math.min(pollMs, deadline - Date.now())))
  } while (Date.now() <= deadline)
  throw new Error('系统保护密钥尚未可靠保存；未创建或更换本地密钥，请保留现有资料后重试。')
}
