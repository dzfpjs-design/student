import { describe, expect, it } from 'vitest'
import { runtimeWebPreferences } from './window-config'

describe('runtimeWebPreferences', () => {
  it('keeps camera observation and qualification running while the window is backgrounded', () => {
    expect(runtimeWebPreferences.backgroundThrottling).toBe(false)
    expect(runtimeWebPreferences).toMatchObject({
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    })
  })
})
