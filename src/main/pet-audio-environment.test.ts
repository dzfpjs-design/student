import { expect, it, vi } from 'vitest'
vi.mock('electron', () => ({ app: { getPath: () => '', isPackaged: false } }))
import { buildLocalServiceChildEnvironment } from './local-service'
it('does not accept shell PET enablement, keys or invented tariff as desktop authority', () => {
  const environment = buildLocalServiceChildEnvironment({ HSC_PET_AUDIO_ENABLED: 'true',
    HSC_PET_AUDIO_API_KEY: 'synthetic-inherited-secret', HSC_PET_AUDIO_TARIFF_VERSION: 'unknown',
    HSC_PET_AUDIO_INPUT_SECOND_MICROS: '1' }, { sessionToken: 's', dataEncryptionKey: 'k',
    dataDir: '/tmp/synthetic', databasePath: '/tmp/synthetic/test.db', localModelDir: '/tmp/models',
    modelConfig: null, voiceConfig: null })
  expect(Object.keys(environment).filter(name => name.startsWith('HSC_PET_AUDIO_'))).toEqual([])
})
