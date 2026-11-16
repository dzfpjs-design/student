import { describe, expect, it } from 'vitest'
import { FrameHealthMonitor } from './frameHealth'

function syntheticCanvasFrame(width: number, height: number, pixel: (x: number, y: number) => number): ImageData {
  const data = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y += 1) for (let x = 0; x < width; x += 1) {
    const value = pixel(x, y)
    const offset = (y * width + x) * 4
    data[offset] = value; data[offset + 1] = value; data[offset + 2] = value; data[offset + 3] = 255
  }
  return { data, width, height, colorSpace: 'srgb' } as ImageData
}

describe('local frame health', () => {
  it('rejects synthetic black and white canvas frames without any person inference', () => {
    expect(new FrameHealthMonitor().inspect(syntheticCanvasFrame(24, 16, () => 0)).status).toBe('too_dark')
    expect(new FrameHealthMonitor().inspect(syntheticCanvasFrame(24, 16, () => 255)).status).toBe('too_bright')
  })

  it('rejects a privacy-shutter placeholder even when a tiny logo raises global variance', () => {
    const placeholder = syntheticCanvasFrame(24, 16, (x, y) => x < 2 && y < 2 ? 235 : 135)
    const result = new FrameHealthMonitor().inspect(placeholder)
    expect(result.status).toBe('low_contrast')
    expect(result.message).toContain('隐私挡板')
  })

  it('keeps a static but detailed reading scene healthy indefinitely', () => {
    const monitor = new FrameHealthMonitor()
    const detailed = syntheticCanvasFrame(24, 16, (x, y) => (x * 17 + y * 23) % 180 + 30)
    expect(monitor.inspect(detailed).status).toBe('healthy')
    // Five minutes at the production two-second sampling interval.
    for (let sample = 0; sample < 150; sample += 1) expect(monitor.inspect(detailed).status).toBe('healthy')
  })

  it('keeps both unchanged and changed detailed frames healthy', () => {
    const monitor = new FrameHealthMonitor()
    const detailed = syntheticCanvasFrame(24, 16, (x, y) => (x * 17 + y * 23) % 180 + 30)
    expect(monitor.inspect(detailed).status).toBe('healthy')
    for (let sample = 0; sample < 20; sample += 1) expect(monitor.inspect(detailed).status).toBe('healthy')
    const changed = syntheticCanvasFrame(24, 16, (x, y) => (x * 17 + y * 23 + 4) % 180 + 30)
    expect(monitor.inspect(changed).status).toBe('healthy')
    for (let sample = 0; sample < 20; sample += 1) expect(monitor.inspect(changed).status).toBe('healthy')
  })
})
