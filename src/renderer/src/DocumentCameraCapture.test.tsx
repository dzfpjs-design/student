import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi, resetRuntimeCache } from './api'
import { DocumentCameraCapture } from './DocumentCameraCapture'
import { mediaDeviceInfoHash } from './mediaDevices'
import { CAPTURE_RECEIPT_EVENT, type CaptureReceipt } from './deviceCommandReceipt'
import { CAMERA_DEVICE_STATUS, queryCameraDevices, type CameraStatusRequest } from './cameraDeviceRuntime'
import { extractDocumentFrameFeatures } from './documentFrameMonitor'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import { spawn } from 'node:child_process'
import { mkdir, mkdtemp, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { once } from 'node:events'

// A successful play() must model the playing state as well as its fulfilled promise.
function mockSuccessfulMediaPlay(this: HTMLMediaElement): Promise<void> {
  Object.defineProperties(this, {
    paused: { configurable: true, value: false },
    ended: { configurable: true, value: false },
  })
  return Promise.resolve()
}

function mockAdvancingVideoFrames(video: HTMLVideoElement): void {
  Object.defineProperties(video, {
    readyState: { configurable: true, value: 4 },
    getVideoPlaybackQuality: { configurable: true, value: () => ({ totalVideoFrames: Math.floor(performance.now() / 20) }) },
  })
}

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const originalDesktop = Object.getOwnPropertyDescriptor(window, 'desktop')

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (originalDesktop) Object.defineProperty(window, 'desktop', originalDesktop)
  else Reflect.deleteProperty(window, 'desktop')
  resetRuntimeCache()
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

describe('DocumentCameraCapture device lifecycle', () => {
  it('submits an intact parent file through its independent source without acquiring a camera', async () => {
    const artifact = { artifact_id: 'parent-file-page', status: 'needs_review', extracted_text: '文件原页',
      confidence: .6, local_path: null, details: { lines: [], engine: 'synthetic' } }
    const parentPort = { acquire: vi.fn(), renew: vi.fn(), release: vi.fn(), recognize: vi.fn(),
      recognizeFile: vi.fn().mockResolvedValue(artifact) }
    const delivered = vi.fn()
    render(<DocumentCameraCapture sessionId="parent-file-source" parentPort={parentPort} onArtifact={delivered} />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: {
      files: [new File(['intact-parent-file'], 'page.png', { type: 'image/png' })],
    } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: /旋转/ }))
    fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(delivered).toHaveBeenCalledWith(artifact))
    expect(parentPort.recognizeFile).toHaveBeenCalledWith(btoa('intact-parent-file'),
      { rotation: 90, crop: { top: 10, right: 0, bottom: 0, left: 0 } }, expect.any(String))
    expect(parentPort.acquire).not.toHaveBeenCalled()
    expect(parentPort.recognize).not.toHaveBeenCalled()
    expect(parentPort.renew).not.toHaveBeenCalled()
    expect(parentPort.release).not.toHaveBeenCalled()
  })

  it('retains a failed file and retries the same source key instead of asking for a camera', async () => {
    const parentPort = { acquire: vi.fn(), renew: vi.fn(), release: vi.fn(), recognize: vi.fn(),
      recognizeFile: vi.fn().mockRejectedValue(new Error('offline')) }
    render(<DocumentCameraCapture sessionId="parent-file-retry" parentPort={parentPort} />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: {
      files: [new File(['original'], 'page.png', { type: 'image/png' })],
    } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await screen.findByText('图片识别失败，原表单内容没有丢失。')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(parentPort.recognizeFile).toHaveBeenCalledTimes(2))
    expect(parentPort.recognizeFile.mock.calls[0]).toEqual(parentPort.recognizeFile.mock.calls[1])
    expect(screen.getByAltText('待识别的作业图片')).toBeInTheDocument()
    expect(parentPort.acquire).not.toHaveBeenCalled()
  })

  it('routes a parent file through the parent file endpoint with a stable frozen request', async () => {
    const calls: { path: string; body: Record<string, unknown> }[] = []
    const api = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
      if (path.endsWith('/file-ocr')) {
        calls.push({ path, body: JSON.parse(String(init?.body)) })
        throw new Error('uncertain transport')
      }
      return { items: [] }
    })
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="parent-file-child" captureOnly />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: {
      files: [new File(['original-page'], 'page.png', { type: 'image/png' })],
    } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await screen.findByText('图片识别失败，原表单内容没有丢失。')
    fireEvent.change(screen.getByRole('textbox', { name: '教材页标题' }), { target: { value: '重试期间新的定位' } })
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[0]).toEqual(calls[1])
    expect(calls[0].path).toBe('/v1/parent/knowledge-graph/textbook-capture/parent-file-child/file-ocr')
    expect(calls[0].body.image_base64).toBe(btoa('original-page'))
    expect(calls[0].body.lease_id).toBeUndefined()
    expect(api.mock.calls.some(([path]) => path.endsWith('/leases') || path.endsWith('/ocr'))).toBe(false)
  })

  it('does not discard an imported file or open a camera when local OCR is at capacity', async () => {
    const parentPort = { acquire: vi.fn(), renew: vi.fn(), release: vi.fn(), recognize: vi.fn(),
      recognizeFile: vi.fn().mockRejectedValue(new ApiError('busy', 503, 'TEXTBOOK_OCR_CAPACITY_UNAVAILABLE')) }
    render(<DocumentCameraCapture sessionId="parent-file-capacity" parentPort={parentPort} />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: {
      files: [new File(['original'], 'page.png', { type: 'image/png' })],
    } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await screen.findByText('本机 CPU 正忙，原页已保留。可以在待处理页面中稍后重试，无需重新拍摄。')
    expect(screen.getByAltText('待识别的作业图片')).toBeInTheDocument()
    expect(parentPort.acquire).not.toHaveBeenCalled()
  })

  it('never turns a blurred file into an automated camera recapture', async () => {
    const artifact = { artifact_id: 'static-file', status: 'completed', extracted_text: '待核对的文字',
      confidence: .4, local_path: null, details: { lines: [], engine: 'synthetic',
        quality: { passed: false, issues: ['blurred'], messages: ['模糊'], metrics: {}, version: 'test' } } }
    const parentPort = { acquire: vi.fn(), renew: vi.fn(), release: vi.fn(), recognize: vi.fn(),
      recognizeFile: vi.fn().mockResolvedValue(artifact) }
    const delivered = vi.fn()
    render(<DocumentCameraCapture sessionId="parent-file-blurred" parentPort={parentPort}
      automation="capture_and_recognize" onArtifact={delivered} />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: {
      files: [new File(['static-file'], 'page.png', { type: 'image/png' })],
    } })
    await waitFor(() => expect(delivered).toHaveBeenCalledWith(artifact))
    expect(parentPort.acquire).not.toHaveBeenCalled()
    expect(screen.queryByText(/我会重新拍/)).not.toBeInTheDocument()
  })

  it('retires the original child file and ignores its late response on a parent child switch', async () => {
    let finish!: (value: unknown) => void
    const api = vi.fn(async (path: string): Promise<unknown> => {
      if (path.endsWith('/file-ocr')) return new Promise(resolve => { finish = resolve })
      return { items: [] }
    })
    const { rerender } = render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="old-child" captureOnly />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: {
      files: [new File(['old-child-file'], 'page.png', { type: 'image/png' })],
    } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(finish).toBeTypeOf('function'))
    rerender(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="new-child" captureOnly />)
    expect(screen.queryByAltText('待识别的作业图片')).not.toBeInTheDocument()
    await act(async () => { finish({ artifact: { capture_ref: 'old-page', status: 'ready', extracted_text: '旧孩子页面',
      confidence: .9, details: { lines: [], engine: 'synthetic' }, page_context: {}, raw_image_retained: true, raw_image_byte_length: 1 } }) })
    expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('')
    expect(api.mock.calls.some(([path]) => path === '/v1/parent/knowledge-graph/textbook-capture/new-child/file-ocr')).toBe(false)
  })

  it('sends the intact parent frame plus frozen crop/rotation, not a canvas-derived replacement', async () => {
    await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
    const intact = 'data:image/jpeg;base64,aW50YWN0LXBhcmVudC1mcmFtZQ=='
    const canvas = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue(intact)
    const artifact = { artifact_id: 'raw-parent-frame', status: 'needs_review', extracted_text: 'Review',
      confidence: 0.62, local_path: null, details: { lines: [], engine: 'synthetic', ocr_confidence: 0.62 } }
    const parentPort = {
      acquire: vi.fn().mockResolvedValue({ lease_id: 'original-camera-lease', label: '家庭高拍仪' }),
      renew: vi.fn().mockResolvedValue({}), release: vi.fn().mockResolvedValue({}),
      recognize: vi.fn().mockResolvedValue(artifact),
    }
    const delivered = vi.fn()
    render(<DocumentCameraCapture sessionId="parent-original-derivation" parentPort={parentPort} onArtifact={delivered} />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByText('家庭高拍仪')
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
    fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
    fireEvent.click(screen.getByRole('button', { name: /旋转/ }))
    fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(delivered).toHaveBeenCalledWith(artifact))
    expect(parentPort.recognize).toHaveBeenCalledWith(intact.split(',')[1], 'original-camera-lease',
      { rotation: 90, crop: { top: 10, right: 0, bottom: 0, left: 0 } })
    expect(canvas).toHaveBeenCalledTimes(1)
  })

  it('captures through real parent HTTP and resumes the encrypted page after backend restart before importing it', async () => {
    const fixture = await installDocumentCamera()
    const hash = (await localApi.deviceBindings())[0].system_id_hash
    const root = await mkdtemp(join(tmpdir(), 'hsc-parent-camera-'))
    const isolatedHome = join(root, 'home')
    await mkdir(isolatedHome)
    const python = resolve('.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
    const helper = 'services/local_agent/tests/fixtures/parent_camera_http.py'
    let origin = '', stopServer: (() => Promise<void>) | undefined
    const startServer = async (): Promise<void> => {
      const process = spawn(python, ['-u', helper, '--data-root', root, '--device-hash', hash], {
        stdio: ['ignore', 'pipe', 'pipe'],
        env: { PATH: globalThis.process.env.PATH, HOME: isolatedHome, USERPROFILE: isolatedHome,
          HSC_DATA_DIR: join(root, 'import-only'),
          HSC_DATABASE_PATH: join(root, 'import-only', 'home-study.db'),
          PYTHONDONTWRITEBYTECODE: '1', SystemRoot: globalThis.process.env.SystemRoot },
      })
      const exit = once(process, 'exit')
      let diagnostic = ''
      process.stderr.on('data', chunk => { diagnostic = (diagnostic + String(chunk)).slice(-2000) })
      stopServer = async () => {
        if (process.exitCode !== null || process.signalCode !== null) return
        process.kill('SIGTERM')
        const timer = setTimeout(() => process.kill('SIGKILL'), 5000)
        try { await exit } finally { clearTimeout(timer) }
      }
      const port = await new Promise<string>((resolvePort, reject) => {
        const timer = setTimeout(() => reject(new Error(`isolated server startup timed out: ${diagnostic}`)), 20_000)
        process.once('error', error => { clearTimeout(timer); reject(error) })
        process.once('exit', () => { clearTimeout(timer); reject(new Error(`isolated server exited: ${diagnostic}`)) })
        let output = ''
        process.stdout.on('data', chunk => {
          output += String(chunk)
          const match = output.match(/HSC_PARENT_PORT=(\d+)/)
          if (match) { clearTimeout(timer); resolvePort(match[1]) }
        })
      })
      origin = `http://127.0.0.1:${port}`
      await waitFor(async () => { expect((await fetch(`${origin}/v1/parent/knowledge-graph/textbook-pages`, { headers: { 'X-HSC-Parent': 'ok' } })).status).toBe(200) }, { timeout: 10_000 })
    }
    const api = async (path: string, init?: RequestInit): Promise<unknown> => {
      const response = await fetch(origin + path, { ...init, headers: { ...init?.headers, 'X-HSC-Parent': 'ok' } })
      if (!response.ok) throw new Error(`parent HTTP ${response.status}: ${await response.text()}`)
      return response.json()
    }
    try {
      await startServer()
      expect(existsSync(join(isolatedHome, '.home-study-companion'))).toBe(false)
      vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
      vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==')
      class TestImage { width = 640; height = 480; onload: (() => void) | null = null; set src(_value: string) { queueMicrotask(() => this.onload?.()) } }
      vi.stubGlobal('Image', TestImage)
      let view = render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="child-4" captureOnly />)
      await waitFor(() => expect(screen.getByLabelText('教材页标题')).toBeEnabled())
      fireEvent.change(screen.getByLabelText('教材页标题'), { target: { value: '四上英语' } })
      fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
      await screen.findByTitle('关闭高拍仪')
      Object.defineProperties(document.querySelector('video')!, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
      fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
      expect(fixture.stop).toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
      await screen.findByLabelText('校对后的教材 OCR 文字（第 1 项）', {}, { timeout: 10_000 })
      const saved = await api('/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts') as { items: { capture_ref: string; raw_image_retained: boolean }[] }
      expect(saved.items).toHaveLength(1); expect(saved.items[0].raw_image_retained).toBe(true)
      view.unmount(); await stopServer!(); await startServer()
      view = render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="child-4" captureOnly />)
      expect(await screen.findByLabelText('校对后的教材 OCR 文字（第 1 项）')).toHaveValue('Words: apple')
      const source = await fetch(`${origin}/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/${saved.items[0].capture_ref}/source`, { headers: { 'X-HSC-Parent': 'ok' } })
      expect(source.status).toBe(200); expect(await source.text()).toBe('hello textbook page image')
      fireEvent.click(screen.getByRole('button', { name: '校对并带入表单' }))
      await waitFor(() => expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('Words: apple'))
      await waitFor(() => expect(screen.getByRole('button', { name: '保存本机 OCR 页' })).toBeEnabled())
      fireEvent.click(screen.getByRole('button', { name: '保存本机 OCR 页' }))
      await screen.findByText(/已保存 1 页资料/)
      view.unmount(); await stopServer!(); await startServer()
      view = render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="child-4" captureOnly />)
      await screen.findByText(/已保存 1 页资料/)
      const pending = await api('/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts') as { items: unknown[] }
      expect(pending.items).toHaveLength(0)
      view.unmount()
    } finally { cleanup(); await stopServer?.(); await rm(root, { recursive: true, force: true }) }
  }, 60_000)

  it('keeps the parent camera lease through the actual textbook OCR request and releases it after the saved artifact', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    class TestImage {
      width = 640; height = 480
      onload: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    let active = false
    const operations: string[] = []
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path.endsWith('/leases')) { active = true; operations.push('acquire'); return { lease_id: 'parent-original-lease' } }
      if (path.endsWith('/release')) { active = false; operations.push('release'); return { status: 'released' } }
      if (path.endsWith('/ocr')) {
        operations.push('ocr')
        const body = JSON.parse(String(init?.body))
        if (!active || body.lease_id !== 'parent-original-lease') throw new Error('textbook capture lease expired')
        if (!body.page_context.title.trim()) throw new Error('textbook page title required')
        return { artifact: { capture_ref: 'parent-saved-page', status: 'ready', extracted_text: '本机已保存的教材页文字',
          confidence: .99, local_path: null, page_context: { title: '初三物理', page_number: 1 }, raw_image_retained: true,
          details: { lines: [], engine: 'synthetic', quality: { passed: true, issues: [], messages: [], metrics: {} } } } }
      }
      return { items: [] }
    })
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="parent-child" captureOnly />)
    fireEvent.change(screen.getByLabelText('教材页标题'), { target: { value: '初三物理' } })
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByTitle('关闭高拍仪')
    Object.defineProperties(document.querySelector('video')!, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
    fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
    expect(fixture.stop).toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('本机已保存的教材页文字'))
    await waitFor(() => expect(active).toBe(false))
    expect(operations).toEqual(['acquire', 'ocr', 'release'])
    expect(api.mock.calls.filter(([path]) => path.endsWith('/ocr'))).toHaveLength(1)
  })

  it('keeps only the newest imported image when file readers finish in reverse order', async () => {
    const readers: TestReader[] = []
    class TestReader {
      result = ''; onload: (() => void) | null = null
      readAsDataURL(): void { readers.push(this) }
    }
    vi.stubGlobal('FileReader', TestReader)
    render(<DocumentCameraCapture sessionId="import-owner" />)
    const input = document.querySelector('input[type=file]')!
    for (const name of ['first', 'second']) fireEvent.change(input, { target: { files: [new File([name], `${name}.png`, { type: 'image/png' })] } })
    await act(async () => { readers[1].result = 'data:image/png;base64,c2Vjb25k'; readers[1].onload?.() })
    expect(screen.getByAltText('待识别的作业图片')).toHaveAttribute('src', 'data:image/png;base64,c2Vjb25k')
    await act(async () => { readers[0].result = 'data:image/png;base64,Zmlyc3Q='; readers[0].onload?.() })
    expect(screen.getByAltText('待识别的作业图片')).toHaveAttribute('src', 'data:image/png;base64,c2Vjb25k')
  })

  it('does not send OCR when the image transform changes while preprocessing is pending', async () => {
    const images: TestImage[] = []
    class TestImage {
      width = 640; height = 480; onload: (() => void) | null = null
      set src(_value: string) { images.push(this) }
    }
    vi.stubGlobal('Image', TestImage)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    const ocr = vi.spyOn(localApi, 'runOcr')
    render(<DocumentCameraCapture sessionId="preprocess-owner" />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: { files: [new File(['page'], 'page.png', { type: 'image/png' })] } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(images).toHaveLength(1))
    fireEvent.click(screen.getByTitle('顺时针旋转90度'))
    await act(async () => { images[0].onload?.() })
    expect(ocr).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '确认并识别' })).toBeEnabled()
  })

  it('does not overwrite manual crop changes with a late suggested crop', async () => {
    const images: TestImage[] = []
    class TestImage {
      width = 640; height = 480; onload: (() => void) | null = null
      set src(_value: string) { images.push(this) }
    }
    vi.stubGlobal('Image', TestImage)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), getImageData: () => ({ data: new Uint8ClampedArray(320 * 240 * 4) }) } as unknown as CanvasRenderingContext2D)
    render(<DocumentCameraCapture sessionId="crop-owner" />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: { files: [new File(['page'], 'page.png', { type: 'image/png' })] } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '建议裁切' }))
    await waitFor(() => expect(images).toHaveLength(1))
    fireEvent.change(screen.getByLabelText('上边'), { target: { value: '13' } })
    await act(async () => { images[0].onload?.() })
    expect(screen.getByLabelText('上边')).toHaveValue('13')
  })

  it('retires a pending OCR consumer when its session changes', async () => {
    class TestImage {
      width = 640; height = 480; onload: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    let finish!: (value: Awaited<ReturnType<typeof localApi.runOcr>>) => void
    const ocr = vi.spyOn(localApi, 'runOcr').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const onArtifact = vi.fn(), onText = vi.fn()
    const view = render(<DocumentCameraCapture sessionId="original-session" onArtifact={onArtifact} onText={onText} />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: { files: [new File(['page'], 'page.png', { type: 'image/png' })] } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(ocr).toHaveBeenCalledTimes(1))
    view.rerender(<DocumentCameraCapture sessionId="new-session" onArtifact={onArtifact} onText={onText} />)
    await act(async () => { finish({ artifact_id: 'old-artifact', extracted_text: '旧页', confidence: .99, local_path: null, status: 'completed', details: { lines: [], engine: 'synthetic' } }) })
    expect(onArtifact).not.toHaveBeenCalled(); expect(onText).not.toHaveBeenCalled()
    expect(screen.queryByAltText('待识别的作业图片')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开高拍仪' })).toBeEnabled()
  })

  it('does not submit a parent imported file without a live capture authorization', async () => {
    const parentPort = { acquire: vi.fn(), renew: vi.fn(), release: vi.fn(), recognize: vi.fn() }
    render(<DocumentCameraCapture sessionId="parent-no-permit" parentPort={parentPort} automation="capture_and_recognize" />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: { files: [new File(['page'], 'page.png', { type: 'image/png' })] } })
    await screen.findByAltText('待识别的作业图片')
    await waitFor(() => expect(screen.getByText(/图片文件尚未取得家长导入授权/)).toBeInTheDocument())
    expect(parentPort.recognize).not.toHaveBeenCalled()
  })

  it('ignores a late renewal failure after the camera has closed', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const intervals = vi.spyOn(window, 'setInterval')
    let fail!: (error: Error) => void
    vi.spyOn(localApi, 'renewDeviceLease').mockImplementation(() => new Promise((_resolve, reject) => { fail = reject }))
    render(<DocumentCameraCapture sessionId="renew-owner" />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByTitle('关闭高拍仪')
    await waitFor(() => expect(intervals.mock.calls.some(([, delay]) => delay === 60_000)).toBe(true))
    const renewal = intervals.mock.calls.find(([, delay]) => delay === 60_000)![0] as () => void
    await act(async () => { renewal() })
    fireEvent.click(screen.getByTitle('关闭高拍仪'))
    await act(async () => { fail(new Error('late old renewal')) })
    expect(fixture.stop).toHaveBeenCalled()
    expect(screen.queryByText(/设备租约续期失败/)).not.toBeInTheDocument()
  })

  it.each([404, 410])('retires an expired camera lease on %s and offers voice reopening without repeated renewals', async status => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const intervals = vi.spyOn(window, 'setInterval')
    const renew = vi.spyOn(localApi, 'renewDeviceLease').mockRejectedValue(new ApiError('expired', status))
    const guidance = vi.fn()
    render(<DocumentCameraCapture sessionId="expired-camera" onGuidance={guidance} />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByTitle('关闭高拍仪')
    await waitFor(() => expect(intervals.mock.calls.some(([, delay]) => delay === 60_000)).toBe(true))
    const renewal = intervals.mock.calls.find(([, delay]) => delay === 60_000)![0] as () => void
    await act(async () => { renewal(); await Promise.resolve() })
    expect(fixture.stop).toHaveBeenCalled()
    expect(screen.getByText(/高拍仪连接已失效/)).toBeInTheDocument()
    expect(guidance).toHaveBeenCalledWith(expect.stringContaining('重新拍题'))
    expect(screen.getByRole('button', { name: '打开高拍仪' })).toBeEnabled()
    await act(async () => { renewal(); await Promise.resolve() })
    expect(renew).toHaveBeenCalledTimes(1)
  })

  it('does not treat a temporary renewal network failure as proof the lease expired', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const intervals = vi.spyOn(window, 'setInterval')
    vi.spyOn(localApi, 'renewDeviceLease').mockRejectedValue(new ApiError('network', 503))
    render(<DocumentCameraCapture sessionId="temporary-renewal" />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByTitle('关闭高拍仪')
    await waitFor(() => expect(intervals.mock.calls.some(([, delay]) => delay === 60_000)).toBe(true))
    const renewal = intervals.mock.calls.find(([, delay]) => delay === 60_000)![0] as () => void
    await act(async () => { renewal(); await Promise.resolve() })
    expect(fixture.stop).not.toHaveBeenCalled()
    expect(screen.getByText(/设备租约续期失败/)).toBeInTheDocument()
  })

  it('keeps a captured page and does not replay an in-flight OCR when its permit expires', async () => {
    await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    const intervals = vi.spyOn(window, 'setInterval')
    let finish!: (value: Awaited<ReturnType<typeof localApi.runOcr>>) => void
    const recognize = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const parentPort = { acquire: vi.fn().mockResolvedValue({ lease_id: 'test-lease' }),
      renew: vi.fn().mockRejectedValue(new ApiError('expired', 404)), release: vi.fn().mockResolvedValue({}), recognize }
    const delivered = vi.fn(), guidance = vi.fn()
    render(<DocumentCameraCapture sessionId="pending-ocr-lease" parentPort={parentPort} onArtifact={delivered} onGuidance={guidance} />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByTitle('关闭高拍仪')
    const video = document.querySelector('video')!
    Object.defineProperties(video, { videoWidth: { configurable: true, value: 640 }, videoHeight: { configurable: true, value: 480 } })
    fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(recognize).toHaveBeenCalledTimes(1))
    const renewal = intervals.mock.calls.filter(([, delay]) => delay === 60_000).at(-1)![0] as () => void
    await act(async () => { renewal(); await Promise.resolve() })
    expect(guidance).toHaveBeenCalledWith(expect.stringContaining('识别结果尚未确认'))
    expect(screen.getByAltText('待识别的作业图片')).toBeInTheDocument()
    await act(async () => { finish({ artifact_id: 'old', extracted_text: 'synthetic', confidence: 1, local_path: null,
      status: 'completed', details: { lines: [], engine: 'test' } }) })
    expect(delivered).not.toHaveBeenCalled()
    expect(recognize).toHaveBeenCalledTimes(1)
    expect(screen.getByAltText('待识别的作业图片')).toBeInTheDocument()
  })

  it.each(['import', 'rotate', 'crop'] as const)('does not deliver an old OCR success after a %s change to its captured page', async change => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    class TestImage {
      width = 640; height = 480
      onload: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    let finish!: (value: Awaited<ReturnType<typeof localApi.runOcr>>) => void
    const ocr = vi.spyOn(localApi, 'runOcr').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const onArtifact = vi.fn(), onText = vi.fn()
    render(<DocumentCameraCapture sessionId="page-owner-session" onArtifact={onArtifact} onText={onText} />)
    const input = document.querySelector('input[type=file]')!
    fireEvent.change(input, { target: { files: [new File(['first-page'], 'first.png', { type: 'image/png' })] } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(ocr).toHaveBeenCalledTimes(1))
    if (change === 'import') {
      fireEvent.change(input, { target: { files: [new File(['second-page'], 'second.png', { type: 'image/png' })] } })
      await waitFor(() => expect(screen.getByAltText('待识别的作业图片')).toHaveAttribute('src', 'data:image/png;base64,c2Vjb25kLXBhZ2U='))
    } else if (change === 'rotate') fireEvent.click(screen.getByTitle('顺时针旋转90度'))
    else fireEvent.change(screen.getByLabelText('上边'), { target: { value: '10' } })
    await act(async () => { finish({ artifact_id: 'old-page-artifact', extracted_text: '旧页面答案', confidence: .99,
      local_path: null, status: 'completed', details: { lines: [], engine: 'test' } }) })
    expect(onArtifact).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()
    expect(ocr).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: '确认并识别' })).toBeEnabled()
  })

  it('confirms the child camera lease only after its own usable frame and once per lease', async () => {
    await installDocumentCamera()
    const confirm = vi.spyOn(localApi, 'confirmDeviceFrame').mockImplementation(async lease => ({
      lease_id: lease, status: 'frame_confirmed', resolved_incidents: 0 }))
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    render(<DocumentCameraCapture sessionId="session-document" />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByText('家庭高拍仪')
    const video = document.querySelector('video')!
    expect(confirm).not.toHaveBeenCalled()
    Object.defineProperties(video, { readyState: { configurable: true, value: 4 },
      videoWidth: { configurable: true, value: 640 }, videoHeight: { configurable: true, value: 480 } })
    fireEvent.canPlay(video)
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce())
    expect(confirm).toHaveBeenCalledWith(expect.any(String), 640, 480)
    fireEvent.canPlay(video)
    expect(confirm).toHaveBeenCalledOnce()
  })

  it('reports its own live frame even when an inactive continuous monitor also answers the query', async () => {
    await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const view = render(<DocumentCameraCapture sessionId="session-document" />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByText('家庭高拍仪')
    const video = document.querySelector('video')!
    Object.defineProperties(video, {
      readyState: { configurable: true, value: 4 },
      videoWidth: { configurable: true, value: 640 },
      videoHeight: { configurable: true, value: 480 },
      getVideoPlaybackQuality: { configurable: true, value: () => ({ totalVideoFrames: 1 }) },
    })
    const yielded = (event: Event): void => {
      const detail = (event as CustomEvent<CameraStatusRequest>).detail
      detail.resolve({ device_type: 'document_camera', track_live: false, frame_confirmed: false, last_frame_at: null, last_error_code: null, monitor_active: false })
    }
    window.addEventListener(CAMERA_DEVICE_STATUS, yielded)
    try {
      await waitFor(() => expect(queryCameraDevices('session-document', 'document_camera').devices[0])
        .toMatchObject({ track_live: true, frame_confirmed: true }))
      expect(queryCameraDevices('different-session', 'document_camera').devices[0].frame_confirmed).toBe(false)
      view.unmount()
      expect(queryCameraDevices('session-document', 'document_camera').devices[0].frame_confirmed).toBe(false)
    } finally { window.removeEventListener(CAMERA_DEVICE_STATUS, yielded) }
  })
  it('auto-opens exactly once through the StrictMode remount cycle', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)

    render(<StrictMode><DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" /></StrictMode>)

    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('家庭高拍仪')).toBeInTheDocument()
  })

  it.each(['clear question', 'unnumbered manuscript', 'readable with framing warnings', 'empty with framing warnings'] as const)(
    'handles the first automatic OCR result without a child click (%s)', async (kind) => {
    const manuscript = kind === 'unnumbered manuscript'
    const empty = kind === 'empty with framing warnings'
    const framingWarning = empty || kind === 'readable with framing warnings'
    const events = vi.spyOn(window, 'dispatchEvent')
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const pixels = new Uint8ClampedArray(320 * 240 * 4)
    for (let y = 0; y < 240; y += 1) for (let x = 0; x < 320; x += 1) {
      const offset = (y * 320 + x) * 4, value = y % 20 < 2 && x > 30 && x < 290 ? 40 : 220
      pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
      pixels[offset + 3] = 255
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn(),
      getImageData: () => ({ data: pixels, width: 320, height: 240 }) } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    class TestImage {
      width = 640
      height = 480
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    const issues = framingWarning ? ['possible_hand_occlusion', 'page_not_detected'] : []
    const artifact = {
      artifact_id: 'auto-ocr-1', extracted_text: empty ? '   ' : manuscript ? 'Dear Alex, I enjoyed our school trip.' : '12加8等于多少？', confidence: 0.93,
      local_path: null, status: framingWarning ? 'quality_warning' : 'completed', details: {
        lines: [], engine: 'local-test', ocr_confidence: 0.93,
        quality: { passed: !framingWarning, issues, messages: framingWarning ? ['画面可能被遮挡。'] : [], metrics: {} },
        local_pipeline: {
          quality: { passed: !framingWarning, issues, metrics: {}, transforms: { orientation_degrees: 180, orientation_auto_corrected: true } },
          engine: 'local-test', route: 'cpu', question_blocks: manuscript ? [{
            text: 'Dear Alex, I enjoyed our school trip.', line_count: 1, confidence: .93,
            block_kind: 'fragment', needs_manual_boundary_check: true, boundary_review_reasons: ['missing_question_start'],
          }] : [],
          manual_review: { required: manuscript || framingWarning, reasons: manuscript ? ['missing_question_start'] : issues, handwriting: '', math: '' }, queue: { task_id: 'test', behavior_fps_cap: 1 }
        }
      }
    }
    const runOcr = vi.spyOn(localApi, 'runOcr').mockResolvedValue(artifact as never)
    const onText = vi.fn()
    const onArtifact = vi.fn()
    const recordRendererDiagnostic = vi.fn().mockResolvedValue({ recorded: true })
    window.desktop = { recordRendererDiagnostic } as unknown as NonNullable<typeof window.desktop>

    render(<DocumentCameraCapture sessionId="session-document" commandId="capture-turn" automation="capture_and_recognize" onText={onText} onArtifact={onArtifact} />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(document.querySelector('video')).not.toBeNull())

    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(1300) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(runOcr).toHaveBeenCalledExactlyOnceWith('session-document', 'ZmFrZQ==')
    if (empty) {
      expect(onText).not.toHaveBeenCalled()
      expect(onArtifact).not.toHaveBeenCalled()
      expect(recordRendererDiagnostic).toHaveBeenCalledWith(expect.objectContaining({
        event: 'document.capture.retry_requested', details: { reason: 'occluded', retry_count: 1 },
      }))
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
      expect(fixture.getUserMedia).toHaveBeenCalledTimes(2)
      window.desktop = undefined
      return
    }
    expect(onArtifact).toHaveBeenCalledExactlyOnceWith(artifact)
    expect(onText).toHaveBeenCalledWith(artifact.extracted_text, artifact)
    expect(events.mock.calls.map(([event]) => event as CustomEvent<CaptureReceipt>).filter((event) => event.type === CAPTURE_RECEIPT_EVENT).map((event) => event.detail)).toEqual([
      expect.objectContaining({ commandId: 'capture-turn', status: 'captured' }),
      expect.objectContaining({ commandId: 'capture-turn', status: 'recognized', artifactId: 'auto-ocr-1' }),
    ])
    expect(screen.getByAltText('待识别的作业图片')).toHaveStyle({ transform: 'rotate(180deg)' })
    expect(screen.getByText(/已自动转正180度/)).toBeInTheDocument()
    if (framingWarning) expect(screen.getByText(/需要人工确认.*请核对识别文字和题目边界/)).toBeInTheDocument()
    expect(fixture.release).toHaveBeenCalledWith('document-lease', 'capture_completed', expect.any(AbortSignal))
    expect(recordRendererDiagnostic).toHaveBeenCalledWith(expect.objectContaining({ event: 'document.capture.camera_ready' }))
    expect(recordRendererDiagnostic).toHaveBeenCalledWith(expect.objectContaining({ event: 'document.capture.frame_captured' }))
    expect(recordRendererDiagnostic).toHaveBeenCalledWith(expect.objectContaining({
      event: 'document.capture.ocr_completed',
      details: expect.objectContaining({ extracted_chars: artifact.extracted_text.length, auto_orientation: 180, quality_passed: !framingWarning }),
    }))
    expect(recordRendererDiagnostic).not.toHaveBeenCalledWith(expect.objectContaining({ event: 'document.capture.retry_requested' }))
    window.desktop = undefined
  })

  it.each(['no context', 'no readback', 'readback throws'] as const)(
    'does not auto-capture when frame analysis is unavailable (%s)', async (failure) => {
      const fixture = await installDocumentCamera()
      vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
      const context = failure === 'no context' ? null : {
        drawImage: vi.fn(),
        ...(failure === 'readback throws' ? { getImageData: () => { throw new Error('readback failed') } } : {}),
      }
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
      const encode = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
      const runOcr = vi.spyOn(localApi, 'runOcr')
      const guidance = vi.fn()
      render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" onGuidance={guidance} />)
      await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(document.querySelector('video')).toBeInTheDocument())
      const video = document.querySelector('video')!
      Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
      Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
      vi.useFakeTimers()
      mockAdvancingVideoFrames(video)
      fireEvent.canPlay(video)
      await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
      expect(screen.getByText(/无法判断画面是否稳定.*自动拍摄已暂停/)).toBeInTheDocument()
      expect(guidance).toHaveBeenCalledOnce()
      expect(encode).not.toHaveBeenCalled()
      expect(runOcr).not.toHaveBeenCalled()
      expect(screen.queryByAltText('待识别的作业图片')).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
      if (failure === 'no context') {
        expect(screen.getByText(/暂时无法拍摄画面/)).toBeInTheDocument()
        expect(encode).not.toHaveBeenCalled()
        expect(document.querySelector('video')).toBeInTheDocument()
      } else {
        expect(encode).toHaveBeenCalledOnce()
        expect(screen.getByAltText('待识别的作业图片')).toBeInTheDocument()
      }
    }
  )

  it('resumes automatic capture only after frame analysis recovers and stability is re-established', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const pixels = new Uint8ClampedArray(320 * 240 * 4)
    for (let y = 0; y < 240; y += 1) for (let x = 0; x < 320; x += 1) {
      const offset = (y * 320 + x) * 4, value = y % 20 < 2 && x > 30 && x < 290 ? 40 : 220
      pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
      pixels[offset + 3] = 255
    }
    let readbackAvailable = false
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), getImageData: () => {
      if (!readbackAvailable) throw new Error('readback failed')
      return { data: pixels, width: 320, height: 240 }
    } } as unknown as CanvasRenderingContext2D)
    const encode = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('video')).toBeInTheDocument())
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(1800) })
    expect(encode).not.toHaveBeenCalled()
    readbackAvailable = true
    await act(async () => { await vi.advanceTimersByTimeAsync(900) })
    expect(encode).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(450) })
    expect(encode).toHaveBeenCalledOnce()
  })

  it.each(['resume', 'manual'] as const)(
    'does not auto-capture a frozen decoded frame and supports %s recovery', async (recovery) => {
      const fixture = await installDocumentCamera()
      vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
      const pixels = new Uint8ClampedArray(320 * 240 * 4)
      for (let y = 0; y < 240; y += 1) for (let x = 0; x < 320; x += 1) {
        const offset = (y * 320 + x) * 4, value = y % 20 < 2 && x > 30 && x < 290 ? 40 : 220
        pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
        pixels[offset + 3] = 255
      }
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(),
        getImageData: () => ({ data: pixels, width: 320, height: 240 }) } as unknown as CanvasRenderingContext2D)
      const encode = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
      const runOcr = vi.spyOn(localApi, 'runOcr')
      render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" />)
      await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(document.querySelector('video')).toBeInTheDocument())
      const video = document.querySelector('video')!
      Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
      Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
      vi.useFakeTimers()
      mockAdvancingVideoFrames(video)
      let frozen = true
      Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true,
        value: () => ({ totalVideoFrames: frozen ? 1 : 1 + Math.floor(performance.now() / 20) }) })
      fireEvent.canPlay(video)
      await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
      expect(screen.getByText(/无法确认高拍仪画面持续更新.*自动拍摄已暂停/)).toBeInTheDocument()
      expect(encode).not.toHaveBeenCalled()
      expect(runOcr).not.toHaveBeenCalled()
      if (recovery === 'manual') {
        fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
      } else {
        frozen = false
        await act(async () => { await vi.advanceTimersByTimeAsync(1350) })
      }
      expect(encode).toHaveBeenCalledOnce()
      expect(screen.getByAltText('待识别的作业图片')).toBeInTheDocument()
    }
  )

  it.each(['missing', 'throws'] as const)('does not treat an unavailable frame counter as fresh video (%s)', async (mode) => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const encode = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('video')).toBeInTheDocument())
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true,
      value: mode === 'missing' ? undefined : () => { throw new Error('counter unavailable') } })
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(screen.getByText(/无法确认高拍仪画面持续更新.*自动拍摄已暂停/)).toBeInTheDocument()
    expect(encode).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '拍摄' })).toBeEnabled()
  })

  it('rejects a change after the stability sample and encodes the same snapshot it verifies', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const pixels = (changed: boolean): Uint8ClampedArray => {
      const data = new Uint8ClampedArray(320 * 240 * 4)
      for (let y = 0; y < 240; y += 1) for (let x = 0; x < 320; x += 1) {
        const offset = (y * 320 + x) * 4
        const mark = changed && x >= 100 && x < 150 && y >= 105 && y < 120
        const value = mark || (y % 20 < 2 && x > 30 && x < 290) ? 40 : 220
        data[offset] = data[offset + 1] = data[offset + 2] = value
        data[offset + 3] = 255
      }
      return data
    }
    const clear = pixels(false), changed = pixels(true)
    let changeBeforeSnapshot = true
    let verifiedCanvas: HTMLCanvasElement | null = null
    let encodedCanvas: HTMLCanvasElement | null = null
    const inputByCanvas = new WeakMap<HTMLCanvasElement, CanvasImageSource>()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement) {
      const canvas = this
      return {
        drawImage: (input: CanvasImageSource) => { inputByCanvas.set(canvas, input); if (input instanceof HTMLCanvasElement) verifiedCanvas = input },
        getImageData: () => ({ data: changeBeforeSnapshot && inputByCanvas.get(canvas) instanceof HTMLCanvasElement ? changed : clear, width: 320, height: 240 }),
      } as unknown as CanvasRenderingContext2D
    } as unknown as typeof HTMLCanvasElement.prototype.getContext)
    const encode = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(function (this: HTMLCanvasElement) {
      encodedCanvas = this
      return 'data:image/jpeg;base64,ZmFrZQ=='
    })
    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('video')).toBeInTheDocument())
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(1200) })
    expect(screen.getByText(/页面刚刚发生变化/)).toBeInTheDocument()
    expect(encode).not.toHaveBeenCalled()
    changeBeforeSnapshot = false
    await act(async () => { await vi.advanceTimersByTimeAsync(1350) })
    expect(encode).toHaveBeenCalledOnce()
    expect(verifiedCanvas).not.toBeNull()
    expect(encodedCanvas).toBe(verifiedCanvas)
  })

  it('keeps the complete oversized camera snapshot while scaling to the OCR pixel budget', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const pixels = new Uint8ClampedArray(320 * 192 * 4)
    for (let y = 0; y < 192; y += 1) for (let x = 0; x < 320; x += 1) {
      const offset = (y * 320 + x) * 4, value = y % 20 < 2 && x > 30 && x < 290 ? 40 : 220
      pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = value
      pixels[offset + 3] = 255
    }
    const inputs = new WeakMap<HTMLCanvasElement, CanvasImageSource>()
    let verifiedSnapshot: HTMLCanvasElement | null = null
    let encodedCanvas: HTMLCanvasElement | null = null
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement) {
      const canvas = this
      return {
        drawImage: (source: CanvasImageSource) => {
          inputs.set(canvas, source)
          if (canvas.width === 320 && source instanceof HTMLCanvasElement) verifiedSnapshot = source
        },
        getImageData: () => ({ data: pixels, width: 320, height: 192 }),
      } as unknown as CanvasRenderingContext2D
    } as unknown as typeof HTMLCanvasElement.prototype.getContext)
    const encode = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(function (this: HTMLCanvasElement) {
      encodedCanvas = this
      return 'data:image/jpeg;base64,ZmFrZQ=='
    })
    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('video')).toBeInTheDocument())
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 5000 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 3000 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(1350) })
    expect(encode).toHaveBeenCalledOnce()
    expect(encodedCanvas).not.toBeNull()
    const output = encodedCanvas as unknown as HTMLCanvasElement
    expect(output.width * output.height).toBeLessThanOrEqual(4160 * 3120)
    expect(output.width / output.height).toBeCloseTo(5000 / 3000, 3)
    expect(inputs.get(output)).toBe(verifiedSnapshot)
    expect(verifiedSnapshot).not.toBeNull()
    expect((verifiedSnapshot as unknown as HTMLCanvasElement).width).toBe(5000)
    expect((verifiedSnapshot as unknown as HTMLCanvasElement).height).toBe(3000)
  })

  it.each(['compresses', 'rejects'] as const)(
    'bounds a manually captured JPEG payload and %s if it remains too large', async (outcome) => {
      const fixture = await installDocumentCamera()
      vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
      vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
      const oversized = 'data:image/jpeg;base64,' + 'A'.repeat(24_000_001)
      const encode = vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValueOnce(oversized)
        .mockReturnValueOnce(outcome === 'compresses' ? 'data:image/jpeg;base64,ZmFrZQ==' : oversized)
      render(<DocumentCameraCapture sessionId="session-document" />)
      fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
      await screen.findByText('家庭高拍仪')
      const video = document.querySelector('video')!
      Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
      Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
      fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
      expect(encode).toHaveBeenNthCalledWith(1, 'image/jpeg', 0.94)
      expect(encode).toHaveBeenNthCalledWith(2, 'image/jpeg', 0.8)
      if (outcome === 'compresses') {
        expect(screen.getByAltText('待识别的作业图片')).toBeInTheDocument()
      } else {
        expect(screen.getByText(/图片过大，暂时无法识别/)).toBeInTheDocument()
        expect(screen.queryByAltText('待识别的作业图片')).not.toBeInTheDocument()
        expect(fixture.release).not.toHaveBeenCalled()
      }
    }
  )

  it('keeps a complete oversized imported page within the OCR pixel budget', async () => {
    class TestImage {
      width = 5000
      height = 3000
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    const drawImage = vi.fn()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage, translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
    let encodedCanvas: HTMLCanvasElement | null = null
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(function (this: HTMLCanvasElement) {
      encodedCanvas = this
      return 'data:image/jpeg;base64,ZmFrZQ=='
    })
    const artifact = { artifact_id: 'imported-page', extracted_text: '文字', confidence: .9,
      local_path: null, status: 'completed', details: { lines: [], engine: 'synthetic' } }
    const runOcr = vi.spyOn(localApi, 'runOcr').mockResolvedValue(artifact as never)
    render(<DocumentCameraCapture sessionId="session-document" />)
    fireEvent.change(document.querySelector('input[type=file]')!, { target: { files: [new File(['page'], 'page.png', { type: 'image/png' })] } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))
    await waitFor(() => expect(runOcr).toHaveBeenCalledOnce())
    const output = encodedCanvas as unknown as HTMLCanvasElement
    expect(output.width * output.height).toBeLessThanOrEqual(4160 * 3120)
    expect(output.width / output.height).toBeCloseTo(5000 / 3000, 3)
    expect(drawImage.mock.calls[0].slice(1, 5)).toEqual([0, 0, 5000, 3000])
  })

  it('automatically resumes voice capture when the bound camera is reconnected', async () => {
    const fixture = await installDocumentCamera({ appearsAfterDeviceChange: true })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const onGuidance = vi.fn()

    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" onGuidance={onGuidance} />)
    expect(await screen.findByText(/未找到已绑定的高拍仪/)).toBeInTheDocument()
    expect(onGuidance).toHaveBeenCalledWith(expect.stringContaining('接好后我会自动继续'))
    expect(fixture.getUserMedia).not.toHaveBeenCalled()

    act(() => fixture.triggerDeviceChange())

    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('家庭高拍仪')).toBeInTheDocument()
  })

  it('silently keeps observing and refuses to capture while a hand covers the page', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const pixels = new Uint8ClampedArray(320 * 180 * 4)
    for (let y = 0; y < 180; y += 1) for (let x = 0; x < 320; x += 1) {
      const offset = (y * 320 + x) * 4
      const skin = x >= 240
      pixels[offset] = skin ? 190 : 230
      pixels[offset + 1] = skin ? 105 : 230
      pixels[offset + 2] = skin ? 70 : 230
      pixels[offset + 3] = 255
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn(), getImageData: () => ({ data: pixels, width: 320, height: 180 }),
    } as unknown as CanvasRenderingContext2D)
    const runOcr = vi.spyOn(localApi, 'runOcr')
    const onGuidance = vi.fn()

    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" onGuidance={onGuidance} />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('video')).not.toBeNull())
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 1280 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 720 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(1800) })

    expect(onGuidance).not.toHaveBeenCalled()
    expect(runOcr).not.toHaveBeenCalled()
    expect(screen.getByText(/持续观察/)).toBeInTheDocument()
    expect(screen.queryByAltText('待识别的作业图片')).not.toBeInTheDocument()
  })

  it('explains persistent occlusion by voice while continuing to withhold capture', async () => {
    const recordRendererDiagnostic = vi.fn().mockResolvedValue({ recorded: true })
    window.desktop = { recordRendererDiagnostic } as unknown as NonNullable<typeof window.desktop>
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const pixels = new Uint8ClampedArray(320 * 180 * 4)
    for (let y = 0; y < 180; y += 1) for (let x = 0; x < 320; x += 1) {
      const i = (y * 320 + x) * 4, skin = x >= 240
      pixels[i] = skin ? 190 : 230; pixels[i + 1] = skin ? 105 : 230
      pixels[i + 2] = skin ? 70 : 230; pixels[i + 3] = 255
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn(), getImageData: () => ({ data: pixels, width: 320, height: 180 }),
    } as unknown as CanvasRenderingContext2D)
    const runOcr = vi.spyOn(localApi, 'runOcr')
    const guidance = vi.fn()
    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" onGuidance={guidance} />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('video')).toBeInTheDocument())
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 1280 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 720 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(7_500) })
    expect(guidance).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(guidance).toHaveBeenCalledOnce()
    expect(guidance).toHaveBeenCalledWith(expect.stringContaining('可能被遮挡'))
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(guidance).toHaveBeenCalledOnce()
    expect(runOcr).not.toHaveBeenCalled()
    expect(screen.queryByAltText('待识别的作业图片')).not.toBeInTheDocument()
    expect(recordRendererDiagnostic).toHaveBeenCalledWith(expect.objectContaining({
      event: 'document.capture.frame_state', details: expect.objectContaining({
        frame_state: 'occluded', frame_skin_band: 'ge_22', frame_motion_band: 'lt_2',
        frame_content_motion: 'unavailable', frame_paper_band: 'ge_10', frame_light_band: 'usable',
      }),
    }))
    expect(recordRendererDiagnostic.mock.calls.filter(([event]) => event.event === 'document.capture.frame_state')).toHaveLength(1)
    window.desktop = undefined
  })

  it('captures a stable page when a small hand-colored region is only at the frame edge', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const pixels = new Uint8ClampedArray(320 * 180 * 4)
    for (let offset = 0; offset < pixels.length; offset += 4) {
      pixels[offset] = 230
      pixels[offset + 1] = 230
      pixels[offset + 2] = 230
      pixels[offset + 3] = 255
    }
    // A real edge region covers three full cells (6.25% of the image), not
    // just the three center pixels that the former point sampler inspected.
    for (let y = 0; y < 90; y += 1) for (let x = 280; x < 320; x += 1) {
      const offset = (y * 320 + x) * 4
      pixels[offset] = 190
      pixels[offset + 1] = 105
      pixels[offset + 2] = 70
    }
    const features = extractDocumentFrameFeatures({ data: pixels, width: 320, height: 180 })
    expect(features.skinRatio).toBe(0.0625)
    expect(features.contrast).toBeGreaterThan(7)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn(),
      getImageData: () => ({ data: pixels, width: 320, height: 180 }),
    } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    class TestImage {
      width = 640
      height = 480
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    const artifact = {
      artifact_id: 'edge-hand-ocr', extracted_text: 'What do you like?', confidence: 0.91,
      local_path: null, status: 'completed', details: { lines: [], engine: 'local-test', ocr_confidence: 0.91 }
    }
    const runOcr = vi.spyOn(localApi, 'runOcr').mockResolvedValue(artifact as never)
    const onGuidance = vi.fn()

    render(<DocumentCameraCapture sessionId="session-document" automation="capture_and_recognize" onGuidance={onGuidance} />)
    await waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 1280 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 720 })
    vi.useFakeTimers()
    mockAdvancingVideoFrames(video)
    fireEvent.canPlay(video)
    await act(async () => { await vi.advanceTimersByTimeAsync(1800) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(runOcr).toHaveBeenCalledWith('session-document', 'ZmFrZQ==')
    expect(onGuidance).not.toHaveBeenCalledWith(expect.stringContaining('移开遮挡物'))
  })

  it('opens the exact binding only once when the button is clicked rapidly', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)

    render(<DocumentCameraCapture sessionId="session-document" />)
    const open = screen.getByRole('button', { name: '打开高拍仪' })
    fireEvent.click(open)
    fireEvent.click(open)

    await waitFor(() => expect(screen.getByText('家庭高拍仪')).toBeInTheDocument())
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    expect(fixture.getUserMedia).toHaveBeenCalledWith(expect.objectContaining({
      audio: false,
      video: {
        deviceId: { exact: 'document-camera-id' },
        width: { exact: 4160 },
        height: { exact: 3120 },
        resizeMode: { ideal: 'none' },
        frameRate: { ideal: 5, max: 10 },
      },
    }))
    expect(fixture.register).toHaveBeenCalledTimes(1)
    expect(fixture.incident).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTitle('关闭高拍仪'))
    await waitFor(() => expect(fixture.release).toHaveBeenCalledWith('document-lease', 'child_closed', expect.any(AbortSignal)))
    expect(fixture.stop).toHaveBeenCalled()
  })

  it('releases the stream and lease when preview playback fails', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new DOMException('busy', 'NotReadableError'))

    render(<DocumentCameraCapture sessionId="session-document" />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))

    expect(await screen.findByText(/高拍仪可能正被其他程序占用/)).toBeInTheDocument()
    expect(fixture.stop).toHaveBeenCalled()
    expect(fixture.release).toHaveBeenCalledWith('document-lease', 'open_failed', expect.any(AbortSignal))
    expect(fixture.incident).toHaveBeenCalledWith(
      'session-document', 'document_camera', 'DEVICE_BUSY_OR_UNAVAILABLE', expect.any(String),
    )
    expect(screen.queryByTitle('关闭高拍仪')).not.toBeInTheDocument()
  })

  it('opens the uniquely named binding when Chromium rotated the saved device id', async () => {
    const fixture = await installDocumentCamera({ rotatedBindingId: true })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)

    render(<DocumentCameraCapture sessionId="session-document" />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))

    await waitFor(() => expect(screen.getByText('家庭高拍仪')).toBeInTheDocument())
    expect(fixture.getUserMedia).toHaveBeenCalledWith(expect.objectContaining({
      video: expect.objectContaining({ deviceId: { exact: 'document-camera-id' } }),
    }))
    expect(fixture.incident).not.toHaveBeenCalled()
  })

  it('keeps the document camera unavailable when its bound label is absent and offers an explicit retry', async () => {
    const fixture = await installDocumentCamera({ missingBinding: true })
    render(<DocumentCameraCapture sessionId="session-document" />)

    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    expect(await screen.findByText(/未找到已绑定的高拍仪“Fic760xUsbCamera”/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新查找高拍仪' })).toBeInTheDocument()
    expect(fixture.getUserMedia).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '重新查找高拍仪' }))
    await waitFor(() => expect(fixture.enumerateDevices).toHaveBeenCalledTimes(2))
    expect(fixture.getUserMedia).not.toHaveBeenCalled()
  })

  it('ends a hung native open with guidance and does not register a late stream after privacy stop', async () => {
    const fixture = await installDocumentCamera()
    let finish!: (stream: MediaStream) => void
    fixture.getUserMedia.mockImplementation(() => new Promise<MediaStream>(resolve => { finish = resolve }))
    const guidance = vi.fn()
    render(<DocumentCameraCapture sessionId="scanner-native-timeout" onGuidance={guidance} />)
    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await act(async () => {
      await vi.waitFor(() => expect(fixture.getUserMedia).toHaveBeenCalledTimes(1))
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(screen.getByRole('button', { name: '打开高拍仪' })).toBeEnabled()
    expect(screen.getByText(/高拍仪启动超过20秒仍未响应/)).toBeInTheDocument()
    expect(guidance).toHaveBeenCalledWith(expect.stringContaining('启动超过20秒'))
    expect(fixture.incident).toHaveBeenCalledWith('scanner-native-timeout', 'document_camera', 'DEVICE_START_TIMEOUT', expect.any(String))
    fireEvent.click(screen.getByRole('button', { name: '重新查找高拍仪' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    act(() => window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
    finish({ getTracks: () => [{ stop: fixture.stop }] } as unknown as MediaStream)
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(fixture.stop).toHaveBeenCalledTimes(1)
    expect(fixture.register).not.toHaveBeenCalled()
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('cancels an in-flight open when privacy pause is requested', async () => {
    const fixture = await installDocumentCamera({ deferEnumeration: true })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)

    render(<DocumentCameraCapture sessionId="session-document" />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await waitFor(() => expect(fixture.enumerateDevices).toHaveBeenCalled())

    window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
    fixture.finishEnumeration()

    await waitFor(() => expect(screen.getByRole('button', { name: '打开高拍仪' })).toBeEnabled())
    expect(fixture.getUserMedia).not.toHaveBeenCalled()
    expect(fixture.register).not.toHaveBeenCalled()
    expect(fixture.release).not.toHaveBeenCalled()
  })

  it('releases tracks synchronously and never reopens on devicechange after privacy stop', async () => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    const events = vi.spyOn(window, 'dispatchEvent')
    fixture.release.mockRejectedValue(new Error('service offline'))
    render(<DocumentCameraCapture sessionId="session-document" commandId="cancel-turn" automation="capture_and_recognize" />)
    await waitFor(() => expect(screen.getByTitle('关闭高拍仪')).toBeInTheDocument())
    act(() => window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
    expect(fixture.stop).toHaveBeenCalled()
    act(() => fixture.triggerDeviceChange())
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    expect(events.mock.calls.some(([event]) => event.type === CAPTURE_RECEIPT_EVENT && (event as CustomEvent<CaptureReceipt>).detail.status === 'cancelled')).toBe(true)
  })

  it.each(['success', 'failure', 'failure_without_stop'] as const)('reports OCR %s truthfully and discards late results after privacy stop', async (outcome) => {
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    class TestImage {
      width = 640
      height = 480
      onload: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    let succeed!: (value: Awaited<ReturnType<typeof localApi.runOcr>>) => void
    let fail!: (error: Error) => void
    const runOcr = vi.spyOn(localApi, 'runOcr').mockImplementation(() => new Promise((resolve, reject) => { succeed = resolve; fail = reject }))
    const onArtifact = vi.fn()
    const events = vi.spyOn(window, 'dispatchEvent')
    render(<DocumentCameraCapture sessionId="session-document" commandId="late-ocr" automation="capture_and_recognize" onArtifact={onArtifact} />)
    await waitFor(() => expect(screen.getByTitle('关闭高拍仪')).toBeInTheDocument())
    const video = document.querySelector('video')!
    Object.defineProperties(video, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
    fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
    await waitFor(() => expect(runOcr).toHaveBeenCalled())
    if (outcome !== 'failure_without_stop') act(() => window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
    await act(async () => {
      if (outcome !== 'success') fail(new Error('OCR failed'))
      else succeed({ artifact_id: 'late-artifact', extracted_text: 'late text', confidence: 0.99, local_path: null, status: 'completed', details: { lines: [], engine: 'test', ocr_confidence: 0.99 } })
    })
    act(() => fixture.triggerDeviceChange())
    expect(onArtifact).not.toHaveBeenCalled()
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    const outcomes = events.mock.calls.filter(([event]) => event.type === CAPTURE_RECEIPT_EVENT).map(([event]) => (event as CustomEvent<CaptureReceipt>).detail.status)
    expect(outcomes).toEqual(['captured', outcome === 'failure_without_stop' ? 'failed' : 'cancelled'])
  })

  it('keeps the original camera preview when OCR capacity fails after page retention', async () => {
    resetRuntimeCache()
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), translate: vi.fn(), rotate: vi.fn() } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,ZmFrZQ==')
    class TestImage {
      width = 640
      height = 480
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      set src(_value: string) { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('Image', TestImage)
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false, status: 409,
      json: async () => ({ detail: { code: 'TEXTBOOK_OCR_CAPACITY_UNAVAILABLE', message: '本机资源不足，已取消这次临时识别。' } })
    })
    vi.stubGlobal('fetch', fetchMock)
    const parentPort = {
      acquire: vi.fn().mockResolvedValue({ lease_id: 'parent-document-lease', label: '家庭高拍仪' }),
      renew: vi.fn().mockResolvedValue({}), release: vi.fn().mockResolvedValue({}),
      recognize: async (_imageBase64: string) => localApi.knowledgeGraphRequest('/v1/parent/knowledge-graph/textbook-capture/child-4/ocr', { method: 'POST' }) as Promise<never>,
    }

    render(<DocumentCameraCapture sessionId="parent-textbook-capture" parentPort={parentPort} />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    await screen.findByText('家庭高拍仪')
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 640 })
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 480 })
    fireEvent.click(screen.getByRole('button', { name: '拍摄' }))
    fireEvent.click(screen.getByRole('button', { name: '确认并识别' }))

    expect(await screen.findByText('本机 CPU 正忙，原页已保留。可以在待处理页面中稍后重试，无需重新拍摄。')).toBeInTheDocument()
    expect(screen.getByAltText('待识别的作业图片')).toHaveAttribute('src', 'data:image/jpeg;base64,ZmFrZQ==')
    expect(fixture.getUserMedia).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:8765/v1/parent/knowledge-graph/textbook-capture/child-4/ocr', expect.any(Object))
  })

  it('preserves semantic FastAPI detail messages and status codes for conflict and missing resource responses', async () => {
    resetRuntimeCache()
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 409, json: async () => ({ detail: { code: 'TEXTBOOK_CAMERA_UNAVAILABLE', message: 'document camera is already in use' } }) })
      .mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({ detail: { code: 'TEXTBOOK_CAMERA_NOT_FOUND', message: 'OCR artifact not found' } }) }))
    await expect(localApi.knowledgeGraphRequest('/capture-conflict')).rejects.toMatchObject({
      status: 409, code: 'TEXTBOOK_CAMERA_UNAVAILABLE', message: 'document camera is already in use',
    })
    await expect(localApi.knowledgeGraphRequest('/capture-missing')).rejects.toMatchObject({
      status: 404, code: 'TEXTBOOK_CAMERA_NOT_FOUND', message: 'OCR artifact not found',
    })
  })

  it.each([
    [409, 'TEXTBOOK_CAMERA_UNAVAILABLE', '高拍仪已被其他学习流程占用'],
    [404, 'TEXTBOOK_CAMERA_NOT_FOUND', '找不到已绑定的高拍仪'],
  ])('shows a semantic %i parent-camera response from the real knowledge-graph request', async (status, code, message) => {
    resetRuntimeCache()
    const fixture = await installDocumentCamera()
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status, json: async () => ({ detail: { code, message } }) }))
    const parentPort = {
      acquire: async (_systemIdHash: string, _capabilities: Record<string, unknown>) => localApi.knowledgeGraphRequest('/v1/parent/knowledge-graph/textbook-capture/child-4/leases', { method: 'POST' }) as Promise<never>,
      renew: vi.fn().mockResolvedValue({}), release: vi.fn().mockResolvedValue({}), recognize: vi.fn(),
    }
    render(<DocumentCameraCapture sessionId="parent-textbook-capture" parentPort={parentPort} />)
    fireEvent.click(screen.getByRole('button', { name: '打开高拍仪' }))
    expect(await screen.findByText(`高拍仪暂不可用：${message}`)).toBeInTheDocument()
    expect(fixture.stop).toHaveBeenCalled()
  })
})

