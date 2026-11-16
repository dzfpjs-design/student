import { isAbsolute, join } from 'node:path'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'

// This loopback-only launcher restores paths, never inherited provider access.
export function parentReportDeliveryEnvironment({ root, pythonBin, dataDir, databasePath, profile }, environment = process.env) {
  for (const [name, path] of Object.entries({ root, pythonBin, dataDir, databasePath, ...(profile === undefined ? {} : { profile }) })) {
    if (typeof path !== 'string' || !isAbsolute(path)) throw new TypeError(`Explicit absolute fixture path required: ${name}`)
  }
  return {
    ...sanitizedHarnessEnvironment(environment),
    PYTHONPATH: join(root, 'services', 'local_agent', 'src'),
    HSC_LOCAL_AGENT_PYTHON: pythonBin,
    HSC_DATA_DIR: dataDir,
    HSC_DATABASE_PATH: databasePath,
    ...(profile === undefined ? {} : { HSC_ELECTRON_USER_DATA_DIR: profile }),
  }
}
