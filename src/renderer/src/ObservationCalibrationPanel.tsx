import { useEffect, useRef, useState } from 'react'
import { Camera, Check, ScanLine, Square } from 'lucide-react'
import { ApiError, localApi } from './api'
import { mediaDeviceFailure, resolveBoundVideoDevice } from './mediaDevices'
import type { DeviceBinding, ObservationCalibrationRun, ObservationCalibrationSummary } from './types'

export function ObservationCalibrationPanel({
  childId,
  cameraBinding,
  disabled,
}: {
  childId: string
  cameraBinding?: DeviceBinding
  disabled: boolean
}): React.JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const runRef = useRef<ObservationCalibrationRun | null>(null)
  const generationRef = useRef(0)
  const [summary, setSummary] = useState<ObservationCalibrationSummary | null>(null)
  const [opening, setOpening] = useState(false)
  const [checking, setChecking] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const release = (): void => {
    generationRef.current += 1
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    if (videoRef.current) videoRef.current.srcObject = null
    setPreviewing(false)
  }

  const refresh = async (): Promise<void> => {
    setSummary(await localApi.observationCalibrationSummary(childId))
  }

  useEffect(() => {
    let active = true
    void localApi.observationCalibrationSummary(childId)
      .then((result) => { if (active) setSummary(result) })
      .catch(() => { if (active) setMessage('暂时无法读取座位取景记录。') })
    return () => {
      active = false
      const run = runRef.current
      runRef.current = null
      release()
      if (run?.status === 'active') {
        void localApi.cancelObservationCalibration(run.run_id, 'PARENT_VIEW_CLOSED').catch(() => undefined)
      }
    }
  }, [childId])

  useEffect(() => {
    const stopForDeletion = (): void => {
      const run = runRef.current
      runRef.current = null
      release()
      if (run?.status === 'active') {
        void localApi.cancelObservationCalibration(run.run_id, 'DATA_DELETION').catch(() => undefined)
      }
      setMessage('取景已因数据删除操作停止，摄像头已释放。')
    }
    window.addEventListener('hsc-parent-observation-stop', stopForDeletion)
    return () => window.removeEventListener('hsc-parent-observation-stop', stopForDeletion)
  }, [])

  const cancelRun = async (failureCode = 'PARENT_CANCELLED'): Promise<void> => {
    const run = runRef.current ?? summary?.active ?? null
    runRef.current = null
    release()
    if (!run) return
    try {
      await localApi.cancelObservationCalibration(run.run_id, failureCode)
      await refresh()
      setMessage(failureCode === 'PARENT_CANCELLED' ? '已结束取景，摄像头已释放。' : '取景已中断，摄像头已释放。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '取景状态未能保存，请重新进入家长中心检查。')
    }
  }

  const open = async (): Promise<void> => {
    if (!cameraBinding || disabled) return
    const generation = ++generationRef.current
    const isCurrent = (): boolean => generationRef.current === generation
    setOpening(true); setMessage(null)
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('MEDIA_API_UNAVAILABLE')
      const run = await localApi.startObservationCalibration(childId)
      if (!isCurrent()) {
        await localApi.cancelObservationCalibration(run.run_id, 'OPEN_SUPERSEDED').catch(() => undefined)
        return
      }
      runRef.current = run
      const selected = await resolveBoundVideoDevice(
        await navigator.mediaDevices.enumerateDevices(), cameraBinding.system_id_hash, cameraBinding.label, 'camera'
      )
      if (!isCurrent()) {
        runRef.current = null
        await localApi.cancelObservationCalibration(run.run_id, 'OPEN_SUPERSEDED').catch(() => undefined)
        return
      }
      if (!selected) throw new Error('BOUND_DEVICE_NOT_FOUND')
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          deviceId: { exact: selected.device.deviceId }, width: { ideal: 640 }, height: { ideal: 360 },
          frameRate: { ideal: 10, max: 15 },
        },
      })
      if (!isCurrent()) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      streamRef.current = stream
      stream.getVideoTracks()[0]?.addEventListener('ended', () => {
        if (streamRef.current === stream) void cancelRun('DEVICE_DISCONNECTED')
      }, { once: true })
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        await videoRef.current.play()
      }
      if (!isCurrent()) return
      setPreviewing(true)
      setMessage('本机取景中，尚未保存检查结果。')
    } catch (cause) {
      const code = cause instanceof Error ? cause.message : 'OPEN_FAILED'
      const run = runRef.current
      runRef.current = null
      release()
      if (run) await localApi.cancelObservationCalibration(run.run_id, code.slice(0, 80)).catch(() => undefined)
      const failure = mediaDeviceFailure(cause, '观察摄像头')
      setMessage(code === 'BOUND_DEVICE_NOT_FOUND' ? `找不到已绑定的观察摄像头“${cameraBinding.label}”。` : failure.message)
      await refresh().catch(() => undefined)
    } finally {
      setOpening(false)
    }
  }

  const checkFrame = async (): Promise<void> => {
    const video = videoRef.current
    const run = runRef.current
    if (!video || !run || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
      setMessage('画面还没有准备好，请稍后再检查。')
      return
    }
    setChecking(true); setMessage(null)
    try {
      const canvas = document.createElement('canvas')
      canvas.width = 320
      canvas.height = Math.max(180, Math.round(320 * video.videoHeight / Math.max(video.videoWidth, 1)))
      canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
      const frameBase64 = canvas.toDataURL('image/jpeg', 0.72).split(',', 2)[1]
      const completed = await localApi.finishObservationCalibration(run.run_id, frameBase64)
      runRef.current = null
      release()
      await refresh()
      setMessage(calibrationMessage(completed))
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '当前画面检查失败，预览仍在本机继续。')
    } finally {
      setChecking(false)
    }
  }

  const latest = summary?.latest ?? null
  const displayedMessage = message ?? (
    latest?.status === 'failed' ? calibrationMessage(latest)
      : latest?.status === 'interrupted' ? '上次取景被应用重启中断，摄像头已由系统释放。'
        : latest && !latest.current_binding ? '当前记录属于之前绑定的摄像头，请重新检查座位取景。'
          : null
  )
  return (
    <div className="observation-calibration">
      <div className="calibration-heading">
        <div><span>座位取景校准</span><h3>{latestCalibrationTitle(latest)}</h3></div>
        {latest?.status === 'passed' && latest.current_binding ? <b className="is-passed"><Check size={15} />当前有效</b> : <b>待校准</b>}
      </div>
      <div className={`calibration-preview ${previewing ? 'is-active' : ''}`}>
        <video ref={videoRef} muted playsInline aria-label="观察摄像头座位取景" />
        {!previewing && <div><Camera size={24} /><span>摄像头未读取</span></div>}
      </div>
      <div className="calibration-detail">
        <div className="calibration-actions">
          {previewing ? (
            <><button className="secondary-button" disabled={checking} onClick={() => void cancelRun()}><Square size={16} />结束取景</button><button className="primary-button" disabled={checking} onClick={() => void checkFrame()}><ScanLine size={16} />{checking ? '正在检查' : '检查当前取景'}</button></>
          ) : summary?.active ? (
            <button className="danger-button" onClick={() => void cancelRun('PARENT_RECOVERED')}><Square size={16} />结束遗留校准</button>
          ) : (
            <button className="secondary-button" disabled={disabled || opening || !cameraBinding} onClick={() => void open()}><Camera size={16} />{opening ? '正在打开' : '打开座位取景'}</button>
          )}
        </div>
        {latest?.result.metrics && latest.status !== 'cancelled' && latest.status !== 'interrupted' && (
          <div className="calibration-metrics">
            <span>{latest.result.metrics.width}×{latest.result.metrics.height}</span>
            <span>亮度 {latest.result.metrics.brightness}</span>
            <span>清晰度 {latest.result.metrics.sharpness}</span>
            <span>{latest.result.person_visible ? '检测到正面人物' : '未检测到正面人物'}</span>
          </div>
        )}
        {displayedMessage && <p className="operation-message" role="status">{displayedMessage}</p>}
      </div>
    </div>
  )
}

function latestCalibrationTitle(latest: ObservationCalibrationRun | null): string {
  if (!latest) return '尚无当前座位证据'
  if (!latest.current_binding) return '摄像头已更换，请重新校准'
  if (latest.status === 'passed') return '取景和人物位置已通过'
  if (latest.status === 'active') return '校准正在进行'
  if (latest.status === 'interrupted') return '上次校准被应用重启中断'
  if (latest.status === 'cancelled') return '上次校准未完成'
  return '当前取景未通过'
}

function calibrationMessage(run: ObservationCalibrationRun): string {
  if (run.status === 'passed') return '当前座位取景通过；原始画面未保存，正式学习时会重新确认人物基线。'
  const result = run.result
  if (result.scene_quality?.status && result.scene_quality.status !== 'ready') return result.scene_quality.message
  if (result.person_message) return result.person_message
  return '当前取景未通过，请调整摄像头后重新检查。'
}
