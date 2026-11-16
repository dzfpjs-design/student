import { lstat, readdir } from 'node:fs/promises'
import { isAbsolute } from 'node:path'
import { resolveFamilyDataPaths } from './data-paths'

export const MEDIA_KEY_RECOVERY_MESSAGE = '本地媒体密钥缺失，但档案中已有资料或未完成的密钥写入；为保护历史数据，系统不会自动换钥。请先保留整个档案，恢复同一档案原有的受保护密钥；可移植备份应在新的隔离档案中恢复，不能删除原库后重试。'

export class MediaKeyRecoveryRequiredError extends Error {
  constructor(message: string, cause: unknown) {
    super(message, { cause })
    this.name = 'MediaKeyRecoveryRequiredError'
  }
}

async function exists(path: string): Promise<boolean> {
  try {
    await lstat(path)
    return true
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false
    throw error
  }
}

export async function assertFreshMediaProfile(userDataPath: string): Promise<void> {
  try {
    const { dataDir, databasePath } = resolveFamilyDataPaths(userDataPath)
    if (!isAbsolute(dataDir) || !isAbsolute(databasePath)) throw new Error('AMBIGUOUS_DATA_PATH')
    for (const suffix of ['', '-wal', '-shm', '-journal']) {
      if (await exists(databasePath + suffix)) throw new Error('EXISTING_DATABASE_ASSET')
    }
    if (await exists(dataDir)) {
      const metadata = await lstat(dataDir)
      if (!metadata.isDirectory() || metadata.isSymbolicLink()) throw new Error('UNKNOWN_DATA_ROOT')
      for (const entry of await readdir(dataDir, { withFileTypes: true })) {
        // Startup diagnostics precede key loading; no learning assets belong here.
        if (entry.name !== 'diagnostics' || !entry.isDirectory() || entry.isSymbolicLink()) {
          throw new Error('EXISTING_OR_UNKNOWN_DATA_ASSET')
        }
      }
    }
    for (const name of await readdir(userDataPath)) {
      if (name.startsWith('data-encryption-key.bin.') && name.endsWith('.tmp')) {
        throw new Error('INTERRUPTED_KEY_PUBLICATION')
      }
    }
  } catch (error) {
    throw new MediaKeyRecoveryRequiredError(MEDIA_KEY_RECOVERY_MESSAGE, error)
  }
}
