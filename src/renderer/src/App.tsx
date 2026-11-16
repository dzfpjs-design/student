import type {ReflectionSavedReceipt} from './reflectionCompletionReceipt'
import { useEffect, useRef, useState } from 'react'
import { AlertCircle, LockKeyhole, Mic, RefreshCw, ShieldCheck, Wifi, WifiOff } from 'lucide-react'
import { ApiError, localApi, resetRuntimeCache, createParentDraftRecoveryPort } from './api'
import { DeviceAvailabilityStrip } from './DeviceAvailabilityStrip'
import { CompanionAvatar } from './CompanionAvatar'
import { SessionWorkspace, type VoiceProfileSwitch } from './SessionWorkspace'
import { ParentCenter } from './ParentCenter'
import { ParentRestoreRecovery } from './ParentRestoreRecovery'
import { ParentRotationRecovery } from './ParentRotationRecovery'
import { requestParentDraftNavigation } from './parentDraftNavigation'
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import { MistakeBook } from './MistakeBook'
import type { BootstrapState, ChildProfile, SessionWorkspaceState } from './types'
import { HouseholdStandbyVoiceInput, type HouseholdWakeProfile } from './LocalInputs'
import { readVoiceInputMode } from './voiceInputPreference'
import { acknowledgeProfileSwitch, markProfileSwitchActivated, readProfileSwitchHandoff, resolveProfileSwitchDestination, type ProfileSwitchHandoff } from './profileSwitchHandoff'
import { InstallationSetupWizard } from './InstallationSetupWizard'
import type { InstallationSetupStatus } from './types'
import { childTrialReadiness, type ChildTrialReadiness } from './childTrialReadiness'

type ScreenState =
  | { kind: 'loading' }
  | { kind: 'parent_recovery' }
  | { kind: 'rotation_recovery' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; data: BootstrapState }

type RecoveryDestination =
  | { kind: 'home' | 'parent' | 'other' | 'transition' }
  | { kind: 'workspace'; sessionId: string; childId: string }
  | { kind: 'profile_transition'; turnId: string; clientActionId: string; sourceSessionId: string; sourceChildId: string; targetChildId: string }

function sameProfileTransition(pending: ProfileSwitchHandoff | null, destination: RecoveryDestination): pending is ProfileSwitchHandoff {
  return destination.kind === 'profile_transition' && pending !== null && !pending.activated
    && pending.turnId === destination.turnId && pending.clientActionId === destination.clientActionId && pending.sourceSessionId === destination.sourceSessionId
    && pending.sourceChildId === destination.sourceChildId && pending.targetChildId === destination.targetChildId
}

function setupIncomplete(setup: InstallationSetupStatus, secretReady: boolean): boolean {
  return !setup.complete || (setup.smtp_secret_check_required && !secretReady)
}

function requiresInstallationSetup(setup: InstallationSetupStatus, secretReady: boolean): boolean {
  return setupIncomplete(setup, secretReady) && !(setup.existing_family === true && setup.parent_pin_configured === true)
}

