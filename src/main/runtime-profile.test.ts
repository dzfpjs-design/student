import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { prepareRuntimeProfile, resolveRuntimeProfile } from './runtime-profile'
import { resolveFamilyDataPaths } from './data-paths'

const roots: string[] = []
afterEach(() => { roots.forEach(root => rmSync(root, { recursive: true, force: true })); roots.length = 0 })
function setup() {
  const directory = mkdtempSync(join(tmpdir(), 'hsc-profile-')); roots.push(directory)
  const formal = join(directory, 'formal')
  mkdirSync(formal)
  return { directory, formal }
}
describe('isolated trial profiles', () => {
  it('allows an explicit packaged trial launcher without an environment setup', () => {
    const { formal } = setup(); const env: NodeJS.ProcessEnv = {}
    const profile = resolveRuntimeProfile(formal, env, true, ['app.exe', '--trial-profile=family-test'])
    prepareRuntimeProfile(profile, env)
    expect(env.HSC_RUNTIME_MODE).toBe('trial')
    expect(env.HSC_TRIAL_USER_DATA_DIR).toBe(profile.userDataPath)
    expect(() => resolveRuntimeProfile(formal, { HSC_RUNTIME_MODE: 'formal' }, true, ['--trial-profile=other'])).toThrow('TRIAL_PROFILE_ID_CONFLICT')
  })
  it('never adopts an unmarked folder or redirected browser/marker storage', () => {
    const { formal } = setup(); const env = { HSC_RUNTIME_MODE: 'trial', HSC_TRIAL_PROFILE_ID: 'safe' }
    const profile = resolveRuntimeProfile(formal, env, true)
    mkdirSync(profile.userDataPath, { recursive: true }); writeFileSync(join(profile.userDataPath, 'existing.db'), 'preserve')
    expect(() => prepareRuntimeProfile(profile, env)).toThrow('TRIAL_PROFILE_UNMARKED_DATA')
    expect(readFileSync(join(profile.userDataPath, 'existing.db'), 'utf8')).toBe('preserve')
    rmSync(join(profile.userDataPath, 'existing.db')); prepareRuntimeProfile(profile, env)
    rmSync(join(profile.userDataPath, 'browser-session'), { recursive: true }); symlinkSync(formal, join(profile.userDataPath, 'browser-session'), 'dir')
    expect(() => prepareRuntimeProfile(profile, env)).toThrow('TRIAL_PROFILE_PATH_ESCAPE')
    rmSync(join(profile.userDataPath, 'browser-session')); rmSync(join(profile.userDataPath, 'trial-profile.json'))
    writeFileSync(join(formal, 'marker'), '{}'); symlinkSync(join(formal, 'marker'), join(profile.userDataPath, 'trial-profile.json'))
    expect(() => prepareRuntimeProfile(profile, env)).toThrow('TRIAL_PROFILE_PATH_ESCAPE')
  })
  it('isolates packaged trial storage, persists identity, and preserves formal routing', () => {
    const { formal } = setup()
    const environment = { HSC_RUNTIME_MODE: 'trial', HSC_TRIAL_PROFILE_ID: 'child-test-1' }
    const profile = resolveRuntimeProfile(formal, environment, true)
    expect(profile.userDataPath).not.toBe(formal)
    prepareRuntimeProfile(profile, environment); prepareRuntimeProfile(profile, environment)
    expect(JSON.parse(readFileSync(join(profile.userDataPath, 'trial-profile.json'), 'utf8')).mode).toBe('trial')
    expect(resolveFamilyDataPaths(profile.userDataPath, environment).databasePath).toContain('child-test-1')
    expect(resolveRuntimeProfile(formal, { HSC_ELECTRON_USER_DATA_DIR: '/ignored' }, true).userDataPath).toBe(formal)
  })
  it.each(['', '../formal', 'a/b', '.', 'a'.repeat(65)])('rejects invalid identities %s', id => {
    expect(() => resolveRuntimeProfile('/family', { HSC_RUNTIME_MODE: 'trial', HSC_TRIAL_PROFILE_ID: id }, true)).toThrow('TRIAL_PROFILE_ID_REQUIRED')
  })
  it('rejects formal database overrides and symlink escapes', () => {
    const { formal } = setup()
    const env: NodeJS.ProcessEnv = { HSC_RUNTIME_MODE: 'trial', HSC_TRIAL_PROFILE_ID: 'isolated' }
    const profile = resolveRuntimeProfile(formal, env, true); prepareRuntimeProfile(profile, env)
    expect(() => resolveFamilyDataPaths(profile.userDataPath, { ...env, HSC_DATA_DIR: formal })).toThrow('TRIAL_PROFILE_PATH_ESCAPE')
    expect(() => resolveFamilyDataPaths(profile.userDataPath, { ...env, HSC_DATABASE_PATH: join(formal, 'home-study.db') })).toThrow('TRIAL_PROFILE_PATH_ESCAPE')
    symlinkSync(formal, join(profile.userDataPath, 'family-data'), 'dir')
    expect(() => resolveFamilyDataPaths(profile.userDataPath, env)).toThrow('TRIAL_PROFILE_PATH_ESCAPE')
  })
})