async function installDocumentCamera(options: { deferEnumeration?: boolean; rotatedBindingId?: boolean; missingBinding?: boolean; appearsAfterDeviceChange?: boolean } = {}): Promise<{
  getUserMedia: ReturnType<typeof vi.fn>
  register: ReturnType<typeof vi.spyOn>
  release: ReturnType<typeof vi.spyOn>
  incident: ReturnType<typeof vi.spyOn>
  stop: ReturnType<typeof vi.fn>
  enumerateDevices: ReturnType<typeof vi.fn>
  finishEnumeration: () => void
  triggerDeviceChange: () => void
}> {
  const device = {
    kind: 'videoinput' as const,
    deviceId: 'document-camera-id',
    groupId: 'document-camera-group',
    label: '家庭高拍仪',
  }
  const stop = vi.fn()
  const track = {
    label: device.label,
    readyState: 'live', enabled: true, muted: false,
    getSettings: () => ({ deviceId: device.deviceId, groupId: device.groupId, width: 2560, height: 1440, frameRate: 30 }),
    getCapabilities: () => ({}),
    addEventListener: vi.fn(),
    stop,
  } as unknown as MediaStreamTrack
  const stream = {
    getVideoTracks: () => [track],
    getTracks: () => [track],
  } as unknown as MediaStream
  const getUserMedia = vi.fn().mockResolvedValue(stream)
  let resolveEnumeration = (): void => undefined
  let enumerationCount = 0
  const enumerateDevices = options.deferEnumeration
    ? vi.fn().mockImplementation(() => new Promise<MediaDeviceInfo[]>((resolve) => {
      resolveEnumeration = () => resolve([device] as MediaDeviceInfo[])
    }))
    : vi.fn().mockImplementation(async () => {
      enumerationCount += 1
      return options.appearsAfterDeviceChange && enumerationCount === 1 ? [] : [device]
    })
  const mediaDeviceEvents = new EventTarget()
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      enumerateDevices,
      getUserMedia,
      addEventListener: mediaDeviceEvents.addEventListener.bind(mediaDeviceEvents),
      removeEventListener: mediaDeviceEvents.removeEventListener.bind(mediaDeviceEvents),
    },
  })
  const binding = options.missingBinding
    ? { ...device, deviceId: 'missing-document-camera-id', label: 'Fic760xUsbCamera' }
    : device
  vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{
    device_type: 'document_camera', system_id_hash: await mediaDeviceInfoHash(options.rotatedBindingId ? { ...binding, deviceId: 'previous-document-camera-id' } : binding), label: binding.label,
    capabilities: {}, verified_at: '2026-08-27T00:00:00Z', updated_at: '2026-08-27T00:00:00Z',
  }])
  const register = vi.spyOn(localApi, 'registerDevice').mockResolvedValue({
    lease_id: 'document-lease', status: 'active', expires_at: '2026-08-27T00:03:00Z',
  })
  const release = vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'document-lease', status: 'released' })
  const incident = vi.spyOn(localApi, 'recordDeviceIncident').mockResolvedValue({} as never)
  return {
    getUserMedia, register, release, incident, stop, enumerateDevices,
    finishEnumeration: () => resolveEnumeration(),
    triggerDeviceChange: () => mediaDeviceEvents.dispatchEvent(new Event('devicechange')),
  }
}