function AppContent(): React.JSX.Element {
  const [screen, setScreen] = useState<ScreenState>({ kind: 'loading' })
  const [startingChildId, setStartingChildId] = useState<string | null>(null)
  const [workspace, setWorkspace] = useState<SessionWorkspaceState | null>(null)
  const [reflectionSavedNotice, setReflectionSavedNotice] = useState(false)
  const [parentOpen, setParentOpen] = useState(false)
  const [parentSetupSection, setParentSetupSection] = useState<'voice_setup' | undefined>()
  const [trialReadiness, setTrialReadiness] = useState<ChildTrialReadiness | null>(null)
  const trialReadinessRef = useRef<ChildTrialReadiness | null>(null)
  const [parentDraftHost]=useState(()=>new ParentTextbookDraftHost(createParentDraftRecoveryPort))
  useEffect(()=>parentDraftHost.attach(),[parentDraftHost])
  const [parentReturnError, setParentReturnError] = useState<string | null>(null)
  const [returningFromParent, setReturningFromParent] = useState(false)
  const parentReturn = useRef<{ sessionId: string; childId: string } | null>(null)
  const parentRequested = useRef(false)
  const parentEntryHandler = useRef<() => void>(() => {})
  const closingParent = useRef(false)
  const [mistakeChild, setMistakeChild] = useState<ChildProfile | null>(null)
  const [online, setOnline] = useState(navigator.onLine)
  const [wakeProfiles, setWakeProfiles] = useState<HouseholdWakeProfile[]>([])
  const [initialHouseholdEntry,setInitialHouseholdEntry]=useState<import('./types').HouseholdNativeEntry|undefined>()
  const [initialVoiceCommand, setInitialVoiceCommand] = useState<string | null>(null)
  const [initialWakeOnly, setInitialWakeOnly] = useState(false)
  const [installationSetup, setInstallationSetup] = useState<InstallationSetupStatus | null>(null)
  const [deliverySetupPending, setDeliverySetupPending] = useState(false)
  const [installationSmtpSecretConfigured, setInstallationSmtpSecretConfigured] = useState(false)
  const initialResumeChecked = useRef(false)
  const automaticRuntimeRecoveryAttempted = useRef(false)
  const handoffInFlight = useRef<symbol | null>(null)
  const viewGeneration = useRef(0)
  const mounted = useRef(true)
  const recoveryDestination = useRef<RecoveryDestination>({ kind: 'home' })
  const navigationVersion = useRef(0)
  const privacyHeld = useRef(false)
  const automaticRecoveryActive = useRef(false)
  const automaticRecoveryGeneration = useRef(0)
  const automaticRecoveryTimer = useRef<number | null>(null)
  const automaticRecoveryRun = useRef<() => Promise<void>>(async () => {})
  const [workspaceRecoveryHeld, setWorkspaceRecoveryHeld] = useState(false)
  const [runtimeRecoveryMessage, setRuntimeRecoveryMessage] = useState<string | null>(null)

  const navigate = (destination: RecoveryDestination): void => {
    recoveryDestination.current = destination
    setReflectionSavedNotice(false)
    navigationVersion.current += 1
    automaticRecoveryActive.current = false
    automaticRecoveryGeneration.current += 1
    if (automaticRecoveryTimer.current !== null) window.clearTimeout(automaticRecoveryTimer.current)
    automaticRecoveryTimer.current = null
    setWorkspaceRecoveryHeld(false)
    setRuntimeRecoveryMessage(null)
  }

  useEffect(() => window.desktop?.onOpenParentCenter?.(() => parentEntryHandler.current()), [])

  const readTrialReadiness = async (data: BootstrapState): Promise<ChildTrialReadiness> => {
    const realtime = await window.desktop?.getQwenRealtimeSettings?.().catch(() => null) ?? null
    return childTrialReadiness(data, realtime)
  }
  const publishTrialReadiness = (value: ChildTrialReadiness | null): void => {
    trialReadinessRef.current = value
    setTrialReadiness(value)
  }

  const load = async (): Promise<void> => {
    const generation = ++viewGeneration.current
    const isCurrent = (): boolean => mounted.current && generation === viewGeneration.current
    publishTrialReadiness(null)
    setScreen({ kind: 'loading' })
    const handoff = Symbol('profile-load')
    handoffInFlight.current = handoff
    try {
      const data = await localApi.bootstrap()
      const setupResult = await localApi.installationSetupStatus().catch(() => null)
      const setup = setupResult?.schema === 'hsc.installation-setup.v1' ? setupResult : null
      if (!setup && window.desktop?.getReportDeliverySecretStatus) {
        throw new Error('安装初始化状态无法核对，孩子端保持锁定。请重启本地服务后重试。')
      }
      const secret = setup?.smtp_secret_check_required
        ? await window.desktop?.getReportDeliverySecretStatus?.().catch(() => null) ?? null
        : null
      const readiness = await readTrialReadiness(data)
      const pending = readProfileSwitchHandoff()
      if (!isCurrent()) return
      window.dispatchEvent(new Event('hsc-rotation-pending-changed'))
      publishTrialReadiness(readiness)
      if (!readiness.allowed) setWorkspace(null)
      if (readiness.allowed && !parentRequested.current && recoveryDestination.current.kind !== 'parent' && pending && !pending.activated) {
        const restored = await resolveProfileSwitchDestination(pending)
        if (!isCurrent()) return
        markProfileSwitchActivated(restored.handoff)
        initialResumeChecked.current = true
        setInitialVoiceCommand(null)
    setInitialHouseholdEntry(undefined)
        setInitialWakeOnly(false)
        recoveryDestination.current = { kind: 'workspace', sessionId: restored.workspace.session.session_id, childId: restored.workspace.session.child_id }
        setWorkspace(restored.workspace)
        void acknowledgeProfileSwitch(restored.handoff)
      } else if (readiness.allowed && pending) void acknowledgeProfileSwitch(pending)
      setInstallationSmtpSecretConfigured(Boolean(secret?.smtpPasswordConfigured))
      setDeliverySetupPending(Boolean(setup && setupIncomplete(setup, Boolean(secret?.smtpPasswordConfigured))))
      setInstallationSetup(setup && requiresInstallationSetup(setup, Boolean(secret?.smtpPasswordConfigured)) ? setup : null)
      setScreen({ kind: 'ready', data })
    } catch (error) {
      if (!isCurrent()) return
      try {
        const health = await localApi.parentRestoreRecoveryHealth()
        if (!isCurrent()) return
        if (health.status === 'rotation_auth_only' && health.normal_business_ready === false) {
          privacyHeld.current = true
          setWorkspace(null); setParentOpen(false); setMistakeChild(null)
          setScreen({ kind: 'rotation_recovery' })
          return
        }
        if (health.status === 'recovery_only' && health.normal_business_ready === false) {
          privacyHeld.current = true
          setWorkspace(null);setParentOpen(false);setMistakeChild(null)
          setScreen({ kind: 'parent_recovery' })
          return
        }
      } catch { /* Unknown service state remains the original closed error surface. */ }
      if (!isCurrent()) return
      setScreen({
        kind: 'error',
        message: error instanceof ApiError ? error.message : '应用初始化失败，请重试。'
      })
    } finally { if (handoffInFlight.current === handoff) handoffInFlight.current = null }
  }

  automaticRecoveryRun.current = async (): Promise<void> => {
    const attempt = ++automaticRecoveryGeneration.current
    const generation = viewGeneration.current, navigation = navigationVersion.current
    const destination = recoveryDestination.current
    const current = (): boolean => mounted.current && automaticRecoveryActive.current
      && attempt === automaticRecoveryGeneration.current && generation === viewGeneration.current
      && navigation === navigationVersion.current
    try {
      const data = await localApi.bootstrap()
      if (!current()) return
      const setupResult = await localApi.installationSetupStatus().catch(() => null)
      if (!current()) return
      const setup = setupResult?.schema === 'hsc.installation-setup.v1' ? setupResult : null
      if (!setup && window.desktop?.getReportDeliverySecretStatus) throw new Error('RECOVERY_SETUP_UNCONFIRMED')
      const secret = setup?.smtp_secret_check_required
        ? await window.desktop?.getReportDeliverySecretStatus?.().catch(() => null) ?? null : null
      if (!current()) return
      if (setup && requiresInstallationSetup(setup, Boolean(secret?.smtpPasswordConfigured))) throw new Error('RECOVERY_SETUP_INCOMPLETE')
      setDeliverySetupPending(Boolean(setup && setupIncomplete(setup, Boolean(secret?.smtpPasswordConfigured))))
      const readiness = await readTrialReadiness(data)
      if (!current()) return
      publishTrialReadiness(readiness)
      if (!readiness.allowed) {
        setWorkspace(null)
        setScreen({ kind: 'ready', data })
        automaticRecoveryActive.current = false
        setRuntimeRecoveryMessage(null)
        return
      }
      if (destination.kind === 'workspace' && !privacyHeld.current) {
        const restored = await localApi.sessionWorkspace(destination.sessionId)
        if (!current() || privacyHeld.current) return
        if (restored.session.session_id !== destination.sessionId || restored.session.child_id !== destination.childId
          || !data.children.some(child => child.child_id === destination.childId)) throw new Error('RECOVERY_WORKSPACE_SCOPE_CHANGED')
        setWorkspace(restored)
        setWorkspaceRecoveryHeld(false)
      } else if (destination.kind === 'profile_transition' && !privacyHeld.current) {
        const pending = readProfileSwitchHandoff()
        if (!sameProfileTransition(pending, destination)) throw new Error('RECOVERY_PROFILE_TRANSITION_CHANGED')
        const restored = await resolveProfileSwitchDestination(pending)
        if (!current() || privacyHeld.current) return
        const latest = readProfileSwitchHandoff()
        if (!sameProfileTransition(latest, destination)
          || latest.receipt?.output.target_session_id !== restored.handoff.receipt?.output.target_session_id
          || restored.workspace.session.child_id !== destination.targetChildId
          || !data.children.some(child => child.child_id === destination.targetChildId)) throw new Error('RECOVERY_PROFILE_TRANSITION_CHANGED')
        markProfileSwitchActivated(restored.handoff)
        void acknowledgeProfileSwitch(restored.handoff)
        recoveryDestination.current = { kind: 'workspace', sessionId: restored.workspace.session.session_id, childId: restored.workspace.session.child_id }
        setInitialVoiceCommand(null)
    setInitialHouseholdEntry(undefined)
        setInitialWakeOnly(false)
        setWorkspace(restored.workspace)
        setWorkspaceRecoveryHeld(false)
      }
      // No initial-session selection: only an exact, still-intended profile
      // transition may use its original durable/idempotent recovery helper.
      // Parent/home/paused surfaces stay mounted, retaining their local stop state.
      if (destination.kind === 'transition') recoveryDestination.current = { kind: 'home' }
      setScreen({ kind: 'ready', data })
      automaticRecoveryActive.current = false
      setRuntimeRecoveryMessage(null)
    } catch {
      if (current()) setRuntimeRecoveryMessage('本地服务正在恢复，原操作不会自动重做；当前页面和暂停状态保持不变。')
    } finally {
      if (mounted.current && automaticRecoveryActive.current && attempt === automaticRecoveryGeneration.current) {
        automaticRecoveryTimer.current = window.setTimeout(() => { automaticRecoveryTimer.current = null; void automaticRecoveryRun.current() }, 2_000)
      }
    }
  }

  const recover = async (): Promise<void> => {
    setScreen({ kind: 'loading' })
    try {
      await localApi.restartRuntime()
      await load()
    } catch (error) {
      setScreen({
        kind: 'error',
        message: error instanceof ApiError ? error.message : error instanceof Error ? error.message : '本地学习服务重启失败，请关闭应用后重试。'
      })
    }
  }

  useEffect(() => {
    mounted.current = true
    void load()
    const retry = (): void => {
      try {
        const pending = readProfileSwitchHandoff()
        // An activated receipt only acknowledges the same result; no view change.
        if (pending?.activated) { void acknowledgeProfileSwitch(pending); return }
        if (handoffInFlight.current || automaticRecoveryActive.current) return
        if (navigationVersion.current === 0) { if (pending) void load(); return }
        if (!privacyHeld.current && sameProfileTransition(pending, recoveryDestination.current)) {
          initialResumeChecked.current = true
          ++viewGeneration.current
          automaticRecoveryActive.current = true
          void automaticRecoveryRun.current()
        }
      } catch { /* Keep invalid or unavailable storage intact for recovery. */ }
    }
    const timer = window.setInterval(retry, 5_000)
    window.addEventListener('online', retry)
    return () => {
      mounted.current = false; ++viewGeneration.current; window.clearInterval(timer); window.removeEventListener('online', retry)
      automaticRecoveryActive.current = false
      automaticRecoveryGeneration.current += 1
      if (automaticRecoveryTimer.current !== null) window.clearTimeout(automaticRecoveryTimer.current)
    }
  }, [])

  useEffect(() => {
    if (screen.kind !== 'error' || !window.desktop?.getRuntimeConfig) return
    // Handoffs have their own bounded retry loop; service-ready polling must
    // not turn a rejected switch into an immediate retry/render loop.
    try { if (readProfileSwitchHandoff()) return } catch { return }
    let cancelled = false
    let timer: number | null = null
    const retry = (): void => {
      timer = window.setTimeout(() => { void inspect() }, 2_000)
    }
    const inspect = async (): Promise<void> => {
      try {
        const runtime = await window.desktop!.getRuntimeConfig()
        if (cancelled) return
        if (runtime.status === 'ready') {
          resetRuntimeCache()
          automaticRuntimeRecoveryAttempted.current = false
          await load()
          return
        }
        if (runtime.status === 'error' && !runtime.nextRetryAt && window.desktop?.restartLocalService && !automaticRuntimeRecoveryAttempted.current) {
          automaticRuntimeRecoveryAttempted.current = true
          const recovered = await localApi.restartRuntime().then(() => true).catch(() => false)
          resetRuntimeCache()
          if (recovered && !cancelled) {
            await load()
            return
          }
        }
      } catch {
        // The main process may itself still be restoring its loopback service.
      }
      if (!cancelled) retry()
    }
    void inspect()
    return () => {
      cancelled = true
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [screen.kind])

  useEffect(() => {
    const update = (): void => setOnline(navigator.onLine)
    const privacyPause = (): void => {
      privacyHeld.current = true
      navigationVersion.current += 1
      automaticRecoveryActive.current = false
      automaticRecoveryGeneration.current += 1
      if (automaticRecoveryTimer.current !== null) window.clearTimeout(automaticRecoveryTimer.current)
      automaticRecoveryTimer.current = null
      // If the old workspace was already unmounted, keep its recovery placeholder
      // instead of remounting input after a newer user stop.
      setRuntimeRecoveryMessage('监听保持暂停，原学习记录已保留。')
    }
    const runtimeRestarted = (event: Event): void => {
      const detail = (event as CustomEvent<{ reason?: string; error?: string; notice?: string }>).detail
      if (detail?.reason === 'automatic_identity_change') {
        ++viewGeneration.current
        if (automaticRecoveryTimer.current !== null) window.clearTimeout(automaticRecoveryTimer.current)
        automaticRecoveryTimer.current = null
        initialResumeChecked.current = true
        automaticRecoveryActive.current = true
        if (recoveryDestination.current.kind === 'workspace' && !privacyHeld.current) setWorkspaceRecoveryHeld(true)
        setRuntimeRecoveryMessage('正在恢复本地服务，原操作不会自动重做。')
        void automaticRecoveryRun.current()
        return
      }
      automaticRecoveryActive.current = false
      automaticRecoveryGeneration.current += 1
      if (automaticRecoveryTimer.current !== null) window.clearTimeout(automaticRecoveryTimer.current)
      automaticRecoveryTimer.current = null
      setWorkspaceRecoveryHeld(false)
      setRuntimeRecoveryMessage(detail?.notice ?? null)
      recoveryDestination.current = { kind: 'home' }
      parentReturn.current = null
      parentRequested.current = false
      closingParent.current = false
      setReturningFromParent(false)
      setParentReturnError(null)
      setParentOpen(false)
      setMistakeChild(null)
      setWorkspace(null)
      initialResumeChecked.current = false
      const error = (event as CustomEvent<{ error?: string }>).detail?.error
      if (error) {
        setScreen({ kind: 'error', message: error })
        return
      }
      void load()
    }
    window.addEventListener('online', update)
    window.addEventListener('offline', update)
    window.addEventListener('hsc-privacy-pause', privacyPause)
    window.addEventListener('hsc-runtime-restarted', runtimeRestarted)
    return () => {
      window.removeEventListener('online', update)
      window.removeEventListener('offline', update)
      window.removeEventListener('hsc-privacy-pause', privacyPause)
      window.removeEventListener('hsc-runtime-restarted', runtimeRestarted)
    }
  }, [])

  const start = async (child: ChildProfile, voiceCommand?: string, wakeOnly = false,householdEntry?:import('./types').HouseholdNativeEntry): Promise<void> => {
    if (!trialReadinessRef.current?.allowed) return
    navigate({ kind: 'transition' })
    privacyHeld.current = false
    const generation = ++viewGeneration.current
    const isCurrent = (): boolean => mounted.current && generation === viewGeneration.current
    setStartingChildId(child.child_id)
    setInitialHouseholdEntry(householdEntry)
    setInitialVoiceCommand(voiceCommand?.trim() || null)
    setInitialWakeOnly(wakeOnly)
    try {
      const pending = readProfileSwitchHandoff()
      if (pending && !pending.activated) { await load(); return }
      const session = screen.kind === 'ready' && screen.data.active_session?.child_id === child.child_id
        ? screen.data.active_session
        : await localApi.startSession(child.child_id)
      if (!isCurrent()) return
      const restored = await localApi.sessionWorkspace(session.session_id)
      if (isCurrent()) {
        if (restored.session.session_id !== session.session_id || restored.session.child_id !== child.child_id) throw new Error('WORKSPACE_SCOPE_CHANGED')
        recoveryDestination.current = { kind: 'workspace', sessionId: session.session_id, childId: child.child_id }
        setWorkspace(restored)
      }
    } catch (error) {
      if (!isCurrent()) return
      setScreen({
        kind: 'error',
        message: error instanceof ApiError ? error.message : '今天的学习会话未能创建。'
      })
    } finally {
      if (isCurrent()) setStartingChildId(null)
    }
  }

  useEffect(() => {
    if (screen.kind !== 'ready') { setWakeProfiles([]); return }
    let current = true
    void Promise.all(screen.data.children.map(async (child): Promise<HouseholdWakeProfile> => {
      const saved = await window.desktop?.getWakeWordSettings?.(child.child_id).catch(() => null) ?? null
      return {
        childId: child.child_id,
        wakeWord: saved?.enabled ? saved.wakeWord : child.nickname,
        configured: Boolean(saved?.enabled),
        entryPhrase: child.grade,
        handsFree: !screen.data.privacy_paused && !screen.data.recovery?.sensors_forced_paused
          && readVoiceInputMode(child.child_id) === 'hands-free',
      }
    })).then((profiles) => { if (current) setWakeProfiles(profiles) })
    return () => { current = false }
  }, [screen])

  const wakeGeneration = viewGeneration.current
  const enterFromWake = async (profile: HouseholdWakeProfile, command: string,entry?:import('./types').HouseholdNativeEntry): Promise<void> => {
    if (!mounted.current || screen.kind !== 'ready' || wakeGeneration !== viewGeneration.current
      || privacyHeld.current || screen.data.privacy_paused || screen.data.recovery?.sensors_forced_paused) return
    const child = screen.data.children.find((item) => item.child_id === profile.childId)
    if (!child) return
    await start(child, command, !command.trim(),command.trim()?entry:undefined)
  }

  const openParent = (initialSection?: 'voice_setup'): void => {
    setParentSetupSection(initialSection)
    navigate({ kind: 'parent' })
    if (screen.kind !== 'ready') {
      parentRequested.current = true
      if (screen.kind === 'error') void load()
      return
    }
    parentRequested.current = false
    ++viewGeneration.current
    closingParent.current = false
    setReturningFromParent(false)
    setParentReturnError(null)
    if (workspace) parentReturn.current = { sessionId: workspace.session.session_id, childId: workspace.session.child_id }
    else if (!parentOpen) parentReturn.current = null
    setWorkspace(null)
    setMistakeChild(null)
    initialResumeChecked.current = true
    setStartingChildId(null)
    setInitialVoiceCommand(null)
    setInitialHouseholdEntry(undefined)
    setInitialWakeOnly(false)
    setParentOpen(true)
  }
  parentEntryHandler.current = openParent

  const closeParent = async (): Promise<void> => {
    if(!requestParentDraftNavigation())return
    if (closingParent.current) return
    const original = parentReturn.current
    navigate(original ? { kind: 'workspace', ...original } : { kind: 'home' })
    if (!original) { setParentOpen(false); await load(); return }
    const generation = ++viewGeneration.current
    closingParent.current = true
    setReturningFromParent(true)
    setParentReturnError(null)
    const isCurrent = (): boolean => mounted.current && viewGeneration.current === generation
    try {
      const data = await localApi.bootstrap()
      const readiness = await readTrialReadiness(data)
      if (!isCurrent()) return
      publishTrialReadiness(readiness)
      if (!readiness.allowed) {
        setWorkspace(null)
        setParentOpen(false)
        setScreen({ kind: 'ready', data })
        return
      }
      const restored = await localApi.sessionWorkspace(original.sessionId)
      if (!isCurrent()) return
      if (restored.session.session_id !== original.sessionId || restored.session.child_id !== original.childId) throw new Error('PARENT_RETURN_SCOPE_CHANGED')
      parentReturn.current = null
      setParentOpen(false)
      setWorkspace(restored)
      setWorkspaceRecoveryHeld(privacyHeld.current)
    } catch {
      if (isCurrent()) setParentReturnError('原学习记录暂时无法核对，没有新建、启动或修改任务。')
    } finally {
      if (isCurrent()) { closingParent.current = false; setReturningFromParent(false) }
    }
  }

  useEffect(() => {
    if (screen.kind === 'ready' && parentRequested.current) openParent()
  }, [screen])

  useEffect(() => {
    if (initialResumeChecked.current || screen.kind !== 'ready' || installationSetup || !trialReadiness?.allowed
      || parentOpen || parentRequested.current || recoveryDestination.current.kind === 'parent') return
    initialResumeChecked.current = true
    const active = screen.data.active_session
    if (!active) return
    const child = screen.data.children.find((item) => item.child_id === active.child_id)
    if (child) void start(child)
  }, [screen, installationSetup, trialReadiness, parentOpen])

  const leaveWorkspace = async (sessionActive: boolean, saved?: ReflectionSavedReceipt): Promise<void> => {
    if (!workspace || (saved && (saved.session_id !== workspace.session.session_id || saved.child_id !== workspace.session.child_id || saved.session_status !== 'completed'))) return
    navigate({ kind: 'home' })
    if (saved) setReflectionSavedNotice(true)
    const generation = ++viewGeneration.current
    const isCurrent = (): boolean => mounted.current && generation === viewGeneration.current
    handoffInFlight.current = null
    const sessionId = workspace.session.session_id
    setWorkspace(null)
    setScreen({ kind: 'loading' })
    try {
      const data = await pauseSessionBeforeHome(sessionId, sessionActive)
      const readiness = await readTrialReadiness(data)
      if (isCurrent()) { publishTrialReadiness(readiness); setScreen({ kind: 'ready', data }) }
    } catch (error) {
      if (!isCurrent()) return
      setScreen({
        kind: 'error',
        message: error instanceof ApiError ? error.message : '返回首页时未能确认传感器释放，请重试。'
      })
    }
  }

  const openMistakeBookFromWorkspace = async (): Promise<void> => {
    if (!workspace || screen.kind !== 'ready') return
    const child = screen.data.children.find((item) => item.child_id === workspace.session.child_id)
    if (!child) return
    navigate({ kind: 'other' })
    const generation = ++viewGeneration.current
    await localApi.pauseObservation(workspace.session.session_id)
    if (!mounted.current || generation !== viewGeneration.current) return
    setWorkspace(null)
    setMistakeChild(child)
  }

  const switchWorkspaceChild: VoiceProfileSwitch = async (targetChildId, beforeActivate, clientActionId) => {
    if (!workspace || screen.kind !== 'ready') throw new Error('当前学习档案尚未就绪。')
    const pending = readProfileSwitchHandoff()
    if (!pending || pending.clientActionId !== clientActionId || pending.targetChildId !== targetChildId
      || pending.sourceSessionId !== workspace.session.session_id || pending.sourceChildId !== workspace.session.child_id) throw new Error('档案切换恢复记录未通过核对。')
    navigate({ kind: 'profile_transition', turnId: pending.turnId, clientActionId, sourceSessionId: pending.sourceSessionId,
      sourceChildId: pending.sourceChildId, targetChildId })
    const generation = ++viewGeneration.current
    const isCurrent = (): boolean => mounted.current && generation === viewGeneration.current
    const handoff = Symbol('profile-switch')
    handoffInFlight.current = handoff
    setWorkspace(null)
    setScreen({ kind: 'loading' })
    try {
      const restored = await resolveProfileSwitchDestination(pending)
      if (!isCurrent()) return
      await beforeActivate(restored.handoff.receipt!.output)
      if (!isCurrent()) return
      markProfileSwitchActivated(restored.handoff)
      initialResumeChecked.current = true
      setInitialVoiceCommand(null)
    setInitialHouseholdEntry(undefined)
      setInitialWakeOnly(false)
      recoveryDestination.current = { kind: 'workspace', sessionId: restored.workspace.session.session_id, childId: restored.workspace.session.child_id }
      setScreen({ kind: 'ready', data: { ...screen.data, active_session: restored.workspace.session } })
      setWorkspace(restored.workspace)
    } catch (cause) {
      if (isCurrent()) {
        setWorkspace(null)
        setScreen({ kind: 'error', message: '正在恢复原档案切换，学习记录已保留，不会重复创建会话。' })
      }
      throw cause
    } finally { if (handoffInFlight.current === handoff) handoffInFlight.current = null }
  }

  if (installationSetup && screen.kind === 'ready') {
    return <InstallationSetupWizard children={screen.data.children} initialStatus={installationSetup} initialSmtpSecretConfigured={installationSmtpSecretConfigured} onComplete={async () => { setInstallationSetup(null); await load() }} />
  }

  if (screen.kind === 'ready' && parentRequested.current) {
    return <CenteredStatus title="正在打开家长中心" detail="孩子语音入口保持关闭。" />
  }

  if (screen.kind === 'ready' && !parentOpen && !trialReadiness?.allowed) {
    return <CenteredStatus title="请家长完成本机学习设置" detail="孩子语音入口暂未开放；原学习记录保留，配置齐全也不代表云连接已验证。">
      {runtimeRecoveryMessage && <p role="status">{runtimeRecoveryMessage}</p>}
      {trialReadiness ? <ul>{trialReadiness.missing.map(item => <li key={item}>{item}</li>)}</ul> : <p role="status">正在核对本机配置。</p>}
      <button className="primary-button" onClick={() => openParent('voice_setup')}><LockKeyhole size={17} />打开家长配置</button>
      <button className="secondary-button" onClick={() => void load()}><RefreshCw size={17} />重新核对配置</button>
    </CenteredStatus>
  }

  if (workspace && screen.kind === 'ready') {
    if (workspaceRecoveryHeld) return <CenteredStatus title="原学习记录已保留" detail={runtimeRecoveryMessage ?? '正在核对原会话，监听暂未恢复。'}>
      <button className="secondary-button" onClick={() => openParent()}><LockKeyhole size={17} />家长中心</button>
    </CenteredStatus>
    const child = screen.data.children.find((item) => item.child_id === workspace.session.child_id)
    const workspaceGeneration = viewGeneration.current
    return <SessionWorkspace key={workspace.session.session_id} activeChild={child} initial={workspace} childName={child?.nickname ?? '同学'} childGrade={child?.grade ?? ''} initialVoiceCommand={initialVoiceCommand ?? undefined} initialHouseholdEntry={initialHouseholdEntry} initialWakeOnly={initialWakeOnly} onInitialVoiceCommandConsumed={() => { setInitialVoiceCommand(null); setInitialHouseholdEntry(undefined); setInitialWakeOnly(false) }} onOpenMistakeBook={openMistakeBookFromWorkspace} onSwitchChild={switchWorkspaceChild} onBack={(sessionActive, saved) => { if (mounted.current && viewGeneration.current === workspaceGeneration) void leaveWorkspace(sessionActive, saved) }} />
  }

  if (parentOpen && screen.kind === 'ready') {
    return <>
      {deliverySetupPending && <p role="status">日报邮件尚未完成验证，请在家长中心核对收件人、发件设置和邮件测试状态。</p>}
      {runtimeRecoveryMessage && <p role="status">{runtimeRecoveryMessage}</p>}
      {returningFromParent && <p role="status">正在核对原学习记录</p>}
      {parentReturnError && <div role="alert">{parentReturnError}<button onClick={() => void closeParent()}>重试返回原学习</button></div>}
      <ParentCenter initialSection={parentSetupSection} draftHost={parentDraftHost} children={screen.data.children} model={screen.data.model} onClose={() => void closeParent()} />
    </>
  }

  if (mistakeChild && screen.kind === 'ready') {
    return <MistakeBook child={mistakeChild} onClose={() => { setMistakeChild(null); void load() }} />
  }

  if (screen.kind === 'parent_recovery') {
    return <ParentRestoreRecovery onRestarted={load} />
  }

  if (screen.kind === 'rotation_recovery') {
    return <ParentRotationRecovery onContinued={load} />
  }

  if (screen.kind === 'loading') {
    return <CenteredStatus title="正在准备今天的学习" detail="正在连接本地数据和设备服务。" />
  }

  if (screen.kind === 'error') {
    return (
      <CenteredStatus title="暂时无法进入" detail={screen.message} tone="error">
        <button className="primary-button" onClick={() => void recover()}>
          <RefreshCw size={18} />重启本地服务
        </button>
      </CenteredStatus>
    )
  }

  const { data } = screen
  const homeVoiceEnabled = !data.privacy_paused && !data.recovery?.sensors_forced_paused && !privacyHeld.current
  return (
    <main className="app-shell child-home-surface">
      {reflectionSavedNotice && <p role="status">今天的复盘已保存</p>}
      {runtimeRecoveryMessage && <p role="status">{runtimeRecoveryMessage}</p>}
      <header className="topbar">
        <div>
          <p className="eyebrow">家庭学习伙伴</p>
          <h1>{data.active_session ? '继续今天的学习' : '今天，我们从哪里开始？'}</h1>
        </div>
        <div className="topbar-actions">
          <div className="privacy-status"><ShieldCheck size={18} /><span>{data.privacy_paused ? '设备已暂停' : '本地隐私保护中'}</span></div>
          <button className="secondary-button" onClick={() => openParent()}><LockKeyhole size={17} />家长中心</button>
        </div>
      </header>

      <section className="workspace">
        {data.recovery?.sensors_forced_paused && (
          <div className="recovery-notice" role="status">
            <ShieldCheck size={20} />
            <div><strong>检测到上次异常退出</strong><span>高拍仪和麦克风暂时关闭。继续学习后，可以按需开启。</span></div>
          </div>
        )}
        <div className="agent-stage" aria-label="学习伙伴">
          <CompanionAvatar />
          <div className="agent-copy">
            <strong>{data.active_session ? '我记得我们学到哪里了。' : '欢迎回家。'}</strong>
            <span>
              {data.active_session
                ? '上次进度已经保存，可以从中断处继续。'
                : '按住空格说出唤醒词。还没设置唤醒词时，可以说你的年级。'}
            </span>
          </div>
        </div>
        <HouseholdStandbyVoiceInput profiles={wakeProfiles} onWake={enterFromWake} enabled={homeVoiceEnabled && !startingChildId} />

        <div className="device-strip" aria-label="设备状态">
          <DeviceAvailabilityStrip devices={data.devices} onRebind={openParent} interactive={false}>
            {data.devices.filter((device) => device.device_type === 'microphone').map((device) => (
              <div className="device-item" key={device.device_type}>
                <Mic size={19} />
                <div>
                  <strong>麦克风</strong>
                  <span>{device.message}</span>
                </div>
                <i className={`status-dot status-${device.status}`} />
              </div>
            ))}
            <div className="device-item">
              {online ? <Wifi size={19} /> : <WifiOff size={19} />}
              <div><strong>网络连接</strong><span>{online ? '系统报告已联网；模型服务需单独验证' : '已离线；计划、计时和本地数据仍可用'}</span></div>
              <i className={`status-dot status-${online ? 'ready' : 'unavailable'}`} />
            </div>
          </DeviceAvailabilityStrip>
        </div>

        {data.children.length === 0 ? (
          <div className="empty-state">
            <AlertCircle size={22} />
            <div>
              <strong>还没有孩子档案</strong>
              <span>请从家长设置中先建立两个彼此独立的档案。</span>
            </div>
          </div>
        ) : (
          <section className="child-list" aria-label="选择孩子">
            {data.children.map((child) => {
              const resumable = data.active_session?.child_id === child.child_id
              const profile = wakeProfiles.find((item) => item.childId === child.child_id)
              const wakeWord = profile?.wakeWord ?? child.nickname
              return (
                <article className="child-row" key={child.child_id}>
                  <div className="child-avatar" aria-hidden="true">
                    {child.nickname.slice(0, 1)}
                  </div>
                  <div className="child-info">
                    <strong>{child.nickname}</strong>
                    <span>{child.grade} · {child.curriculum_region}</span>
                  </div>
                  <div className="child-voice-entry">
                    {homeVoiceEnabled ? <>
                      <Mic size={17} />
                      <span>{startingChildId === child.child_id ? '正在进入学习空间'
                        : profile?.configured ? `${profile.handsFree ? '' : '按住空格'}说“${wakeWord}”${resumable ? '继续学习' : '开始今天的学习'}`
                          : `首次使用时，按住空格说“${child.grade}”`}</span>
                    </> : <button type="button" className="secondary-button" disabled={Boolean(startingChildId)}
                      onClick={() => { void start(child) }}>
                      {startingChildId === child.child_id ? '正在进入学习空间' : resumable ? '继续学习' : '进入学习'}
                    </button>}
                  </div>
                </article>
              )
            })}
          </section>
        )}
      </section>
    </main>
  )
}

export function App(): React.JSX.Element {
  const [pending, setPending] = useState(false)
  useEffect(() => {
    let active = true
    let requestVersion = 0
    const refresh = (): void => {
      const version = ++requestVersion
      void window.desktop?.getRuntimeConfig().then(runtime => {
        if (active && version === requestVersion && runtime.status === 'ready') setPending(runtime.rotationKeyUpdatePending === true)
      }).catch(() => undefined)
    }
    window.addEventListener('hsc-rotation-pending-changed', refresh)
    window.addEventListener('hsc-runtime-restarted', refresh)
    return () => { active = false; window.removeEventListener('hsc-rotation-pending-changed', refresh); window.removeEventListener('hsc-runtime-restarted', refresh) }
  }, [])
  return <><AppContent />{pending && <aside role="alert" style={{ position: 'fixed', bottom: 0, left: 0, right: 0, zIndex: 1000, padding: '0.65rem 1rem', background: '#fff4d6', color: '#542b00', textAlign: 'center' }}>新密钥尚未更新，本次正在使用原配置。请家长联系维护者核实模型后重启更新。</aside>}</>
}

export async function pauseSessionBeforeHome(sessionId: string, sessionActive = true): Promise<BootstrapState> {
  window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
  if (sessionActive) await localApi.pauseObservation(sessionId)
  return localApi.bootstrap()
}

function CenteredStatus({
  title,
  detail,
  tone,
  children
}: {
  title: string
  detail: string
  tone?: 'error'
  children?: React.ReactNode
}): React.JSX.Element {
  return (
    <main className={`centered-status ${tone === 'error' ? 'is-error' : ''}`}>
      <div className="status-symbol">{tone === 'error' ? <AlertCircle /> : <span className="loader" />}</div>
      <h1>{title}</h1>
      <p>{detail}</p>
      {children}
    </main>
  )
}
