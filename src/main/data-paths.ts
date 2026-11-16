import { join, resolve } from 'node:path'
import { assertTrialPath } from './runtime-profile'

export type FamilyDataPaths = {
  dataDir: string
  databasePath: string
}

export function resolveFamilyDataPaths(
  userDataPath: string,
  environment: NodeJS.ProcessEnv = process.env
): FamilyDataPaths {
  const dataDir = environment.HSC_DATA_DIR ?? join(userDataPath, 'family-data')
  const databasePath = environment.HSC_DATABASE_PATH ?? join(dataDir, 'home-study.db')
  if (environment.HSC_RUNTIME_MODE === 'trial') {
    if (environment.HSC_TRIAL_USER_DATA_DIR !== userDataPath) throw new Error('TRIAL_PROFILE_ROOT_INVALID')
    if (resolve(dataDir) === resolve(userDataPath)) throw new Error('TRIAL_PROFILE_PATH_ESCAPE')
    assertTrialPath(userDataPath, dataDir)
    assertTrialPath(dataDir, databasePath)
  }
  return {
    dataDir,
    databasePath
  }
}
