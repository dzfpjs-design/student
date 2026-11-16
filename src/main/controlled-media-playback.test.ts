import { describe, expect, it, vi } from 'vitest'
import { prepareControlledMediaPlayback } from './controlled-media-playback'

const source = 'file:///synthetic/approved.webm'
function setup() {
  const state = { url: source, currentSrc: source, readyState: 4, width: 32, height: 32, advanced: true, paused: false, error: null }
  const window = { isDestroyed: () => false, loadURL: vi.fn().mockResolvedValue(undefined), webContents: { executeJavaScript: vi.fn().mockResolvedValue(state) } }
  return { window, state }
}
describe('controlled media uses the original playable media, not navigation success', () => {
  it('accepts a direct media navigation rejection only after exact media playback is observed', async () => {
    const { window } = setup(); window.loadURL.mockRejectedValue(new Error('ERR_FAILED direct media document'))
    await expect(prepareControlledMediaPlayback(window, source, () => true)).resolves.toBeUndefined()
    expect(window.loadURL).toHaveBeenCalledExactlyOnceWith(source)
  })
  it.each([
    { url: 'file:///other.webm' }, { currentSrc: 'file:///other.webm' }, { readyState: 1 },
    { advanced: false }, { paused: true }, { error: 4 }, { width: 0 }, { height: 0 },
  ])('does not turn an unready or different source into opened: %j', patch => {
    const { window, state } = setup(); window.webContents.executeJavaScript.mockResolvedValue({ ...state, ...patch })
    return expect(prepareControlledMediaPlayback(window, source, () => true)).rejects.toThrow('播放')
  })
  it('does not prepare a superseded window after its navigation completes late', async () => {
    const { window } = setup(); let current = true; let release!: () => void
    window.loadURL.mockReturnValue(new Promise<void>(resolve => { release = resolve }))
    const pending = prepareControlledMediaPlayback(window, source, () => current)
    current = false; release()
    await expect(pending).rejects.toThrow('变化')
    expect(window.webContents.executeJavaScript).not.toHaveBeenCalled()
  })
  it('does not accept a late ready response after replacement', async () => {
    const { window } = setup(); let current = true
    window.webContents.executeJavaScript.mockImplementation(async () => { current = false; return { ...setup().state } })
    await expect(prepareControlledMediaPlayback(window, source, () => current)).rejects.toThrow('变化')
  })
  it('bounds an unresolved renderer operation without claiming playback', async () => {
    const { window } = setup(); window.webContents.executeJavaScript.mockReturnValue(new Promise(() => {}))
    await expect(prepareControlledMediaPlayback(window, source, () => true, 15)).rejects.toThrow('超时')
  })
})
