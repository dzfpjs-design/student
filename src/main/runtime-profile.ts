import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'

export type RuntimeProfile = { mode: 'formal' | 'trial'; userDataPath: string; profileId?: string }

export function resolveRuntimeProfile(formalPath: string, environment: NodeJS.ProcessEnv, packaged: boolean, argv: string[] = []): RuntimeProfile {
  const trialArguments = argv.filter(value => value.startsWith('--trial-profile='))
  if (trialArguments.length > 1) throw new Error('TRIAL_PROFILE_ID_CONFLICT')
  if (trialArguments.length) {
    const id = trialArguments[0].slice('--trial-profile='.length)
    if (environment.HSC_RUNTIME_MODE === 'formal' || (environment.HSC_TRIAL_PROFILE_ID && environment.HSC_TRIAL_PROFILE_ID !== id)) throw new Error('TRIAL_PROFILE_ID_CONFLICT')
    environment.HSC_RUNTIME_MODE = 'trial'
    environment.HSC_TRIAL_PROFILE_ID = id
  }
  const mode = environment.HSC_RUNTIME_MODE ?? 'formal'
  if (mode !== 'formal' && mode !== 'trial') throw new Error('RUNTIME_PROFILE_MODE_INVALID')
  if (mode === 'formal') return { mode, userDataPath: !packaged && environment.HSC_ELECTRON_USER_DATA_DIR
    ? resolve(environment.HSC_ELECTRON_USER_DATA_DIR) : formalPath }
  const profileId = environment.HSC_TRIAL_PROFILE_ID
  if (!profileId || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(profileId)) throw new Error('TRIAL_PROFILE_ID_REQUIRED')
  const base = !packaged && environment.HSC_ELECTRON_USER_DATA_DIR ? resolve(environment.HSC_ELECTRON_USER_DATA_DIR) : formalPath
  return { mode, profileId, userDataPath: join(dirname(base), 'hsc-trial-profiles', profileId) }
}

export function assertTrialPath(root: string, target: string): void {
  const within = (base: string, value: string): boolean => {
    const suffix = relative(resolve(base), resolve(value))
    return suffix === '' || (!isAbsolute(suffix) && suffix !== '..' && !suffix.startsWith(`..${sep}`))
  }
  if (!within(root, target)) throw new Error('TRIAL_PROFILE_PATH_ESCAPE')
  // Check existing ancestors too: a symlink below the lexical root must not
  // redirect an otherwise isolated profile into a family's durable storage.
  let ancestor = resolve(target)
  while (!existsSync(ancestor) && dirname(ancestor) !== ancestor) ancestor = dirname(ancestor)
  if (existsSync(root) && !within(realpathSync(root), realpathSync(ancestor))) throw new Error('TRIAL_PROFILE_PATH_ESCAPE')
}

export function prepareRuntimeProfile(profile: RuntimeProfile, environment: NodeJS.ProcessEnv): void {
  if (profile.mode !== 'trial') return
  const root = profile.userDataPath
  const parent = dirname(root)
  if (existsSync(parent) && lstatSync(parent).isSymbolicLink()) throw new Error('TRIAL_PROFILE_PATH_ESCAPE')
  if (existsSync(root) && lstatSync(root).isSymbolicLink()) throw new Error('TRIAL_PROFILE_PATH_ESCAPE')
  mkdirSync(root, { recursive: true, mode: 0o700 })
  const marker = join(root, 'trial-profile.json')
  const expected = { schema: 1, mode: 'trial', profileId: profile.profileId }
  if (existsSync(marker)) {
    if (lstatSync(marker).isSymbolicLink()) throw new Error('TRIAL_PROFILE_PATH_ESCAPE')
    if (JSON.stringify(JSON.parse(readFileSync(marker, 'utf8'))) !== JSON.stringify(expected)) throw new Error('TRIAL_PROFILE_ID_CONFLICT')
  } else {
    if (readdirSync(root).length) throw new Error('TRIAL_PROFILE_UNMARKED_DATA')
    writeFileSync(marker, JSON.stringify(expected), { flag: 'wx', mode: 0o600 })
  }
  assertTrialPath(root, join(root, 'browser-session'))
  mkdirSync(join(root, 'browser-session'), { recursive: true, mode: 0o700 })
  environment.HSC_TRIAL_USER_DATA_DIR = root
}
