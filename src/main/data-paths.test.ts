import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { resolveFamilyDataPaths } from './data-paths'

describe('resolveFamilyDataPaths', () => {
  it('keeps family data under Electron userData by default', () => {
    expect(resolveFamilyDataPaths('/app-profile', {})).toEqual({
      dataDir: join('/app-profile', 'family-data'),
      databasePath: join('/app-profile', 'family-data', 'home-study.db')
    })
  })

  it('derives the database from an operator-provided data directory', () => {
    expect(resolveFamilyDataPaths('/app-profile', { HSC_DATA_DIR: '/durable-family-data' })).toEqual({
      dataDir: '/durable-family-data',
      databasePath: join('/durable-family-data', 'home-study.db')
    })
  })

  it('allows installer smoke tests to override the database path explicitly', () => {
    expect(resolveFamilyDataPaths('/app-profile', {
      HSC_DATA_DIR: '/durable-family-data',
      HSC_DATABASE_PATH: '/smoke/home-study.db'
    })).toEqual({
      dataDir: '/durable-family-data',
      databasePath: '/smoke/home-study.db'
    })
  })
})
