import { describe, expect, it, vi } from 'vitest'
import { applyWindowsCameraCompatibility } from './camera-capture-compatibility'

describe('Windows UVC capture compatibility', () => {
  it('uses the validated Windows capture path before Chromium starts', () => {
    const commandLine = { getSwitchValue: vi.fn(() => ''), appendSwitch: vi.fn() }
    applyWindowsCameraCompatibility('win32', commandLine)
    expect(commandLine.appendSwitch).toHaveBeenCalledExactlyOnceWith('disable-features', 'MediaFoundationVideoCapture')
  })

  it('preserves other feature overrides including their trial parameters', () => {
    const existing = 'ExistingFeature<ExistingTrial,OtherFeature:mode/existing'
    const commandLine = { getSwitchValue: vi.fn(() => existing), appendSwitch: vi.fn() }
    applyWindowsCameraCompatibility('win32', commandLine)
    expect(commandLine.appendSwitch).toHaveBeenCalledExactlyOnceWith('disable-features', `${existing},MediaFoundationVideoCapture`)
  })

  it.each(['MediaFoundationVideoCapture', 'OtherFeature,MediaFoundationVideoCapture<ExistingTrial'])('keeps an existing disable override unchanged: %s', existing => {
    const commandLine = { getSwitchValue: vi.fn(() => existing), appendSwitch: vi.fn() }
    applyWindowsCameraCompatibility('win32', commandLine)
    expect(commandLine.appendSwitch).not.toHaveBeenCalled()
  })

  it.each(['darwin', 'linux'])('leaves %s capture and acceleration policy alone', platform => {
    const commandLine = { getSwitchValue: vi.fn(), appendSwitch: vi.fn() }
    applyWindowsCameraCompatibility(platform, commandLine)
    expect(commandLine.getSwitchValue).not.toHaveBeenCalled()
    expect(commandLine.appendSwitch).not.toHaveBeenCalled()
  })
})
