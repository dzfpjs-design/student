import { useEffect, useRef, useState } from 'react'
import { Mic, Trash2 } from 'lucide-react'
import { VoiceInputButton, voiceCalibrationQuality } from './LocalInputs'
import { addLocalVoiceCalibrationSample, deleteLocalVoiceProfile, hydrateLocalVoiceProfileFromService, isLocalVoiceProfileReady, localVoiceProfile } from './voiceProfile'
import { localApi, type VoiceCalibrationMicrophone } from './api'
import { mediaDeviceInfoHash, resolveCompanionAudioDevice } from './mediaDevices'
import { clearPendingVoiceCalibrationDelete, clearPendingVoiceCalibrationSave, loadPendingVoiceCalibrationDelete, loadPendingVoiceCalibrationSave, preparePendingVoiceCalibrationDelete, preparePendingVoiceCalibrationSave, type PendingVoiceCalibrationSave } from './voiceCalibrationPending'

const calibrationPrompts = ['安静靠近麦克风说一句', '正常坐姿说一句', '有轻微日常环境声时说一句']

function hydrateServiceProfile(childId: string, saved: { profile: Record<string, unknown> | null; consented_at?: string; updated_at?: string }): ReturnType<typeof localVoiceProfile> {
  return saved.profile ? hydrateLocalVoiceProfileFromService(childId, saved.profile, { consentedAt: saved.consented_at, updatedAt: saved.updated_at }) : null
}

