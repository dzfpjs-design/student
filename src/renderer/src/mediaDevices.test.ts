import { describe, expect, it, vi } from 'vitest'
import { browserMediaErrorMessage, browserMediaErrorName, configureDocumentCameraAutofocus, documentCameraVideoConstraints, findBoundAudioDevice, findBoundVideoDevice, isMediaDeviceError, matchVideoHardwareInventory, mediaDeviceFailure, mediaDeviceInfoHash, observationCameraVideoConstraints, openDocumentCameraStream, recommendVideoRoleDevices, resolveBoundVideoDevice, resolveCompanionAudioDevice } from './mediaDevices'

const observation = { kind: 'videoinput' as const, deviceId: 'room-camera', groupId: 'group-a', label: '观察摄像头' }
const documentCamera = { kind: 'videoinput' as const, deviceId: 'desk-camera', groupId: 'group-b', label: '高拍仪' }

describe('bound media device selection', () => {
  it('assigns the two external Windows cameras by role and excludes the integrated cameras', () => {
    const devices = [
      { kind: 'videoinput' as const, deviceId: 'integrated-rgb', groupId: 'g1', label: 'Integrated Camera' },
      { kind: 'videoinput' as const, deviceId: 'integrated-ir', groupId: 'g2', label: 'Integrated IR Camera' },
      { kind: 'videoinput' as const, deviceId: 'l523', groupId: 'g3', label: 'L523' },
      { kind: 'videoinput' as const, deviceId: 'desk', groupId: 'g4', label: 'Usb HDCamera' },
    ]

    expect(recommendVideoRoleDevices(devices)).toEqual({ cameraId: 'l523', documentCameraId: 'desk' })
  })

  it('rejects a persisted integrated camera binding for both external-only roles', async () => {
    const integrated = { kind: 'videoinput' as const, deviceId: 'integrated', groupId: 'g1', label: 'Integrated Camera' }
    const hash = await mediaDeviceInfoHash(integrated)

    await expect(resolveBoundVideoDevice([integrated], hash, integrated.label, 'camera')).resolves.toBeNull()
    await expect(resolveBoundVideoDevice([integrated], hash, integrated.label, 'document_camera')).resolves.toBeNull()
  })

  it('preserves media error identity after Electron turns a DOMException into a plain object', () => {
    const bridged = { name: 'NotAllowedError', message: 'Permission denied by system' }
    expect(browserMediaErrorName(bridged)).toBe('NotAllowedError')
    expect(browserMediaErrorMessage(bridged)).toBe('Permission denied by system')
    expect(isMediaDeviceError(bridged)).toBe(true)
    expect(mediaDeviceFailure(bridged, '麦克风').code).toBe('MEDIA_PERMISSION_DENIED')
  })
  it('requests continuous autofocus when the document camera exposes it', async () => {
    let focusMode = 'manual'
    const applyConstraints = vi.fn(async () => { focusMode = 'continuous' })
    const track = {
      getCapabilities: () => ({ focusMode: ['manual', 'continuous'] }),
      getSettings: () => ({ focusMode }), applyConstraints,
    } as unknown as MediaStreamTrack

    await expect(configureDocumentCameraAutofocus(track)).resolves.toEqual({
      exposed: true, applied: true, requested_mode: 'continuous', active_mode: 'continuous'
    })
    expect(applyConstraints).toHaveBeenCalledWith({ advanced: [{ focusMode: 'continuous' }] })
  })

  it('leaves device-managed autofocus alone when Chromium does not expose focus controls', async () => {
    const applyConstraints = vi.fn()
    const track = { getCapabilities: () => ({}), getSettings: () => ({}), applyConstraints } as unknown as MediaStreamTrack

    await expect(configureDocumentCameraAutofocus(track)).resolves.toEqual({
      exposed: false, applied: false, requested_mode: null, active_mode: null
    })
    expect(applyConstraints).not.toHaveBeenCalled()
  })

  it('requests the hardware-proven maximum document resolution and keeps observation conservative', () => {
    const expected = {
      deviceId: { exact: 'camera-id' },
      width: { ideal: 1280, max: 1280 },
      height: { ideal: 720, max: 720 },
      frameRate: { ideal: 10, max: 15 },
    }
    expect(documentCameraVideoConstraints('camera-id')).toEqual({
      deviceId: { exact: 'camera-id' }, width: { exact: 4160 }, height: { exact: 3120 },
      resizeMode: { ideal: 'none' }, frameRate: { ideal: 5, max: 10 },
    })
    expect(documentCameraVideoConstraints('camera-id', 'monitor').height).toEqual({ exact: 3120 })
    expect(observationCameraVideoConstraints('camera-id')).toEqual(expected)
  })

  it('limits the proven Windows capture exception to the bound L523', () => {
    const userAgent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Windows NT 10.0')
    try {
      const standard = observationCameraVideoConstraints('bound-id')
      expect(observationCameraVideoConstraints('bound-id', 'Usb HDCamera')).toEqual(standard)
      expect(observationCameraVideoConstraints('bound-id', 'L523')).toMatchObject({
        deviceId: { exact: 'bound-id' }, width: { exact: 1920 }, height: { exact: 1088 }, frameRate: { max: 10 },
      })
      userAgent.mockReturnValue('Macintosh')
      expect(observationCameraVideoConstraints('bound-id', 'L523')).toEqual(standard)
    } finally { userAgent.mockRestore() }
  })

  it('falls back to the next exact document mode when hardware rejects the maximum pair', async () => {
    const stream = { getTracks: () => [] } as unknown as MediaStream
    const unsupported = new DOMException('unsupported mode', 'OverconstrainedError')
    const getUserMedia = vi.fn().mockRejectedValueOnce(unsupported).mockResolvedValueOnce(stream)

    await expect(openDocumentCameraStream('desk-camera', 'monitor', getUserMedia)).resolves.toBe(stream)
    expect(getUserMedia).toHaveBeenNthCalledWith(1, expect.objectContaining({ video: expect.objectContaining({ width: { exact: 4160 }, height: { exact: 3120 } }) }))
    expect(getUserMedia).toHaveBeenNthCalledWith(2, expect.objectContaining({ video: expect.objectContaining({ width: { exact: 3840 }, height: { exact: 2160 } }) }))
  })

  it('retries a transient Windows video-source start failure before giving up', async () => {
    vi.useFakeTimers()
    const stream = { getTracks: () => [] } as unknown as MediaStream
    const getUserMedia = vi.fn()
      .mockRejectedValueOnce(new DOMException('Timeout starting video source', 'AbortError'))
      .mockResolvedValueOnce(stream)

    const result = openDocumentCameraStream('desk-camera', 'monitor', getUserMedia)
    await vi.advanceTimersByTimeAsync(1_200)

    await expect(result).resolves.toBe(stream)
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    vi.useRealTimers()
  })

  it('does not retry a permission denial for the document camera', async () => {
    const denied = new DOMException('denied', 'NotAllowedError')
    const getUserMedia = vi.fn().mockRejectedValue(denied)

    await expect(openDocumentCameraStream('desk-camera', 'monitor', getUserMedia)).rejects.toBe(denied)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('matches the persisted hash without depending on enumeration order', async () => {
    const expected = await mediaDeviceInfoHash(observation)
    const matched = await findBoundVideoDevice([documentCamera, observation], expected)
    expect(matched?.deviceId).toBe('room-camera')
  })

  it('fails closed when the bound hardware is not enumerated', async () => {
    const expected = await mediaDeviceInfoHash(observation)
    const matched = await findBoundVideoDevice([documentCamera], expected)
    expect(matched).toBeNull()
  })

  it('recovers one previously bound device after Chromium rotates its device id', async () => {
    const expected = await mediaDeviceInfoHash({ ...documentCamera, deviceId: 'previous-browser-id' })
    const matched = await findBoundVideoDevice([observation, documentCamera], expected, '高拍仪')
    expect(matched?.deviceId).toBe('desk-camera')
    await expect(resolveBoundVideoDevice([observation, documentCamera], expected, '高拍仪')).resolves.toMatchObject({
      method: 'unique_exact_label', device: { deviceId: 'desk-camera' },
    })
  })

  it('refuses a label fallback when more than one video device has that label', async () => {
    const expected = await mediaDeviceInfoHash({ ...documentCamera, deviceId: 'previous-browser-id' })
    const matched = await findBoundVideoDevice([
      { ...documentCamera, deviceId: 'desk-camera-a' },
      { ...documentCamera, deviceId: 'desk-camera-b' },
    ], expected, '高拍仪')
    expect(matched).toBeNull()
    await expect(resolveBoundVideoDevice([
      { ...documentCamera, deviceId: 'desk-camera-a' },
      { ...documentCamera, deviceId: 'desk-camera-b' },
    ], expected, '高拍仪')).resolves.toBeNull()
  })

  it('keeps the same identity when Windows changes only the display label', async () => {
    const original = await mediaDeviceInfoHash(observation)
    const renamed = await mediaDeviceInfoHash({ ...observation, label: 'USB Camera (updated driver)' })
    expect(renamed).toBe(original)
  })

  it('keeps the same identity when the browser rotates the media group id', async () => {
    const original = await mediaDeviceInfoHash(observation)
    const nextSession = await mediaDeviceInfoHash({ ...observation, groupId: 'group-next-session' })
    expect(nextSession).toBe(original)
  })

  it('restores a microphone by its persisted hash before considering the label', async () => {
    const microphone = { kind: 'audioinput' as const, deviceId: 'parent-mic', groupId: 'audio-group', label: 'USB Microphone' }
    const matched = await findBoundAudioDevice([microphone], { system_id_hash: await mediaDeviceInfoHash(microphone), label: 'USB Microphone' })
    expect(matched?.deviceId).toBe('parent-mic')
  })

  it('uses only one exact non-empty microphone-label fallback after an id rotation', async () => {
    const old = { kind: 'audioinput' as const, deviceId: 'old-mic', groupId: 'old-group', label: '桌面麦克风' }
    const rotated = { ...old, deviceId: 'new-mic', groupId: 'new-group' }
    const matched = await findBoundAudioDevice([rotated], { system_id_hash: await mediaDeviceInfoHash(old), label: old.label })
    expect(matched?.deviceId).toBe('new-mic')
  })

  it('rejects duplicate or empty microphone labels instead of guessing', async () => {
    const old = { kind: 'audioinput' as const, deviceId: 'old-mic', groupId: '', label: 'USB Microphone' }
    const duplicate = await findBoundAudioDevice([
      { ...old, deviceId: 'mic-a' }, { ...old, deviceId: 'mic-b' },
    ], { system_id_hash: await mediaDeviceInfoHash(old), label: old.label })
    const empty = await findBoundAudioDevice([{ ...old, deviceId: 'empty-mic', label: '' }], { system_id_hash: await mediaDeviceInfoHash(old), label: '' })
    expect(duplicate).toBeNull()
    expect(empty).toBeNull()
  })

  it('uses the Windows default instead of coupling listening to a document camera', async () => {
    const camera = { kind: 'videoinput' as const, deviceId: 'desk-camera', groupId: 'desk-video-group', label: 'Usb HDCamera (eba4:1303)' }
    const highCameraMic = { kind: 'audioinput' as const, deviceId: 'desk-mic', groupId: 'desk-audio-group', label: '麦克风 (HDAudio) (eba4:1303)' }
    const systemMic = { kind: 'audioinput' as const, deviceId: 'laptop-mic', groupId: 'laptop-group', label: '麦克风阵列 (8086:51ca)' }
    const result = await resolveCompanionAudioDevice(
      [systemMic, highCameraMic, camera],
      null,
      { system_id_hash: await mediaDeviceInfoHash(camera), label: camera.label },
    )

    expect(result).toEqual({ method: 'system_default', device: null })
  })

  it('falls back to the Windows default when an explicit microphone is offline', async () => {
    const old = { kind: 'audioinput' as const, deviceId: 'old-mic', groupId: '', label: '独立麦克风' }
    const result = await resolveCompanionAudioDevice(
      [{ kind: 'audioinput', deviceId: 'laptop-mic', groupId: '', label: '麦克风阵列' }],
      { system_id_hash: await mediaDeviceInfoHash(old), label: old.label },
    )

    expect(result).toEqual({ method: 'system_default', device: null })
  })

  it('matches unique Windows inventory names but refuses ambiguous generic cameras', () => {
    const inventory: DesktopVideoHardwareItem[] = [
      { name: 'CZUR Document Camera', manufacturer: 'CZUR', driver_version: '1', driver_provider: 'Microsoft', driver_date: null, usb_vid: '1234', usb_pid: '5678', transport_hint: 'usb', instance_id_hash: 'a'.repeat(64) },
      { name: 'USB Camera', manufacturer: 'Vendor A', driver_version: '1', driver_provider: 'Microsoft', driver_date: null, usb_vid: '1111', usb_pid: '2222', transport_hint: 'usb', instance_id_hash: 'b'.repeat(64) },
      { name: 'USB Camera', manufacturer: 'Vendor B', driver_version: '2', driver_provider: 'Microsoft', driver_date: null, usb_vid: '3333', usb_pid: '4444', transport_hint: 'usb', instance_id_hash: 'c'.repeat(64) },
    ]
    expect(matchVideoHardwareInventory('CZUR Document Camera (Interface 0)', inventory)?.item.manufacturer).toBe('CZUR')
    expect(matchVideoHardwareInventory('USB Camera', inventory)).toBeNull()
  })
})
