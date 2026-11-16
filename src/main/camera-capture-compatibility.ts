type CaptureCommandLine = {
  getSwitchValue(name: string): string
  appendSwitch(name: string, value?: string): void
}

/** Apply before app.whenReady(): Windows L523 can end its Media Foundation
 * track and leave subsequent opens pending. The isolated DirectShow path is
 * under real-device long-run verification; keep GPU/audio policy intact. */
export function applyWindowsCameraCompatibility(platform: string, commandLine: CaptureCommandLine): void {
  if (platform !== 'win32') return
  const feature = 'MediaFoundationVideoCapture'
  const existing = commandLine.getSwitchValue('disable-features')
  if (existing.split(',').some(value => value.trim().split(/[<:]/, 1)[0] === feature)) return
  commandLine.appendSwitch('disable-features', existing ? `${existing},${feature}` : feature)
}