export function VoiceCalibrationPanel({ childId, sessionId, parentManaged = false, sessionScoped = false, onBoundAudioDeviceIdChange, onMicrophoneBindingReadyChange }: {
  childId: string
  sessionId?: string
  parentManaged?: boolean
  /** Child learning surfaces can only recover the calibration for their own session. */
  sessionScoped?: boolean
  /** Exact device id for a live explicit binding; undefined means Windows default. */
  onBoundAudioDeviceIdChange?: (deviceId: string | undefined) => void
  /** True after either an explicit binding or Windows-default fallback is ready. */
  onMicrophoneBindingReadyChange?: (ready: boolean) => void
}): React.JSX.Element {
  const serviceManaged = parentManaged || sessionScoped
  const initial = serviceManaged ? null : localVoiceProfile(childId)
  const [consent, setConsent] = useState(false)
  const [profile, setProfile] = useState(initial)
  const [vocabulary, setVocabulary] = useState('')
  const [vocabularyApplied, setVocabularyApplied] = useState(false)
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [deviceId, setDeviceId] = useState('')
  const [pendingSave, setPendingSave] = useState(false)
  const [pendingDelete, setPendingDelete] = useState(false)
  const [deviceResolutionSignal, setDeviceResolutionSignal] = useState(0)
  const childGeneration = useRef(0)
  const deviceSelectionRevision = useRef(0)
  const isCurrentGeneration = (value: number): boolean => childGeneration.current === value
  const selectedMicrophone = async (): Promise<VoiceCalibrationMicrophone> => {
    const device = devices.find((item) => item.deviceId === deviceId)
    if (!device || !device.label.trim()) throw new Error('MICROPHONE_SELECTION_REQUIRED')
    if (devices.filter((item) => item.label.trim() === device.label.trim()).length !== 1) throw new Error('MICROPHONE_LABEL_AMBIGUOUS')
    return {
      system_id_hash: await mediaDeviceInfoHash(device), label: device.label.trim(),
      capabilities: {
        transport: 'browser-media-device', echo_cancellation_requested: true,
        noise_suppression_requested: true, auto_gain_control_requested: true,
      },
    }
  }
  const ready = isLocalVoiceProfileReady(profile) && !pendingSave && !pendingDelete
  const count = profile?.calibrationCount ?? 0
  const [message, setMessage] = useState(ready ? '已保存三段本机环境特征；不保存原始录音。' : '可选：三段短时校准能更稳妥地区分麦克风或环境变化。')
  const [deviceMessage, setDeviceMessage] = useState('')
  const words = (): string[] => vocabulary.split(/[、,，\s]+/).filter(Boolean)
  const loadSummary = (): ReturnType<typeof localApi.voiceCalibrationSummary> => {
    if (parentManaged) return localApi.voiceCalibrationSummary(childId)
    if (sessionScoped && sessionId) return localApi.sessionVoiceCalibrationSummary(sessionId)
    return Promise.reject(new Error('VOICE_CALIBRATION_SESSION_REQUIRED'))
  }
  useEffect(() => {
    childGeneration.current += 1
    deviceSelectionRevision.current += 1
    setProfile(serviceManaged ? null : localVoiceProfile(childId))
    setVocabulary('')
    setVocabularyApplied(false)
    setDeviceId('')
    setPendingSave(false)
    setPendingDelete(false)
    setConsent(false)
    setDeviceMessage('')
    onBoundAudioDeviceIdChange?.(undefined)
    onMicrophoneBindingReadyChange?.(false)
  }, [childId, parentManaged, sessionId, sessionScoped, serviceManaged, onBoundAudioDeviceIdChange, onMicrophoneBindingReadyChange])
  const savePending = async (pending: PendingVoiceCalibrationSave, operationGeneration = childGeneration.current): Promise<void> => {
    try {
      const saved = await localApi.saveVoiceCalibration(childId, pending.profile, pending.vocabulary, pending.microphone, pending.clientActionId)
      if (!isCurrentGeneration(operationGeneration)) return
      const hydrated = hydrateServiceProfile(childId, saved)
      if (!hydrated) throw new Error('VOICE_CALIBRATION_SERVICE_PROFILE_INVALID')
      clearPendingVoiceCalibrationSave(childId)
      setProfile(hydrated); setPendingSave(false)
      setVocabularyApplied(Boolean(saved.vocabulary_applied))
      setMessage('校准完成：三段特征已保存在本机服务；原始录音已从内存释放。')
    } catch {
      if (!isCurrentGeneration(operationGeneration)) return
      setProfile(pending.profile); setPendingSave(true)
      setMessage('三段采样已完成，待保存到本机服务；可重试，不需要重新录音。')
    }
  }
  const persist = async (candidate: NonNullable<typeof profile>): Promise<void> => {
    const operationGeneration = childGeneration.current
    const microphone = await selectedMicrophone()
    if (!isCurrentGeneration(operationGeneration)) return
    const pending = await preparePendingVoiceCalibrationSave(childId, candidate, words(), microphone)
    if (!isCurrentGeneration(operationGeneration)) return
    await savePending(pending, operationGeneration)
  }
  useEffect(() => {
    if (!serviceManaged) return
    const operationGeneration = childGeneration.current
    let cancelled = false
    const isCurrent = (): boolean => !cancelled && isCurrentGeneration(operationGeneration)
    if (sessionScoped && !sessionId) {
      setMessage('当前学习会话无效，不能读取本机校准特征。')
      return
    }
    if (sessionScoped) setMessage('正在恢复由家长保存的本机声音特征…')
    void loadSummary().then((saved) => {
      if (!isCurrent()) return
      if (saved.child_id !== childId) {
        setProfile(null); setMessage('当前学习会话的校准记录不匹配；免按键对话不会使用它。')
        return
      }
      if (sessionScoped) {
        if (!saved.profile) {
          deleteLocalVoiceProfile(childId); setProfile(null); setVocabulary('')
          setMessage('尚未由家长完成三段本机声音校准；免按键对话会继续使用通用本机保护。')
          return
        }
        const hydrated = hydrateServiceProfile(childId, saved)
        if (!hydrated) { setProfile(null); setMessage('本机服务中的校准特征格式无效；免按键对话不会使用它。'); return }
        setProfile(hydrated); setVocabulary((saved.vocabulary ?? []).join('、')); setVocabularyApplied(Boolean(saved.vocabulary_applied))
        setMessage('已恢复家长保存的三段本机环境特征；不保存原始录音。')
        return
      }
      const deleting = loadPendingVoiceCalibrationDelete(childId)
      if (saved.profile) {
        const hydrated = hydrateServiceProfile(childId, saved)
        if (!hydrated) { setMessage('本机服务中的校准特征格式无效；免按键对话不会使用它。'); return }
        clearPendingVoiceCalibrationSave(childId)
        setProfile(hydrated); setPendingSave(false); setPendingDelete(Boolean(deleting)); setVocabulary((saved.vocabulary ?? []).join('、')); setVocabularyApplied(Boolean(saved.vocabulary_applied))
        setMessage(deleting ? '删除请求尚未得到本机服务确认；请重试，校准特征仍保留。' : '已保存三段本机环境特征；不保存原始录音。')
        return
      }
      if (deleting) {
        clearPendingVoiceCalibrationDelete(childId); clearPendingVoiceCalibrationSave(childId); deleteLocalVoiceProfile(childId)
        setProfile(null); setPendingSave(false); setPendingDelete(false); setVocabulary('')
        setMessage('本机声音特征已删除。')
        return
      }
      const pending = loadPendingVoiceCalibrationSave(childId)
      if (pending) {
        setProfile(pending.profile); setVocabulary(pending.vocabulary.join('、')); setPendingSave(true)
        setMessage('三段采样待保存到本机服务；可重试，不需要重新录音。')
        return
      }
      deleteLocalVoiceProfile(childId); setProfile(null); setPendingSave(false); setVocabulary(''); setVocabularyApplied(false)
    }).catch(() => { if (isCurrent()) setMessage('本机校准记录暂时无法读取；不会上传或保存原始录音。') })
    return () => { cancelled = true }
  }, [childId, parentManaged, sessionId, sessionScoped, serviceManaged])
  useEffect(() => {
    // Enumeration is permission-neutral; recording only begins after the
    // parent chooses a device and presses the visible button.
    const operationGeneration = childGeneration.current
    const selectionRevision = deviceSelectionRevision.current
    let cancelled = false
    const isCurrent = (): boolean => !cancelled && isCurrentGeneration(operationGeneration)
    void navigator.mediaDevices?.enumerateDevices().then(async (items) => {
      if (!isCurrent()) return
      const audio = items.filter((item) => item.kind === 'audioinput')
      if (!isCurrent()) return
      setDevices(audio)
      const [saved] = await Promise.all([
        serviceManaged ? loadSummary().catch(() => null) : Promise.resolve(null),
      ])
      if (!isCurrent()) return
      // A parent's explicit selection wins over an older asynchronous binding
      // lookup. Applying the stale result would cancel an in-flight recording.
      if (parentManaged && selectionRevision !== deviceSelectionRevision.current) return
      if (saved && saved.child_id !== childId) {
        onBoundAudioDeviceIdChange?.(undefined)
        onMicrophoneBindingReadyChange?.(false)
        setDeviceMessage('当前学习会话的麦克风绑定不匹配；不会使用它。')
        return
      }
      // A child session never retries a parent-side pending write.  Its only
      // authority is the durable summary bound to this session.
      const pending = sessionScoped || saved?.profile ? null : loadPendingVoiceCalibrationSave(childId)
      const binding = saved?.microphone ?? pending?.microphone
      const restored = await resolveCompanionAudioDevice(items, binding)
      if (!isCurrent()) return
      if (restored?.method === 'persisted_microphone' && restored.device) {
        setDeviceId(restored.device.deviceId)
        onBoundAudioDeviceIdChange?.(restored.device.deviceId)
        onMicrophoneBindingReadyChange?.(true)
        setDeviceMessage(`已连接家长绑定的麦克风：${restored.device.label || '已选设备'}。`)
      } else if (restored?.method === 'system_default') {
        setDeviceId('')
        onBoundAudioDeviceIdChange?.(undefined)
        onMicrophoneBindingReadyChange?.(true)
        setDeviceMessage(binding
          ? '已绑定麦克风当前不在线，正在使用Windows默认麦克风。'
          : '正在使用Windows默认麦克风。')
      } else {
        onBoundAudioDeviceIdChange?.(undefined)
        onMicrophoneBindingReadyChange?.(false)
        setDeviceMessage('无法准备Windows默认麦克风，请检查系统权限。')
      }
    }).catch(() => {
      if (!isCurrent()) return
      onMicrophoneBindingReadyChange?.(false)
      setDeviceMessage('无法读取麦克风列表；请检查系统权限后重试。')
    })
    return () => { cancelled = true }
  }, [childId, parentManaged, sessionId, sessionScoped, serviceManaged, onBoundAudioDeviceIdChange, onMicrophoneBindingReadyChange, deviceResolutionSignal])
  useEffect(() => {
    const mediaDevices = navigator.mediaDevices
    if (!mediaDevices?.addEventListener) return
    const changed = (): void => {
      onMicrophoneBindingReadyChange?.(false)
      setDeviceResolutionSignal((value) => value + 1)
    }
    mediaDevices.addEventListener('devicechange', changed)
    return () => mediaDevices.removeEventListener('devicechange', changed)
  }, [onMicrophoneBindingReadyChange])
  const reset = async (notice: string): Promise<void> => {
    const operationGeneration = childGeneration.current
    if (!parentManaged) {
      deleteLocalVoiceProfile(childId); clearPendingVoiceCalibrationSave(childId); setProfile(null); setConsent(false); setPendingSave(false); setMessage(notice)
      return
    }
    const pending = preparePendingVoiceCalibrationDelete(childId)
    setPendingDelete(true); setMessage('正在向本机服务删除声音特征…')
    try {
      await localApi.deleteVoiceCalibration(childId, pending.clientActionId)
      if (!isCurrentGeneration(operationGeneration)) return
      clearPendingVoiceCalibrationDelete(childId); clearPendingVoiceCalibrationSave(childId); deleteLocalVoiceProfile(childId)
      setProfile(null); setConsent(false); setPendingSave(false); setPendingDelete(false); setMessage(notice)
    } catch {
      if (!isCurrentGeneration(operationGeneration)) return
      setPendingDelete(true); setMessage('删除尚未得到本机服务确认；特征仍保留，可重试删除。')
    }
  }
  const cancelDeleteWait = async (): Promise<void> => {
    const operationGeneration = childGeneration.current
    try {
      const saved = await localApi.voiceCalibrationSummary(childId)
      if (!isCurrentGeneration(operationGeneration)) return
      if (saved.profile) {
        const hydrated = hydrateServiceProfile(childId, saved)
        if (!hydrated) { setMessage('本机服务中的校准特征格式无效；不能恢复。'); return }
        clearPendingVoiceCalibrationDelete(childId); setProfile(hydrated); setPendingDelete(false); setMessage('删除未执行：已按本机服务当前记录恢复。')
      } else {
        clearPendingVoiceCalibrationDelete(childId); clearPendingVoiceCalibrationSave(childId); deleteLocalVoiceProfile(childId)
        setProfile(null); setPendingDelete(false); setConsent(false); setMessage('本机服务已确认声音特征不存在。')
      }
    } catch {
      if (isCurrentGeneration(operationGeneration)) setMessage('无法确认本机服务当前状态；请保持待删除状态后重试。')
    }
  }
  if (ready) return <div className="voice-calibration compact"><span>{message}</span>{deviceMessage && <span>{deviceMessage}</span>}{serviceManaged && <span>{vocabularyApplied ? '云端识别会结合孩子常用词' : '常用词已记录，当前识别模型尚未启用'}{vocabulary ? `：${vocabulary}` : ''}。</span>}{sessionScoped && <span>校准与麦克风绑定由家长中心管理。</span>}{!sessionScoped && <><button className="text-button" onClick={() => void reset('旧的本机声音特征已删除。请再次同意后重新完成三段校准。')}><Mic size={14} />重新校准</button><button className="text-button" onClick={() => void reset('本机声音特征已删除。')}><Trash2 size={14} />删除声音特征</button></>}</div>
  if (sessionScoped) return <div className="voice-calibration compact"><span>{message}</span>{deviceMessage && <span>{deviceMessage}</span>}<span>校准与麦克风绑定由家长中心管理；不会在孩子页面保存或删除声音特征。</span></div>
  return <div className="voice-calibration">
    <label><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />我同意仅在这台设备保存三段短时声音特征，用于麦克风与说话人提示；不会保存原始录音，也不会上传。</label>
    {pendingDelete ? <span><Trash2 size={13} />删除尚未确认：三段本机特征仍保留，不能继续采样或保存；可重试删除，或取消本次删除等待。</span> : consent && <>
      <label>选择真实麦克风<select aria-label="选择校准麦克风" value={deviceId} onChange={(event) => { deviceSelectionRevision.current += 1; setDeviceId(event.target.value) }}><option value="">请选择</option>{devices.map((device, index) => <option key={device.deviceId} value={device.deviceId} disabled={!device.label.trim()}>{device.label || `麦克风 ${index + 1}（系统尚未提供名称）`}</option>)}</select></label>
      {!pendingSave && <><span><Mic size={13} />第 {Math.min(count + 1, 3)} / 3 段：{calibrationPrompts[Math.min(count, 2)]}。每段只保存不可逆的统计特征。</span>
      <VoiceInputButton sessionId={sessionId} purpose="voice_calibration" onTranscript={() => undefined} skipTranscription audioDeviceId={deviceId || undefined} onCaptured={(wav) => {
        try {
          const quality = voiceCalibrationQuality(wav)
          if (!quality.accepted) { setMessage(quality.message); return }
          const next = addLocalVoiceCalibrationSample(childId, wav)
          if (isLocalVoiceProfileReady(next) && parentManaged) void persist(next)
          else { setProfile(next); setMessage(isLocalVoiceProfileReady(next) ? '校准完成：三段特征仅保存在本机，原始录音已从内存释放。' : `已完成第 ${next.calibrationCount} 段；请按提示再录一段。`) }
        } catch { setMessage('校准需要至少说一句完整的话，请重试。') }
      }} label={count ? '录下一段校准' : '录第一段校准'} /></>}
    </>}
    <span><Mic size={13} />{message}</span>
    {deviceMessage && <span>{deviceMessage}</span>}
    {pendingSave && profile && <button className="secondary-button" onClick={() => { const pending = loadPendingVoiceCalibrationSave(childId); if (pending) void savePending(pending) }}>重试保存</button>}
    {pendingDelete && <><button className="secondary-button" onClick={() => void reset('本机声音特征已删除。')}>重试删除</button><button className="text-button" onClick={() => void cancelDeleteWait()}>取消删除等待</button></>}
  </div>
}
