import { TutorExplanationSettingsPanel } from './TutorExplanationSettingsPanel'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { COSYVOICE_DEFAULT_MODEL, supportsCompanionVoice } from '../../shared/cosyvoice-catalog'
import { LegacyOcrDispositionPanel } from './LegacyOcrDispositionPanel'
import { KnowledgeSourceRevisionPanel } from './KnowledgeSourceRevisionPanel'
import { ArrowLeft, Check, CirclePlay, Clock, DatabaseBackup, Download, FileScan, FolderOpen, History, ListChecks, LockKeyhole, MessageSquareText, MonitorCog, RefreshCw, RotateCcw, Search, ShieldAlert, ShieldCheck, Square, Trash2 } from 'lucide-react'
import { ApiError, localApi, requestParentLocal, requestParentRubricAudio, requestParentTextbookSource, requestParentTrialImage, createParentDraftRecoveryPort } from './api'
import { ParentDocumentTrialPanel } from './ParentDocumentTrialPanel'
import { MistakeBookPrint } from './MistakeBookPrint'
import { qualificationEvidenceLabel, runVideoQualification, type QualificationProgress } from './deviceQualification'
import { findBoundVideoDevice, isNamedDocumentCamera, matchVideoHardwareInventory, mediaDeviceFailure, mediaDeviceRegistration } from './mediaDevices'
import type { BackupManifest, BehaviorBaseline, BehaviorPersonalization, BehaviorReplay, HabitExperimentList, BootstrapState, CaptureArtifactSummary, ChildModelCorrection, ChildModelCorrectionTarget, ChildProfile, ChildSafetyEvent, CurriculumCapability, CurriculumCorpus, CurriculumCoverage, CurriculumInternalizationJob, CurriculumInternalizationScope, CurriculumSearchResult, CurriculumStandardAlignmentSummary, CurriculumStandardSource, DailyReport, DeviceBinding, DeviceQualificationMode, DeviceQualificationRun, DeviceQualificationSummary, ExamBlueprintReadiness, FamilyRule, GoalProgramCycle, KnowledgeSource, LearningState, LongitudinalChildModel, MasteryHistoryItem, ModelCallAudit, ModelPricing, ModelRuntimePolicyStatus, ParentStatus, ParentToday, PeriodReport, PrivacyPreferences, SensorReleaseReview, SpecializedRoadmap, SpecializedSupplyCandidate, SpecializedSupplyGap, StrategyExperiment, SystemHealth, VariantCandidate, WeaknessCluster, WeeklyGoal } from './types'
import { PetBaselineProgress } from './PetBaselineProgress'
import { ParentReportDeliveryPanel } from './ParentReportDeliveryPanel'
import { ParentDailyVisualPreview } from './ParentDailyVisualPreview'
import { beginParentVisualInvalidation, endParentVisualInvalidation, beginCaptureDeletion, confirmCaptureDeletion, parentVisualInvalidationActive } from './parentVisualInvalidation'
import { ParentDiagnosticEmail } from './ParentDiagnosticEmail'
import { HistoricalParentActionFeedback, ParentActionFeedbackPanel } from './ParentActionFeedbackPanel'
import { BehaviorGoalReviewPanel } from './BehaviorGoalReviewPanel'
import { CurriculumSupplyReviewPanel } from './CurriculumSupplyReviewPanel'
import { ManualStandardScopePanel } from './ManualStandardScopePanel'
import { SpecializedReviewQueue } from './SpecializedReviewQueue'
import { LearningRubricReviewPanel } from './LearningRubricReviewPanel'
import { ParentVisualMaterialsHost } from './ParentVisualMaterialsHost'
import { ParentVisualRetention } from './ParentVisualRetention'
import { GoalRoadmapPanel } from './GoalRoadmapPanel'
import { GoalRuleReviewPanel } from './GoalRuleReviewPanel'
import { KnowledgeGraphReviewQueue } from './KnowledgeGraphReviewQueue'
import { OcrReviewQueuePanel } from './OcrReviewQueuePanel'
import { VoiceCalibrationPanel } from './VoiceCalibrationPanel'
import { ControlledContentPanel } from './ControlledContentPanel'
import { ControlledGamePanel } from './ControlledGamePanel'
import { CompanionMemoryPanel } from './CompanionMemoryPanel'
import { VariantGenerationStatus } from './VariantGenerationStatus'
import { ExecutionTracePanel } from './ExecutionTracePanel'
import { ParentPetMaterialsHost } from './ParentPetMaterialsHost'
import { clearOcrArtifactRecovery } from './ocrReviewRecovery'
import { preparePrivacyIntent, readPrivacyIntent, hasPrivacyIntent, operationIdentity, recoverPrivacyCleanup, PrivacyCleanupPendingError, canReleasePrivacyIntent, releaseUnappliedPrivacyIntent, canFinalizePrivacyIntent } from './privacyOperationRecovery'
import { useParentWorkFocus, type ParentWorkFocusProps } from './parentWorkFocus'
import { ParentFamilyWorkQueue, type FamilyWorkTarget } from './ParentFamilyWorkQueue'
import { ParentSupplyFollowupPanel } from './ParentSupplyFollowupPanel'
import { PeriodSupplyFollowup } from './PeriodSupplyFollowup'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import { requestParentDraftNavigation } from './parentDraftNavigation'
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import { changeParentDraftAuthority,parentDraftAuthorityEvent,parentDraftAuthorityVersion } from './parentDraftAuthority'
import './ParentReportSupplyLayout.css'
import './ParentCenterLayout.css'

type ViewState =
  | { kind: 'loading' }
  | { kind: 'status_error' }
  | { kind: 'locked'; status: ParentStatus }
  | { kind: 'ready'; today: ParentToday; reports: Record<string, DailyReport[]>; periodReports: Record<string, PeriodReport[]>; learningStates: Record<string, LearningState>; familyRules: Record<string, FamilyRule>; weeklyGoals: Record<string, WeeklyGoal[]>; strategyExperiments: Record<string, StrategyExperiment[]>; privacy: PrivacyPreferences; health: SystemHealth; deviceBindings: DeviceBinding[]; deviceQualification: DeviceQualificationSummary; sensorReleaseReviews: SensorReleaseReview[]; sensorReleasePlatformEligible: boolean; safetyEvents: ChildSafetyEvent[] }

export function ParentCenter({ children, model, onClose, draftHost: externalDraftHost, initialSection }: {
  children: ChildProfile[]
  model: BootstrapState['model']
  onClose: () => void
  draftHost?: ParentTextbookDraftHost
  initialSection?: 'voice_setup'
}): React.JSX.Element {
  const [view, setView] = useState<ViewState>({ kind: 'loading' })
  const [printChildId, setPrintChildId] = useState<string | null>(null)
  const advancedSettings = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    if (initialSection === 'voice_setup' && view.kind === 'ready') {
      document.getElementById('parent-voice-setup')?.scrollIntoView?.({ block: 'start' })
    }
  }, [initialSection, view.kind])
  const [privateDraftHost]=useState(()=>new ParentTextbookDraftHost(createParentDraftRecoveryPort))
  const draftHost=externalDraftHost??privateDraftHost
  useEffect(()=>draftHost.attach(),[draftHost])
  const unlockAttempt=useRef(0)
  const statusAttempt=useRef(0),statusTimer=useRef<number|null>(null)
  const renderedAuthority=parentDraftAuthorityVersion()
  const requireParentAuth=():void=>{if(renderedAuthority===parentDraftAuthorityVersion())changeParentDraftAuthority('locked')}
  useEffect(()=>{
    const changed=(event:Event):void=>{
      statusAttempt.current++
      if(statusTimer.current!==null){window.clearTimeout(statusTimer.current);statusTimer.current=null}
      if((event as CustomEvent<{reason:string}>).detail.reason==='unlocked')return
      unlockAttempt.current++;setBusy(false)
      draftHost.suspendAll()
      setPrintChildId(null)
      setView({kind:'locked',status:{configured:true,locked_seconds:0}})
      setPin('');setError('家长身份已锁定；本窗口草稿保留在内存，重新解锁后需核对恢复。')
    }
    window.addEventListener(parentDraftAuthorityEvent,changed)
    return()=>{window.removeEventListener(parentDraftAuthorityEvent,changed);draftHost.suspendAll()}
  },[draftHost])
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [profiles, setProfiles] = useState(children)
  const [selectedChildId, commitSelectedChildId] = useState(children[0]?.child_id ?? '')
  const selectedChild = profiles.find(child => child.child_id === selectedChildId)
  const setSelectedChildId=(childId:string):boolean=>{
    if(childId!==selectedChildId&&!requestParentDraftNavigation())return false
    commitSelectedChildId(childId);return true
  }
  const closeAfterDraft=():void=>{if(requestParentDraftNavigation())onClose()}
  const [selectedReportId, setSelectedReportId] = useState<string | null>(null)
  const [reportDeliveryRefresh, setReportDeliveryRefresh] = useState(0)
  const reportReviewEpoch = useRef<Record<string, number>>({})
  const [workTarget, setWorkTarget] = useState<(FamilyWorkTarget & { nonce: string }) | null>(null)
  const pendingWorkOpen = useRef<{ nonce: string; resolve: () => void; reject: (error: Error) => void; timer: number } | null>(null)
  const resolveWorkFocus = useCallback((nonce: string, error?: string): void => {
    const pending = pendingWorkOpen.current
    if (!pending || pending.nonce !== nonce) return
    window.clearTimeout(pending.timer); pendingWorkOpen.current = null
    if (error) pending.reject(new Error(error)); else pending.resolve()
  }, [])
  useEffect(() => () => {
    const pending = pendingWorkOpen.current
    if (pending) { window.clearTimeout(pending.timer); pending.reject(new Error('家长页面已关闭，未继续打开旧事项。')); pendingWorkOpen.current = null }
  }, [])
  useEffect(() => {
    if (workTarget && (view.kind !== 'ready' || workTarget.child_id !== selectedChildId)) {
      resolveWorkFocus(workTarget.nonce, '页面或孩子已切换，未继续打开旧事项。')
      setWorkTarget(null)
    }
  }, [workTarget, selectedChildId, view.kind, resolveWorkFocus])
  const openWork = (target: FamilyWorkTarget): Promise<void> => {
    if (!profiles.some(child => child.child_id === target.child_id)) return Promise.reject(new Error('找不到这名孩子，未打开其他孩子的事项。'))
    if(target.child_id!==selectedChildId&&!requestParentDraftNavigation())return Promise.reject(new Error('草稿尚未确认保存，未切换孩子。'))
    if (pendingWorkOpen.current) resolveWorkFocus(pendingWorkOpen.current.nonce, '已改为打开另一条事项。')
    const nonce = crypto.randomUUID()
    const result = new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => resolveWorkFocus(nonce, '该事项尚未准确加载，请刷新来源后重试。'), 15000)
      pendingWorkOpen.current = { nonce, resolve, reject, timer }
    })
    setSelectedChildId(target.child_id); setSelectedReportId(null); setWorkTarget({ ...target, nonce })
    return result
  }
  const workFocus = (panel: string): ParentWorkFocusProps => ({
    focusRequest: workTarget?.child_id === selectedChildId && workTarget.panel === panel
      ? { objectId: workTarget.object_id, relatedId: workTarget.related_id, nonce: workTarget.nonce } : null,
    onFocusResolved: resolveWorkFocus,
  })

  const loadParentStatus=useCallback(async():Promise<void>=>{
    const attempt=++statusAttempt.current,authority=parentDraftAuthorityVersion()
    if(statusTimer.current!==null)window.clearTimeout(statusTimer.current)
    setView({kind:'loading'});setError(null);setPin('')
    let timer:number|null=null
    try{
      const status=await Promise.race([localApi.parentStatus(),new Promise<never>((_resolve,reject)=>{
        timer=window.setTimeout(()=>reject(new Error('PARENT_STATUS_WAIT_EXPIRED')),10000)
        statusTimer.current=timer
      })])
      if(attempt!==statusAttempt.current||authority!==parentDraftAuthorityVersion())return
      setView({kind:'locked',status})
    }catch{
      if(attempt!==statusAttempt.current||authority!==parentDraftAuthorityVersion())return
      setView({kind:'status_error'})
      setError('暂时无法核对家长身份状态，请稍后重试。')
    }finally{
      if(timer!==null)window.clearTimeout(timer)
      if(statusTimer.current===timer)statusTimer.current=null
    }
  },[])
  useEffect(()=>{
    void loadParentStatus()
    return()=>{statusAttempt.current++;if(statusTimer.current!==null){window.clearTimeout(statusTimer.current);statusTimer.current=null}}
  },[loadParentStatus])

  useEffect(() => {
    if (view.kind !== 'locked' || view.status.locked_seconds <= 0) return
    const timer = window.setTimeout(() => {
      setView((current) => current.kind === 'locked'
        ? { ...current, status: { ...current.status, locked_seconds: Math.max(0, current.status.locked_seconds - 1) } }
        : current)
    }, 1000)
    return () => window.clearTimeout(timer)
  }, [view])

  const unlock = async (): Promise<void> => {
    if (view.kind !== 'locked') return
    const attempt=++unlockAttempt.current
    let authority=parentDraftAuthorityVersion()
    setBusy(true)
    setError(null)
    try {
      await localApi.parentUnlock(pin, !view.status.configured)
      authority=parentDraftAuthorityVersion()
      const dashboard = await localApi.parentDashboard()
      if(attempt!==unlockAttempt.current||authority!==parentDraftAuthorityVersion())return
      setView({
        kind: 'ready',
        today: dashboard.today,
        reports: Object.fromEntries(dashboard.children.map((item) => [item.child_id, item.reports])),
        learningStates: Object.fromEntries(dashboard.children.map((item) => [item.child_id, item.learning_state])),
        periodReports: Object.fromEntries(dashboard.children.map((item) => [item.child_id, item.period_reports])),
        familyRules: Object.fromEntries(dashboard.children.map((item) => [item.child_id, item.family_rule])),
        weeklyGoals: Object.fromEntries(dashboard.children.map((item) => [item.child_id, item.weekly_goals])),
        strategyExperiments: Object.fromEntries(dashboard.children.map((item) => [item.child_id, item.strategy_experiments])),
        privacy: dashboard.privacy,
        health: dashboard.health,
        deviceBindings: dashboard.device_bindings,
        deviceQualification: dashboard.device_qualification,
        sensorReleaseReviews: dashboard.sensor_release_reviews,
        sensorReleasePlatformEligible: dashboard.sensor_release_platform_eligible,
        safetyEvents: dashboard.safety_events
      })
      setPin('')
    } catch (cause) {
      if(attempt!==unlockAttempt.current||authority!==parentDraftAuthorityVersion())return
      if (cause instanceof ApiError && cause.code === 'PARENT_LOCKED') {
        const status = await localApi.parentStatus().catch(() => null)
        if (status&&attempt===unlockAttempt.current&&authority===parentDraftAuthorityVersion()) setView({ kind: 'locked', status })
      }
      setError(cause instanceof ApiError ? cause.message : 'PIN 未能验证，请重试。')
    } finally {
      if(attempt===unlockAttempt.current)setBusy(false)
    }
  }

  const reports = view.kind === 'ready' ? (view.reports[selectedChildId] ?? []) : []
  const lockedSeconds = view.kind === 'locked' ? view.status.locked_seconds : 0
  const learningState = view.kind === 'ready' ? view.learningStates[selectedChildId] : null
  const selected = useMemo(
    () => reports.find((report) => report.report_id === selectedReportId) ?? reports[0] ?? null,
    [reports, selectedReportId]
  )

  const retryReport = async (childId: string): Promise<void> => {
    if (view.kind !== 'ready') return
    setBusy(true); setError(null)
    try {
      const report = await localApi.retryChildReport(childId)
      const today = await localApi.parentToday()
      setView((current) => current.kind === 'ready' ? {
        ...current,
        today,
        reports: {
          ...current.reports,
          [childId]: [report, ...(current.reports[childId] ?? []).filter((item) => item.report_id !== report.report_id)]
        }
      } : current)
      setSelectedChildId(childId)
      setSelectedReportId(report.report_id)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '日报仍未生成，重试任务已保留。')
    } finally { setBusy(false) }
  }

  const updateHealth = (health: SystemHealth): void => {
    setView((current) => current.kind === 'ready' ? { ...current, health } : current)
  }

  const refreshReviewedActions = async (childId: string): Promise<void> => {
    const epoch = (reportReviewEpoch.current[childId] ?? 0) + 1
    reportReviewEpoch.current[childId] = epoch
    const refreshed = await localApi.childReports(childId)
    if (refreshed.some((report) => report.child_id !== childId)) throw new Error('报告归属不一致，未更新当前界面。')
    if (reportReviewEpoch.current[childId] !== epoch) return
    setView((current) => current.kind === 'ready'
      ? { ...current, reports: { ...current.reports, [childId]: refreshed } } : current)
  }

  if (view.kind === 'ready' && selectedChild && selectedChild.child_id === printChildId) {
    return <MistakeBookPrint key={selectedChild.child_id} child={selectedChild} backLabel="返回家长中心" onClose={() => setPrintChildId(null)} />
  }

  return (
    <main className="parent-shell">
      <header className="parent-header">
        <div className="parent-header-inner">
          <button className="icon-button" title="返回孩子首页" onClick={closeAfterDraft}><ArrowLeft size={20} /></button>
          <div className="parent-header-title"><span>仅家长可见</span><h1>家长中心</h1></div>
          <div className={`model-badge ${model.configured ? 'is-ready' : ''}`}>
            {model.configured ? <Check size={16} /> : <ShieldAlert size={16} />}
            <span>{model.configured ? `${model.provider} · ${model.model}` : '讲题模型未配置'}</span>
          </div>
        </div>
      </header>

      {view.kind === 'loading' || view.kind === 'status_error' ? (
        <section className="parent-unlock" aria-busy={view.kind==='loading'}>
          <LockKeyhole size={36} />
          <h2>{view.kind==='loading'?'正在核对家长身份状态':'暂时无法核对家长身份状态'}</h2>
          {view.kind==='loading'?<p role="status">正在读取本机家长访问状态，请稍候。</p>:<><p role="alert">{error}</p><button className="primary-button" onClick={()=>void loadParentStatus()}>重试读取家长状态</button></>}
        </section>
      ) : view.kind === 'locked' ? (
        <form className="parent-unlock" onSubmit={(event) => {
          event.preventDefault()
          if (!busy && lockedSeconds === 0 && pin.length >= 4) void unlock()
        }}>
          <LockKeyhole size={36} />
          <h2>{view.kind === 'locked' && view.status.configured ? '输入家长 PIN' : '首次设置家长 PIN'}</h2>
          <p>{view.kind === 'locked' && view.status.configured ? '报告、沟通建议和设置只在解锁后显示。' : '请设置4到8位数字。PIN只保存为本地加盐哈希。'}</p>
          {lockedSeconds > 0 && <div className="parent-lock-note" role="status">连续输错次数过多，{formatLockTime(lockedSeconds)} 后可再试。应用重启不会提前解除。</div>}
          <input aria-label="家长 PIN" type="password" inputMode="numeric" maxLength={8} value={pin} disabled={lockedSeconds > 0} onChange={(event) => setPin(event.target.value.replace(/\D/g, ''))} />
          {error && <div className="tutor-error" role="alert">{error}</div>}
          <button type="submit" className="primary-button" disabled={busy || lockedSeconds > 0 || pin.length < 4}>
            <LockKeyhole size={17} />{busy ? '正在验证' : lockedSeconds > 0 ? '暂时锁定' : view.kind === 'locked' && view.status.configured ? '解锁' : '设置并进入'}
          </button>
        </form>
      ) : (
        <section className="parent-workspace">
          {error && <div className="tutor-error">{error}</div>}
          <nav className="parent-section-nav" aria-label="家长中心分区">
            <a href="#parent-overview">学习概览</a>
            <a href="#parent-reports">报告与日志</a>
            <a href="#parent-settings" onClick={() => { if (advancedSettings.current) advancedSettings.current.open = true }}>设备与设置</a>
          </nav>
          <div id="parent-overview" className="parent-tabs" role="group" aria-label="选择孩子">
            {profiles.map((child) => (
              <button aria-pressed={selectedChildId === child.child_id} className={selectedChildId === child.child_id ? 'is-active' : ''} key={child.child_id} onClick={() => { if (setSelectedChildId(child.child_id)) setSelectedReportId(null) }}>
                <strong>{child.nickname}</strong><span>{child.grade}</span>
              </button>
            ))}
          </div>
          {selectedChild && <button type="button" className="secondary-button" onClick={() => {
            if (requestParentDraftNavigation()) setPrintChildId(selectedChild.child_id)
          }}>打印错题本</button>}
          <ParentFamilyWorkQueue audience="parent-decisions" children={profiles} request={requestParentLocal} onOpen={openWork} refreshToken={reportDeliveryRefresh}
            onAuthNeeded={requireParentAuth} />
          <TodayOverview today={view.today} selectedChildId={selectedChildId} busy={busy} onSelectChild={setSelectedChildId} onRetryReport={retryReport} />
          <TextbookLanguageCandidateReview key={`parent-material-capture-${selectedChildId}`} api={localApi.knowledgeGraphRequest} childId={selectedChildId} captureOnly draftHost={draftHost} readSource={requestParentTextbookSource} />
          <details id="parent-settings" ref={advancedSettings} className="parent-advanced-workspace" open={initialSection === 'voice_setup' ? true : undefined}><summary>系统核对、设备与高级设置</summary>
            <p className="report-uncertainty">这里展示自动核对的证据、失败恢复和设备设置。正常使用时无需逐项审核；系统不能可靠判定时才会回到上方“需要家长决定”。</p>
          <ParentSupplyFollowupPanel {...workFocus('parent-work-supply-followup')} key={`supply-followup-${selectedChildId}`} childId={selectedChildId} request={requestParentLocal}
            onOpenMaterial={objectId=>openWork({child_id:selectedChildId,kind:'curriculum',panel:'parent-work-curriculum',object_id:objectId,related_id:null})}
            onAuthNeeded={requireParentAuth} />
          <div id="parent-work-safety"><SafetyAlertsPanel {...workFocus('parent-work-safety')} childId={selectedChildId} events={view.safetyEvents} onEventsChange={(safetyEvents) => setView((current) => current.kind === 'ready' ? { ...current, safetyEvents } : current)} /></div>
          {profiles.find((child) => child.child_id === selectedChildId) && (
            <ChildProfilePanel
              child={profiles.find((child) => child.child_id === selectedChildId)!}
              onSaved={(saved) => setProfiles((current) => current.map((child) => child.child_id === saved.child_id ? saved : child))}
            />
          )}
          <DesktopSettingsPanel />
          <VoiceCalibrationPanel key={`voice-calibration-${selectedChildId}`} childId={selectedChildId} parentManaged />
          <DeviceSetupPanel initial={view.deviceBindings} initialQualification={view.deviceQualification} sessionActive={view.today.items.some((item) => Boolean(item.session && item.session.status === 'active'))} />
          <ParentDocumentTrialPanel key={`document-trial-${selectedChildId}`} childId={selectedChildId} request={requestParentLocal} readImage={requestParentTrialImage} />
          {view.familyRules[selectedChildId] && <FamilyRulesPanel childId={selectedChildId} initial={view.familyRules[selectedChildId]} />}
          {profiles.find((child) => child.child_id === selectedChildId) && <CurriculumCorpusPanel child={profiles.find((child) => child.child_id === selectedChildId)!} modelConfigured={model.configured} />}
          <ControlledContentPanel key={`controlled-content-${selectedChildId}`} childId={selectedChildId} />
          <ControlledGamePanel key={`controlled-game-${selectedChildId}`} childId={selectedChildId} />
          <div id="parent-work-materials">{profiles.find((child) => child.child_id === selectedChildId) && <KnowledgeSourcesPanel key={selectedChildId} child={profiles.find((child) => child.child_id === selectedChildId)!} />}</div>
          {profiles.find((child) => child.child_id === selectedChildId) && <LearningRubricReviewPanel
            {...workFocus('parent-work-rubric')}
            requestAudio={requestParentRubricAudio}
            key={`learning-rubrics-${selectedChildId}`} child={profiles.find((child) => child.child_id === selectedChildId)!}
            request={requestParentLocal}
            authneeded={requireParentAuth}
          />}
          {selectedChildId && <ParentVisualMaterialsHost key={`visual-materials-${selectedChildId}`} childId={selectedChildId} />}
          {selectedChildId && <ParentVisualRetention key={`visual-retention-${selectedChildId}`} childId={selectedChildId} />}
          {profiles.find((child) => child.child_id === selectedChildId) && <CurriculumSupplyReviewPanel
            {...workFocus('parent-work-curriculum')}
            key={`curriculum-supply-${selectedChildId}`} child={profiles.find((child) => child.child_id === selectedChildId)!}
            request={requestParentLocal}
            onOpenMaterials={() => document.getElementById('parent-work-materials')?.scrollIntoView({ behavior: 'smooth' })}
            onRequireParentAuth={requireParentAuth}
          />}
          <KnowledgeGraphReviewQueue api={localApi.knowledgeGraphRequest} reviewer="家长" childId={selectedChildId} showMaterialCapture={false} />
          <div id="parent-work-ocr"><OcrReviewQueuePanel {...workFocus('parent-work-ocr')} key={`ocr-review-${selectedChildId}`} childId={selectedChildId} /></div>
          <div id="parent-voice-setup">
            {initialSection === 'voice_setup' && <p role="status">请核对讲题模型和实时语音，并在下方隐私设置中明确授权云端音频。保存配置后可返回继续；本机配置齐全不代表云连接已通过验证。</p>}
            <ModelSettingsPanel initial={model} />
            <TutorExplanationSettingsPanel />
            <QwenRealtimeSettingsPanel />
            <VoiceSettingsPanel />
            {initialSection === 'voice_setup' && <a href="#parent-voice-privacy">核对云端音频授权</a>}
          </div>
          <ModelOperationsPanel key={`model-operations-${selectedChildId}`} childId={selectedChildId} initialModel={model} />
          <OperationsHealthPanel initial={view.health} onHealthChange={updateHealth} onReturnToChild={closeAfterDraft} />
          <ExecutionTracePanel key={`execution-trace-${selectedChildId}`} childId={selectedChildId} />
          <SensorReleaseVerificationPanel initial={view.sensorReleaseReviews} platformEligible={view.sensorReleasePlatformEligible} />
          <div id="parent-voice-privacy"><PrivacyOperations childId={selectedChildId} initial={view.privacy} health={view.health} onHealthChange={updateHealth} /></div>
          <ChildGrowthModelPanel key={`growth-model-${selectedChildId}`} childId={selectedChildId} />
          <CompanionMemoryPanel childId={selectedChildId} load={(query) => localApi.companionMemories(selectedChildId, query)} forget={(memoryId) => localApi.forgetCompanionMemory(selectedChildId, memoryId)} />
          <WeeklyGoalsPanel goals={view.weeklyGoals[selectedChildId] ?? []} />
          <StrategyEvolutionPanel key={`strategy-evolution-${selectedChildId}`} childId={selectedChildId} initial={view.strategyExperiments[selectedChildId] ?? []} />
          <BehaviorEvidencePanel key={`behavior-evidence-${selectedChildId}`} childId={selectedChildId} />
          <div id="parent-work-specialized"><SpecializedSupplyPanel {...workFocus('parent-work-specialized')} key={`specialized-supply-${selectedChildId}`} childId={selectedChildId}
            onOpenCurriculum={(candidateId) => openWork({ child_id: selectedChildId, kind: 'curriculum', panel: 'parent-work-curriculum', object_id: candidateId, related_id: null })} /></div>
          <ParentPetMaterialsHost key={`pet-materials-${selectedChildId}`} childId={selectedChildId} />
          <div id="parent-work-variant"><VariantCandidateReviewPanel {...workFocus('parent-work-variant')} key={`variant-review-${selectedChildId}`} childId={selectedChildId}
            onRequireParentAuth={requireParentAuth} /></div>
          <MistakeWeaknessPanel key={`mistake-weakness-${selectedChildId}`} childId={selectedChildId} />
          {learningState && <LearningOverview key={`learning-overview-${selectedChildId}`} state={learningState} />}
          </details>
          <section id="parent-reports" className="parent-report-section" aria-label="报告与日志">
          <PeriodReportsPanel childId={selectedChildId} initial={view.periodReports[selectedChildId] ?? []}
            onOpenSupply={rootId=>openWork({child_id:selectedChildId,kind:'supply_followup',panel:'parent-work-supply-followup',object_id:rootId,related_id:null})} onGenerated={(report) => {
            setView((current) => current.kind === 'ready' ? {
              ...current,
              periodReports: {
                ...current.periodReports,
                [selectedChildId]: [report, ...(current.periodReports[selectedChildId] ?? []).filter((item) => item.period_report_id !== report.period_report_id)]
              }
            } : current)
            setReportDeliveryRefresh((current) => current + 1)
          }} />
          {selectedChildId && <ParentDailyVisualPreview key={`daily-visual-${selectedChildId}`} childId={selectedChildId} />}
          <ParentReportDeliveryPanel {...workFocus('parent-work-reports')} key={`report-delivery-${selectedChildId}`} childId={selectedChildId} refreshToken={reportDeliveryRefresh} onReviewChanged={() => refreshReviewedActions(selectedChildId)}
            onOpenSupply={rootId=>openWork({child_id:selectedChildId,kind:'supply_followup',panel:'parent-work-supply-followup',object_id:rootId,related_id:null})} />
          <ParentDiagnosticEmail />
          {reports.length === 0 ? (
            <div className="parent-empty"><MessageSquareText size={30} /><h2>还没有日报</h2><p>孩子完成一次学习复盘后，这里会出现基于当天证据的报告。</p></div>
          ) : (
            <div className="report-layout">
              <aside className="report-list">
                {reports.map((report) => (
                  <button className={selected?.report_id === report.report_id ? 'is-active' : ''} key={report.report_id} onClick={() => setSelectedReportId(report.report_id)}>
                    <span>{report.report_date}</span><strong>{report.summary.headline}</strong>
                  </button>
                ))}
              </aside>
              {selected && <ReportDetail report={selected} />}
            </div>
          )}
          </section>
        </section>
      )}
    </main>
  )
}

export function ParentWorkNavigator({ childId, safetyEvents }: {
  childId: string
  safetyEvents: ChildSafetyEvent[]
}): React.JSX.Element {
  type QueueKey = 'ocr' | 'variant' | 'specialized'
  type QueueState = { count: number | null; loading: boolean; error: boolean }
  const emptyQueues = (): Record<QueueKey, QueueState> => ({
    ocr: { count: null, loading: true, error: false },
    variant: { count: null, loading: true, error: false },
    specialized: { count: null, loading: true, error: false },
  })
  const [snapshot, setSnapshot] = useState({ childId, queues: emptyQueues() })
  const requestEpoch = useRef(0)
  const queues = snapshot.childId === childId ? snapshot.queues : emptyQueues()
  const loading = Object.values(queues).some((queue) => queue.loading)
  const error = Object.values(queues).some((queue) => queue.error)

  const load = async (): Promise<void> => {
    const epoch = ++requestEpoch.current
    setSnapshot((current) => {
      const previous = current.childId === childId ? current.queues : emptyQueues()
      return { childId, queues: Object.fromEntries(Object.entries(previous).map(([key, queue]) =>
        [key, { ...queue, loading: true, error: false }])) as Record<QueueKey, QueueState> }
    })
    const read = async (key: QueueKey, getItems: () => Promise<Array<{ child_id: string }>>): Promise<void> => {
      let count: number | null = null
      try {
        const items = await getItems()
        if (items.some((item) => item.child_id !== childId)) throw new Error('Queue child mismatch')
        count = items.length
      } catch { /* Keep the last confirmed count, never turn an unread queue into zero. */ }
      if (requestEpoch.current !== epoch) return
      setSnapshot((current) => current.childId !== childId ? current : {
        childId, queues: { ...current.queues, [key]: {
          count: count ?? current.queues[key].count, loading: false, error: count === null,
        } },
      })
    }
    await Promise.all([
      read('ocr', () => localApi.ocrReviewQueue(childId)),
      read('variant', () => localApi.variantCandidates(childId)),
      read('specialized', () => localApi.specializedSupplyCandidates(childId)),
    ])
  }
  useEffect(() => { void load(); return () => { requestEpoch.current += 1 } }, [childId])

  const pendingSafety = safetyEvents.filter((item) => item.child_id === childId && item.status === 'pending').length
  const workItems = [
    { key: 'safety', label: '安全提醒', count: pendingSafety, loading: false, error: false, target: 'parent-work-safety', urgent: true },
    { key: 'ocr', label: 'OCR 校对', ...queues.ocr, target: 'parent-work-ocr', urgent: false },
    { key: 'variant', label: '错题变式审核', ...queues.variant, target: 'parent-work-variant', urgent: false },
    { key: 'specialized', label: '专项题审核', ...queues.specialized, target: 'parent-work-specialized', urgent: false },
  ].filter((item) => (item.count ?? 0) > 0 || item.error || item.loading)
  const goTo = (target: string): void => document.getElementById(target)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  return <section className="parent-panel parent-work-navigator" aria-label="家长待处理工作台">
    <div className="panel-heading"><div><span>当前孩子 · 下一工作</span><h2><ListChecks size={20} />家长待处理</h2></div><button className="secondary-button" disabled={loading} onClick={() => void load()}><RefreshCw size={15} />{loading ? '正在汇总' : '刷新'}</button></div>
    {error && <p className="operation-message">部分待办暂未读取成功，已保留其他可处理项，可刷新重试。</p>}
    {!loading && !error && workItems.length === 0 && <p className="parent-work-clear"><Check size={17} />当前没有需要家长处理的审核项。</p>}
    {workItems.length > 0 && <div className="parent-work-items">{workItems.map((item) => <button key={item.key} className={item.urgent ? 'is-urgent' : ''} onClick={() => goTo(item.target)}><strong>{item.count ?? '待确认'}</strong><span>{item.label}</span>{item.error ? <small>{item.count === null ? '读取失败' : '上次结果，未更新'}</small> : item.loading ? <small>读取中</small> : null}</button>)}</div>}
  </section>
}

const variantStageLabel: Record<string, string> = {
  near_transfer: '同知识点换条件',
  representation_transfer: '换表达方式',
  discrimination: '易错概念辨析',
  mixed_transfer: '综合迁移',
}

export function VariantCandidateReviewPanel({ childId, focusRequest, onFocusResolved, onRequireParentAuth }: { childId: string; onRequireParentAuth?: () => void } & ParentWorkFocusProps): React.JSX.Element {
  const root = useRef<HTMLElement>(null)
  const loadEpoch = useRef(0)
  const [focusReadNonce, setFocusReadNonce] = useState('')
  const [items, setItems] = useState<VariantCandidate[]>([])
  const [loading, setLoading] = useState(true)
  const [reviewingId, setReviewingId] = useState<string | null>(null)
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [message, setMessage] = useState<string | null>(null)

  const load = async (): Promise<void> => {
    const epoch = ++loadEpoch.current
    setLoading(true); setMessage(null)
    try {
      const next = await localApi.variantCandidates(childId)
      if (epoch !== loadEpoch.current) return
      if (next.some(item => item.child_id !== childId)) throw new Error('candidate owner mismatch')
      setItems(next)
      setFocusReadNonce(focusRequest?.nonce ?? '')
    }
    catch (cause) {
      if (epoch !== loadEpoch.current) return
      setMessage(cause instanceof ApiError ? cause.message : '待审核变式题没有加载成功。')
      if (focusRequest) onFocusResolved?.(focusRequest.nonce, '该变式题读取失败，未打开其他题目。')
    }
    finally { if (epoch === loadEpoch.current) setLoading(false) }
  }

  useEffect(() => { void load(); return () => { loadEpoch.current++ } }, [childId, focusRequest?.nonce])

  useParentWorkFocus(focusRequest, !loading && (!focusRequest || focusReadNonce === focusRequest.nonce), root, onFocusResolved)

  const review = async (item: VariantCandidate, decision: 'approve' | 'reject'): Promise<void> => {
    setReviewingId(item.candidate_id); setMessage(null)
    try {
      await localApi.reviewVariantCandidate(item.candidate_id, decision, notes[item.candidate_id]?.trim() ?? '')
      setItems((current) => current.filter((candidate) => candidate.candidate_id !== item.candidate_id))
      setMessage(decision === 'approve' ? item.expected_answer.local_rule === 'local_exact_v1'
        ? '已批准：该题可进入孩子的后续复测。'
        : '已批准为参考练习：开放答案评价不计入可靠掌握。' : '已拒绝：该题不会展示给孩子。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '审核结果没有保存，请重试。')
    } finally { setReviewingId(null) }
  }

  return <section ref={root} className="parent-panel variant-review-panel">
    <div className="panel-heading"><div><span>错题强化 · 家长质量门</span><h2>待审核变式题</h2></div><button onClick={() => void load()} disabled={loading}><RefreshCw size={16} />{loading ? '读取中' : '刷新'}</button></div>
    <p>请核对原题、变式题意、答案及解析。封闭答案题批准后可用于复测；开放题的答案要点仅作参考，不据此判定掌握。</p>
    <VariantGenerationStatus key={childId} childId={childId} request={requestParentLocal} onRequireParentAuth={onRequireParentAuth} onUpdated={load} />
    {message && <p className="operation-message">{message}</p>}
    {!loading && items.length === 0 && <p className="parent-empty">当前没有待审核题。新错题完成诊断后，系统会在后台生成并停在这里等待核对。</p>}
    {items.length > 0 && <div className="variant-review-list">{items.map((item, index) => <article data-work-object={item.candidate_id} className="variant-review-item" key={item.candidate_id}>
      <div className="panel-heading"><div><span>{variantStageLabel[item.stage] ?? item.stage} · 难度 {item.difficulty}/5</span><h3>候选题 {index + 1}</h3></div><span>{item.validation.passed ? '结构校验通过 · 答案待核对' : '校验异常'}</span></div>
      {item.source_context && <p><strong>{item.source_context.kind === 'document_camera' ? '纸面原题：' : '来源题目：'}</strong>{item.source_context.prompt ?? '来源已不可用'}</p>}
      {item.source_context?.valid === false && <p role="alert">来源已修改或撤回，不能批准此题。请重新核对原题。</p>}
      <p><strong>题目：</strong>{item.prompt}</p>
      <p><strong>{item.expected_answer.local_rule === 'local_exact_v1' ? '可接受的完整答案：' : '参考答案要点：'}</strong>{(item.expected_answer.exact_answers ?? item.expected_answer.keywords).join('、')}</p>
      <p><strong>解析：</strong>{item.explanation}</p>
      <label>审核备注（可选）<textarea aria-label={`候选题${index + 1}审核备注`} maxLength={500} value={notes[item.candidate_id] ?? ''} onChange={(event) => setNotes((current) => ({ ...current, [item.candidate_id]: event.target.value }))} /></label>
      <div className="review-actions"><button disabled={reviewingId !== null || !item.validation.passed || item.source_context?.valid === false} onClick={() => void review(item, 'approve')}><Check size={16} />批准进入复测</button><button disabled={reviewingId !== null} onClick={() => void review(item, 'reject')}><Trash2 size={16} />拒绝这道题</button></div>
    </article>)}</div>}
  </section>
}

const specializedSupplyStageLabel = {
  parallel: '平行测量', discrimination: '易混辨析', transfer: '新情境迁移',
}

function specializedAnswerText(answer: SpecializedSupplyCandidate['expected_answer']): string | null {
  if (!answer || typeof answer !== 'object') return null
  for (const key of ['task_points', 'exact_answers', 'keywords']) {
    const values = answer[key]
    if (Array.isArray(values) && values.length > 0 && values.every(value => typeof value === 'string' && value.trim())) return values.join('、')
  }
  return null
}

export function SpecializedSupplyPanel({ childId, focusRequest, onFocusResolved, onOpenCurriculum }: { childId: string; onOpenCurriculum?: (candidateId: string) => Promise<void> } & ParentWorkFocusProps): React.JSX.Element {
  const root = useRef<HTMLElement>(null)
  const [loaded, setLoaded] = useState(false)
  const loadEpoch = useRef(0)
  const [focusReadNonce, setFocusReadNonce] = useState('')
  const [tracks, setTracks] = useState<Array<'pet' | 'competition_math' | 'junior_exam'>>([])
  const [selectedTrack, setSelectedTrack] = useState<'pet' | 'competition_math' | 'junior_exam' | null>(null)
  const [selectedSubject, setSelectedSubject] = useState('数学')
  const [gaps, setGaps] = useState<SpecializedSupplyGap[]>([])
  const [blueprintReadiness, setBlueprintReadiness] = useState<ExamBlueprintReadiness | null>(null)
  const [candidates, setCandidates] = useState<SpecializedSupplyCandidate[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const load = async (trackOverride?: 'pet' | 'competition_math' | 'junior_exam', subjectOverride?: string, preserveMessage = false): Promise<void> => {
    const epoch = ++loadEpoch.current
    setBusy(true); if (!preserveMessage) setMessage(null)
    try {
      const roadmaps = await localApi.specializedRoadmaps(childId)
      if (epoch !== loadEpoch.current) return
      const available = roadmaps.map((item) => item.track) as Array<'pet' | 'competition_math' | 'junior_exam'>
      const track = trackOverride ?? selectedTrack ?? available[0] ?? null
      const subject = track === 'pet' ? '英语' : track === 'competition_math' ? '数学' : subjectOverride ?? selectedSubject
      setTracks(available); setSelectedTrack(track)
      setSelectedSubject(subject)
      const [gapResult, pending, readiness] = await Promise.all([
        track ? localApi.specializedSupplyGaps(childId, track) : Promise.resolve(null),
        localApi.specializedSupplyCandidates(childId),
        track ? localApi.examBlueprintReadiness(childId, track, subject) : Promise.resolve(null),
      ])
      if (epoch !== loadEpoch.current) return
      if (pending.some(item => item.child_id !== childId)) throw new Error('candidate owner mismatch')
      setGaps(gapResult?.domains ?? []); setCandidates(pending); setBlueprintReadiness(readiness)
      setFocusReadNonce(focusRequest?.nonce ?? '')
    } catch (cause) {
      if (epoch !== loadEpoch.current) return
      setMessage(cause instanceof ApiError ? cause.message : '专项题库状态暂时无法读取。')
      if (focusRequest) onFocusResolved?.(focusRequest.nonce, '该专项题读取失败，未打开其他题目。')
    } finally { if (epoch === loadEpoch.current) { setBusy(false); setLoaded(true) } }
  }

  useEffect(() => { void load(); return () => { loadEpoch.current++ } }, [childId, focusRequest?.nonce])
  useParentWorkFocus(focusRequest, loaded && !busy && (!focusRequest || focusReadNonce === focusRequest.nonce), root, onFocusResolved)

  const prepare = async (): Promise<void> => {
    if (!selectedTrack) return
    setBusy(true); setMessage(null)
    try {
      const result = await localApi.prepareSpecializedSupply(childId, selectedTrack, 4)
      setMessage(result.created_count ? `已创建${result.created_count}个能力域的候选题任务。生成后仍需家长审核。` : '当前没有需要新建的任务；可能已经达标或正在生成。')
      await load(selectedTrack)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '专项题准备失败，可稍后重试。') }
    finally { setBusy(false) }
  }

  const prepareBlueprint = async (): Promise<void> => {
    if (!selectedTrack || !blueprintReadiness) return
    setBusy(true); setMessage(null)
    try {
      const result = await localApi.prepareExamBlueprint(
        childId, selectedTrack, selectedSubject, blueprintReadiness.blueprint.blueprint_id,
      )
      const unsupported = result.skipped.filter((item) =>
        item.reason === 'SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED').length
      setMessage(result.created_count
        ? `已按${blueprintReadiness.blueprint.title}创建${result.created_count}个补题任务。题目生成后必须由家长审核。${unsupported ? `另有${unsupported}个能力域缺少可确定评分的题源，未用文本题伪装补齐。` : ''}`
        : unsupported
          ? `有${unsupported}个能力域缺少可确定评分的题源，未创建不可靠题目。`
          : '蓝图没有新的生成缺口；请先处理已有待审核题目。')
      await load(selectedTrack, selectedSubject, true)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '蓝图补题任务没有创建成功。') }
    finally { setBusy(false) }
  }

  const missing = gaps.reduce((total, item) => total + item.missing_count, 0)
  const ready = gaps.filter((item) => item.ready).length
  return <section ref={root} className="parent-panel variant-review-panel">
    <div className="panel-heading"><div><span>专项学习 · 测量深度</span><h2>多题型诊断供给</h2></div><button disabled={busy} onClick={() => void load()}><RefreshCw size={16} />刷新</button></div>
    <p>每个能力域必须具备基线、平行测量、易混辨析和新情境迁移，避免把记住答案当成掌握。候选题只使用学科信息生成，不发送孩子身份、学习历史或家庭数据。</p>
    {tracks.length > 1 && <div className="segmented-control" role="tablist" aria-label="选择专项">{tracks.map((track) => <button className={selectedTrack === track ? 'is-active' : ''} key={track} onClick={() => void load(track, track === 'junior_exam' ? '数学' : undefined)}>{track === 'pet' ? 'PET' : track === 'competition_math' ? '数学竞赛' : '中考七科'}</button>)}</div>}
    {selectedTrack === 'junior_exam' && <label>中考科目<select aria-label="中考蓝图科目" value={selectedSubject} onChange={(event) => void load('junior_exam', event.target.value)}>{['语文', '数学', '英语', '物理', '化学', '道德与法治', '历史'].map((subject) => <option key={subject}>{subject}</option>)}</select></label>}
    {blueprintReadiness && <div className="exam-blueprint-readiness">
      <div><strong>{blueprintReadiness.blueprint.title}</strong><span>{blueprintReadiness.blueprint.authority} · {blueprintReadiness.blueprint.version}</span></div>
      <div className="growth-evidence-summary"><strong>{blueprintReadiness.ready ? '整卷已就绪' : '整卷未就绪'}</strong><span>{blueprintReadiness.active_item_count}/{blueprintReadiness.required_item_count}道审核题</span><span>{blueprintReadiness.pending_review_count}道待审核</span></div>
      <div className="blueprint-section-list">{blueprintReadiness.sections.map((section) => <span key={section.section_id}>{section.title} {section.active_count}/{section.item_target}{section.pending_review_count ? `，待审${section.pending_review_count}` : ''}</span>)}</div>
      <p className="report-uncertainty">{blueprintReadiness.blueprint.score_boundary}</p>
      <button disabled={busy || blueprintReadiness.ready || blueprintReadiness.sections.every((section) => section.pending_generation_count === 0)} onClick={() => void prepareBlueprint()}>按蓝图补齐题库</button>
    </div>}
    <div className="growth-evidence-summary"><strong>题库覆盖</strong><span>{ready}/{gaps.length}个能力域具备四类测量</span><span>仍缺{missing}类候选</span><span>{candidates.length}道候选待审核</span></div>
    <button disabled={busy || !selectedTrack || missing === 0} onClick={() => void prepare()}>准备缺口候选题</button>
    {message && <p className="operation-message">{message}</p>}
    {gaps.filter((item) => item.missing_count > 0 && !item.seed_available).length > 0 && <p className="report-uncertainty">有{gaps.filter((item) => item.missing_count > 0 && !item.seed_available).length}个能力域尚无人工基线题，系统不会凭空生成；需先补入可靠种子题。</p>}
    <SpecializedReviewQueue key={childId} childId={childId} candidates={candidates} onSaved={() => { void load(undefined, undefined, true) }} />
    {candidates.length > 0 && <div className="variant-review-list">{candidates.filter(candidate => candidate.review_workflow === 'curriculum_review').map((candidate, index) => <article data-work-object={candidate.candidate_id} className="variant-review-item" key={candidate.candidate_id}>
      <div className="panel-heading"><div><span>{candidate.subject} · {specializedSupplyStageLabel[candidate.stage]}</span><h3>专项候选题 {index + 1}</h3></div><span>难度 {candidate.difficulty}/5</span></div>
      <p><strong>题目：</strong>{candidate.prompt}</p><p><strong>答案要点：</strong>{specializedAnswerText(candidate.expected_answer) ?? '答案依据暂不可读'}</p><p><strong>解析：</strong>{candidate.explanation}</p>
      <div className="review-actions"><button disabled={busy || !onOpenCurriculum}
        onClick={() => { void onOpenCurriculum?.(candidate.candidate_id).catch(cause => setMessage(cause instanceof Error ? cause.message : '原资料审核暂时无法打开，请重试。')) }}><FolderOpen size={16} />查看资料审核</button></div>
    </article>)}</div>}
  </section>
}

const goalStatusLabel: Record<GoalProgramCycle['status'] | 'quota_met', string> = {
  not_started: '执行尚未开始', on_track: '执行进度正常', behind: '执行进度落后', completed: '执行量达标', quota_met: '执行量达标',
}

const learningOutcomeLabels = {
  recheck_needed: '作答仍需核对', supported_only: '仍需提示支持',
  independent_observed: '已观察到独立作答', retention_observed: '已观察到延迟保持',
  insufficient_evidence: '证据不足，学习效果待验证',
}

const learningActionLabels: Record<string, string> = {
  needs_baseline: '先建立能力基线', clarify_then_recheck: '核实卡住的步骤后复测',
  fade_hint_then_check: '减少提示，再检查独立作答', schedule_delayed_check: '安排跨天复测',
  check_transfer: '使用不同题目检查迁移',
  fresh_independent_check: '换一道合格题，重新检查独立作答',
}

function GoalEvidenceExplanation({ goal }: { goal: WeeklyGoal | GoalProgramCycle }): React.JSX.Element {
  const outcome = goal.learning_outcome
  const focus = goal.focus
  const schedulingLabels = {
    ready: '可在剩余预算内安排', quota_met: '执行量已达标，停止自动追加',
    budget_exhausted: '分钟预算用尽，暂停自动追加',
  }
  return <>
    <p><strong>学习结果：</strong>{outcome ? learningOutcomeLabels[outcome.status] ?? '学习效果待验证' : '学习效果待验证'}。执行量达标不代表学习目标达成。</p>
    {outcome?.reason && outcome.reason !== focus?.reason && <p>{outcome.reason}</p>}
    {outcome && <p><strong>学习下一步：</strong>{learningActionLabels[outcome.next_action] ?? '待核对有效作答后安排'}。</p>}
    {outcome?.window_days != null && <small>{outcome.evidence_scope === 'as_of_week_end' && outcome.as_of ? `证据截止 ${outcome.as_of.slice(0, 10)}（UTC），回看 ${outcome.window_days} 天` : `依据最近 ${outcome.window_days} 天的有效作答`}，不等同于本周新增提升，也不预测考试或获奖结果。</small>}
    {focus && <p><strong>当前重点：</strong>{focus.title ? `${focus.subject ?? ''} ${focus.title}` : focus.status === 'needs_baseline' ? '先建立能力基线' : '待确认'}。{focus.reason ? `依据：${focus.reason}` : '重点依据尚未提供。'}</p>}
    <p><strong>时间预算：</strong>{goal.budget_status === 'exhausted' ? '本周分钟预算已用尽。' : goal.budget_status === 'available' ? `剩余 ${goal.remaining_minutes} 分钟。` : '预算说明尚未提供。'}</p>
    <p><strong>后续安排：</strong>{goal.scheduling_status ? schedulingLabels[goal.scheduling_status] ?? '排程说明尚未提供' : '排程说明尚未提供'}。{goal.scheduling_reason}</p>
  </>
}

const correctionTargetLabel: Record<ChildModelCorrectionTarget, string> = {
  explanation_pacing: '讲解节奏', task_segment_minutes: '单段学习时长', interaction_tone: '互动语气', mistake_priority: '错题排序',
}

const correctionValueOptions: Record<ChildModelCorrectionTarget, Array<{ value: string; label: string }>> = {
  explanation_pacing: [{ value: 'standard_layered', label: '分层讲解' }, { value: 'short_step_then_restate', label: '短步讲解后让孩子复述' }],
  task_segment_minutes: [10, 15, 20, 25, 30, 35, 40, 45].map((value) => ({ value: String(value), label: `${value} 分钟` })),
  interaction_tone: [{ value: 'gentle', label: '温和鼓励' }, { value: 'brief', label: '简短直接' }, { value: 'direct', label: '明确果断' }],
  mistake_priority: [{ value: 'due_or_relapsed_first', label: '到期复习或反复错题优先' }, { value: 'schoolwork_first', label: '当天校内作业优先' }],
}

function correctionValueLabel(item: ChildModelCorrection): string {
  const option = correctionValueOptions[item.target_key].find((candidate) => candidate.value === String(item.corrected_value))
  return option?.label ?? String(item.corrected_value)
}

export function ChildGrowthModelPanel({ childId }: { childId: string }): React.JSX.Element {
  const ruleReviewChild = useRef(childId)
  ruleReviewChild.current = childId
  const [model, setModel] = useState<LongitudinalChildModel | null>(null)
  const [cycles, setCycles] = useState<GoalProgramCycle[]>([])
  const [nextAction, setNextAction] = useState<null | { subject: string; title: string; minutes: number; completion_standard: string }>(null)
  const [roadmaps, setRoadmaps] = useState<SpecializedRoadmap[]>([])
  const [corrections, setCorrections] = useState<ChildModelCorrection[]>([])
  const [outcomeReason, setOutcomeReason] = useState<Record<string, string>>({})
  const [outcomeBusy, setOutcomeBusy] = useState<string | null>(null)
  const [correctionTarget, setCorrectionTarget] = useState<ChildModelCorrectionTarget>('explanation_pacing')
  const [correctionValue, setCorrectionValue] = useState('short_step_then_restate')
  const [correctionReason, setCorrectionReason] = useState('')
  const [correctionDays, setCorrectionDays] = useState('30')
  const [savingCorrection, setSavingCorrection] = useState(false)
  const [loading, setLoading] = useState(true)
  const [message, setMessage] = useState<string | null>(null)

  const load = async (): Promise<void> => {
    setLoading(true); setMessage(null)
    const [modelResult, goalResult, roadmapResult, correctionResult] = await Promise.allSettled([
      localApi.longitudinalChildModel(childId), localApi.goalProgramCycles(childId), localApi.specializedRoadmaps(childId), localApi.childModelCorrections(childId),
    ])
    if (modelResult.status === 'fulfilled') setModel(modelResult.value)
    if (goalResult.status === 'fulfilled') { setCycles(goalResult.value.items); setNextAction(goalResult.value.next_daily_action) }
    if (roadmapResult.status === 'fulfilled') setRoadmaps(roadmapResult.value)
    if (correctionResult.status === 'fulfilled') setCorrections(correctionResult.value)
    if (modelResult.status === 'rejected' || goalResult.status === 'rejected' || roadmapResult.status === 'rejected' || correctionResult.status === 'rejected') {
      setMessage('部分成长证据没有加载成功；已保留可读内容，可重试。')
    }
    setLoading(false)
  }
  useEffect(() => { void load() }, [childId])

  const changeCorrectionTarget = (target: ChildModelCorrectionTarget): void => {
    setCorrectionTarget(target)
    setCorrectionValue(correctionValueOptions[target][0].value)
  }

  const saveCorrection = async (): Promise<void> => {
    if (correctionReason.trim().length < 2) return
    setSavingCorrection(true); setMessage(null)
    try {
      const days = Number(correctionDays)
      const result = await localApi.createChildModelCorrection(childId, {
        target_key: correctionTarget,
        value: correctionTarget === 'task_segment_minutes' ? Number(correctionValue) : correctionValue,
        reason: correctionReason.trim(),
        expires_at: days > 0 ? new Date(Date.now() + days * 86_400_000).toISOString() : null,
      })
      setModel(result.model)
      setCorrections(await localApi.childModelCorrections(childId))
      setCorrectionReason('')
      setMessage('校正已保存，下一轮对话将使用新策略；学习证据和掌握度未被改动。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '校正没有保存，请重试。')
    } finally { setSavingCorrection(false) }
  }

  const revokeCorrection = async (item: ChildModelCorrection): Promise<void> => {
    setSavingCorrection(true); setMessage(null)
    try {
      const result = await localApi.revokeChildModelCorrection(item.correction_id, '家长撤销，恢复当前证据推导策略。')
      setModel(result.model)
      setCorrections(await localApi.childModelCorrections(childId))
      setMessage('已撤销这条校正，已保留历史和审计记录。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '撤销没有保存，请刷新后重试。')
    } finally { setSavingCorrection(false) }
  }

  const evidenceLabel = model?.payload.evidence_level === 'established' ? '历史记录覆盖较多' : model?.payload.evidence_level === 'emerging' ? '历史记录正在积累' : '冷启动，记录不足'
  const reliableFeedback = model?.payload.learning.reliable_feedback
  return <section className="parent-panel child-growth-model-panel">
    <div className="panel-heading"><div><span>长期建模 · 目标执行</span><h2>学习成长与目标周期</h2></div><button onClick={() => void load()} disabled={loading}><RefreshCw size={16} />{loading ? '更新中' : '更新证据'}</button></div>
    {message && <p className="operation-message">{message}</p>}
    {model && <div className="growth-evidence-summary">
      <p><strong>历史记录覆盖：{evidenceLabel}</strong>。历史记录涉及 {model.payload.learning.evidenced_node_count} 个知识节点，近 {model.payload.execution.window_days} 天有 {model.payload.execution.sessions} 次学习会话。记录数量不代表学习稳定或已经掌握。</p>
      <p>任务证据：计划 {model.payload.execution.planned_tasks} 项，完成 {model.payload.execution.completed_tasks} 项，延后/跳过 {model.payload.execution.deferred_tasks} 项{model.payload.execution.completion_rate == null ? '；样本不足，不计算完成率。' : `，完成率 ${Math.round(model.payload.execution.completion_rate * 100)}%。`}</p>
      <p>当前支持策略：{model.payload.support_strategy.explanation_pacing === 'short_step_then_restate' ? '短步讲解后让孩子复述' : '分层讲解'}，单段约 {model.payload.support_strategy.task_segment_minutes} 分钟，错题按到期或再错优先，保留独立检查。</p>
      {model.payload.support_strategy.strategy_evolution && <p>讲题策略进化：{model.payload.support_strategy.strategy_evolution.status === 'active' ? '已启用受控候选' : model.payload.support_strategy.strategy_evolution.status === 'shadow' ? '正在影子评估' : model.payload.support_strategy.strategy_evolution.status === 'rolled_back' ? '已自动恢复基线' : '维持基线或待评估'}；已纳入 {model.payload.support_strategy.strategy_evolution.feedback_count} 条反馈。</p>}
      {model.payload.behavior.habit_experiments && <p>习惯实验：{model.payload.behavior.habit_experiments.open ? `正在进行“${model.payload.behavior.habit_experiments.open.title}”` : '当前没有进行中的实验'}；累计完成 {model.payload.behavior.habit_experiments.completed_count} 个，其中 {model.payload.behavior.habit_experiments.improving_count} 个出现改善信号。</p>}
      {(model.payload.specialized_progress?.length ?? 0) > 0 && <div className="growth-evidence-summary"><strong>最近专项测评</strong>{model.payload.specialized_progress?.map((item) => <span key={item.track}>{item.track === 'pet' ? 'PET' : item.track === 'competition_math' ? '竞赛数学' : '中考'}：覆盖 {item.domain_count} 个能力域，薄弱候选 {item.weak_domain_count} 个{item.score != null && item.max_score != null ? `，记录 ${item.score}/${item.max_score}` : ''}</span>)}</div>}
      {model.payload.parent_feedback && <p>近 {model.payload.parent_feedback.window_days} 天家长反馈 {model.payload.parent_feedback.feedback_count} 次，实际执行 {model.payload.parent_feedback.executed_count} 次{model.payload.parent_feedback.average_effectiveness == null ? '' : `，平均有效度 ${model.payload.parent_feedback.average_effectiveness}/5`}{model.payload.parent_feedback.high_stress_count ? `；有 ${model.payload.parent_feedback.high_stress_count} 次高压力反馈，后续建议会缩短并降低要求。` : '。'}</p>}
      {model.payload.companion_balance && <div>
        <p>近 {model.payload.companion_balance.window_days} 天陪伴记录：故事/离屏活动 {model.payload.companion_balance.activity_sessions} 次、视频 {model.payload.companion_balance.video_sessions} 次、游戏 {model.payload.companion_balance.game_sessions} 次，共 {model.payload.companion_balance.total_minutes} 分钟。</p>
        {['companion_balance_v2', 'companion_balance_v3', 'companion_balance_v4'].includes(model.payload.companion_balance.version ?? '') ? <>
          <p>孩子自述 {model.payload.companion_balance.self_report_sessions ?? 0} 次；原任务恢复计时回执 {model.payload.companion_balance.task_resume_receipt_sessions ?? 0} 次；实际返回学习核验 {model.payload.companion_balance.verified_child_return_sessions ?? 0} 次。自述、结束活动或恢复计时不代表已经投入学习或习惯改善。</p>
          <p>{model.payload.companion_balance.guidance}</p>
          {model.payload.companion_balance.version === 'companion_balance_v4' && (model.payload.companion_balance.not_dispatched_launches ?? 0) > 0 && <p>另有 {model.payload.companion_balance.not_dispatched_launches} 次启动已核实未执行，未计入视频或游戏使用次数；原记录仍保留。</p>}
        </> : <p>历史统计尚未按实际返回证据复核，不据此判断活动效果或孩子已回到学习。</p>}
      </div>}
      {model.payload.learning.subjects.length > 0 && <table><caption>历史估计，仅供回看，不代表当前掌握</caption><thead><tr><th>学科</th><th>记录涉及节点</th><th>历史薄弱估计</th><th>到期复习</th><th>历史较高估计</th></tr></thead><tbody>{model.payload.learning.subjects.map((subject) => <tr key={subject.subject}><td>{subject.subject}</td><td>{subject.node_count}</td><td>{subject.weak_nodes}</td><td>{subject.due_reviews}</td><td>{subject.stable_nodes}</td></tr>)}</tbody></table>}
      <details>
        <summary>有效作答证据{reliableFeedback ? `（${reliableFeedback.nodes.length} 个知识点）` : '（待提供）'}</summary>
        {!reliableFeedback ? <p>尚未提供有效作答分析，学习效果待验证；不以历史估计代替。</p> : reliableFeedback.nodes.length === 0 ? <p>尚无符合条件的有效作答，先建立能力基线。没有记录不等于不会。</p> : <>
          <p>最近 {reliableFeedback.window_days} 天的有效作答；独立作答、不同题目和跨天保持分别记录，不据此承诺成绩。</p>
          {reliableFeedback.nodes.map((node) => <div key={node.knowledge_node_id}>
            <p><strong>{node.subject} · {node.title}</strong>：{learningOutcomeLabels[node.status] ?? '学习效果待验证'}。</p>
            <p>{node.reason}</p>
            <p>下一步：{learningActionLabels[node.next_action] ?? '待核对有效作答后安排'}。</p>
            <p>独立答对的不同题目：{node.independent_distinct_items == null ? '尚未提供' : `${node.independent_distinct_items} 道`}；延迟保持成功：{node.retention_success_count} 次。同题重复不算迁移。</p>
          </div>)}
        </>}
      </details>
      <p className="report-uncertainty">{model.payload.limitations.join(' ')}</p>
      {model.payload.tutor_outcomes && <div className="tutor-outcome-evidence">
        <h3>讲解后的作答证据</h3>
        <p>{model.payload.tutor_outcomes.outcome_count} 次记录，{model.payload.tutor_outcomes.mapped_count} 次已关联审核知识点，{model.payload.tutor_outcomes.unmapped_count} 次尚不能可靠归因。模型评价不等于已掌握。</p>
        {model.payload.tutor_outcomes.recent_outcomes.map((item) => <details key={item.outcome_id}>
          <summary>{new Date(item.created_at).toLocaleString('zh-CN')} · {item.mapping_status === 'mapped' ? '已关联知识点' : '待核对知识点'}</summary>
          <p>{item.summary}</p>
          {item.evidence_status !== 'available' && <p>原始依据已不可用，这条历史评价不参与当前能力判断。</p>}
          <label>校正理由<input aria-label={`评价校正理由 ${item.outcome_id}`} value={outcomeReason[item.outcome_id] ?? ''} onChange={(event) => setOutcomeReason((previous) => ({ ...previous, [item.outcome_id]: event.target.value }))} /></label>
          <button disabled={outcomeBusy !== null || !outcomeReason[item.outcome_id]?.trim()} onClick={() => {
            setOutcomeBusy(item.outcome_id); setMessage(null)
            void localApi.reviewTutorOutcome(childId, item.outcome_id, item.evidence_status === 'revoked' ? 'restore' : 'revoke', outcomeReason[item.outcome_id].trim())
              .then((result) => { setModel(result.model); setMessage('评价校正已保存，历史依据保留。') })
              .catch(() => setMessage('校正尚未保存，请重试。'))
              .finally(() => setOutcomeBusy(null))
          }}>{item.evidence_status === 'revoked' ? <RotateCcw size={14} /> : <Trash2 size={14} />}{item.evidence_status === 'revoked' ? '恢复这条评价' : '撤回这条评价'}</button>
        </details>)}
      </div>}
    </div>}
    <div className="growth-correction-editor">
      <h3>校正陪伴策略</h3>
      <p>这里只调整智能体如何帮助孩子，不会改写做题证据、错题或掌握度。</p>
      <div className="growth-correction-fields">
        <label>校正项目<select aria-label="校正项目" value={correctionTarget} onChange={(event) => changeCorrectionTarget(event.target.value as ChildModelCorrectionTarget)}>{Object.entries(correctionTargetLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>校正值<select aria-label="校正值" value={correctionValue} onChange={(event) => setCorrectionValue(event.target.value)}>{correctionValueOptions[correctionTarget].map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <label>有效期<select aria-label="校正有效期" value={correctionDays} onChange={(event) => setCorrectionDays(event.target.value)}><option value="7">7 天</option><option value="30">30 天</option><option value="90">90 天</option><option value="0">持续到手动撤销</option></select></label>
      </div>
      <label>判断依据<textarea aria-label="校正依据" maxLength={500} value={correctionReason} onChange={(event) => setCorrectionReason(event.target.value)} placeholder="例如：连续三天观察到长段讲解后容易遗漏中间步骤" /></label>
      <button disabled={savingCorrection || correctionReason.trim().length < 2} onClick={() => void saveCorrection()}><Check size={16} />{savingCorrection ? '保存中' : '保存并应用'}</button>
      {corrections.length > 0 && <div className="growth-correction-history"><strong>校正记录</strong>{corrections.slice(0, 10).map((item) => <div key={item.correction_id} className="growth-correction-row"><span>{correctionTargetLabel[item.target_key]}：{correctionValueLabel(item)}（{item.status === 'active' ? '生效中' : item.status === 'expired' ? '已到期' : '已撤销'}）</span><small>{item.reason}</small>{item.status === 'active' && <button className="secondary-button" disabled={savingCorrection} onClick={() => void revokeCorrection(item)}><RotateCcw size={15} />撤销</button>}</div>)}</div>}
    </div>
    <div className="goal-cycle-list">{cycles.map((cycle) => {
      const minuteProgress = Math.min(100, Math.round(cycle.completed_minutes / Math.max(1, cycle.target_minutes) * 100))
      return <article key={cycle.cycle_id}>
        <div className="panel-heading"><div><span>{cycle.week_start} 至 {cycle.week_end}</span><h3>{cycle.title}</h3></div><strong>{goalStatusLabel[cycle.participation_status ?? cycle.status]}</strong></div>
        <p>{cycle.target_statement}</p>
        <p>本周执行量：{cycle.completed_minutes}/{cycle.target_minutes} 分钟，{cycle.completed_sessions}/{cycle.target_sessions} 次（分钟执行量 {minuteProgress}%）。</p>
        <GoalEvidenceExplanation goal={cycle} />
        {cycle.deadline && <p><strong>目标节点：</strong>{cycle.deadline.target_date}{cycle.deadline.days_remaining != null ? `，剩余 ${cycle.deadline.days_remaining} 天` : ''}{cycle.deadline.target_score != null ? `，${cycle.deadline.target_score_label || '目标分数'} ${cycle.deadline.target_score}` : ''}。</p>}
        {Boolean(cycle.target_horizon.event_name) && <p><strong>目标赛事：</strong>{String(cycle.target_horizon.event_name)} · {String(cycle.target_horizon.event_version || '规则版本待确认')}{cycle.target_horizon.award_target ? ` · ${String(cycle.target_horizon.award_target)}` : ''}{cycle.target_horizon.registration_deadline ? `，报名截止 ${String(cycle.target_horizon.registration_deadline)}` : ''}。</p>}
        <GoalProgramEditor childId={childId} cycle={cycle} onSaved={(result) => { setCycles(result.items); setNextAction(result.next_daily_action) }} />
        <GoalRuleReviewPanel key={`${childId}:${cycle.program_id}`} childId={childId} programId={cycle.program_id} track={cycle.track} onSaved={async () => {
          const result = await localApi.goalProgramCycles(childId)
          if (ruleReviewChild.current !== childId) return
          setCycles(result.items); setNextAction(result.next_daily_action)
        }} />
        <GoalRoadmapPanel childId={childId} roadmap={cycle.roadmap} />
      </article>
    })}</div>
    <div className="goal-cycle-list">{roadmaps.map((roadmap) => <article key={roadmap.track}>
      <div className="panel-heading"><div><span>{roadmap.track === 'pet' ? 'PET 听说读写' : roadmap.track === 'competition_math' ? '竞赛数学与奥赛思维' : '南京中考七科'}</span><h3>{roadmap.phase === 'baseline' ? '建立能力基线' : roadmap.phase === 'foundation' ? '基础修复' : roadmap.phase === 'strengthening' ? '薄弱点强化' : '迁移与模拟'}</h3></div><strong>{Math.round(roadmap.evidence_coverage * 100)}% 参考域覆盖</strong></div>
      <p>有当前参考记录 {roadmap.evidence_domain_count}/{roadmap.total_domain_count} 个能力域。{roadmap.reference_readiness == null ? '证据不足，暂不计算参考均分。' : `已知能力域参考均分 ${roadmap.reference_readiness} 分，不代表考试准备度或掌握率。`}</p>
      <p>本地可执行题目已覆盖 {roadmap.catalog_domain_count ?? 0}/{roadmap.total_domain_count} 个能力域；未覆盖能力域不会生成虚假训练任务。</p>
      {roadmap.stage_plan && <p><strong>当前阶段：</strong>{roadmap.stage_plan.focus} {roadmap.stage_plan.reassessment_rule}</p>}
      {roadmap.stage_requirements && <p>{roadmap.stage_requirements.reason}</p>}
      {roadmap.target?.target_date && <p><strong>目标节点：</strong>{roadmap.target.target_date}{roadmap.target.days_remaining != null ? `，剩余 ${roadmap.target.days_remaining} 天` : ''}{roadmap.target.target_score != null ? `，${roadmap.target.target_score_label || '目标分数'} ${roadmap.target.target_score}` : ''}。</p>}
      {roadmap.whole_exam_analysis?.latest_exam && <p><strong>{roadmap.whole_exam_analysis.latest_exam.score_scope === 'current_exam_reference_only' ? '最近整卷训练参考：' : '最近整卷：'}</strong>{roadmap.whole_exam_analysis.latest_exam.subject} {roadmap.whole_exam_analysis.latest_exam.score}/{roadmap.whole_exam_analysis.latest_exam.max_score}（{roadmap.whole_exam_analysis.latest_exam.percentage}%）；薄弱能力域 {(roadmap.whole_exam_analysis.latest_exam.score_scope === 'current_exam_reference_only' ? roadmap.whole_exam_analysis.exam_reference_weak_domains ?? [] : roadmap.whole_exam_analysis.weak_domains).length} 个，继续按错题和独立复测核对。</p>}
      {roadmap.whole_exam_analysis?.latest_exam_review && !roadmap.whole_exam_analysis.latest_exam && <p>最近整卷原回答已保存 {roadmap.whole_exam_analysis.latest_exam_review.answered_items}/{roadmap.whole_exam_analysis.latest_exam_review.total_items} 题，{roadmap.whole_exam_analysis.latest_exam_review.verified_items} 题有当前有效评分。其余需核对后才能形成参考结果。</p>}
      <p className="report-uncertainty">{roadmap.forecast.statement}</p>
      {roadmap.next_actions.map((action) => <p key={action.domain_id}><strong>{action.priority}. {action.subject}</strong> · {action.title}，约 {action.minutes} 分钟。{action.selection_reason}</p>)}
      {roadmap.domains.some(domain => domain.evidence_conflict || domain.assessment_planning_status === 'expired_reference') && <details>
        <summary>需要重新核对的依据</summary>
        {roadmap.domains.filter(domain => domain.evidence_conflict || domain.assessment_planning_status === 'expired_reference').map(domain => <p key={domain.domain_id}>
          <strong>{domain.subject} · {domain.label}</strong>：历史参考 {domain.assessment_reference_score ?? '未知'} 分，
          {domain.assessment_planning_status === 'expired_reference' ? '已超过本次规划参考窗口，需要重新检查。' : `${domain.reference_score_basis === 'qualified_independent_observations' ? '当前合格作答参考' : '当前规划参考'} ${domain.reference_score ?? '未知'} 分。`}
          {domain.priority_reason}
        </p>)}
      </details>}
    </article>)}</div>
    {nextAction && <p><strong>下一个可执行行动：</strong>{nextAction.subject} · {nextAction.title}，约 {nextAction.minutes} 分钟。{nextAction.completion_standard}</p>}
  </section>
}

function GoalProgramEditor({ childId, cycle, onSaved }: {
  childId: string
  cycle: GoalProgramCycle
  onSaved: (result: { items: GoalProgramCycle[]; next_daily_action: null | { subject: string; title: string; minutes: number; completion_standard: string } }) => void
}): React.JSX.Element {
  const [editing, setEditing] = useState(false)
  const [statement, setStatement] = useState(cycle.target_statement)
  const [targetDate, setTargetDate] = useState(String(cycle.target_horizon.target_date ?? ''))
  const [targetScore, setTargetScore] = useState(cycle.target_horizon.target_score == null ? '' : String(cycle.target_horizon.target_score))
  const [targetScoreLabel, setTargetScoreLabel] = useState(String(cycle.target_horizon.target_score_label ?? ''))
  const [eventName, setEventName] = useState(String(cycle.target_horizon.event_name ?? ''))
  const [eventRulesUrl, setEventRulesUrl] = useState(String(cycle.target_horizon.event_rules_url ?? ''))
  const [eventVersion, setEventVersion] = useState(String(cycle.target_horizon.event_version ?? ''))
  const [awardTarget, setAwardTarget] = useState(String(cycle.target_horizon.award_target ?? ''))
  const [registrationDeadline, setRegistrationDeadline] = useState(String(cycle.target_horizon.registration_deadline ?? ''))
  const [weeklyMinutes, setWeeklyMinutes] = useState(String(cycle.target_minutes))
  const [weeklySessions, setWeeklySessions] = useState(String(cycle.target_sessions))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async (): Promise<void> => {
    setSaving(true); setError(null)
    try {
      const result = await localApi.configureGoalProgram(childId, cycle.track, {
        target_statement: statement.trim(),
        target_date: targetDate || null,
        target_score: targetScore ? Number(targetScore) : null,
        target_score_label: targetScoreLabel.trim() || null,
        event_name: cycle.track === 'competition_math_path' ? eventName.trim() || null : null,
        event_rules_url: cycle.track === 'competition_math_path' ? eventRulesUrl.trim() || null : null,
        event_version: cycle.track === 'competition_math_path' ? eventVersion.trim() || null : null,
        award_target: cycle.track === 'competition_math_path' ? awardTarget.trim() || null : null,
        registration_deadline: cycle.track === 'competition_math_path' ? registrationDeadline || null : null,
        weekly_minutes: Number(weeklyMinutes),
        weekly_sessions: Number(weeklySessions),
      })
      onSaved(result); setEditing(false)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '目标配置没有保存，请重试。')
    } finally { setSaving(false) }
  }

  if (!editing) return <button className="secondary-button" onClick={() => setEditing(true)}>调整目标和每周节奏</button>
  return <div className="goal-program-editor">
    <label>目标说明<textarea value={statement} maxLength={500} onChange={(event) => setStatement(event.target.value)} /></label>
    <label>{cycle.track === 'competition_math_path' ? '赛事日期' : '目标日期'}<input type="date" value={targetDate} onChange={(event) => setTargetDate(event.target.value)} /></label>
    <label>目标分数<input type="number" min="0" max="1000" value={targetScore} onChange={(event) => setTargetScore(event.target.value)} placeholder="可选" /></label>
    <label>分数说明<input value={targetScoreLabel} maxLength={80} onChange={(event) => setTargetScoreLabel(event.target.value)} placeholder="例如 PET 参考目标分" /></label>
    {cycle.track === 'competition_math_path' && <>
      <label>目标赛事<input value={eventName} maxLength={120} onChange={(event) => setEventName(event.target.value)} placeholder="填写确定参加的赛事全名" /></label>
      <label>报名截止<input type="date" value={registrationDeadline} onChange={(event) => setRegistrationDeadline(event.target.value)} /></label>
      <label>规则版本<input value={eventVersion} maxLength={80} onChange={(event) => setEventVersion(event.target.value)} placeholder="例如 2026 小学四年级组" /></label>
      <label>官方规则链接<input type="url" value={eventRulesUrl} maxLength={500} onChange={(event) => setEventRulesUrl(event.target.value)} placeholder="https://" /></label>
      <label>目标奖项<input value={awardTarget} maxLength={120} onChange={(event) => setAwardTarget(event.target.value)} placeholder="例如 进入复赛或获得三等奖" /></label>
    </>}
    <label>每周分钟<input type="number" min="15" max="600" value={weeklyMinutes} onChange={(event) => setWeeklyMinutes(event.target.value)} /></label>
    <label>每周次数<input type="number" min="1" max="7" value={weeklySessions} onChange={(event) => setWeeklySessions(event.target.value)} /></label>
    {error && <p className="operation-message">{error}</p>}
    <div className="review-actions"><button className="secondary-button" disabled={saving} onClick={() => setEditing(false)}>取消</button><button disabled={saving || statement.trim().length < 4} onClick={() => void save()}><Check size={16} />{saving ? '保存中' : '保存目标'}</button></div>
  </div>
}

export function BehaviorEvidencePanel({ childId }: { childId: string }): React.JSX.Element {
  const [replay, setReplay] = useState<BehaviorReplay | null>(null)
  const [baseline, setBaseline] = useState<BehaviorBaseline | null>(null)
  const [personalization, setPersonalization] = useState<BehaviorPersonalization | null>(null)
  const [habitExperiments, setHabitExperiments] = useState<HabitExperimentList | null>(null)
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [correctionStateId, setCorrectionStateId] = useState<string | null>(null)
  const [correctionNote, setCorrectionNote] = useState('')
  const [correcting, setCorrecting] = useState(false)

  const load = async (): Promise<void> => {
    setLoading(true); setMessage(null); setCorrectionStateId(null); setCorrectionNote('')
    const [replayResult, baselineResult, personalizationResult, habitsResult] = await Promise.allSettled([localApi.behaviorReplay(childId), localApi.behaviorBaseline(childId), localApi.behaviorPersonalization(childId), localApi.behaviorHabitExperiments(childId)])
    if (replayResult.status === 'fulfilled') setReplay(replayResult.value)
    if (baselineResult.status === 'fulfilled') setBaseline(baselineResult.value)
    if (personalizationResult.status === 'fulfilled') setPersonalization(personalizationResult.value)
    if (habitsResult.status === 'fulfilled') setHabitExperiments(habitsResult.value)
    if (replayResult.status === 'rejected' || baselineResult.status === 'rejected' || personalizationResult.status === 'rejected' || habitsResult.status === 'rejected') {
      const failure = replayResult.status === 'rejected' ? replayResult.reason : baselineResult.status === 'rejected' ? baselineResult.reason : personalizationResult.status === 'rejected' ? personalizationResult.reason : null
      setMessage(failure instanceof ApiError ? failure.message : '部分本地行为记录没有加载成功；已保留可用信息。')
    }
    setLoading(false)
  }

  const recentStates = replay?.states.slice(-6).reverse() ?? []
  const recentFacts = replay?.events.slice(-6).reverse() ?? []
  const recentInterventions = replay?.interventions.slice(-3).reverse() ?? []
  const retract = async (): Promise<void> => {
    if (!correctionStateId || correctionNote.trim().length < 2) return
    setCorrecting(true); setMessage(null)
    try {
      await localApi.retractBehaviorEvidence(childId, correctionStateId, correctionNote.trim())
      await load()
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '这条观察没有成功回撤，请重试。') }
    finally { setCorrecting(false) }
  }
  const resetPersonalization = async (): Promise<void> => {
    setLoading(true); setMessage(null)
    try { setPersonalization(await localApi.resetBehaviorPersonalization(childId)) }
    catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '提示偏好没有重置，请重试。') }
    finally { setLoading(false) }
  }
  const stopHabitExperiment = async (experimentId: string): Promise<void> => {
    setLoading(true); setMessage(null)
    try {
      await localApi.stopBehaviorHabitExperiment(childId, experimentId)
      setHabitExperiments(await localApi.behaviorHabitExperiments(childId))
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '小实验没有成功停止，请重试。') }
    finally { setLoading(false) }
  }
  return (
    <section className="parent-panel behavior-evidence-panel">
      <div className="panel-heading"><div><span>仅家长可见 · 本机摘要</span><h2>学习过程证据</h2></div><button onClick={() => void load()} disabled={loading}>{loading ? '正在读取' : replay ? '刷新' : '查看证据'}</button></div>
      {!replay && !loading && !message && <p>显示可观察行为、证据缺失与替代解释；不推断情绪、人格或医学状态，也不展示原始画面或音频。</p>}
      {message && <p className="operation-message">{message} <button className="secondary-button" disabled={loading} onClick={() => void load()}>重试读取</button></p>}
      {baseline && <div className="behavior-baseline">
        {baseline.status === 'ready' && baseline.switch_count_baseline
          ? <p>已观察 {baseline.days_observed} 天：任务切换个人基线为每天约 {baseline.switch_count_baseline.mean} 次（波动 {baseline.switch_count_baseline.stddev}）。这是比较参照，不是标签或诊断。</p>
          : <p>已观察 {baseline.days_observed} 天；至少 {baseline.minimum_days ?? 3} 天后才建立个人基线。在此之前只保留证据，不做趋势判断。</p>}
        {baseline.status === 'ready' && baseline.habit_trends && <div className="behavior-trend-summary"><h3>日级可观察趋势</h3><p>仅比较已记录的本地事实；缺失观察、回撤记录和自然任务差异都可能影响计数，不代表情绪、人格或因果。</p>
          {['seated_count', 'away_count', 'writing_count', 'reading_count', 'switch_count', 'stall_count', 'help_count', 'plan_deviation_count'].map((feature) => {
            const trend = baseline.habit_trends?.[feature]
            return trend?.status === 'ready' ? <span key={feature}>{behaviorFeatureLabel(feature)}：{behaviorTrendLabel(trend.direction)}（近期日均 {trend.recent_daily_mean}）</span> : null
          })}
        </div>}
      </div>}
      {personalization && <div className="behavior-personalization"><div><h3>温和提醒偏好</h3><p>{personalization.boundary}</p></div><button className="secondary-button" disabled={loading} onClick={() => void resetPersonalization()}>重置提示偏好</button>{personalization.items.length === 0 ? <p>还没有反馈样本，系统保持默认温和、先询问的提醒。</p> : personalization.items.map((item) => <article key={item.candidate_state}><strong>{behaviorStateLabel(item.candidate_state)}</strong><span>孩子反馈 {item.sample_size}/{personalization.minimum_feedback_samples} 次：接受 {item.accepted}，拒绝 {item.declined}；后续可观察结果 {item.effect_samples} 次，其中 {item.helpful_effects} 次记录到书写、阅读或任务结束候选。</span><small>{item.adjusted ? `已降低该场景的提醒频率：间隔至少 ${item.cooldown_minutes} 分钟、每天最多 ${item.daily_cap} 次，仍先询问。` : item.explanation[0]}</small></article>)}</div>}
      {habitExperiments && <div className="behavior-personalization"><div><h3>孩子同意的七天小实验</h3><p>{habitExperiments.boundary}</p></div>{habitExperiments.items.length === 0 ? <p>尚未形成跨天、可重复的候选，因此不会提出习惯实验。</p> : habitExperiments.items.map((item) => <article key={item.experiment_id}><strong>{item.title}</strong><span>{item.stop_pending ? (item.stop_retrying ? '提醒已暂停；停止暂未完成，系统会重试' : '提醒已暂停，正在完成停止') : habitExperimentStatus(item.status)}{item.outcome ? ` · 已观察${item.outcome.observed_days}天 · ${item.outcome.direction === 'improving' ? '出现改善信号' : '暂未形成清晰变化'}` : ''}</span><small>孩子行动：{item.child_action}；观察信号：{item.success_signal}</small>{item.status === 'active' && !item.stop_pending && <button className="secondary-button" disabled={loading} onClick={() => void stopHabitExperiment(item.experiment_id)}><Square size={15} />停止这个实验</button>}<BehaviorGoalReviewPanel childId={childId} experimentId={item.experiment_id} title={item.title} childAction={item.child_action} successSignal={item.success_signal} request={requestParentLocal} onChanged={async () => { setHabitExperiments(await localApi.behaviorHabitExperiments(childId)) }} /></article>)}</div>}
      {replay && <div className="behavior-evidence-grid">
        <div><h3>已观察事实</h3>{recentFacts.length === 0 ? <p>还没有可观察事件。</p> : recentFacts.map((item) => <article key={item.event_id}><strong>{behaviorFactLabel(item.event_type)}</strong><span>观察置信度 {Math.round(item.confidence * 100)}% · {formatBehaviorTime(item.occurred_at)}</span><small>{item.retracted ? `已回撤：${item.retracted.note}` : behaviorEventExplanation(item)}</small></article>)}</div>
        <div><h3>候选状态与替代解释</h3>{recentStates.length === 0 ? <p>还没有足够的可观察事件。</p> : recentStates.map((item) => <article key={item.state_id}><strong>{behaviorStateLabel(item.state)}</strong><span>{item.retracted ? '已回撤，不再用于基线或提醒' : `原始支持度 ${Math.round(item.probability * 100)}% · 当前衰减后 ${Math.round((item.effective_probability ?? item.probability) * 100)}%`} · {formatBehaviorTime(item.occurred_at)}</span><small>{item.retracted ? item.retracted.note : item.alternatives[0] ?? '需要与孩子或其他本地证据确认。'}</small>{item.evidence.source === 'local_vision' && !item.retracted && <button className="secondary-button" disabled={correcting} onClick={() => { setCorrectionStateId(item.state_id); setCorrectionNote('') }}>这条观察有误</button>}</article>)}</div>
        <div><h3>温和提醒与反馈</h3>{recentInterventions.length === 0 ? <p>最近没有满足条件的提醒；冷却和每日上限会抑制频繁打扰。</p> : recentInterventions.map((item) => <article key={item.intervention_id}><strong>{item.message}</strong><span>{item.withdrawal_pending ? (item.feedback ? '已记录孩子反馈，提醒正在撤回' : '提醒已暂停，正在完成撤回') : item.status === 'withdrawn' ? (item.withdrawal_reason === 'habit_support_source_changed' ? '提醒依据已变化，已撤回，不再等待反馈' : '已因孩子或家长纠正撤回，不再等待反馈或参与后续决策') : item.status === 'expired' ? '反馈时间已过，不会再次追问' : item.feedback === 'accepted' ? '孩子接受' : item.feedback === 'declined' ? '孩子拒绝' : '等待孩子反馈'} · {formatBehaviorTime(item.decided_at)}</span><small>{item.withdrawal_pending ? '来源已变化，提醒已暂停；如暂时无法完成，系统会自动重试。' : item.status === 'withdrawn' ? (item.withdrawal_reason === 'habit_support_source_changed' ? '提醒依据已变化；保留记录用于核对，不作为当前建议。' : '触发这条提醒的本地候选已回撤；保留记录只用于审计，不作为学习结论。') : item.status === 'expired' ? '这条提醒保留为审计记录，但不再允许反馈或参与后续决策。' : item.outcome ? `提示后的可观察结果：${behaviorStateLabel(item.outcome.state)}（${Math.round(item.outcome.probability * 100)}%）。这只记录先后顺序，不证明提醒造成变化。` : '尚未记录后续可观察结果。'}</small></article>)}</div>
      </div>}
      {replay && correctionStateId && <div className="behavior-correction"><strong>回撤错误观察</strong><p>回放会保留这条审计记录并标记为已回撤，但它不再计入个人基线、提醒或后续决策；原始媒体不会被读取或展示。</p><label>说明<textarea aria-label="回撤观察说明" value={correctionNote} maxLength={500} onChange={(event) => setCorrectionNote(event.target.value)} placeholder="例如：孩子在心算，并非卡顿" /></label><div><button className="secondary-button" disabled={correcting} onClick={() => setCorrectionStateId(null)}>取消</button><button disabled={correcting || correctionNote.trim().length < 2} onClick={() => void retract()}>{correcting ? '正在回撤' : '确认回撤并重算基线'}</button></div></div>}
    </section>
  )
}

function behaviorEventExplanation(item: { alternative_explanations?: unknown; alternatives?: unknown }): string {
  const values = item.alternatives ?? item.alternative_explanations
  return Array.isArray(values) && typeof values[0] === 'string' && values[0].trim()
    ? values[0] : '存在未覆盖的替代解释。'
}

function behaviorStateLabel(state: string): string {
  const labels: Record<string, string> = { seated: '在座', away: '离座', away_from_seat: '离座', writing: '持续书写', reading: '持续阅读', help_seeking: '主动求助', blocked_candidate: '卡顿候选', distraction_candidate: '分心候选', fatigue_candidate: '疲劳候选', frequent_switching: '频繁切换候选', task_started: '任务开始', task_finished: '任务结束', plan_deviation: '计划偏差' }
  return labels[state] ?? '待确认的可观察状态'
}

function behaviorFactLabel(eventType: string): string {
  const labels: Record<string, string> = { 'presence.seated': '记录到在座', 'presence.away': '记录到离座候选', 'work.writing': '记录到书写样活动', 'work.reading': '记录到阅读样活动', 'interaction.switch': '记录到任务切换', 'help.request': '记录到主动求助', 'work.stall': '记录到卡顿候选', 'task.started': '任务开始', 'task.finished': '任务结束', 'plan.deviation': '记录到计划偏差' }
  return labels[eventType] ?? '本地可观察事件'
}

function formatBehaviorTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间不可用' : date.toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function behaviorFeatureLabel(feature: string): string {
  return ({ seated_count: '在座', away_count: '离座候选', writing_count: '书写事件', reading_count: '阅读事件', switch_count: '任务切换', stall_count: '卡顿候选', help_count: '主动求助', plan_deviation_count: '计划偏差' } as Record<string, string>)[feature] ?? '可观察事件'
}

function behaviorTrendLabel(direction: 'increasing' | 'decreasing' | 'stable' | undefined): string {
  return direction === 'increasing' ? '近期增加' : direction === 'decreasing' ? '近期减少' : '近期稳定'
}

function habitExperimentStatus(status: string): string {
  return ({ proposed: '等待孩子语音选择', active: '进行中', completed: '已结束', declined: '孩子未接受', stopped: '已停止' } as Record<string, string>)[status] ?? '状态待确认'
}

function CurriculumCorpusPanel({ child, modelConfigured }: { child: ChildProfile; modelConfigured: boolean }): React.JSX.Element {
  const [corpora, setCorpora] = useState<CurriculumCorpus[]>([])
  const [coverage, setCoverage] = useState<Record<string, CurriculumCoverage[]>>({})
  const [standards, setStandards] = useState<CurriculumStandardSource[]>([])
  const [standardAlignment, setStandardAlignment] = useState<CurriculumStandardAlignmentSummary | null>(null)
  const [scopes, setScopes] = useState<CurriculumInternalizationScope[]>([])
  const [jobs, setJobs] = useState<CurriculumInternalizationJob[]>([])
  const [capabilities, setCapabilities] = useState<CurriculumCapability[]>([])
  const [selectedScopeKey, setSelectedScopeKey] = useState('')
  const [selection, setSelection] = useState<{ databasePath: string; transcriptRoot: string; videoRoot: string } | null>(null)
  const [query, setQuery] = useState('')
  const [subject, setSubject] = useState('')
  const [results, setResults] = useState<CurriculumSearchResult[]>([])
  const [busy, setBusy] = useState<'loading' | 'importing' | 'standards' | 'internalizing' | 'reviewing' | 'searching' | 'deleting' | null>('loading')
  const [message, setMessage] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)

  const load = async (): Promise<void> => {
    try {
      const [items, standardItems, capabilityItems] = await Promise.all([
        localApi.knowledgeCorpora(), localApi.curriculumStandards(), localApi.curriculumCapabilities(child.child_id)
      ])
      setCorpora(items)
      setStandards(standardItems)
      setCapabilities(capabilityItems)
      const entries = await Promise.all(items.filter((item) => item.status === 'ready').map(async (item) => [item.corpus_id, await localApi.knowledgeCorpusCoverage(item.corpus_id)] as const))
      setCoverage(Object.fromEntries(entries))
      const active = items.find((item) => item.status === 'ready')
      if (active) {
        const [scopeItems, jobItems, alignment] = await Promise.all([
          localApi.curriculumInternalizationScopes(active.corpus_id, child.child_id),
          localApi.curriculumInternalizationJobs(active.corpus_id, child.child_id),
          localApi.curriculumStandardAlignment(active.corpus_id)
        ])
        setScopes(scopeItems); setJobs(jobItems); setStandardAlignment(alignment)
        setSelectedScopeKey((current) => current || scopeKey(scopeItems[0]))
      } else { setScopes([]); setJobs([]); setStandardAlignment(null) }
    }
    catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '课程语料库暂时无法读取。') }
    finally { setBusy(null) }
  }
  useEffect(() => { setResults([]); setQuery(''); setMessage(null); setBusy('loading'); void load() }, [child.child_id])
  useEffect(() => {
    if (!jobs.some((item) => item.status === 'processing')) return
    const timer = window.setInterval(() => { void load() }, 1500)
    return () => window.clearInterval(timer)
  }, [jobs, child.child_id])

  const selectedScope = scopes.find((item) => scopeKey(item) === selectedScopeKey) || scopes[0]
  const scopeSubjects = Array.from(new Set(scopes.map((item) => item.subject_title)))
  const scopeSemesters = Array.from(new Set(scopes.filter((item) => item.subject_title === selectedScope?.subject_title).map((item) => item.semester)))
  const scopeUnits = scopes.filter((item) => item.subject_title === selectedScope?.subject_title && item.semester === selectedScope?.semester)
  const selectFirstScope = (predicate: (item: CurriculumInternalizationScope) => boolean): void => {
    const match = scopes.find(predicate)
    if (match) setSelectedScopeKey(scopeKey(match))
  }

  const chooseRoot = async (): Promise<void> => {
    if (!window.desktop) { setMessage('请在桌面应用中选择课程库目录。'); return }
    const selected = await window.desktop.chooseSeykKnowledgeRoot()
    if (!selected) return
    if (!selected.valid) { setSelection(null); setMessage('所选文件夹缺少 seyk_kb.db、transcripts 或 videos。'); return }
    setSelection(selected); setMessage('已识别课程数据库、转录和视频目录。')
  }
  const importCorpus = async (): Promise<void> => {
    if (!selection) return
    setBusy('importing'); setMessage(null)
    try {
      const saved = await localApi.importKnowledgeCorpus({
        title: '苏e优课四至九年级课程库', database_path: selection.databasePath,
        transcript_root: selection.transcriptRoot, video_root: selection.videoRoot,
        client_action_id: crypto.randomUUID()
      })
      setCorpora((current) => [saved, ...current.filter((item) => item.corpus_id !== saved.corpus_id)])
      setCoverage((current) => ({ ...current, [saved.corpus_id]: [] }))
      const matrix = await localApi.knowledgeCorpusCoverage(saved.corpus_id)
      setCoverage((current) => ({ ...current, [saved.corpus_id]: matrix }))
      setStandardAlignment(await localApi.curriculumStandardAlignment(saved.corpus_id))
      setMessage(`已整理 ${saved.topic_count} 个课程主题；其中 ${saved.model_drafted_topic_count} 个完成模型内化草案，${saved.verified_topic_count} 个已验证。`)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '课程库导入失败。') }
    finally { setBusy(null) }
  }
  const importStandards = async (): Promise<void> => {
    if (!window.desktop) { setMessage('请在桌面应用中选择官方课标目录。'); return }
    const selected = await window.desktop.chooseCurriculumStandardsRoot()
    if (!selected) return
    if (!selected.valid) { setMessage('所选目录缺少官方课标 manifest.json。'); return }
    setBusy('standards'); setMessage(null)
    try {
      const result = await localApi.importCurriculumStandards(selected.rootPath)
      setStandards(await localApi.curriculumStandards())
      const active = corpora.find((item) => item.status === 'ready')
      if (active) setStandardAlignment(await localApi.curriculumStandardAlignment(active.corpus_id))
      setMessage(`已校验并导入 ${result.source_count} 份官方文件，其中 ${result.page_count} 页已经完成OCR并可检索。`)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '官方课标没有导入。') }
    finally { setBusy(null) }
  }
  const startInternalization = async (): Promise<void> => {
    const corpus = corpora.find((item) => item.status === 'ready')
    if (!corpus || !selectedScope) return
    setBusy('internalizing'); setMessage(null)
    try {
      const job = await localApi.startCurriculumInternalization(corpus.corpus_id, {
        child_id: child.child_id, subject_title: selectedScope.subject_title,
        version_title: selectedScope.version_title || undefined, semester: selectedScope.semester,
        unit_title: selectedScope.unit_title, max_units: 1, client_action_id: crypto.randomUUID()
      })
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)])
      setMessage('内化任务已开始。只有通过来源校验的结构化草案才会进入审核列表。')
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '知识内化任务没有启动。') }
    finally { setBusy(null) }
  }
  const reviewCapability = async (capabilityId: string, decision: 'verify' | 'reject'): Promise<void> => {
    setBusy('reviewing'); setMessage(null)
    try {
      const saved = await localApi.reviewCurriculumCapability(capabilityId, decision, decision === 'verify' ? '家长已对照课程来源确认' : '来源或能力定义需要重新处理')
      setCapabilities((current) => current.map((item) => item.capability_id === capabilityId ? saved : item))
      setMessage(decision === 'verify' ? '已确认该能力定义；后续仍保留官方课标对齐状态。' : '已退回该草案，不会进入学习计划和诊断。')
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '审核结果没有保存。') }
    finally { setBusy(null) }
  }
  const searchCorpus = async (): Promise<void> => {
    if (query.trim().length < 1) return
    setBusy('searching'); setMessage(null)
    try {
      const items = await localApi.searchKnowledgeCorpus(child.child_id, query.trim(), subject || undefined)
      setResults(items)
      if (items.length === 0) setMessage(`没有找到适合${child.grade}的匹配课程。`)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '课程检索失败。') }
    finally { setBusy(null) }
  }
  const deleteCorpus = async (corpusId: string): Promise<void> => {
    setBusy('deleting'); setMessage(null)
    try {
      const deleted = await localApi.deleteKnowledgeCorpus(corpusId)
      setCorpora((current) => current.filter((item) => item.corpus_id !== corpusId))
      setCoverage((current) => { const next = { ...current }; delete next[corpusId]; return next })
      setResults([]); setPendingDelete(null)
      setStandardAlignment(null)
      setMessage(`已删除 ${deleted.resource_count} 条本地课程索引；原视频和转录文件未改动。`)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '课程库没有删除。') }
    finally { setBusy(null) }
  }

  return (
    <section className="curriculum-corpus-panel">
      <div className="knowledge-heading"><div><span>共享课程语料库</span><h2>江苏课程与讲解基线</h2><p>两个孩子按各自年级检索。原视频保持在本机，不上传云端。</p></div><button className="secondary-button" onClick={() => void chooseRoot()}><FolderOpen size={16} />选择课程库文件夹</button></div>
      {selection && <div className="corpus-selection"><span>已选择完整目录</span><strong>{selection.databasePath}</strong><button className="primary-button" disabled={busy !== null} onClick={() => void importCorpus()}>{busy === 'importing' ? '正在建立索引' : '导入或刷新索引'}</button></div>}
      {corpora.map((corpus) => <div className="corpus-summary" key={corpus.corpus_id}>
        <div><strong>{corpus.title}</strong><span>{corpus.resource_count}节课程 → {corpus.topic_count}个结构化主题 · 模型草案{corpus.model_drafted_topic_count} · 已验证{corpus.verified_topic_count}</span><small>{corpus.transcript_count}份转录 · {corpus.local_video_count}个本地视频 · {corpus.source_ref} · {corpus.status === 'ready' ? '结构化基线可用' : corpus.status === 'importing' ? `处理中 ${corpus.processed_count || 0}/${corpus.total_count || 0}` : '导入失败，可重新选择目录刷新'}</small></div>
        {pendingDelete === corpus.corpus_id ? <div className="corpus-delete-confirm"><span>仅删除应用索引，不删除原文件。</span><button className="danger-button" disabled={busy !== null} onClick={() => void deleteCorpus(corpus.corpus_id)}>确认删除</button><button className="secondary-button" onClick={() => setPendingDelete(null)}>取消</button></div> : <button className="icon-button" title="删除课程索引" onClick={() => setPendingDelete(corpus.corpus_id)}><Trash2 size={16} /></button>}
      </div>)}
      {corpora.filter((item) => item.status === 'ready').map((corpus) => <details className="coverage-matrix" key={`coverage-${corpus.corpus_id}`}><summary>查看年级、学科与学期覆盖矩阵</summary><div><div className="coverage-row coverage-header"><span>年级</span><span>学科</span><span>版本 / 学期</span><span>主题</span><span>内化草案</span><span>已验证</span></div>{(coverage[corpus.corpus_id] || []).map((item) => <div className="coverage-row" key={`${item.grade_title}-${item.subject_title}-${item.version_title}-${item.semester}`}><span>{item.grade_title}</span><strong>{item.subject_title}</strong><span>{item.version_title} / {item.semester}</span><span>{item.topic_count}</span><span>{item.model_drafted_topic_count}</span><span>{item.verified_topic_count}</span></div>)}</div></details>)}
      <div className="standards-workspace">
        <div className="standards-heading"><div><strong>国家课标与江苏实施依据</strong><span>国家课标定义能力骨架，江苏文件定义教材和实施边界。</span></div><button className="secondary-button" disabled={busy !== null} onClick={() => void importStandards()}><FileScan size={16} />导入官方课标</button></div>
        <div className="standards-summary"><span>官方来源 {standards.length}</span><span>OCR可检索 {standards.filter((item) => item.status !== 'downloaded').length}</span><span>已提取页 {standards.reduce((sum, item) => sum + item.extracted_page_count, 0)}</span><span>已验证要求 {standards.reduce((sum, item) => sum + item.verified_requirement_count, 0)}</span></div>
        {standardAlignment && <div className="standard-alignment-summary"><strong>课程候选对齐 {standardAlignment.candidate_topic_count}/{standardAlignment.total_topic_count}</strong><span>待解析 {standardAlignment.unresolved_topic_count} · 缺对应学科标准 {standardAlignment.no_standard_topic_count}。候选只用于内化检索，未经模型与人工复核不进入掌握判断。</span></div>}
        {standards.length > 0 && <details><summary>查看来源状态</summary><div className="standard-source-list">{standards.map((item) => <div key={item.source_id}><strong>{item.subject_title}</strong><span>{item.jurisdiction === 'CN' ? '国家' : '江苏'} · {item.version_label} · {item.status === 'downloaded' ? `待OCR（${item.page_count}页）` : `${item.extracted_page_count}/${item.page_count}页可检索`}</span></div>)}</div></details>}
      </div>
      <ManualStandardScopePanel key={child.child_id} child={child} />
      {scopes.length > 0 && <div className="internalization-workspace">
        <div className="internalization-controls"><label>学科<select value={selectedScope?.subject_title || ''} onChange={(event) => selectFirstScope((item) => item.subject_title === event.target.value)}>{scopeSubjects.map((item) => <option key={item}>{item}</option>)}</select></label><label>学期<select value={selectedScope?.semester || ''} onChange={(event) => selectFirstScope((item) => item.subject_title === selectedScope?.subject_title && item.semester === event.target.value)}>{scopeSemesters.map((item) => <option key={item}>{item}</option>)}</select></label><label>单元<select value={selectedScopeKey} onChange={(event) => setSelectedScopeKey(event.target.value)}>{scopeUnits.map((item) => <option key={scopeKey(item)} value={scopeKey(item)}>{item.unit_title}（{item.topic_count}主题）</option>)}</select></label><button className="primary-button" disabled={busy !== null || !modelConfigured} onClick={() => void startInternalization()}><RefreshCw size={16} />内化当前单元</button></div>
        {!modelConfigured && <small>先在模型设置中配置并启用云模型，课标和课程原文仍只按限长文本批次发送。</small>}
        {jobs.slice(0, 3).map((job) => <div className="internalization-job" key={job.job_id}><strong>{job.subject_title} · {job.semester}{job.unit_title ? ` · ${job.unit_title}` : ''}</strong><span>{job.status === 'processing' ? `处理中 ${job.processed_batches}/${job.total_batches}` : job.status === 'completed' ? `完成 · ${job.capability_count}个能力草案` : `未完成 · ${job.error_code || job.status}`}</span></div>)}
      </div>}
        {capabilities.filter((item) => item.maturity !== 'retired').length > 0 && <details className="capability-review"><summary>审核可执行知识能力（{capabilities.filter((item) => item.maturity === 'model_drafted').length}项待审）</summary><div>{capabilities.filter((item) => item.maturity !== 'retired').map((item) => <article key={item.capability_id}><div><span>{item.subject_title} · {item.semester} · {item.unit_title} · 置信度{Math.round(item.confidence * 100)}%</span><h3>{item.title}</h3><p>{item.learning_objective}</p><small>课程证据：{item.source_titles.join('、')}</small><small>课标依据：{item.standard_refs.length ? item.standard_refs.join('、') : '尚未对齐，不可确认为课标能力'}</small><small>常见错误：{item.misconceptions.join('；')}</small><small>掌握标准：{item.mastery_criteria.join('；')}</small></div>{item.maturity === 'model_drafted' ? <div className="capability-actions"><button className="primary-button" disabled={busy !== null || (standards.some((source) => source.subject_title === item.subject_title.replace('小学', '').replace('初中', '') && source.status !== 'downloaded') && item.standard_refs.length === 0)} onClick={() => void reviewCapability(item.capability_id, 'verify')}><Check size={15} />确认</button><button className="secondary-button" disabled={busy !== null} onClick={() => void reviewCapability(item.capability_id, 'reject')}>退回</button></div> : <span className="verified-label"><ShieldCheck size={14} />已确认</span>}</article>)}</div></details>}
      {corpora.some((item) => item.status === 'ready') && <div className="corpus-search"><label>检索课程<input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void searchCorpus() }} placeholder={`例如：${child.grade}数学知识点`} /></label><label>学科<select value={subject} onChange={(event) => setSubject(event.target.value)}><option value="">全部学科</option>{['语文', '数学', '英语', '物理', '化学', '历史', '地理', '生物', '道德与法治', '科学'].map((item) => <option key={item}>{item}</option>)}</select></label><button className="primary-button" disabled={busy !== null || query.trim().length < 1} onClick={() => void searchCorpus()}><Search size={16} />{busy === 'searching' ? '正在检索' : '检索'}</button></div>}
      {message && <p className="operation-message">{message}</p>}
      {results.length > 0 && <div className="corpus-results">{results.map((item) => <article key={`${item.corpus_id}-${item.external_resource_id}`}><div><span>{item.grade_title} · {item.subject_title} · {item.version_title || '未标版本'} {item.volume_title || ''}</span><h3>{item.title}</h3><small>{item.unit_path}{item.teacher ? ` · ${item.teacher}` : ''}</small></div><p>{item.excerpt}</p>{item.video_available && item.video_path && <button className="secondary-button" onClick={() => void window.desktop?.openLocalPath(item.video_path!)}><CirclePlay size={15} />打开本地课程</button>}</article>)}</div>}
    </section>
  )
}

function scopeKey(scope?: CurriculumInternalizationScope): string {
  return scope ? [scope.subject_title, scope.version_title, scope.semester, scope.unit_title].join('|') : ''
}

function formatLockTime(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
}

type DeviceCapabilityProfile = {
  manufacturer: string
  model: string
  hardware_version: string
  driver_version: string
  driver_provider: string
  usb_vid: string
  usb_pid: string
  capture_protocol: string
  ptz_capability: string
  audio_capability: string
  hardware_identity_hash: string
  network_identifier_hash: string
}

const documentCameraPurchase = {
  sku: '10184877516348',
  listingName: '汇博士 X90L · 4K教学展台（带麦克风/扬声器）',
  sourceUrl: 'https://item.jd.com/10184877516348.html',
  checkedAt: '2026-08-26',
}

export function DeviceSetupPanel({ initial, initialQualification, sessionActive }: { initial: DeviceBinding[]; initialQualification: DeviceQualificationSummary; sessionActive: boolean }): React.JSX.Element {
  const [bindings, setBindings] = useState(initial)
  const [qualification, setQualification] = useState(initialQualification)
  const [profile, setProfile] = useState(() => documentCameraProfile(initial))
  const [hardwareInventory, setHardwareInventory] = useState<DesktopVideoHardwareItem[]>([])
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [documentCameraId, setDocumentCameraId] = useState('')
  const [progress, setProgress] = useState<QualificationProgress | null>(null)
  const [qualifying, setQualifying] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const qualificationAbort = useRef<AbortController | null>(null)
  const documentBinding = bindings.find((item) => item.device_type === 'document_camera')

  useEffect(() => { setBindings(initial) }, [initial])
  useEffect(() => { setProfile(documentCameraProfile(initial)) }, [initial])
  useEffect(() => { setQualification(initialQualification) }, [initialQualification])
  useEffect(() => () => qualificationAbort.current?.abort(), [])

  const scan = async (): Promise<void> => {
    if (sessionActive) {
      setMessage('请先结束当前学习会话，再重新绑定高拍仪。')
      return
    }
    setBusy(true); setMessage(null)
    try {
      let inventoryItems = hardwareInventory
      let inventoryNote = ''
      if (window.desktop?.getVideoHardwareInventory) {
        try {
          const inventory = await window.desktop.getVideoHardwareInventory()
          inventoryItems = inventory.items
          setHardwareInventory(inventory.items)
          inventoryNote = inventory.source === 'windows_pnp_cim_v1'
            ? `Windows识别到${inventory.items.length}条视频硬件记录。`
            : inventory.source === 'windows_inventory_failed' ? 'Windows硬件信息读取失败，可手工核对档案。' : '当前不是Windows，设备档案需手工核对。'
        } catch { inventoryNote = 'Windows硬件信息读取失败，仍会检查可识别的高拍仪。' }
      }
      const media = navigator.mediaDevices
      if (!media?.enumerateDevices) throw new Error('MEDIA_API_UNAVAILABLE')
      const found = (await media.enumerateDevices())
        .filter(item => item.kind === 'videoinput' && item.deviceId && item.deviceId !== 'default')
      const matchedDocument = documentBinding
        ? await findBoundVideoDevice(found, documentBinding.system_id_hash, documentBinding.label, 'document_camera')
        : null
      // Only a verified binding or a named document camera can be opened.
      const candidates = found.filter(item => item.deviceId === matchedDocument?.deviceId || isNamedDocumentCamera(item))
      setDevices(candidates)
      const nextDocumentId = matchedDocument?.deviceId ?? (candidates.length === 1 ? candidates[0].deviceId : '')
      setDocumentCameraId(nextDocumentId)
      const selectedLabel = candidates.find((item) => item.deviceId === nextDocumentId)?.label
      if (selectedLabel) setProfile((current) => mergeProfileWithInventory(current, selectedLabel, inventoryItems))
      const result = candidates.length > 0
        ? `发现${candidates.length}台可选高拍仪。`
        : await window.desktop?.getCameraPermissionStatus?.().catch(() => 'unknown') === 'denied'
          ? '系统未授予视频访问权限，请在 Windows 隐私设置中检查摄像头权限后重新扫描。'
          : '未识别到可绑定的高拍仪。请检查 USB 连接和系统摄像头权限后再扫描。'
      setMessage(inventoryNote ? `${result} ${inventoryNote}` : result)
    } catch (cause) {
      const failure = mediaDeviceFailure(cause, '高拍仪')
      setMessage(cause instanceof Error && cause.message === 'MEDIA_API_UNAVAILABLE' ? '当前运行环境没有标准视频设备接口。' : failure.message)
    } finally { setBusy(false) }
  }

  const verifyAndSave = async (): Promise<void> => {
    if (!devices.some((device) => device.deviceId === documentCameraId)) return
    setBusy(true); setMessage(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { deviceId: { exact: documentCameraId }, width: { ideal: 2560 }, height: { ideal: 1440 } }
      })
      let verified: Omit<DeviceBinding, 'updated_at'>
      try {
        const track = stream.getVideoTracks()[0]
        const registration = await mediaDeviceRegistration(track)
        const label = track.label || documentBinding?.label || '高拍仪'
        verified = {
          device_type: 'document_camera',
          system_id_hash: registration.systemIdHash,
          label,
          capabilities: deviceCapabilities(registration.capabilities, mergeProfileWithInventory(profile, label, hardwareInventory), label),
          verified_at: new Date().toISOString(),
        }
      } finally {
        stream.getTracks().forEach((track) => track.stop())
      }
      const saved = await localApi.saveDeviceBindings([verified])
      setBindings(saved)
      setProfile(documentCameraProfile(saved))
      setQualification(await localApi.deviceQualification())
      setMessage('高拍仪已验证并绑定。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : mediaDeviceFailure(cause, '高拍仪').message)
    } finally { setBusy(false) }
  }

  const saveProfile = async (): Promise<void> => {
    if (!documentBinding) return
    setBusy(true); setMessage(null)
    try {
      const { device_type, system_id_hash, label, capabilities, verified_at } = documentBinding
      const saved = await localApi.saveDeviceBindings([{
        device_type, system_id_hash, label, verified_at,
        capabilities: deviceCapabilities(capabilities, profile, label),
      }])
      setBindings(saved)
      setProfile(documentCameraProfile(saved))
      setQualification(await localApi.deviceQualification())
      setMessage('高拍仪能力档案已保存。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '高拍仪能力档案未能保存。')
    } finally { setBusy(false) }
  }

  const chooseDocumentCamera = (deviceId: string): void => {
    setDocumentCameraId(deviceId)
    const label = devices.find((device) => device.deviceId === deviceId)?.label
    if (label) setProfile((current) => mergeProfileWithInventory(current, label, hardwareInventory))
  }

  const startQualification = async (): Promise<void> => {
    if (!documentBinding) return
    setQualifying(true); setMessage(null); setProgress(null)
    const controller = new AbortController()
    qualificationAbort.current = controller
    let awakeLeaseId: string | undefined
    try {
      awakeLeaseId = crypto.randomUUID()
      await window.desktop?.setDeviceTestAwake(true, awakeLeaseId)
      const run = await localApi.startDeviceQualification('document_stability')
      const result = await runVideoQualification({
        mode: 'document_stability',
        bindings: [documentBinding],
        expectedSeconds: run.expected_seconds,
        signal: controller.signal,
        onProgress: setProgress,
      })
      const completed = await localApi.finishDeviceQualification(run.run_id, result.outcome, result.metrics as unknown as Record<string, unknown>, result.failureCode, run.completion_token)
      setQualification((current) => mergeQualificationResult(current, completed))
      const completionMessage =
        completed.status === 'passed'
          ? '高拍仪稳定性验收通过并已保存。'
          : completed.status === 'cancelled' ? '设备验收已取消，本次已取得的视频轨道均已释放。' : `设备验收失败：${qualificationFailureLabel(completed.failure_code ?? result.failureCode)}`
      setMessage(completionMessage)
      try {
        setQualification(await localApi.deviceQualification())
      } catch (cause) {
        if (cause instanceof ApiError && cause.status === 401) {
          setMessage(`${completionMessage} 家长会话已到期，重新解锁可刷新其他家长数据。`)
        } else {
          throw cause
        }
      }
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '设备验收未能启动或保存结果。')
    } finally {
      if (awakeLeaseId) await window.desktop?.setDeviceTestAwake(false, awakeLeaseId).catch(() => undefined)
      qualificationAbort.current = null
      setQualifying(false)
    }
  }

  const cancelQualification = (): void => {
    qualificationAbort.current?.abort()
    setMessage('正在取消设备验收；已停止已取得的视频轨道，仍在等待原始设备请求结束并核对关闭。')
  }

  const activeQualification = qualification.recent_runs.find((run) => run.status === 'active')
  const clearActiveQualification = async (): Promise<void> => {
    if (!activeQualification) return
    setBusy(true); setMessage(null)
    try {
      await localApi.finishDeviceQualification(activeQualification.run_id, 'cancelled', { recovered_by_parent: true }, 'PARENT_RECOVERED')
      setQualification(await localApi.deviceQualification())
      setMessage('遗留设备验收已结束，可以重新开始。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '遗留设备验收未能结束。')
    } finally { setBusy(false) }
  }

  const documentResult = qualification.required.find((item) => item.mode === 'document_stability')
  const passed = documentResult?.status === 'passed'
  const profileMissing = qualification.profile_missing?.document_camera ?? []
  const disabled = busy || qualifying || Boolean(activeQualification) || sessionActive
  return (
    <section className="device-setup-panel">
      <div className="device-setup-heading"><div><h2>高拍仪设置</h2></div><button className="secondary-button" disabled={disabled} onClick={() => void scan()}><RefreshCw size={17} />扫描高拍仪</button></div>
      <div className="device-binding-grid">
        <label><FileScan size={18} /><span>高拍仪<small>{documentBinding ? `已绑定：${documentBinding.label}` : '尚未绑定'}</small></span><select aria-label="选择高拍仪" value={documentCameraId} disabled={disabled || devices.length === 0} onChange={(event) => chooseDocumentCamera(event.target.value)}><option value="">请选择高拍仪</option>{devices.map((device) => <option key={device.deviceId} value={device.deviceId}>{device.label || documentBinding?.label || '已验证的高拍仪'}</option>)}</select></label>
      </div>
      <div className="device-profile-grid">
        <DeviceCapabilityEditor profile={profile} missing={profileMissing} disabled={disabled} onChange={(key, value) => setProfile((current) => ({ ...current, [key]: value }))} />
      </div>
      <div className="device-profile-actions"><button className="secondary-button" disabled={disabled || !documentBinding} onClick={() => void saveProfile()}>保存能力档案</button><button className="primary-button" disabled={disabled || !documentCameraId} onClick={() => void verifyAndSave()}>{busy ? '正在验证' : '验证并保存高拍仪'}</button></div>
      <div className="device-qualification">
        <div className="qualification-heading"><div><h3>高拍仪稳定性验收</h3></div><b className={passed ? 'is-passed' : ''}>{passed ? '已通过' : '未完成'}</b></div>
        <div className="qualification-controls">
          <span>60 分钟</span>
          {qualifying ? <button className="danger-button" onClick={cancelQualification}><Square size={16} />取消验收</button> : activeQualification ? <button className="danger-button" disabled={busy} onClick={() => void clearActiveQualification()}><Square size={16} />结束遗留验收</button> : <button className="secondary-button" disabled={disabled || !documentBinding} onClick={() => void startQualification()}><Clock size={16} />开始验收</button>}
        </div>
        {progress && <div className="qualification-progress"><progress max={progress.expectedSeconds} value={progress.elapsedSeconds} /><span>{formatDuration(progress.elapsedSeconds)} / {formatDuration(progress.expectedSeconds)}</span></div>}
        {documentResult?.completed_at && <div className="qualification-gates"><div><span>最近验收</span><b className={passed ? 'is-passed' : 'is-failed'}>{qualificationStatusLabel(documentResult.status)}</b><small>{new Date(documentResult.completed_at).toLocaleString()}</small></div></div>}
        {qualification.recent_runs.length > 0 && <div className="qualification-history"><strong>历史验收记录</strong>{qualification.recent_runs.slice(0, 4).map((run) => <article key={run.run_id}><span>{qualificationModeLabel(run.mode)} · {qualificationStatusLabel(run.status)} · {new Date(run.completed_at ?? run.started_at).toLocaleString()}</span><small>{qualificationEvidenceLabel(run)}</small></article>)}</div>}
      </div>
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

function DeviceCapabilityEditor({ profile, missing, disabled, onChange }: {
  profile: DeviceCapabilityProfile
  missing: string[]
  disabled: boolean
  onChange: (key: keyof DeviceCapabilityProfile, value: string) => void
}): React.JSX.Element {
  const roleName = '高拍仪'
  const purchase = documentCameraPurchase
  return (
    <article>
      <div><strong>{roleName}能力档案</strong><span>{profile.hardware_identity_hash ? '已记录脱敏Windows硬件身份' : '系统硬件身份待匹配'}</span></div>
      <p className="device-purchase-reference">采购页预期：{purchase.listingName} · SKU {purchase.sku}。只用于到货核对，能力以铭牌和Windows实测为准。</p>
      <div className="device-profile-fields">
        <label>厂商<input aria-label={`${roleName}厂商`} value={profile.manufacturer} disabled={disabled} maxLength={120} onChange={(event) => onChange('manufacturer', event.target.value)} /></label>
        <label>型号<input aria-label={`${roleName}型号`} value={profile.model} disabled={disabled} maxLength={120} onChange={(event) => onChange('model', event.target.value)} /></label>
        <label>硬件版本<input aria-label={`${roleName}硬件版本`} value={profile.hardware_version} disabled={disabled} maxLength={80} placeholder="选填" onChange={(event) => onChange('hardware_version', event.target.value)} /></label>
        <label>驱动版本<input aria-label={`${roleName}驱动版本`} value={profile.driver_version} disabled={disabled} maxLength={120} onChange={(event) => onChange('driver_version', event.target.value)} /></label>
        <label>USB VID<input aria-label={`${roleName} USB VID`} value={profile.usb_vid} disabled={disabled} maxLength={4} onChange={(event) => onChange('usb_vid', event.target.value.replace(/[^0-9a-f]/gi, '').toUpperCase())} /></label>
        <label>USB PID<input aria-label={`${roleName} USB PID`} value={profile.usb_pid} disabled={disabled} maxLength={4} onChange={(event) => onChange('usb_pid', event.target.value.replace(/[^0-9a-f]/gi, '').toUpperCase())} /></label>
        <label>接入协议<select aria-label={`${roleName}接入协议`} value={profile.capture_protocol} disabled={disabled} onChange={(event) => onChange('capture_protocol', event.target.value)}><option value="unverified">未验证</option><option value="uvc_media_foundation">UVC / 系统摄像头接口</option><option value="local_rtsp">本地RTSP</option><option value="onvif">ONVIF</option><option value="vendor_sdk">厂商本地SDK</option><option value="vendor_file_export">厂商软件本地导出</option><option value="manual_import">人工文件导入</option></select></label>
        <label>PTZ结论<select aria-label={`${roleName} PTZ结论`} value={profile.ptz_capability} disabled={disabled} onChange={(event) => onChange('ptz_capability', event.target.value)}><option value="unverified">未验证</option><option value="browser_control_verified">应用内控制已实测</option><option value="onvif_verified">ONVIF控制已实测</option><option value="vendor_sdk_verified">厂商SDK已实测</option><option value="device_auto_tracking_only">设备自动跟踪，应用不可控</option><option value="vendor_app_only">仅厂商App可控</option><option value="fixed_no_control">固定视角/无控制</option></select></label>
        <label>音频结论<select aria-label={`${roleName}音频结论`} value={profile.audio_capability} disabled={disabled} onChange={(event) => onChange('audio_capability', event.target.value)}><option value="unverified">未验证</option><option value="built_in_verified">内置麦克风已实测</option><option value="separate_device">使用独立麦克风</option><option value="none_verified">已确认无音频</option></select></label>
      </div>
      <small className={missing.length ? 'is-missing' : 'is-complete'}>{missing.length ? `待核对：${missing.map(profileFieldLabel).join('、')}` : '档案完整，稳定性结果仍需单独运行。'}</small>
    </article>
  )
}

function documentCameraProfile(bindings: DeviceBinding[]): DeviceCapabilityProfile {
  const capabilities = bindings.find((binding) => binding.device_type === 'document_camera')?.capabilities ?? {}
  const value = (key: string): string => typeof capabilities[key] === 'string' ? capabilities[key] as string : ''
  return {
    manufacturer: value('manufacturer'), model: value('model'), hardware_version: value('hardware_version'),
    driver_version: value('driver_version'), driver_provider: value('driver_provider'),
    usb_vid: value('usb_vid'), usb_pid: value('usb_pid'), capture_protocol: value('capture_protocol') || 'unverified',
    ptz_capability: value('ptz_capability') || 'unverified', audio_capability: value('audio_capability') || 'unverified',
    hardware_identity_hash: value('hardware_identity_hash'), network_identifier_hash: value('network_identifier_hash'),
  }
}

function mergeProfileWithInventory(profile: DeviceCapabilityProfile, label: string, inventory: DesktopVideoHardwareItem[]): DeviceCapabilityProfile {
  const match = matchVideoHardwareInventory(label, inventory)
  if (!match) return profile.model ? profile : { ...profile, model: label }
  const item = match.item
  return {
    ...profile,
    manufacturer: profile.manufacturer || item.manufacturer || '',
    model: profile.model || item.name || label,
    driver_version: profile.driver_version || item.driver_version || '',
    driver_provider: profile.driver_provider || item.driver_provider || '',
    usb_vid: profile.usb_vid || item.usb_vid || '',
    usb_pid: profile.usb_pid || item.usb_pid || '',
    capture_protocol: profile.capture_protocol === 'unverified' && item.transport_hint === 'usb' ? 'uvc_media_foundation' : profile.capture_protocol,
    hardware_identity_hash: item.instance_id_hash,
    network_identifier_hash: item.transport_hint === 'usb' ? profile.network_identifier_hash : item.instance_id_hash,
  }
}

function deviceCapabilities(existing: Record<string, unknown>, profile: DeviceCapabilityProfile, label: string): Record<string, unknown> {
  const purchase = documentCameraPurchase
  return {
    ...existing,
    role: 'document_camera',
    system_enumeration_name: existing.system_enumeration_name ?? label,
    manufacturer: profile.manufacturer.trim(), model: profile.model.trim() || label,
    hardware_version: profile.hardware_version.trim(), driver_version: profile.driver_version.trim(),
    driver_provider: profile.driver_provider.trim(), usb_vid: profile.usb_vid.trim().toUpperCase(), usb_pid: profile.usb_pid.trim().toUpperCase(),
    capture_protocol: profile.capture_protocol, ptz_capability: profile.ptz_capability, audio_capability: profile.audio_capability,
    hardware_identity_hash: profile.hardware_identity_hash,
    network_identifier_hash: profile.network_identifier_hash,
    purchase_sku: purchase.sku,
    purchase_listing_name: purchase.listingName,
    purchase_source_url: purchase.sourceUrl,
    purchase_listing_checked_at: purchase.checkedAt,
    capability_profile_version: 'device_capability_profile_v1',
  }
}

function profileFieldLabel(value: string): string {
  return ({ manufacturer: '厂商', model: '型号', driver_version: '驱动版本', capture_protocol: '接入协议', ptz_capability: 'PTZ结论', audio_capability: '音频结论', hardware_identity: 'VID/PID或脱敏网络身份' } as Record<string, string>)[value] ?? value
}

function qualificationModeLabel(mode: DeviceQualificationMode): string {
  return ({ quick_dual: '双路快速检查', dual_concurrency: '双路并发30分钟', camera_stability: '观察摄像头60分钟', document_stability: '高拍仪60分钟' } as const)[mode]
}

function qualificationStatusLabel(status: string): string {
  return ({ not_run: '未运行', active: '进行中', passed: '通过', failed: '失败', cancelled: '已取消', interrupted: '意外中断' } as Record<string, string>)[status] ?? status
}

export function mergeQualificationResult(
  current: DeviceQualificationSummary,
  completed: DeviceQualificationRun,
): DeviceQualificationSummary {
  const required = current.required.map((item) => item.mode === completed.mode
    ? { ...item, status: completed.status, completed_at: completed.completed_at }
    : item)
  return {
    ...current,
    required,
    acceptance_status: current.profiles_ready && required.every((item) => item.status === 'passed') ? 'passed' : 'pending',
    recent_runs: [completed, ...current.recent_runs.filter((item) => item.run_id !== completed.run_id)].slice(0, 10),
  }
}

function qualificationFailureLabel(code?: string): string {
  return ({ DEVICE_DISCONNECTED: '设备连接中断', FRAME_STALLED: '画面长时间未推进', TEST_INCOMPLETE: '测试未达到规定时长', NO_FRAME_SAMPLES: '没有获得画面采样', INSUFFICIENT_FRAME_SAMPLES: '有效采样未达到规定时长的98%', INVALID_FRAME_METRICS: '画面计数不一致', DEVICE_CAPABILITIES_INCOMPLETE: '缺少实际分辨率或FPS证据', DEVICE_NOT_BOUND: '设备尚未绑定', BOUND_DEVICE_NOT_FOUND: '找不到已绑定设备', DEVICE_IDENTITY_MISMATCH: '设备身份与绑定不一致', DEVICE_ACQUISITION_PENDING: '上次设备开启请求尚未结束，请等待关闭确认后再开始验收', MEDIA_PERMISSION_DENIED: '摄像头权限被拒绝', DEVICE_BUSY_OR_UNAVAILABLE: '设备被占用或不可用', USER_CANCELLED: '家长取消' } as Record<string, string>)[code ?? ''] ?? '请检查设备连接、权限和占用情况'
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
}

function TodayOverview({ today, selectedChildId, busy, onSelectChild, onRetryReport }: { today: ParentToday; selectedChildId: string; busy: boolean; onSelectChild: (childId: string) => void; onRetryReport: (childId: string) => Promise<void> }): React.JSX.Element {
  return (
    <section className="today-overview">
      <div className="today-heading"><span>今日概览</span><h2>两个孩子的学习进度</h2></div>
      <div className="today-grid">
        {today.items.map((item) => {
          const failureCount = item.failures.pending_reports + item.failures.model_failures_last_7_days
          return (
            <article className={selectedChildId === item.child.child_id ? 'is-active' : ''} key={item.child.child_id}>
              <button className="today-card-main" onClick={() => onSelectChild(item.child.child_id)}>
                <div><strong>{item.child.nickname}</strong><span>{item.child.grade} · {item.session ? stateTitleForParent(item.session.state) : '今日尚未开始'}</span></div>
                <b>{item.plan ? `${item.plan.completed}/${item.plan.total}项完成` : '暂无计划'}</b>
                <p>{item.current_task ? `当前：${item.current_task.subject} · ${item.current_task.title}` : item.latest_report?.headline ?? '完成学习后将在这里生成摘要。'}</p>
                <small>{item.due_reviews}项到期复测{item.plan?.deferred ? ` · ${item.plan.deferred}项延期` : ''}{failureCount ? ` · ${failureCount}项系统待处理` : ' · 系统无待处理失败'}</small>
              </button>
              {item.failures.pending_reports > 0 && <button className="secondary-button today-retry" disabled={busy} onClick={() => void onRetryReport(item.child.child_id)}>重试日报</button>}
            </article>
          )
        })}
      </div>
    </section>
  )
}

type SafetyAlertsProps = { childId: string; events: ChildSafetyEvent[]; onEventsChange: (events: ChildSafetyEvent[]) => void } & ParentWorkFocusProps
export function SafetyAlertsPanel(props: SafetyAlertsProps): React.JSX.Element {
  return <SafetyAlertsForChild key={props.childId} {...props} />
}
function SafetyAlertsForChild({ childId, events, onEventsChange, focusRequest, onFocusResolved }: SafetyAlertsProps): React.JSX.Element | null {
  const root = useRef<HTMLElement>(null)
  const alive = useRef(true)
  const eventsRef = useRef(events)
  eventsRef.current = events
  const [focusedEvent, setFocusedEvent] = useState<ChildSafetyEvent | null>(null)
  const [focusReadNonce, setFocusReadNonce] = useState('')
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useParentWorkFocus(focusRequest, !focusRequest || focusReadNonce === focusRequest.nonce, root, onFocusResolved)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => {
    if (!focusRequest) return
    let current = true
    setFocusReadNonce(''); setMessage(null)
    void localApi.safetyEvent(childId, focusRequest.objectId).then(event => {
      if (!current) return
      if (event.child_id !== childId || event.safety_event_id !== focusRequest.objectId) throw new Error('SAFETY_EVENT_SCOPE_MISMATCH')
      setFocusedEvent(event); setFocusReadNonce(focusRequest.nonce)
    }).catch(() => {
      if (!current) return
      const error = '这条安全提醒未能读取，请刷新后重试，未打开其他提醒。'
      setMessage(error); onFocusResolved?.(focusRequest.nonce, error)
    })
    return () => { current = false }
  }, [childId, focusRequest?.nonce, focusRequest?.objectId, onFocusResolved])
  const visible = events.filter(event => event.child_id === childId && event.safety_event_id !== focusedEvent?.safety_event_id)
  if (focusedEvent?.child_id === childId) visible.push(focusedEvent)
  const pending = visible.filter((event) => event.status === 'pending')
  const acknowledge = async (event: ChildSafetyEvent): Promise<void> => {
    setBusyId(event.safety_event_id); setMessage(null)
    try {
      const updated = await localApi.acknowledgeSafetyEvent(event.safety_event_id)
      if (!alive.current) return
      if (updated.child_id !== childId || updated.safety_event_id !== event.safety_event_id) throw new Error('SAFETY_EVENT_SCOPE_MISMATCH')
      setFocusedEvent(current => current?.safety_event_id === updated.safety_event_id ? updated : current)
      onEventsChange([...eventsRef.current.filter(item => item.safety_event_id !== updated.safety_event_id), updated])
      setMessage(updated.risk_level === 'R3' ? '已记录家长接手。请先在线下确认孩子此刻安全。' : '已记录家长看到提醒；请按上面的步骤线下沟通。')
    } catch (cause) {
      if (!alive.current) return
      setMessage(cause instanceof ApiError ? cause.message : '确认状态没有保存，请重试。')
    } finally { if (alive.current) setBusyId(null) }
  }
  if (visible.length === 0 && !focusRequest && !message) return null
  return (
    <section ref={root} className={`safety-alerts ${pending.some((event) => event.risk_level === 'R3') ? 'has-urgent' : ''}`} aria-label="孩子安全提醒">
      <div className="safety-alerts-heading"><div><span>本地安全提醒</span><h2>{pending.length ? `${pending.length}项需要家长线下确认` : '近期提醒已确认'}</h2></div><ShieldAlert size={24} /></div>
      <p>这里只显示风险分类和行动建议，不展示孩子原话，也不代表系统已自动联系学校、专业机构或紧急服务。</p>
      <div className="safety-alert-list">
        {visible.map((event) => (
          <article data-work-object={event.safety_event_id} className={`safety-alert is-${event.risk_level.toLowerCase()} is-${event.status}`} key={event.safety_event_id}>
            <header><div><b>{event.risk_level === 'R3' ? '立即确认现实安全' : event.risk_level === 'R2' ? '今天及时沟通' : '今晚低压力关注'}</b><span>{event.child_nickname} · {new Date(event.created_at).toLocaleString()}</span></div><strong>{event.status === 'pending' ? '待家长确认' : '家长已确认'}</strong></header>
            <p>{event.guidance.summary}</p>
            {event.status === 'pending' && <>
              <div className="safety-guidance-grid">
                <div><h3>可以直接这样说</h3><ol>{event.guidance.script.map((line) => <li key={line}>{line}</li>)}</ol></div>
                <div><h3>{event.guidance.action.what}</h3><ol>{event.guidance.action.steps.map((step) => <li key={step}>{step}</li>)}</ol><small>{event.guidance.action.stop_condition}</small></div>
              </div>
              <details><summary>避免事项与替代表达</summary>{event.guidance.avoid.map((item) => <p key={item.do_not_say}><b>不要说：</b>{item.do_not_say}<br /><b>可以说：</b>{item.say_instead}</p>)}</details>
              <button className={event.risk_level === 'R3' ? 'danger-button' : 'primary-button'} disabled={busyId === event.safety_event_id} onClick={() => void acknowledge(event)}>{busyId === event.safety_event_id ? '正在保存' : event.risk_level === 'R3' ? '我已接手，先确认孩子安全' : '我已看到，会线下沟通'}</button>
            </>}
            {event.acknowledged_at && <small>确认时间：{new Date(event.acknowledged_at).toLocaleString()}</small>}
          </article>
        ))}
      </div>
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

function stateTitleForParent(state: string): string {
  return ({ check_in: '报到中', classroom_recall: '课堂复盘中', homework_capture: '确认作业中', plan_review: '确认计划中', study: '学习中', break: '休息中', reflection: '复盘中', evolving: '整理记录中', completed: '已完成' } as Record<string, string>)[state] ?? '进行中'
}

function ChildProfilePanel({ child, onSaved }: { child: ChildProfile; onSaved: (child: ChildProfile) => void }): React.JSX.Element {
  const [nickname, setNickname] = useState(child.nickname)
  const [region, setRegion] = useState(child.curriculum_region)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => { setNickname(child.nickname); setRegion(child.curriculum_region); setMessage(null) }, [child.child_id, child.nickname, child.curriculum_region])
  const save = async (): Promise<void> => {
    setBusy(true); setMessage(null)
    try {
      const saved = await localApi.updateChildProfile(child.child_id, { nickname: nickname.trim(), curriculum_region: region.trim() })
      onSaved(saved)
      setMessage('孩子档案已保存。年级关联学习路径，当前版本不允许在这里随意修改。')
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '孩子档案没有保存。') } finally { setBusy(false) }
  }
  return (
    <section className="child-profile-panel">
      <div><span>孩子档案</span><h2>称呼与学习地区</h2><p>年级：{child.grade}。两个孩子的数据、报告和知识状态始终隔离。</p></div>
      <label>孩子称呼<input maxLength={20} value={nickname} onChange={(event) => setNickname(event.target.value)} /></label>
      <label>所在地区<input maxLength={40} value={region} onChange={(event) => setRegion(event.target.value)} /></label>
      <button className="primary-button" disabled={busy || nickname.trim().length < 1 || region.trim().length < 2} onClick={() => void save()}>{busy ? '正在保存' : '保存档案'}</button>
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

function KnowledgeSourcesPanel({ child }: { child: ChildProfile }): React.JSX.Element {
  const [revisionSource, setRevisionSource] = useState('')
  const [revisionContent, setRevisionContent] = useState('')
  const [sources, setSources] = useState<KnowledgeSource[]>([])
  const [sourceType, setSourceType] = useState<KnowledgeSource['source_type']>('textbook_index')
  const [title, setTitle] = useState('')
  const [subject, setSubject] = useState('数学')
  const [publisher, setPublisher] = useState('')
  const [versionLabel, setVersionLabel] = useState('')
  const [sourceRef, setSourceRef] = useState('')
  const [effectiveFrom, setEffectiveFrom] = useState('')
  const [effectiveTo, setEffectiveTo] = useState('')
  const [licenseStatus, setLicenseStatus] = useState<KnowledgeSource['license_status']>('family_owned_index')
  const [parentReviewed, setParentReviewed] = useState(false)
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    setSources([]); setMessage(null); setRevisionSource(''); setRevisionContent('')
    void localApi.knowledgeSources(child.child_id)
      .then(setSources)
      .catch((cause) => setMessage(cause instanceof ApiError ? cause.message : '知识资料暂时无法读取。'))
  }, [child.child_id])

  const importTextFile = async (file: File | undefined): Promise<void> => {
    if (!file) return
    if (file.size > 2_000_000) { setMessage('请选择2MB以内的纯文本、Markdown或CSV文件。'); return }
    setContent(await file.text())
    if (!title) setTitle(file.name.replace(/\.[^.]+$/, ''))
  }
  const save = async (): Promise<void> => {
    if (title.trim().length < 2 || content.trim().length < 2) return
    setBusy(true); setMessage(null)
    try {
      const saved = await localApi.importKnowledgeSource(child.child_id, {
        source_type: sourceType, title: title.trim(), subject, grade_band: child.grade,
        publisher: publisher.trim(), version_label: versionLabel.trim(), source_ref: sourceRef.trim(),
        effective_from: effectiveFrom || undefined, effective_to: effectiveTo || undefined,
        license_status: licenseStatus, parent_reviewed: parentReviewed, content_text: content.trim()
      })
      setSources((current) => [saved, ...current.filter((item) => item.source_id !== saved.source_id)])
      setTitle(''); setContent(''); setSourceRef(''); setParentReviewed(false)
      setMessage(saved.review_status === 'approved' ? '资料已导入并可用于低置信候选。' : '资料已进入隔离区，不会用于讲题或确定性推断。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '资料没有导入。')
      if (cause instanceof ApiError && cause.code === 'KNOWLEDGE_SOURCE_REVISION_REQUIRED'
        && typeof cause.details?.source_id === 'string') {
        setRevisionSource(cause.details.source_id); setRevisionContent(content.trim())
      }
    } finally { setBusy(false) }
  }

  return (
    <section className="knowledge-sources-panel">
      <div className="knowledge-heading"><div><span>家庭知识库</span><h2>教材目录与课程摘要</h2><p>只导入目录、页码索引或家庭摘要，不批量复制商业教材正文。</p></div><label className="secondary-button file-input-button">导入文本<input type="file" accept=".txt,.md,.csv,text/plain,text/csv" onChange={(event) => void importTextFile(event.target.files?.[0])} /></label></div>
      <div className="knowledge-fields">
        <label>资料类型<select value={sourceType} onChange={(event) => setSourceType(event.target.value as KnowledgeSource['source_type'])}><option value="textbook_index">教材目录</option><option value="curriculum">课程标准摘要</option><option value="school_material">学校资料索引</option><option value="family_note">家庭学习笔记</option></select></label>
        <label>标题<input value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        <label>学科<select value={subject} onChange={(event) => setSubject(event.target.value)}>{['语文', '数学', '英语', '物理', '化学', '其他'].map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>出版或来源<input value={publisher} onChange={(event) => setPublisher(event.target.value)} /></label>
        <label>版本<input value={versionLabel} onChange={(event) => setVersionLabel(event.target.value)} /></label>
        <label>来源引用<input value={sourceRef} onChange={(event) => setSourceRef(event.target.value)} placeholder="书名/URL/文件名" /></label>
        <label>生效日期<input type="date" value={effectiveFrom} onChange={(event) => setEffectiveFrom(event.target.value)} /></label>
        <label>失效日期<input type="date" min={effectiveFrom || undefined} value={effectiveTo} onChange={(event) => setEffectiveTo(event.target.value)} /></label>
        <label>许可状态<select value={licenseStatus} onChange={(event) => setLicenseStatus(event.target.value as KnowledgeSource['license_status'])}><option value="family_owned_index">家庭拥有的教材索引</option><option value="public">公开可用</option><option value="private_note">家庭原创笔记</option><option value="unknown">来源不明</option></select></label>
        <label className="toggle-row"><input type="checkbox" checked={parentReviewed} onChange={() => setParentReviewed((value) => !value)} /><span>我已核对来源和内容</span></label>
      </div>
      <textarea aria-label="知识资料内容" value={content} onChange={(event) => setContent(event.target.value)} placeholder="每行一个章节、知识点或页码索引。" />
      <button className="primary-button" disabled={busy || title.trim().length < 2 || content.trim().length < 2} onClick={() => void save()}>{busy ? '正在导入' : '导入知识库'}</button>
      {message && <p className="operation-message">{message}</p>}
      <KnowledgeSourceRevisionPanel key={child.child_id} childId={child.child_id}
        initialSourceId={revisionSource} initialContent={revisionContent}
        onChanged={async () => setSources(await localApi.knowledgeSources(child.child_id))} />
      {sources.length > 0 && <div className="knowledge-source-list">{sources.map((source) => <div key={source.source_id}><strong>{source.title}</strong><span>{source.subject} · {source.version_label || '未标版本'} · {source.effective_from ? `${source.effective_from}起` : '未标生效期'}{source.effective_to ? `至${source.effective_to}` : ''} · {source.excerpt_count}条 · {source.review_status === 'superseded' ? '历史旧稿，已被新版本替代' : source.review_status === 'approved' ? '已审核' : '隔离区'}</span></div>)}</div>}
    </section>
  )
}

export function DesktopSettingsPanel(): React.JSX.Element {
  const [enabled, setEnabled] = useState(false)
  const [ready, setReady] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => {
    if (!window.desktop) return
    void window.desktop.getAutoLaunch()
      .then((settings) => {
        setEnabled(settings.openAtLogin)
        setMessage(settings.openAtLogin ? null : '当前未启用自动启动。可以直接修复。')
        setReady(true)
      })
      .catch(() => { setMessage('无法读取自动启动状态。请关闭并重新打开应用后重试。'); setReady(true) })
  }, [])
  const setAutoLaunch = async (nextEnabled: boolean): Promise<void> => {
    if (!window.desktop) return
    setReady(false); setMessage(null)
    try {
      const settings = await window.desktop.setAutoLaunch(nextEnabled)
      setEnabled(settings.openAtLogin)
      setMessage(settings.openAtLogin
        ? '自动启动已修复并启用。'
        : nextEnabled ? 'Windows 尚未确认自动启动设置，请重启应用后再次修复。' : '已关闭登录后自动启动。')
    } catch { setMessage('自动启动设置没有保存。请重启应用后再试。') } finally { setReady(true) }
  }
  return (
    <section className="desktop-settings-panel">
      <div><span>Windows 运行</span><h2>回家后自动准备</h2><p>安装版首次运行默认开启，家长可以随时关闭。</p></div>
      <label className="toggle-row"><input type="checkbox" checked={enabled} disabled={!ready || !window.desktop} onChange={() => void setAutoLaunch(!enabled)} /><span>登录 Windows 后自动启动</span></label>
      {!enabled && window.desktop && <button className="secondary-button" disabled={!ready} onClick={() => void setAutoLaunch(true)}>修复自动启动</button>}
      {message && <p>{message}</p>}
    </section>
  )
}

export function SensorReleaseVerificationPanel({ initial, platformEligible }: { initial: SensorReleaseReview[]; platformEligible: boolean }): React.JSX.Element {
  const [reviews, setReviews] = useState(initial)
  const [selectedSessionId, setSelectedSessionId] = useState(() => initial.find((item) => !item.sensor_release.os_release_verified)?.session_id ?? initial[0]?.session_id ?? '')
  const [deviceManagerHealthy, setDeviceManagerHealthy] = useState(false)
  const [windowsPrivacyClear, setWindowsPrivacyClear] = useState(false)
  const [physicalIndicatorStatus, setPhysicalIndicatorStatus] = useState<'clear' | 'not_available' | 'still_on'>('clear')
  const [reopenedDeviceTypes, setReopenedDeviceTypes] = useState<Array<'camera' | 'document_camera' | 'microphone'>>([])
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const selected = reviews.find((item) => item.session_id === selectedSessionId) ?? reviews[0] ?? null
  const verification = selected?.sensor_release.release_verification
  const usedDeviceTypes = selected
    ? (Object.keys(selected.sensor_release.by_device) as Array<'camera' | 'document_camera' | 'microphone'>)
    : []

  useEffect(() => {
    setReviews(initial)
    setSelectedSessionId((current) => initial.some((item) => item.session_id === current)
      ? current
      : initial.find((item) => !item.sensor_release.os_release_verified)?.session_id ?? initial[0]?.session_id ?? '')
  }, [initial])

  useEffect(() => {
    setDeviceManagerHealthy(verification?.device_manager_healthy ?? false)
    setWindowsPrivacyClear(verification?.windows_privacy_indicator_clear ?? false)
    setPhysicalIndicatorStatus(verification?.physical_indicator_status ?? 'clear')
    setReopenedDeviceTypes(verification?.reopened_device_types ?? [])
    setNotes(verification?.notes ?? '')
    setMessage(null)
  }, [selected?.session_id])

  const toggleReopened = (deviceType: 'camera' | 'document_camera' | 'microphone'): void => {
    setReopenedDeviceTypes((current) => current.includes(deviceType)
      ? current.filter((item) => item !== deviceType)
      : [...current, deviceType])
  }

  const save = async (): Promise<void> => {
    if (!selected) return
    setBusy(true); setMessage(null)
    try {
      const saved = await localApi.recordSensorReleaseVerification(selected.session_id, {
        reopened_device_types: reopenedDeviceTypes,
        device_manager_healthy: deviceManagerHealthy,
        windows_privacy_indicator_clear: windowsPrivacyClear,
        physical_indicator_status: physicalIndicatorStatus,
        notes,
      })
      setReviews((current) => current.map((item) => item.session_id === selected.session_id ? {
        ...item,
        sensor_release: {
          ...item.sensor_release,
          release_verification: saved,
          os_release_verified: saved.result === 'passed',
          evidence_level: saved.result === 'passed' ? 'windows_session_verified' : 'application_record_only',
        }
      } : item))
      setMessage(saved.result === 'passed'
        ? '当次会话的 Windows 传感器释放已复核通过。'
        : '本次复核未通过，检查项已保存；排除占用后请再次复核。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '传感器释放复核没有保存。')
    } finally { setBusy(false) }
  }

  return (
    <section className="sensor-release-verification">
      <div className="release-verification-heading">
        <div><span>Windows 隐私释放</span><h2>按学习会话复核传感器</h2><p>应用记录为零只是第一层证据；系统级通过必须在 Windows 和其他应用中实测。</p></div>
        <ShieldCheck size={24} />
      </div>
      {reviews.length === 0 ? (
        <p className="release-review-empty">完成一次使用高拍仪或麦克风的学习后，这里会出现待复核记录。</p>
      ) : (
        <>
          <div className="release-review-list" role="tablist" aria-label="选择传感器释放复核会话">
            {reviews.slice(0, 8).map((item) => (
              <button className={item.session_id === selected?.session_id ? 'is-active' : ''} key={item.session_id} onClick={() => setSelectedSessionId(item.session_id)}>
                <span>{item.child_nickname} · {new Date(item.ended_at).toLocaleString()}</span>
                <strong>{item.sensor_release.os_release_verified ? '系统释放已验证' : item.sensor_release.release_verification ? '最近复核未通过' : '待系统级复核'}</strong>
              </button>
            ))}
          </div>
          {selected && <div className="release-review-form">
            <div className="release-review-summary">
              <div><span>当次使用</span><strong>{usedDeviceTypes.map(deviceTypeLabel).join('、')}</strong></div>
              <div><span>应用租约</span><strong>{selected.sensor_release.active_leases === 0 ? `已释放 ${selected.sensor_release.released_leases}/${selected.sensor_release.opened_leases}` : `仍活动 ${selected.sensor_release.active_leases}`}</strong></div>
              <div><span>最近系统复核</span><strong>{verification ? `${verification.result === 'passed' ? '通过' : '未通过'} · ${new Date(verification.verified_at).toLocaleString()}` : '尚未复核'}</strong></div>
            </div>
            <div className="release-checks">
              <label><input type="checkbox" checked={deviceManagerHealthy} onChange={() => setDeviceManagerHealthy((value) => !value)} /><span>Windows 设备管理器显示设备正常、无异常状态</span></label>
              <label><input type="checkbox" checked={windowsPrivacyClear} onChange={() => setWindowsPrivacyClear((value) => !value)} /><span>Windows 隐私使用指示已停止</span></label>
              {usedDeviceTypes.map((deviceType) => <label key={deviceType}><input type="checkbox" checked={reopenedDeviceTypes.includes(deviceType)} onChange={() => toggleReopened(deviceType)} /><span>已在其他应用重新打开{deviceTypeLabel(deviceType)}</span></label>)}
            </div>
            <div className="release-review-notes">
              <label>物理指示灯<select value={physicalIndicatorStatus} onChange={(event) => setPhysicalIndicatorStatus(event.target.value as 'clear' | 'not_available' | 'still_on')}><option value="clear">已熄灭</option><option value="not_available">设备无物理指示灯</option><option value="still_on">仍然亮起</option></select></label>
              <label>备注（选填）<textarea maxLength={500} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="例如：已在 Windows 相机和录音机中重新打开" /></label>
              <button className="primary-button" disabled={busy || selected.sensor_release.active_leases > 0 || !platformEligible} onClick={() => void save()}>{busy ? '正在保存' : '记录本次复核'}</button>
            </div>
          </div>}
        </>
      )}
      {!platformEligible && <p className="operation-message">当前不是目标 Windows 安装版，可查看流程，但不能登记系统级通过。</p>}
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

export function OperationsHealthPanel({ initial, onHealthChange, onReturnToChild }: { initial: SystemHealth; onHealthChange: (health: SystemHealth) => void; onReturnToChild: () => void }): React.JSX.Element {
  const [health, setHealth] = useState(initial)
  const [diagnostics, setDiagnostics] = useState<DesktopDiagnosticSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => { setHealth(initial) }, [initial])
  useEffect(() => {
    void window.desktop?.getDiagnosticSummary?.().then(setDiagnostics).catch(() => undefined)
  }, [])

  const refresh = async (): Promise<void> => {
    setBusy(true)
    setMessage(null)
    try {
      const updated = await localApi.systemHealth()
      setHealth(updated)
      onHealthChange(updated)
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '运行状态暂时无法刷新。')
    } finally {
      setBusy(false)
    }
  }
  const retryMaintenance = async (): Promise<void> => {
    setBusy(true)
    setMessage(null)
    try {
      await localApi.runMaintenance()
      const updated = await localApi.systemHealth()
      setHealth(updated)
      onHealthChange(updated)
      setMessage('本地维护任务已重新执行。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '本地维护任务重试失败。')
    } finally {
      setBusy(false)
    }
  }
  const retryDeletion = async (deletionId: string): Promise<void> => {
    setBusy(true)
    setMessage(null)
    try {
      await localApi.retryDeletion(deletionId)
      const updated = await localApi.systemHealth()
      setHealth(updated)
      onHealthChange(updated)
      setMessage('删除任务已完成并通过残留验证。')
    } catch (cause) {
      const failureMessage = cause instanceof ApiError ? cause.message : '删除任务仍未完成，可检查文件占用后再试。'
      try {
        const updated = await localApi.systemHealth()
        setHealth(updated)
        onHealthChange(updated)
      } catch {
        // Keep the actionable deletion error when health refresh also fails.
      }
      setMessage(failureMessage)
    } finally {
      setBusy(false)
    }
  }
  const revealDiagnostics = async (): Promise<void> => {
    setMessage(null)
    try {
      if (!window.desktop?.revealDiagnosticFolder) throw new Error('当前运行时不支持打开诊断目录。')
      await window.desktop.revealDiagnosticFolder()
      const updated = await window.desktop.getDiagnosticSummary?.()
      if (updated) setDiagnostics(updated)
      setMessage('已打开本机诊断目录。运行日志和崩溃转储可用于定位问题。')
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '本机诊断目录无法打开。')
    }
  }
  const deviceTypes = ['document_camera', 'microphone'] as const
  const openIncidents = health.device_incidents.filter((item) => item.status === 'open' && item.device_type !== 'camera')
  return (
    <section className="operations-health">
      <div className="operations-heading">
        <div><span>设备与故障</span><h2>当前运行状态</h2></div>
        <button className="icon-button" title="刷新运行状态" disabled={busy} onClick={() => void refresh()}><RefreshCw size={18} /></button>
      </div>
      <div className="operations-device-list">
        {deviceTypes.map((deviceType) => {
          const device = health.devices.find((item) => item.device_type === deviceType)
          const incident = openIncidents.find((item) => item.device_type === deviceType)
          return (
            <div className={incident ? 'has-incident' : ''} key={deviceType}>
              <MonitorCog size={18} />
              <div><strong>{deviceTypeLabel(deviceType)}</strong><span>{incident ? incident.message : device?.application_reading ? '应用正在本地读取' : device?.last_seen_at ? `${device.binding_label ? `已绑定：${device.binding_label}；` : ''}最近验证 ${new Date(device.last_seen_at).toLocaleString()}` : '尚未在本机验证'}</span></div>
              <b>{incident ? '待处理' : device?.application_reading ? '使用中' : device ? '已就绪' : '未验证'}</b>
            </div>
          )
        })}
      </div>
      {(health.observation_quality?.length ?? 0) > 0 && (
        <div className="observation-quality-panel">
          <h3>历史观察质量记录</h3>
          {health.observation_quality!.map((item) => (
            <div key={`${item.child_nickname}-${item.session_id ?? 'quality'}`} className={item.qualification_status === 'qualified' ? 'is-usable' : 'is-unusable'}>
              <div><strong>{item.child_nickname}</strong><span>{item.latest_reason}</span></div>
              <b>{observationQualificationLabel(item.qualification_status, item.usable_sample_count, item.sample_count)}</b>
              <small>{new Date(item.latest_observed_at).toLocaleString()}</small>
              {item.correction_count_7d ? <small>近7天曾收到{item.correction_count_7d}次孩子或家长纠正。</small> : null}
              {item.auto_shadowed ? <small>曾因纠正自动暂停提醒和长期行为推断。</small> : null}
            </div>
          ))}
          <p>此处保留过去的质量与纠正记录。持续观察已停用。</p>
        </div>
      )}
      {health.database_upgrade && (
        <div className={`operations-upgrade ${health.database_upgrade.status === 'restored_after_failure' ? 'is-recovered' : ''}`}>
          <DatabaseBackup size={18} />
          <div>
            <strong>数据库升级 v{health.database_upgrade.source_schema_version ?? '?'} → v{health.database_upgrade.target_schema_version ?? '?'}</strong>
            <span>{health.database_upgrade.status === 'completed' ? '升级完成，升级前恢复点已保留。' : health.database_upgrade.status === 'restored_after_failure' ? '升级失败后已自动还原原数据库，应用没有继续使用半迁移数据。' : '升级恢复点已建立，状态仍需复核。'}</span>
          </div>
          <b>{health.database_upgrade.status === 'completed' ? '已保护' : health.database_upgrade.status === 'restored_after_failure' ? '已还原' : '待复核'}</b>
        </div>
      )}
      {diagnostics && (
        <div className="operations-diagnostics">
          <FolderOpen size={18} />
          <div>
            <strong>本地问题记录</strong>
            <span>运行日志 {formatDiagnosticBytes(diagnostics.currentLogBytes)} · 已轮转 {diagnostics.rotatedLogCount}/{diagnostics.maxRotatedFiles} 份 · 崩溃转储仅留在本机</span>
            <small>学习、错题、观察、Agent 回合和能力调度保存在本地数据库；新诊断事件仅记录筛选后的运行信息，不自动上传。{!diagnostics.existingLogsVerified && '已有历史日志尚未完成内容审核。'}崩溃转储尚未检查，分享前需核对其中的敏感信息。</small>
          </div>
          <button className="secondary-button" onClick={() => void revealDiagnostics()}><FolderOpen size={16} />打开诊断目录</button>
        </div>
      )}
      {(openIncidents.length > 0 || health.recent_maintenance_failures.length > 0 || health.recent_model_failures.length > 0 || health.recent_report_failures.length > 0 || health.recent_agent_failures.length > 0 || (health.recent_controlled_game_failures?.length ?? 0) > 0 || (health.recent_content_generation_failures?.length ?? 0) > 0 || health.active_controlled_games?.some((item) => item.overdue) || health.recent_deletion_failures.length > 0 || health.pending_agent_turns > 0 || health.pending_deletion_jobs > 0) && (
        <div className="operations-failures">
          <h3>待处理与最近失败</h3>
          {openIncidents.map((item, index) => <p key={`${item.device_type}-${item.occurred_at}-${index}`}><b>{item.child_nickname} · {deviceTypeLabel(item.device_type)}：</b>{item.message}<small>{new Date(item.occurred_at).toLocaleString()}</small></p>)}
          {health.recent_maintenance_failures.map((item) => <p key={`${item.job_type}-${item.run_date}`}><b>{item.job_type === 'daily_backup' ? '自动备份' : '数据清理'}：</b>{item.error_message ?? item.error_code ?? '执行失败'}<small>{item.completed_at ? new Date(item.completed_at).toLocaleString() : item.run_date}</small></p>)}
          {health.recent_model_failures.slice(0, 3).map((item, index) => <p key={`${item.created_at}-${index}`}><b>模型调用：</b>{item.purpose} · {item.provider}/{item.model} · {item.error_code ?? '未知错误'}<small>{new Date(item.created_at).toLocaleString()}</small></p>)}
          {health.recent_report_failures.slice(0, 3).map((item, index) => <p key={`${item.updated_at}-${index}`}><b>{item.child_nickname ? `${item.child_nickname} · ` : ''}{item.report_type === 'weekly' ? '周报' : item.report_type === 'monthly' ? '月报' : '日报'}生成：</b>{item.last_error ?? '失败'} · 已尝试{item.attempts}次<small>{new Date(item.updated_at).toLocaleString()}</small></p>)}
          {health.pending_agent_turns > 0 && <p><b>语音工具：</b>{health.pending_agent_turns}次交互尚未收到执行结果，可能在操作中断时退出。</p>}
          {(health.pending_content_generation_jobs ?? 0) > 0 && <p><b>学习内容准备：</b>{health.pending_content_generation_jobs}个变式或专项题任务正在后台处理。</p>}
          {health.recent_agent_failures.slice(0, 3).map((item, index) => <p key={`${item.created_at}-${index}`}><b>{item.child_nickname} · 语音工具：</b>{item.intent} · {item.tool_error_code ?? '执行失败'}<small>{new Date(item.created_at).toLocaleString()}</small></p>)}
          {(health.active_controlled_games ?? []).filter((item) => item.overdue).map((item) => <p key={item.game_session_id}><b>{item.child_nickname} · 受控游戏：</b>{item.title}已超过结束时间，需要核对结束回执；继续学习仍需孩子明确确认。<small>{new Date(item.ends_at).toLocaleString()}</small></p>)}
          {(health.recent_controlled_game_failures ?? []).slice(0, 3).map((item, index) => <p key={`${item.updated_at}-${index}`}><b>{item.child_nickname} · 受控游戏：</b>{item.title} · {item.status === 'interrupted' ? '应用中断，需核对活动结束和学习恢复状态' : item.end_reason ?? '启动或运行失败'}<small>{new Date(item.updated_at).toLocaleString()}</small></p>)}
          {(health.recent_content_generation_failures ?? []).slice(0, 3).map((item, index) => <p key={`${item.updated_at}-${index}`}><b>{item.source}：</b>{item.reference} · {item.error_code === 'MODEL_NOT_CONFIGURED' ? '模型尚未配置，任务已保留' : item.error_code ?? '生成失败'} · 已尝试{item.attempt_count}次<small>{new Date(item.updated_at).toLocaleString()}</small></p>)}
          {health.pending_deletion_jobs > 0 && <p><b>隐私删除：</b>{health.pending_deletion_jobs}个任务正在处理，可刷新查看结果。</p>}
          {health.recent_deletion_failures.map((item) => (
            <div className="operation-retry-row" key={item.deletion_id}>
              <p><b>{item.child_nickname} · {item.historical ? '历史删除记录' : '隐私删除'}：</b>{item.error_message ?? '未通过完整性验证'}{!item.historical && item.remaining_file_count > 0 ? `（剩余${item.remaining_file_count}个本地文件）` : ''}<small>{new Date(item.updated_at).toLocaleString()}</small></p>
              {item.retry_allowed !== false && <button className="secondary-button" disabled={busy} onClick={() => void retryDeletion(item.deletion_id)}>重试并验证删除</button>}
            </div>
          ))}
          {health.recent_maintenance_failures.length > 0 && <button className="secondary-button" disabled={busy} onClick={() => void retryMaintenance()}>立即重试本地维护</button>}
          {openIncidents.length > 0 && <button className="secondary-button" onClick={onReturnToChild}>返回孩子端重连</button>}
        </div>
      )}
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

function formatDiagnosticBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function deviceTypeLabel(value: string): string {
  return ({ camera: '观察摄像头', document_camera: '高拍仪', microphone: '麦克风' } as Record<string, string>)[value] ?? '采集设备'
}

type ObservationQualityHealth = NonNullable<SystemHealth['observation_quality']>[number]

function observationQualificationLabel(status: ObservationQualityHealth['qualification_status'], usable: number, sampleCount: number): string {
  if (status === 'qualified') return '当时已通过取景与座位基线确认'
  if (status === 'qualifying') return '当时尚未完成取景确认'
  if (status === 'paused') return '孩子当时已暂停观察'
  if (status === 'unavailable') return '当时画面不可用'
  if (status === 'unqualified') return '当时取景或座位基线未通过'
  return `${usable}/${sampleCount}个状态样本可用`
}

function FamilyRulesPanel({ childId, initial }: { childId: string; initial: FamilyRule }): React.JSX.Element {
  const [rule, setRule] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => { setRule(initial); setMessage(null) }, [childId, initial])

  const setNumber = (key: 'max_learning_minutes' | 'task_segment_minutes' | 'break_minutes' | 'pet_min_minutes' | 'competition_minutes', value: string): void => {
    setRule((current) => ({ ...current, [key]: Number(value) }))
  }
  const save = async (): Promise<void> => {
    setBusy(true); setMessage(null)
    try {
      const saved = await localApi.updateFamilyRule(childId, {
        latest_end_time: rule.latest_end_time || null,
        max_learning_minutes: rule.max_learning_minutes,
        task_segment_minutes: rule.task_segment_minutes,
        break_minutes: rule.break_minutes,
        pet_min_minutes: rule.pet_min_minutes,
        competition_minutes: rule.competition_minutes,
        observation_mode: rule.observation_mode,
        reminder_budget_per_10m: rule.reminder_budget_per_10m,
        misclassification_shadow_threshold: rule.misclassification_shadow_threshold
      })
      setRule(saved)
      setMessage('家庭时间和长期目标规则已保存，下一份计划将使用新版本。')
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '家庭规则没有保存。') } finally { setBusy(false) }
  }

  return (
    <section className="family-rules-panel">
      <div><span>家庭硬约束</span><h2>学习时间与长期目标</h2><p>最晚结束时间可留空；设置后，计划不会越过该时间。</p></div>
      <div className="family-rule-fields">
        <label>最晚结束<input type="time" value={rule.latest_end_time ?? ''} onChange={(event) => setRule((current) => ({ ...current, latest_end_time: event.target.value || null }))} /></label>
        <label>最长学习<input type="number" min="30" max="240" value={rule.max_learning_minutes} onChange={(event) => setNumber('max_learning_minutes', event.target.value)} /></label>
        <label>单段上限<input type="number" min="10" max="60" value={rule.task_segment_minutes} onChange={(event) => setNumber('task_segment_minutes', event.target.value)} /></label>
        <label>休息时长<input type="number" min="3" max="20" value={rule.break_minutes} onChange={(event) => setNumber('break_minutes', event.target.value)} /></label>
        <label>PET保留<input type="number" min="0" max="45" value={rule.pet_min_minutes} onChange={(event) => setNumber('pet_min_minutes', event.target.value)} /></label>
        <label>数学思维<input type="number" min="0" max="60" value={rule.competition_minutes} onChange={(event) => setNumber('competition_minutes', event.target.value)} /></label>
      </div>
      <button className="primary-button" disabled={busy} onClick={() => void save()}>{busy ? '正在保存' : '保存规则'}</button>
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

export function PeriodReportsPanel({ childId, initial, onGenerated, onOpenSupply }: { childId: string; initial: PeriodReport[]; onGenerated?: (report: PeriodReport) => void; onOpenSupply?: (rootId: string) => Promise<void> }): React.JSX.Element {
  const [reports, setReports] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { setReports(initial); setError(null) }, [childId, initial])
  const generate = async (type: 'weekly' | 'monthly'): Promise<void> => {
    setBusy(true); setError(null)
    try {
      const report = await localApi.generatePeriodReport(childId, type)
      setReports((current) => [report, ...current.filter((item) => item.period_report_id !== report.period_report_id)])
      onGenerated?.(report)
    } catch (cause) { setError(cause instanceof ApiError ? cause.message : '阶段报告生成失败。') } finally { setBusy(false) }
  }
  return (
    <section className="period-reports">
      <div className="period-heading"><div><span>自动阶段报告</span><h2>周报与月报</h2></div><div><button className="secondary-button" disabled={busy} onClick={() => void generate('weekly')}><RefreshCw size={16} />刷新本周</button><button className="secondary-button" disabled={busy} onClick={() => void generate('monthly')}><RefreshCw size={16} />刷新本月</button></div></div>
      {error && <div className="tutor-error">{error}</div>}
      {reports.length === 0 ? <p className="muted-copy">当前周期尚无学习报告。</p> : (
        <div className="period-list">{reports.map((report) => <article key={report.period_report_id}><span>{report.report_type === 'weekly' ? '周报' : '月报'} · {report.period_start} 至 {report.period_end} · 截至 {report.content.as_of_date}</span><h3>{report.content.headline}</h3><p>{report.content.interpretation}</p>{report.content.goal_progress?.length ? <div><strong>长期目标执行证据</strong><ul>{report.content.goal_progress.map((goal) => <li key={goal.track}>{goal.title}：{goal.completed_minutes}/{goal.target_minutes}分钟，{goal.completed_sessions}/{goal.target_sessions}次，来自{goal.evidence_items}项已完成任务</li>)}</ul></div> : null}<small>{report.content.coverage.evidence_count}条证据 · {report.content.coverage.recorded_days}天 · {report.content.coverage.recorded_sessions}次会话</small><small>最近刷新 {new Date(report.updated_at).toLocaleString()}</small><PeriodSupplyFollowup key={`period-supply-${childId}-${report.period_report_id}`} childId={childId} periodReportId={report.period_report_id} onOpenSupply={onOpenSupply}/></article>)}</div>
      )}
    </section>
  )
}

export function PrivacyOperations({ childId, initial, health, onHealthChange }: { childId: string; initial: PrivacyPreferences; health: SystemHealth; onHealthChange: (health: SystemHealth) => void }): React.JSX.Element {
  const [cleanupPending, setCleanupPending] = useState(() => {
    try { return readPrivacyIntent() !== null } catch { return true }
  })
  const [cleanupBusy, setCleanupBusy] = useState(false)
  const [continuationBusy,setContinuationBusy]=useState(false)
  const continuationBusyOwner=useRef<symbol|null>(null)
  const recoveryBusy=cleanupBusy||continuationBusy
  const [cleanupMessage, setCleanupMessage] = useState<string | null>(null)
  const [canRelease, setCanRelease] = useState(false)
  const [canFinalize, setCanFinalize] = useState(false)
  const [operationBusy, setOperationBusy] = useState(false)
  type KnowledgeContinuation={raw:string;child:string;authority:number;epoch:number}
  const [knowledgeContinuation,setKnowledgeContinuation]=useState<KnowledgeContinuation|null>(null)
  const [knowledgeConfirmation,setKnowledgeConfirmation]=useState('')
  const continuationOwner=useRef({child:childId,epoch:0,mounted:true})
  // Every child transition permanently retires all earlier confirmations, including A→B→A.
  if(continuationOwner.current.child!==childId){continuationOwner.current.epoch++;continuationOwner.current.child=childId}
  const continuationOwnerCurrent=(proof:KnowledgeContinuation):boolean=>continuationOwner.current.mounted&&proof.child===continuationOwner.current.child
    &&proof.epoch===continuationOwner.current.epoch&&proof.authority===parentDraftAuthorityVersion()
  const continuationCurrent=(proof:KnowledgeContinuation):boolean=>{
    try{return continuationOwnerCurrent(proof)&&JSON.stringify(readPrivacyIntent())===proof.raw}catch{return false}
  }
  useEffect(()=>{
    const owner=continuationOwner.current;owner.mounted=true;owner.epoch++
    const invalidate=():void=>{owner.epoch++;setKnowledgeContinuation(null);setKnowledgeConfirmation('')}
    window.addEventListener(parentDraftAuthorityEvent,invalidate);window.addEventListener('hsc-runtime-restarted',invalidate)
    return()=>{owner.mounted=false;owner.epoch++;window.removeEventListener(parentDraftAuthorityEvent,invalidate);window.removeEventListener('hsc-runtime-restarted',invalidate)}
  },[])
  useEffect(()=>{
    setKnowledgeContinuation(null);setKnowledgeConfirmation('')
    let intent
    try{intent=readPrivacyIntent()}catch{return}
    if(!intent||intent.kind!=='delete'||intent.scope!=='knowledge'||intent.childId!==childId)return
    const proof:KnowledgeContinuation={raw:JSON.stringify(intent),child:childId,authority:parentDraftAuthorityVersion(),epoch:continuationOwner.current.epoch}
    // Discovery is read-only and independent of the cleanup promise. StrictMode's
    // replacement owner starts fresh reads; it never revalidates an old confirmation.
    void(async()=>{
      try{await localApi.privacyOperationReceipt(intent.requestId)}catch(error){
        if(!(error instanceof ApiError)||error.status!==404||!continuationCurrent(proof))return
        try{
          const actual=await localApi.privacyOperationGeneration()
          if(continuationCurrent(proof)&&actual.generation===intent.generation){setKnowledgeContinuation(proof);setKnowledgeConfirmation('')}
        }catch{/* no verified missing receipt/generation, no continuation offered */}
      }
    })()
  },[childId])
  const operation = useRef<symbol | null>(null)
  const visualInvalidation = useRef<symbol | null>(null)
  const committedCleanup = useRef<{ token: symbol; kind: 'restore' | 'delete'; run: () => Promise<void> } | null>(null)
  const beginPrivacyOperation = (): symbol | null => {
    if (operation.current) return null
    const token = Symbol('privacy-operation')
    operation.current = token
    visualInvalidation.current = beginParentVisualInvalidation()
    setOperationBusy(true)
    return token
  }
  const releasePrivacyOperation = (token: symbol): void => {
    if (operation.current === token && !committedCleanup.current) {
      operation.current = null
      if (visualInvalidation.current) { endParentVisualInvalidation(visualInvalidation.current); visualInvalidation.current = null }
      setOperationBusy(false)
    }
  }
  const defaultCleanupTail = async (kind: 'restore' | 'delete'): Promise<void> => {
    if (kind === 'delete') onHealthChange(await localApi.systemHealth())
  }
  const finishCommittedCleanup = async (token: symbol, kind: 'restore' | 'delete', run = () => defaultCleanupTail(kind), current:()=>boolean=()=>true): Promise<void> => {
    if (operation.current !== token) throw new Error('PRIVACY_OPERATION_IDENTITY_MISMATCH')
    const retrying = committedCleanup.current?.run === run
    committedCleanup.current = { token, kind, run }
    setCleanupPending(true); setCleanupBusy(true); setCanRelease(false); setCanFinalize(false)
    try {
      const intent = readPrivacyIntent()
      if (!intent) throw new Error('PRIVACY_OPERATION_IDENTITY_MISSING')
      await recoverPrivacyCleanup(intent, async requestId => {
        if(!current())throw Error('PRIVACY_OPERATION_AUTHORITY_CHANGED')
        const receipt=await localApi.privacyOperationReceipt(requestId)
        if(!current())throw Error('PRIVACY_OPERATION_AUTHORITY_CHANGED')
        setCanRelease(canReleasePrivacyIntent(intent, receipt))
        setCanFinalize(canFinalizePrivacyIntent(intent, receipt))
        return receipt
      }, async()=>{if(!current())throw Error('PRIVACY_OPERATION_AUTHORITY_CHANGED');await run();if(!current())throw Error('PRIVACY_OPERATION_AUTHORITY_CHANGED')})
      if(!current())return
      committedCleanup.current = null
      setCleanupPending(false)
      setCleanupMessage(retrying ? '原操作的本机清理已经完成，没有再次恢复或删除数据库。' : null)
      if (kind === 'restore') window.location.reload()
    } catch (cause) {
      if(!current())return
      setCleanupMessage(cause instanceof PrivacyCleanupPendingError
        ? (kind === 'restore' ? '备份已经恢复，但本机清理或状态刷新尚未完成。请重试本机清理，不要重复恢复备份。'
          : '数据已经删除，但本机清理或状态刷新尚未完成。请重试本机清理，不要重复删除。')
        : '原隐私操作的提交状态或本机清理尚未核对完成。原请求已保留，请重试核对原操作，不要重复恢复或删除。')
    } finally { setCleanupBusy(false) }
  }
  const continueOriginalKnowledgeDeletion=async():Promise<void>=>{
    const proof=knowledgeContinuation,pending=committedCleanup.current
    if(!proof||!pending||recoveryBusy||continuationBusyOwner.current!==null||knowledgeConfirmation!=='确认删除'||!continuationCurrent(proof))return
    const intent=readPrivacyIntent()
    if(!intent||intent.kind!=='delete'||intent.scope!=='knowledge'||intent.childId!==proof.child)return
    const current=()=>continuationCurrent(proof)
    const busyToken=Symbol('knowledge-continuation');continuationBusyOwner.current=busyToken
    setContinuationBusy(true);setKnowledgeConfirmation('')
    try{
      let missing=false
      try{await localApi.privacyOperationReceipt(intent.requestId)}catch(error){if(error instanceof ApiError&&error.status===404)missing=true;else throw error}
      if(!current()||!missing)throw Error('PRIVACY_OPERATION_RECEIPT_CHANGED')
      const actual=await localApi.privacyOperationGeneration()
      if(!current()||actual.generation!==intent.generation)throw Error('PRIVACY_OPERATION_GENERATION_CHANGED')
      await localApi.deleteChildData(intent.childId,'knowledge','确认删除',operationIdentity(intent))
      if(!current())return
      const cleanupCurrent=():boolean=>{
        try{const remaining=readPrivacyIntent();return continuationOwnerCurrent(proof)&&(remaining===null||JSON.stringify(remaining)===proof.raw)}catch{return false}
      }
      await finishCommittedCleanup(pending.token,'delete',pending.run,cleanupCurrent)
      if(cleanupCurrent()&&readPrivacyIntent()===null)setKnowledgeContinuation(null)
    }catch{
      if(current())setCleanupMessage('原学习资料删除尚未核对完成；原请求身份和本机记录保留。没有创建新的删除请求，请重新核对原操作。')
    }finally{
      // An expired confirmation still releases its own waiting state; it cannot
      // release a different continuation's token or revive its retired proof.
      if(continuationBusyOwner.current===busyToken){continuationBusyOwner.current=null;setContinuationBusy(false)}
      releasePrivacyOperation(pending.token)
    }
  }
  const retryCommittedCleanup = async (): Promise<void> => {
    const pending = committedCleanup.current
    if (pending && !recoveryBusy&&continuationBusyOwner.current===null) {
      try {
        setCleanupBusy(true)
        const intent=readPrivacyIntent()
        if(!intent)throw new Error('PRIVACY_OPERATION_IDENTITY_MISSING')
        await localApi.recoverNativePrivacyOperation(intent.requestId)
        await finishCommittedCleanup(pending.token, pending.kind, pending.run)
      }
      catch {setCleanupMessage('本机资源或原操作尚未核对完成，原请求仍保留。没有再次删除或恢复。')}
      finally {setCleanupBusy(false);releasePrivacyOperation(pending.token)}
    }
  }
  const openOriginalRestoreRecovery = async (): Promise<void> => {
    const pending = committedCleanup.current
    if (!pending || pending.kind !== 'restore' || recoveryBusy || continuationBusyOwner.current !== null) return
    let intent
    try { intent = readPrivacyIntent() } catch { return }
    if (!intent || !['restore', 'portable_restore'].includes(intent.kind)) return
    const proof: KnowledgeContinuation = { raw: JSON.stringify(intent), child: childId,
      authority: parentDraftAuthorityVersion(), epoch: continuationOwner.current.epoch }
    const current = (): boolean => continuationCurrent(proof) && committedCleanup.current === pending && operation.current === pending.token
    if (!current()) return
    const owner = Symbol('original-restore-recovery-entry')
    continuationBusyOwner.current = owner; setContinuationBusy(true)
    try {
      const status = await localApi.parentRestoreRecoveryHealth()
      if (!current()) return
      if (status.status !== 'recovery_only' || status.normal_business_ready !== false) throw Error('ORIGINAL_RESTORE_RECOVERY_UNCONFIRMED')
      if (!requestParentDraftNavigation()) {
        if (current()) setCleanupMessage('当前草稿尚未确认保护，暂不离开。请在教材编辑中保存或核对草稿，确认保护后再点此按钮。')
        return
      }
      if (!current()) return
      setCleanupMessage('正在进入原恢复核对。若草稿保护阻止离开，请先保存或核对教材草稿，再重试此按钮。')
      // Normal reload keeps the intent and PDR intact. App.bootstrap selects
      // the existing recovery surface, whose original PIN/review/restart owns
      // all further authority; this button never releases or retries restore.
      window.location.reload()
    } catch {
      if (current()) setCleanupMessage('尚未确认原恢复核对页面，当前资料与草稿均保留。请重试核对，不要再次恢复备份。')
    } finally {
      if (continuationBusyOwner.current === owner) {
        continuationBusyOwner.current = null
        if (continuationOwner.current.mounted) setContinuationBusy(false)
      }
    }
  }
  const acknowledgeUnfinished = async (): Promise<void> => {
    const pending = committedCleanup.current
    if (!pending || !canRelease || recoveryBusy||continuationBusyOwner.current!==null) return
    setCleanupBusy(true)
    try {
      const intent = readPrivacyIntent()
      if (!intent) throw new Error('PRIVACY_OPERATION_IDENTITY_MISSING')
      const receipt = await localApi.privacyOperationReceipt(intent.requestId)
      releaseUnappliedPrivacyIntent(intent, receipt)
      committedCleanup.current = null
      setCleanupPending(false); setCanRelease(false)
      setCleanupMessage('已结束本次核对，本机原记录全部保留；没有再次恢复、删除或补报成功。')
      releasePrivacyOperation(pending.token)
    } catch { setCleanupMessage('原操作状态发生变化，未结束核对，也未清理本机记录。请重新核对。') }
    finally { setCleanupBusy(false) }
  }
  const finalizeOriginalDeletion = async (): Promise<void> => {
    const pending = committedCleanup.current
    if (!pending || !canFinalize || recoveryBusy||continuationBusyOwner.current!==null) return
    setCleanupBusy(true)
    try {
      const intent = readPrivacyIntent()
      if (!intent) throw new Error('PRIVACY_OPERATION_IDENTITY_MISSING')
      const receipt = await localApi.privacyOperationReceipt(intent.requestId)
      if (canFinalizePrivacyIntent(intent, receipt)) await localApi.finalizePrivacyOperation(intent.requestId)
      else if (receipt.status !== 'committed') throw new Error('PRIVACY_OPERATION_FINALIZATION_CHANGED')
      await finishCommittedCleanup(pending.token, 'delete', pending.run)
    } catch { setCleanupMessage('原删除收尾尚未核对完成。没有再次删除数据，请重试核验原操作。') }
    finally { setCleanupBusy(false); releasePrivacyOperation(pending.token) }
  }
  useEffect(() => {
    let intent
    try { intent = readPrivacyIntent() }
    catch { setCleanupMessage('本机隐私恢复记录无法核对。未清理任何记录，也未重复恢复或删除。'); return }
    if (!intent) return
    const token = beginPrivacyOperation()
    if (!token) return
    const kind = intent.kind === 'delete' ? 'delete' : 'restore'
    void finishCommittedCleanup(token, kind).finally(() => releasePrivacyOperation(token))
    // One mounted owner resumes the stored identity, never the current child's operation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  let restoreRecoveryPending = false
  try { restoreRecoveryPending = cleanupPending && ['restore', 'portable_restore'].includes(readPrivacyIntent()?.kind ?? '') } catch { /* Unknown intent grants no recovery navigation. */ }
  return <>
    <ChildPrivacyOperations key={childId} childId={childId} initial={initial} health={health} onHealthChange={onHealthChange}
      cleanupPending={cleanupPending || operationBusy} finishCommittedCleanup={finishCommittedCleanup}
      beginPrivacyOperation={beginPrivacyOperation} releasePrivacyOperation={releasePrivacyOperation} />
    {cleanupMessage && <p role="status" className="operation-message">{cleanupMessage}</p>}
    {cleanupPending && <button className="secondary-button" disabled={recoveryBusy} onClick={() => void retryCommittedCleanup()}><RotateCcw size={17} />重试本机清理</button>}
    {knowledgeContinuation&&continuationCurrent(knowledgeContinuation)&&<div className="operation-message">
      <p>原学习资料删除还没有服务回执。下面会继续同一份删除请求，不是仅清理本机记录；不会创建新的删除请求。请再次确认。</p>
      <label>继续原学习资料删除确认<input value={knowledgeConfirmation} disabled={recoveryBusy} onChange={event=>setKnowledgeConfirmation(event.target.value)} placeholder="输入确认删除"/></label>
      <button className="danger-button" disabled={recoveryBusy||knowledgeConfirmation!=='确认删除'} onClick={()=>void continueOriginalKnowledgeDeletion()}>继续原学习资料删除</button>
    </div>}
    {restoreRecoveryPending && <div className="operation-message">
      <p>原恢复尚未完成核对。资料与当前草稿会保留；进入核对页面后，请使用原家长 PIN 核对原操作，再按提示重启本地服务。不会再次恢复备份。</p>
      <button className="secondary-button" disabled={recoveryBusy || continuationBusyOwner.current !== null || committedCleanup.current?.kind !== 'restore'} onClick={() => void openOriginalRestoreRecovery()}>核对原恢复，保留当前草稿</button>
    </div>}
    {canRelease && <button className="secondary-button" disabled={recoveryBusy} onClick={() => void acknowledgeUnfinished()}>保留本机记录，结束本次核对</button>}
    {canFinalize && <button className="secondary-button" disabled={recoveryBusy} onClick={() => void finalizeOriginalDeletion()}>核验原删除收尾</button>}
  </>
}

const restoreProtectionNoticeKey = 'hsc.restore-protection-notice.v1'
const restoreProtectionNotice = '恢复已完成。恢复前的登记资料不完整，本次使用了内部数据库保护，没有可用的完整恢复前撤销点。'
function rememberRestoreProtection(result: { recovery_point_backup_id: string | null; restore_protection?: 'internal_database_only' }): boolean {
  // Display-only finite flag across the existing post-restore reload. No IDs,
  // paths, permission or operation authority are persisted in this notice.
  const internal = result.recovery_point_backup_id === null && result.restore_protection === 'internal_database_only'
  try {
    if (internal) sessionStorage.setItem(restoreProtectionNoticeKey, 'internal_database_only')
    else if (typeof result.recovery_point_backup_id === 'string') sessionStorage.removeItem(restoreProtectionNoticeKey)
  } catch { /* Display-only persistence must not affect committed cleanup. */ }
  return internal
}

function ChildPrivacyOperations({ childId, initial, health, onHealthChange, cleanupPending, finishCommittedCleanup, beginPrivacyOperation, releasePrivacyOperation }: {
  childId: string; initial: PrivacyPreferences; health: SystemHealth; onHealthChange: (health: SystemHealth) => void;
  cleanupPending: boolean; finishCommittedCleanup: (token: symbol, kind: 'restore' | 'delete', run?: () => Promise<void>) => Promise<void>;
  beginPrivacyOperation: () => symbol | null; releasePrivacyOperation: (token: symbol) => void
}): React.JSX.Element {
  const captureOwner = useRef({ child: childId, epoch: 0, mounted: true })
  if (captureOwner.current.child !== childId) { captureOwner.current.child = childId; captureOwner.current.epoch++ }
  useEffect(() => {
    const owner = captureOwner.current; owner.mounted = true; owner.epoch++
    const invalidate = (): void => { owner.epoch++ }
    window.addEventListener(parentDraftAuthorityEvent, invalidate)
    window.addEventListener('hsc-runtime-restarted', invalidate)
    return () => { owner.mounted = false; owner.epoch++; window.removeEventListener(parentDraftAuthorityEvent, invalidate); window.removeEventListener('hsc-runtime-restarted', invalidate) }
  }, [])
  const [protectionNotice, setProtectionNotice] = useState(() => {
    try { return sessionStorage.getItem(restoreProtectionNoticeKey) === 'internal_database_only' } catch { return false }
  })
  const [preferences, setPreferences] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [scope, setScope] = useState('learning')
  const [confirmation, setConfirmation] = useState('')
  const [backups, setBackups] = useState<BackupManifest[]>([])
  const [selectedBackupId, setSelectedBackupId] = useState('')
  const [restoreConfirmation, setRestoreConfirmation] = useState('')
  const [portablePassphrase, setPortablePassphrase] = useState('')
  const [portableRestorePath, setPortableRestorePath] = useState('')
  const [portableRestoreConfirmation, setPortableRestoreConfirmation] = useState('')
  const [captures, setCaptures] = useState<CaptureArtifactSummary[]>([])
  const automaticBackup = health.maintenance.daily_backup

  useEffect(() => {
    void localApi.listBackups().then((items) => { setBackups(items); setSelectedBackupId(items[0]?.backup_id ?? '') }).catch(() => undefined)
  }, [])

  useEffect(() => {
    setCaptures([])
    void localApi.captureArtifacts(childId).then(setCaptures).catch(() => undefined)
  }, [childId])

  const save = async (): Promise<void> => {
    setBusy(true); setMessage(null)
    try {
      setPreferences(await localApi.updatePrivacyPreferences(preferences))
      setMessage('隐私设置已经保存并写入授权记录。')
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '隐私设置没有保存。') } finally { setBusy(false) }
  }
  const exportData = async (): Promise<void> => {
    setBusy(true); setMessage(null)
    try { const result = await localApi.exportChild(childId); setMessage(`导出完成：${result.path}`) } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '导出失败。') } finally { setBusy(false) }
  }
  const backup = async (): Promise<void> => {
    setBusy(true); setMessage(null)
    try {
      const result = await localApi.createBackup()
      const [items, updatedHealth] = await Promise.all([localApi.listBackups(), localApi.systemHealth()])
      setBackups(items); setSelectedBackupId(items[0]?.backup_id ?? '')
      onHealthChange(updatedHealth)
      setMessage(`备份完成并通过完整性检查：数据库与 ${result.media_file_count} 个加密媒体文件。${result.missing_media_references ? `另有 ${result.missing_media_references} 个历史文件引用已缺失。` : ''}`)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '备份失败。') } finally { setBusy(false) }
  }
  const restore = async (): Promise<void> => {
    if (!selectedBackupId || cleanupPending) return
    const token = beginPrivacyOperation()
    if (!token) return
    setBusy(true); setMessage(null)
    try {
      const { generation } = await localApi.privacyOperationGeneration()
      const intent = await preparePrivacyIntent('restore', null, null, generation)
      const result = await localApi.restoreBackup(selectedBackupId, restoreConfirmation, operationIdentity(intent))
      setProtectionNotice(rememberRestoreProtection(result))
      await finishCommittedCleanup(token, 'restore')
    } catch (cause) {
      if (cause instanceof ApiError) setMessage(cause.message)
      if (hasPrivacyIntent()) await finishCommittedCleanup(token, 'restore')
      else setMessage(cause instanceof ApiError ? cause.message : '本机恢复请求尚未保存，未发送恢复操作。')
    } finally { releasePrivacyOperation(token); setBusy(false) }
  }
  const createPortableBackup = async (): Promise<void> => {
    setBusy(true); setMessage(null)
    try {
      const result = await localApi.createPortableBackup(portablePassphrase)
      setMessage(`可迁移加密备份已生成：${result.media_file_count}个媒体文件，${Math.ceil(result.size_bytes / 1024)}KB。口令不会写入备份或日志。`)
      setPortablePassphrase('')
      await window.desktop?.revealLocalFile?.(result.path)
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '可迁移备份创建失败。') } finally { setBusy(false) }
  }
  const choosePortableBackup = async (): Promise<void> => {
    const selected = await window.desktop?.choosePortableBackup?.()
    if (selected) setPortableRestorePath(selected.path)
  }
  const restorePortableBackup = async (): Promise<void> => {
    if (cleanupPending) return
    const token = beginPrivacyOperation()
    if (!token) return
    setBusy(true); setMessage(null)
    try {
      const { generation } = await localApi.privacyOperationGeneration()
      const intent = await preparePrivacyIntent('portable_restore', null, null, generation)
      const result = await localApi.restorePortableBackup(
        portableRestorePath, portablePassphrase, portableRestoreConfirmation, operationIdentity(intent)
      )
      setProtectionNotice(rememberRestoreProtection(result))
      await finishCommittedCleanup(token, 'restore', async () => {
      setPortablePassphrase('')
      })
    } catch (cause) {
      if (cause instanceof ApiError) setMessage(cause.message)
      if (hasPrivacyIntent()) await finishCommittedCleanup(token, 'restore')
      else setMessage(cause instanceof ApiError ? cause.message : '本机迁移请求尚未保存，未发送恢复操作。')
    } finally { releasePrivacyOperation(token); setBusy(false) }
  }
  const showDeletionFailure = async (cause: unknown, fallback: string): Promise<void> => {
    if (cause instanceof ApiError && cause.recoveryAction === 'retry_deletion') {
      try { onHealthChange(await localApi.systemHealth()) } catch { /* Preserve the deletion error. */ }
    }
    setMessage(cause instanceof ApiError ? cause.message : fallback)
  }
  const deleteData = async (): Promise<void> => {
    if (cleanupPending) return
    const token = beginPrivacyOperation()
    if (!token) return
    setBusy(true); setMessage(null)
    try {
      if (scope === 'media' || scope === 'all_history') {
        window.dispatchEvent(new Event('hsc-parent-observation-stop'))
      }
      const preview = await localApi.deletionPreview(childId)
      const { generation } = await localApi.privacyOperationGeneration()
      const intent = await preparePrivacyIntent('delete', childId, scope, generation)
      const result = await localApi.deleteChildData(childId, scope, confirmation, operationIdentity(intent))
      await finishCommittedCleanup(token, 'delete', async () => {
      const [updatedBackups, updatedHealth] = await Promise.all([localApi.listBackups(), localApi.systemHealth()])
      setBackups(updatedBackups); setSelectedBackupId(updatedBackups[0]?.backup_id ?? '')
      onHealthChange(updatedHealth)
      const previewTotal = Object.values(preview).reduce((sum, value) => sum + value, 0)
      const deletedFiles = result.deleted_counts.raw_media_files ?? 0
      const deletedRecords = Object.entries(result.deleted_counts)
        .filter(([key]) => key !== 'raw_media_files')
        .reduce((sum, [, value]) => sum + value, 0)
      setMessage(`${deletionScopeLabel(scope)}删除完成：删除前共确认${previewTotal}条关联数据，本次清理${deletedRecords}条记录${deletedFiles ? `和${deletedFiles}个原始媒体文件` : ''}；${result.recovery_rotation.invalidated_backup_count}个旧恢复点已失效，并已生成删除后的新恢复点。`)
      setConfirmation('')
      })
    } catch (cause) {
      if (cause instanceof ApiError) setMessage(cause.message)
      if (hasPrivacyIntent()) {
        await showDeletionFailure(cause, '原删除状态尚未核对。')
        await finishCommittedCleanup(token, 'delete')
      }
      else await showDeletionFailure(cause, '本机删除请求尚未保存，未发送删除操作。')
    } finally { releasePrivacyOperation(token); setBusy(false) }
  }
  const toggle = (key: keyof PrivacyPreferences): void => setPreferences((current) => ({ ...current, [key]: !current[key] }))
  const deleteCapture = async (artifactId: string): Promise<void> => {
    const proof = { child: childId, authority: parentDraftAuthorityVersion(), epoch: captureOwner.current.epoch }
    const current = (): boolean => captureOwner.current.mounted && captureOwner.current.child === proof.child && captureOwner.current.epoch === proof.epoch && parentDraftAuthorityVersion() === proof.authority
    let deletion: ReturnType<typeof beginCaptureDeletion>
    try { deletion = beginCaptureDeletion(childId, artifactId) } catch { setMessage('原采集删除状态尚未核对，暂不能再次删除或查看页面。'); return }
    setBusy(true); setMessage(null)
    try {
      const result = await localApi.deleteCaptureArtifact(artifactId, confirmation, deletion.intent.actionId)
      if (!current()) return
      if (result.status !== 'completed' || !result.database_reference_deleted || result.recovery_rotation?.status !== 'completed') throw new Error('CAPTURE_DELETE_UNCONFIRMED')
      const receipt = await requestParentLocal<{verified:boolean; child_id?:string; artifact_id?:string; action_id?:string}>(`/api/parent/children/${encodeURIComponent(childId)}/daily-visual-preview/deletion-receipt/${encodeURIComponent(artifactId)}/${encodeURIComponent(deletion.intent.actionId)}`, {cache:'no-store'})
      if (!current()) return
      if (receipt.verified !== true || receipt.child_id !== childId || receipt.artifact_id !== artifactId || receipt.action_id !== deletion.intent.actionId) throw new Error('CAPTURE_DELETE_RECEIPT_UNCONFIRMED')
      const session = await requestParentLocal<{expires_at:string}>(`/api/parent/children/${encodeURIComponent(childId)}/daily-visual-preview/session`, {cache:'no-store'})
      if (!current()) return
      const expiry = Date.parse(session.expires_at)
      if (!Number.isFinite(expiry) || expiry <= Date.now()) throw new Error('PARENT_SESSION_EXPIRED')
      confirmCaptureDeletion(deletion.intent)
      clearOcrArtifactRecovery(childId, artifactId)
      const [updatedBackups, updatedHealth] = await Promise.all([localApi.listBackups(), localApi.systemHealth()])
      setBackups(updatedBackups); setSelectedBackupId(updatedBackups[0]?.backup_id ?? '')
      onHealthChange(updatedHealth)
      setCaptures((current) => current.filter((item) => item.artifact_id !== artifactId))
      setMessage(`单条采集记录已删除。数据库引用：${result.database_reference_deleted ? '已清理' : '未清理'}；原始文件：${result.raw_file_deleted ? '已删除' : '原本未保存'}；旧恢复点已失效，并已生成删除后的新恢复点。`)
      setConfirmation('')
    } catch { if (current()) setMessage('这条记录的删除结果尚未确认。暂时不能重新查看或再次删除，请先核对原操作。') } finally { if (current()) setBusy(false) }
  }

  return (
    <section className="privacy-operations">
      <div className="privacy-title"><span>隐私与数据</span><h2>授权、保留与恢复</h2><p>数据库：{health.database_integrity === 'ok' ? '正常' : health.database_integrity} · 原始媒体文件 {health.raw_media_files_stored} 个 · 活动传感器 {health.active_sensor_leases} 个 · 待生成报告 {health.pending_report_jobs} 个 · 待确认语音工具 {health.pending_agent_turns} 个</p><p>每日自动备份：{automaticBackup?.status === 'completed' ? `正常，${automaticBackup.run_date} 已完成` : automaticBackup?.status === 'failed' ? `失败，${automaticBackup.error_message ?? '请在运行状态中查看并重试'}` : automaticBackup?.status === 'processing' ? '正在执行' : '等待首次运行'}{health.last_backup ? ` · 最近恢复点 ${new Date(health.last_backup.created_at).toLocaleString()}（${backupReasonLabel(health.last_backup.reason)}）` : ' · 尚无恢复点'}</p></div>
      <div className="privacy-toggles">
        {([
          ['cloud_model_text_allowed', '允许必要文本调用云模型'],
          ['cloud_audio_allowed', '允许音频上传云端'],
          ['raw_media_storage_allowed', '允许保存单次原始图片或语音'],
          ['model_improvement_allowed', '允许用于模型改进']
        ] as Array<[keyof PrivacyPreferences, string]>).map(([key, label]) => (
          <label className="toggle-row" key={key}><input type="checkbox" checked={Boolean(preferences[key])} onChange={() => toggle(key)} /><span>{label}</span></label>
        ))}
        <button className="secondary-button" disabled={busy} onClick={() => void save()}>保存授权</button>
      </div>
      <div className="data-actions">
        <button className="secondary-button" disabled={busy} onClick={() => void exportData()}><Download size={17} />导出当前孩子</button>
        <button className="secondary-button" disabled={busy} onClick={() => void backup()}><DatabaseBackup size={17} />备份学习数据</button>
      </div>
      <div className="delete-controls">
        <select aria-label="删除范围" value={scope} onChange={(event) => setScope(event.target.value)}><option value="learning">学习诊断</option><option value="reports">家长报告</option><option value="tutor">讲题对话</option><option value="media">语音、OCR与Agent记录</option><option value="knowledge">家庭导入的知识资料</option><option value="all_history">全部学习历史与知识资料</option></select>
        <input aria-label="删除确认" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="输入：确认删除" />
        <button className="danger-button" disabled={busy || cleanupPending || confirmation !== '确认删除'} onClick={() => void deleteData()}><Trash2 size={17} />删除</button>
      </div>
      <div className="restore-controls">
        <select aria-label="选择学习数据备份" value={selectedBackupId} onChange={(event) => setSelectedBackupId(event.target.value)}><option value="">暂无备份</option>{backups.map((item) => <option key={item.backup_id} value={item.backup_id}>{new Date(item.created_at).toLocaleString()} · {backupReasonLabel(item.reason)} · {Math.ceil(item.size_bytes / 1024)}KB · {item.integrity_status}</option>)}</select>
        <input aria-label="恢复确认" value={restoreConfirmation} onChange={(event) => setRestoreConfirmation(event.target.value)} placeholder="输入：确认恢复" />
        <button className="danger-button" disabled={busy || cleanupPending || !selectedBackupId || restoreConfirmation !== '确认恢复'} onClick={() => void restore()}><History size={17} />恢复</button>
      </div>
      <LegacyOcrDispositionPanel />
      <p className="muted-copy">恢复后不会自动重发历史报告。请重新核对报告内容、批准发送并确认收件人；结果未知的投递需单独确认是否重发。</p>
      <div className="portable-backup-controls">
        <div>
          <strong>换电脑迁移</strong>
          <span>学习记录和媒体封装为口令加密文件，恢复时换用新电脑密钥。通知通道的密码需在新电脑重新配置。</span>
        </div>
        <input aria-label="迁移备份口令" type="password" autoComplete="new-password" value={portablePassphrase} onChange={(event) => setPortablePassphrase(event.target.value)} placeholder="输入至少12位独立口令" />
        <button className="secondary-button" disabled={busy || portablePassphrase.length < 12} onClick={() => void createPortableBackup()}><DatabaseBackup size={17} />生成迁移包</button>
        <button className="secondary-button" disabled={busy || !window.desktop?.choosePortableBackup} onClick={() => void choosePortableBackup()}><FolderOpen size={17} />选择迁移包</button>
        <span className="portable-backup-path">{portableRestorePath ? '已选择迁移包' : '尚未选择迁移包'}</span>
        <input aria-label="迁移恢复确认" value={portableRestoreConfirmation} onChange={(event) => setPortableRestoreConfirmation(event.target.value)} placeholder="输入：确认迁移恢复" />
        <button className="danger-button" disabled={busy || cleanupPending || !portableRestorePath || portablePassphrase.length < 12 || portableRestoreConfirmation !== '确认迁移恢复'} onClick={() => void restorePortableBackup()}><History size={17} />迁移恢复</button>
      </div>
      {captures.length > 0 && <div className="capture-records">
        <h3>最近采集记录</h3>
        <p>不展示识别正文和本地路径。删除按钮使用上方同一个“确认删除”确认词。</p>
        {captures.map((item) => <div key={item.artifact_id}><span>{new Date(item.created_at).toLocaleString()} · {item.artifact_type === 'ocr' ? 'OCR图片' : item.artifact_type} · {item.extracted_text_chars}字 · {item.raw_file_stored ? '有原始文件' : '无原始文件'}</span><button className="danger-button" disabled={busy || parentVisualInvalidationActive() || confirmation !== '确认删除'} title="删除这一条采集记录" onClick={() => void deleteCapture(item.artifact_id)}><Trash2 size={16} />删除此条</button></div>)}
      </div>}
      {protectionNotice && <p role="status" className="operation-message">{restoreProtectionNotice}</p>}
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

function deletionScopeLabel(scope: string): string {
  return ({
    learning: '学习诊断数据',
    reports: '家长报告',
    tutor: '讲题对话',
    media: '语音与采集记录',
    knowledge: '家庭知识资料',
    all_history: '全部学习历史'
  } as Record<string, string>)[scope] ?? '所选数据'
}

function backupReasonLabel(reason: BackupManifest['reason']): string {
  return ({ manual: '家长手动', session_completed: '学习结束', daily_automatic: '每日自动', restore_recovery_point: '恢复前保护', post_deletion: '删除后保护' } as const)[reason]
}

export function ModelSettingsPanel({ initial, onRuntimeRestart = () => window.location.reload() }: {
  initial: BootstrapState['model']
  onRuntimeRestart?: () => void
}): React.JSX.Element {
  const [provider, setProvider] = useState<'qwen' | 'deepseek'>(initial.provider === 'deepseek' ? 'deepseek' : 'qwen')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState(initial.model || 'qwen3.7-plus')
  const [voiceIntentModel, setVoiceIntentModel] = useState(initial.voice_intent_model || (initial.provider === 'qwen' ? 'qwen3.8-flash' : initial.model) || 'qwen3.8-flash')
  const [baseUrl, setBaseUrl] = useState(initial.base_url || (provider === 'deepseek' ? 'https://api.deepseek.com' : 'https://dashscope.aliyuncs.com/compatible-mode/v1'))
  const [fallbackEnabled, setFallbackEnabled] = useState(initial.fallback_configured)
  const [fallbackProvider, setFallbackProvider] = useState<'qwen' | 'deepseek'>(initial.fallback_provider === 'qwen' ? 'qwen' : 'deepseek')
  const [fallbackApiKey, setFallbackApiKey] = useState('')
  const [fallbackModel, setFallbackModel] = useState(initial.fallback_model || (initial.fallback_provider === 'qwen' ? 'qwen3.7-plus' : 'deepseek-v4-pro'))
  const [fallbackBaseUrl, setFallbackBaseUrl] = useState(initial.fallback_base_url || (initial.fallback_provider === 'qwen' ? 'https://dashscope.aliyuncs.com/compatible-mode/v1' : 'https://api.deepseek.com'))
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [removed, setRemoved] = useState(false)
  const [removeArmed, setRemoveArmed] = useState(false)
  const configured = initial.configured && !removed
  const [loadDiagnostic, setLoadDiagnostic] = useState<NonNullable<DesktopModelSummary['loadDiagnostic']> | 'checking' | 'status_unavailable' | null>(window.desktop ? 'checking' : null)
  const loadRequest = useRef(0)
  const readLoadStatus = useCallback(async (): Promise<void> => {
    if (!window.desktop?.getModelSettings) return
    const request = ++loadRequest.current
    setLoadDiagnostic('checking')
    try {
      const result = await window.desktop.getModelSettings()
      const allowed = ['loaded', 'not_configured', 'read_failed', 'parse_failed', 'secure_storage_unavailable', 'config_unreadable'] as const
      const reason = result.loadDiagnostic
      if (request === loadRequest.current) setLoadDiagnostic(reason && allowed.includes(reason) ? reason : 'status_unavailable')
    } catch {
      if (request === loadRequest.current) setLoadDiagnostic('status_unavailable')
    }
  }, [])
  useEffect(() => {
    if (window.desktop?.getModelSettings) void readLoadStatus()
    else if (window.desktop) setLoadDiagnostic('status_unavailable')
    return () => { loadRequest.current += 1 }
  }, [readLoadStatus])
  const loadMessage = loadDiagnostic === 'checking' ? '正在读取本机模型配置状态。'
    : loadDiagnostic === 'loaded' ? '本机保存的模型配置可读取。运行服务是否已启用仍以当前连接状态为准。'
    : loadDiagnostic === 'not_configured' ? '本机尚未保存模型配置，可以填写并保存。'
    : loadDiagnostic === 'secure_storage_unavailable' ? '系统安全存储暂不可用，无法读取已保存的配置。本次读取未修改配置文件；请稍后重新读取。'
    : loadDiagnostic === 'read_failed' ? '暂时无法访问本机模型配置，不能确认是否已配置。本次读取未修改配置文件；请检查本机访问状态后重新读取。'
    : loadDiagnostic === 'parse_failed' || loadDiagnostic === 'config_unreadable' ? '本机已有模型配置，但本次未能读取。本次读取未修改配置文件；请先核对原系统安全存储或配置备份。重新保存会替换已有配置。'
    : loadDiagnostic === 'status_unavailable' ? '暂时无法确认本机模型配置状态。请重新读取；重新保存会替换已有配置。'
    : null

  const changeProvider = (next: 'qwen' | 'deepseek'): void => {
    setProvider(next)
    setBaseUrl(next === 'deepseek' ? 'https://api.deepseek.com' : 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    setModel(next === 'deepseek' ? 'deepseek-v4-pro' : 'qwen3.7-plus')
    setVoiceIntentModel(next === 'deepseek' ? 'deepseek-v4-pro' : 'qwen3.8-flash')
  }

  const changeFallbackProvider = (next: 'qwen' | 'deepseek'): void => {
    setFallbackProvider(next)
    setFallbackBaseUrl(next === 'deepseek' ? 'https://api.deepseek.com' : 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    setFallbackModel(next === 'deepseek' ? 'deepseek-v4-pro' : 'qwen3.7-plus')
  }

  const primaryConfig = { provider, apiKey: apiKey.trim(), baseUrl, model, voiceIntentModel }
  const intentTestConfig: DesktopModelEndpoint = { provider, apiKey: apiKey.trim(), baseUrl, model: voiceIntentModel }
  const fallbackConfig = { provider: fallbackProvider, apiKey: fallbackApiKey.trim(), baseUrl: fallbackBaseUrl, model: fallbackModel }
  const normalizeEndpoint = (value: string): string => value.trim().replace(/\/+$/, '')
  const primaryCredentialReady = apiKey.trim().length >= 8 || (configured && provider === initial.provider && normalizeEndpoint(baseUrl) === normalizeEndpoint(initial.base_url || ''))
  const fallbackCredentialReady = fallbackApiKey.trim().length >= 8 || (configured && initial.fallback_configured && fallbackProvider === initial.fallback_provider && normalizeEndpoint(fallbackBaseUrl) === normalizeEndpoint(initial.fallback_base_url || ''))
  const duplicateTarget = provider === fallbackProvider && normalizeEndpoint(baseUrl) === normalizeEndpoint(fallbackBaseUrl) && model.trim() === fallbackModel.trim()
  const canSave = primaryCredentialReady && model.trim().length > 0 && voiceIntentModel.trim().length > 0 && (!fallbackEnabled || (fallbackCredentialReady && fallbackModel.trim().length > 0 && !duplicateTarget))

  const save = async (): Promise<void> => {
    if (!window.desktop || !canSave) return
    setBusy(true)
    setMessage(null)
    try {
      await window.desktop.saveModelSettings({ ...primaryConfig, ...(fallbackEnabled ? { fallback: fallbackConfig } : {}) })
      onRuntimeRestart()
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '模型设置没有保存。')
      setBusy(false)
    }
  }

  const testConnection = async (config: DesktopModelEndpoint, label: string): Promise<void> => {
    if (!window.desktop || config.apiKey.length < 8) return
    setBusy(true); setMessage(null)
    try {
      const result = await window.desktop.testModelSettings({ ...config, voiceIntentModel: config.model })
      setMessage(`${label}连接成功：${result.model} · ${result.latencyMs}毫秒。本次测试会产生极少量API用量。`)
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '连接测试失败。') } finally { setBusy(false) }
  }

  const clear = async (): Promise<void> => {
    if (!window.desktop || !removeArmed) return
    setBusy(true); setMessage(null)
    try {
      await window.desktop.clearModelSettings()
      setRemoved(true); setRemoveArmed(false); setApiKey(''); setFallbackApiKey(''); setFallbackEnabled(false)
      setMessage('云模型已停用，本机保存的首选与备用模型密钥已经删除。需要时可重新配置。')
      onRuntimeRestart()
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '云模型凭据没有删除。')
    } finally { setBusy(false) }
  }

  return (
    <section className="model-settings-panel">
      <div><span>模型网关</span><h2>{configured ? '模型已配置' : '配置云端模型'}</h2><p>学习辅导重质量，实时意图与短陪伴对话重响应速度；三类能力共用加密保存的服务商密钥。</p></div>
      {loadMessage && <div className="model-actions"><p role="status" aria-live="polite">{loadMessage}</p>{window.desktop?.getModelSettings && <button className="secondary-button" disabled={busy || loadDiagnostic === 'checking'} onClick={() => void readLoadStatus()}>重新读取状态</button>}</div>}
      <label>服务商<select value={provider} onChange={(event) => changeProvider(event.target.value as 'qwen' | 'deepseek')}><option value="qwen">阿里云千问</option><option value="deepseek">DeepSeek</option></select></label>
      <label>学习辅导模型<input value={model} onChange={(event) => setModel(event.target.value)} /></label>
      <label>实时意图模型<input value={voiceIntentModel} onChange={(event) => setVoiceIntentModel(event.target.value)} /></label>
      <label>API地址<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
      <label>API Key<input type="password" value={apiKey} placeholder={configured ? '输入新密钥才会覆盖' : '请输入密钥'} onChange={(event) => setApiKey(event.target.value)} /></label>
      <div className="model-actions"><button className="secondary-button" disabled={!window.desktop || busy || apiKey.trim().length < 8 || !model.trim()} onClick={() => void testConnection(primaryConfig, '学习辅导模型')}>测试辅导</button><button className="secondary-button" disabled={!window.desktop || busy || apiKey.trim().length < 8 || !voiceIntentModel.trim()} onClick={() => void testConnection(intentTestConfig, '实时意图模型')}>测试意图</button></div>
      <fieldset className="model-fallback"><legend><label><input type="checkbox" checked={fallbackEnabled} onChange={(event) => setFallbackEnabled(event.target.checked)} />启用备用模型端点</label></legend>{fallbackEnabled && <><label>备用服务商<select value={fallbackProvider} onChange={(event) => changeFallbackProvider(event.target.value as 'qwen' | 'deepseek')}><option value="qwen">阿里云千问</option><option value="deepseek">DeepSeek</option></select></label><label>备用模型<input value={fallbackModel} onChange={(event) => setFallbackModel(event.target.value)} /></label><label>备用API地址<input value={fallbackBaseUrl} onChange={(event) => setFallbackBaseUrl(event.target.value)} /></label><label>备用API Key<input type="password" value={fallbackApiKey} placeholder={configured && initial.fallback_configured ? '输入新密钥才会覆盖' : '请输入备用密钥'} onChange={(event) => setFallbackApiKey(event.target.value)} /></label><div className="model-actions"><button className="secondary-button" disabled={!window.desktop || busy || fallbackApiKey.trim().length < 8 || !fallbackModel.trim() || duplicateTarget} onClick={() => void testConnection(fallbackConfig, '备用模型')}>测试备用</button></div></>}</fieldset>
      <div className="model-actions"><button className="primary-button" disabled={!window.desktop || busy || !canSave} onClick={() => void save()}>{busy ? '正在处理' : '保存并启用'}</button></div>
      {configured && !removeArmed && <div className="model-removal"><button className="danger-button" disabled={!window.desktop || busy} onClick={() => setRemoveArmed(true)}><Trash2 size={16} />停用云模型</button></div>}
      {configured && removeArmed && <div className="model-removal-confirmation"><p>将停止云端讲题与意图调用，并删除本机保存的首选和备用密钥。学习记录不会删除。</p><button className="danger-button" disabled={busy} onClick={() => void clear()}><Trash2 size={16} />确认停用并删除密钥</button><button className="secondary-button" disabled={busy} onClick={() => setRemoveArmed(false)}>取消</button></div>}
      {!window.desktop && <small>模型安全配置仅在桌面 App 中可用。</small>}
      {message && <div className="tutor-error">{message}</div>}
    </section>
  )
}

export function QwenRealtimeSettingsPanel(): React.JSX.Element {
  const [summary, setSummary] = useState<DesktopQwenRealtimeSummary | null>(null)
  const [workspaceId, setWorkspaceId] = useState('')
  const [region, setRegion] = useState<'cn-beijing' | 'ap-southeast-1'>('cn-beijing')
  const [apiKey, setApiKey] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [removeArmed, setRemoveArmed] = useState(false)

  useEffect(() => {
    let current = true
    void window.desktop?.getQwenRealtimeSettings?.().then((value) => {
      if (!current) return
      setSummary(value)
      setEnabled(value.enabled)
      if (value.endpoint) {
        try {
          const host = new URL(value.endpoint).hostname
          setWorkspaceId(host.split('.')[0] ?? '')
          setRegion(host.includes('.ap-southeast-1.') ? 'ap-southeast-1' : 'cn-beijing')
        } catch { /* invalid legacy settings remain visibly unconfigured */ }
      }
    }).catch((cause) => {
      if (current) setMessage(cause instanceof Error ? cause.message : '无法读取实时语音配置。')
    })
    return () => { current = false }
  }, [])

  const endpoint = workspaceId.trim() ? `https://${workspaceId.trim()}.${region}.maas.aliyuncs.com` : ''
  const config: DesktopQwenRealtimeConfig = {
    apiKey: apiKey.trim(), endpoint, model: 'qwen3.8-omni-flash-realtime', voice: 'Tina', enabled,
  }
  const workspaceValid = /^[a-zA-Z0-9_-]{2,100}$/.test(workspaceId.trim())
  // The desktop main process can also reuse the already authorized system
  // keychain credential without exposing it to this renderer.
  const credentialReady = apiKey.trim().length >= 16 || Boolean(summary?.configured) || Boolean(window.desktop)
  const ready = workspaceValid && credentialReady

  const test = async (): Promise<void> => {
    if (!window.desktop?.testQwenRealtimeSettings || !ready) return
    setBusy(true); setMessage(null)
    try {
      const result = await window.desktop.testQwenRealtimeSettings(config)
      setMessage(`工作空间和密钥验证成功：${result.model} · ${result.latencyMs}毫秒。此处只验证模型权限，真实麦克风和打断在孩子端建立 WebRTC 后验证。`)
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '实时语音配置验证失败。')
    } finally { setBusy(false) }
  }

  const save = async (): Promise<void> => {
    if (!window.desktop?.saveQwenRealtimeSettings || !ready) return
    setBusy(true); setMessage(null)
    try {
      const saved = await window.desktop.saveQwenRealtimeSettings(config)
      setSummary(saved); setApiKey(''); setMessage('实时语音配置已验证并加密保存。新建学习会话将优先使用 Qwen3.8 双向语音。')
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '实时语音配置没有保存。')
    } finally { setBusy(false) }
  }

  const clear = async (): Promise<void> => {
    if (!window.desktop?.clearQwenRealtimeSettings || !removeArmed) return
    setBusy(true); setMessage(null)
    try {
      const cleared = await window.desktop.clearQwenRealtimeSettings()
      setSummary(cleared); setApiKey(''); setRemoveArmed(false)
      setMessage('实时语音密钥和工作空间已从 App 安全配置中删除。')
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '实时语音配置没有删除。')
    } finally { setBusy(false) }
  }

  return <section className="model-settings-panel voice-settings-panel">
    <div><span>实时对话</span><h2>{summary?.configured ? 'Qwen3.8 实时语音已配置' : '配置 Qwen3.8 双向语音'}</h2><p>该通道负责流式听说、语义断句和打断；学习计划、记忆、设备及错题仍由本地 Agent Runtime 调度和持久化。</p></div>
    <label>百炼 Workspace ID<input aria-label="实时语音 Workspace ID" value={workspaceId} placeholder="例如 ws_xxx 或 llm-xxx" onChange={(event) => setWorkspaceId(event.target.value)} /></label>
    <label>地域<select aria-label="实时语音地域" value={region} onChange={(event) => setRegion(event.target.value as typeof region)}><option value="cn-beijing">华北2（北京）</option><option value="ap-southeast-1">新加坡</option></select></label>
    <label>实时模型<input aria-label="实时语音模型" value="qwen3.8-omni-flash-realtime" readOnly /></label>
    <label>默认女声<input aria-label="实时语音音色" value="Tina" readOnly /></label>
    <label>API Key<input aria-label="实时语音 API Key" type="password" value={apiKey} placeholder={summary?.configured ? '留空复用已加密密钥' : '请输入与 Workspace 同地域的密钥'} onChange={(event) => setApiKey(event.target.value)} /></label>
    <label className="toggle-row"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /><span>新建学习会话优先使用实时语音，建连失败时明确回退到分段语音。</span></label>
    <div className="model-actions"><button className="secondary-button" disabled={busy || !ready || !window.desktop?.testQwenRealtimeSettings} onClick={() => void test()}>{busy ? '正在验证' : '验证工作空间'}</button><button className="primary-button" disabled={busy || !ready || !window.desktop?.saveQwenRealtimeSettings} onClick={() => void save()}>{busy ? '正在保存' : '验证并启用'}</button></div>
    {summary?.configured && !removeArmed && <button className="danger-button" disabled={busy} onClick={() => setRemoveArmed(true)}><Trash2 size={16} />停用并删除实时语音密钥</button>}
    {summary?.configured && removeArmed && <div className="model-removal-confirmation"><p>将回退到分段语音，不删除学习记录。</p><button className="danger-button" disabled={busy} onClick={() => void clear()}>确认删除</button><button className="secondary-button" disabled={busy} onClick={() => setRemoveArmed(false)}>取消</button></div>}
    {message && <div className="tutor-error">{message}</div>}
  </section>
}

export function VoiceSettingsPanel({ onRuntimeRestart = () => window.location.reload() }: { onRuntimeRestart?: () => void }): React.JSX.Element {
  const [summary, setSummary] = useState<DesktopVoiceSummary | null>(null)
  const [voiceProvider, setVoiceProvider] = useState<DesktopVoiceProvider>('relay_pcm')
  const [relayUrl, setRelayUrl] = useState('')
  // Parent-selected default: the first reviewed adult candidate.  This is a
  // pending choice, not a claim that the cloud voice has been licensed or
  // successfully synthesized.
  const [voiceId, setVoiceId] = useState('longyingtao_v3')
  const [apiKey, setApiKey] = useState('')
  const [cosyWorkspaceId, setCosyWorkspaceId] = useState('')
  const [cosyRegion, setCosyRegion] = useState('cn-beijing')
  const [cosyModel, setCosyModel] = useState(COSYVOICE_DEFAULT_MODEL)
  const [licensed, setLicensed] = useState(false)
  const [naturalness, setNaturalness] = useState(0)
  const [childIntelligibility, setChildIntelligibility] = useState(0)
  const [lastPreview, setLastPreview] = useState<{ firstFrameMs: number; totalMs: number } | null>(null)
  const [trials, setTrials] = useState<DesktopVoiceTrial[]>([])
  const [evaluationCandidates, setEvaluationCandidates] = useState<DesktopVoiceEvaluationCandidate[]>([])
  const [referenceSamples, setReferenceSamples] = useState<DesktopReferenceVoiceSample[]>([])
  const [selectedReferenceSampleId, setSelectedReferenceSampleId] = useState('')
  const [playingCandidate, setPlayingCandidate] = useState<string | null>(null)
  const [selectedEvaluationCandidateId, setSelectedEvaluationCandidateId] = useState<string | null>(null)
  const [listeningRatings, setListeningRatings] = useState<DesktopVoiceListeningRating[]>([])
  const [evaluatorRole, setEvaluatorRole] = useState<'parent' | 'child' | 'joint'>('joint')
  const [listeningScores, setListeningScores] = useState({ naturalness: 0, chineseClarity: 0, englishMath: 0, lowPressureReminder: 0, seriousCorrection: 0, fatigue30Minutes: 0, childAcceptance: 0 })
  const [listeningNotes, setListeningNotes] = useState('')
  const evaluationAudio = useRef<HTMLAudioElement | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [removeArmed, setRemoveArmed] = useState(false)

  useEffect(() => {
    const getSettings = window.desktop?.getVoiceSettings
    if (!getSettings) return
    void getSettings().then((value) => {
      setSummary(value); setVoiceProvider(value.provider === 'disabled' ? 'relay_pcm' : value.provider); setRelayUrl(value.relayUrl ?? ''); setVoiceId(value.voiceId ?? 'longyingtao_v3'); setCosyWorkspaceId(value.workspaceId ?? ''); setCosyRegion(value.region ?? 'cn-beijing'); setCosyModel(value.model ?? COSYVOICE_DEFAULT_MODEL)
    }).catch(() => setMessage('无法读取本机语音配置。'))
    const getTrials = window.desktop?.getVoiceTrials
    if (getTrials) void getTrials().then(setTrials).catch(() => setMessage('无法读取本机试听记录。'))
    const listCandidates = window.desktop?.listVoiceEvaluationCandidates
    if (listCandidates) void listCandidates().then(setEvaluationCandidates).catch(() => setMessage('无法读取只读声音评测包。'))
    const getRatings = window.desktop?.getVoiceListeningRatings
    if (getRatings) void getRatings().then(setListeningRatings).catch(() => setMessage('无法读取本机盲听评分。'))
    const listReferenceSamples = window.desktop?.listReferenceVoiceSamples
    if (listReferenceSamples) void listReferenceSamples().then((samples) => { setReferenceSamples(samples); setSelectedReferenceSampleId(samples.find((item) => item.recommended)?.id ?? samples[0]?.id ?? '') }).catch(() => setMessage('无法读取本机参考声音目录。'))
    return () => { evaluationAudio.current?.pause(); evaluationAudio.current = null }
  }, [])

  const config = { provider: voiceProvider, relayUrl: relayUrl.trim(), voiceId: voiceId.trim(), apiKey: apiKey.trim(), workspaceId: cosyWorkspaceId.trim(), region: cosyRegion.trim(), model: cosyModel.trim() }
  const ready = config.provider !== 'qwen_voice_clone' && (config.provider === 'relay_pcm' ? /^https:\/\//.test(config.relayUrl) : config.provider === 'aliyun_cosyvoice' ? /^[a-zA-Z0-9-]{2,80}$/.test(config.workspaceId) && supportsCompanionVoice(config.model, config.voiceId, config.region) : /^[a-zA-Z0-9-]{2,80}$/.test(config.workspaceId) && config.region === 'cn-beijing' && config.model === 'qwen-audio-3.0-tts-plus' && ['longanlingxin', 'longanlufeng'].includes(config.voiceId)) && /^[a-zA-Z0-9._:-]{2,120}$/.test(config.voiceId) && config.apiKey.length >= 8 && licensed
  const canEnrollReference = Boolean(selectedReferenceSampleId && licensed)

  const playPcm = (pcmBase64: string, sampleRate: number): void => {
    const bytes = Uint8Array.from(atob(pcmBase64), (item) => item.charCodeAt(0)); const samples = new Int16Array(bytes.buffer)
    const context = new AudioContext(); const buffer = context.createBuffer(1, samples.length, sampleRate); const channel = buffer.getChannelData(0)
    for (let index = 0; index < samples.length; index += 1) channel[index] = samples[index] / 32768
    const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination); source.start(); source.onended = () => { void context.close() }
  }
  const preview = async (): Promise<void> => {
    if (!window.desktop?.previewVoiceSettings || !ready) return
    setBusy(true); setMessage(null)
    try {
      const result = await window.desktop.previewVoiceSettings(config)
      playPcm(result.pcmBase64, result.sampleRate)
      setLastPreview({ firstFrameMs: result.firstFrameMs, totalMs: result.totalMs })
      setMessage(`已播放完整固定试听文案；首包 ${result.firstFrameMs}ms，完成 ${result.totalMs}ms。请评分并确认音色原创、商用/儿童场景许可后再启用。`)
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '试听失败，未保存任何语音设置。') } finally { setBusy(false) }
  }
  const enrollReference = async (): Promise<void> => {
    if (!window.desktop?.enrollReferenceVoice || !canEnrollReference) return
    setBusy(true); setMessage(null)
    try {
      const result = await window.desktop.enrollReferenceVoice({ sampleId: selectedReferenceSampleId, apiKey: apiKey.trim() || undefined, rightsConfirmed: licensed })
      setSummary(result); setVoiceProvider('qwen_voice_clone'); setVoiceId(result.voiceId ?? ''); setCosyModel(result.model ?? 'qwen3-tts-vc-2026-01-22'); setApiKey(''); playPcm(result.pcmBase64, result.sampleRate)
      setLastPreview({ firstFrameMs: result.firstFrameMs, totalMs: result.totalMs })
      setMessage(`已用“${result.sampleName}”注册并播放固定试听文案；音色已加密启用。参考音频只在本次注册时上传，日常对话只发送回复文本。`)
      onRuntimeRestart()
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '参考音色没有完成注册和试听，未启用。') } finally { setBusy(false) }
  }
  const save = async (): Promise<void> => {
    if (!window.desktop?.saveVoiceSettings || !ready) return
    setBusy(true); setMessage(null)
    try { const next = await window.desktop.saveVoiceSettings(config); setSummary(next); setApiKey(''); setMessage('自然语音通道已加密保存并重启本地服务。孩子语音不会上传；只有回复文本会按家长授权发送。'); onRuntimeRestart() } catch (cause) { setMessage(cause instanceof Error ? cause.message : '语音设置没有保存。') } finally { setBusy(false) }
  }
  const clear = async (): Promise<void> => {
    if (!window.desktop?.clearVoiceSettings || !removeArmed) return
    setBusy(true); setMessage(null)
    try { const next = await window.desktop.clearVoiceSettings(); setSummary(next); setApiKey(''); setRemoveArmed(false); setMessage('自然语音密钥已从本机安全配置中删除；界面将只保留文字和明确的系统临时播报。'); onRuntimeRestart() } catch (cause) { setMessage(cause instanceof Error ? cause.message : '语音密钥没有删除。') } finally { setBusy(false) }
  }
  const recordTrial = async (): Promise<void> => {
    if (!window.desktop?.recordVoiceTrial || !lastPreview || !licensed || naturalness < 1 || childIntelligibility < 1) return
    setBusy(true); setMessage(null)
    try {
      const next = await window.desktop.recordVoiceTrial({ trialId: crypto.randomUUID(), createdAt: new Date().toISOString(), provider: 'relay_pcm', relayUrl: config.relayUrl, voiceId: config.voiceId, firstFrameMs: lastPreview.firstFrameMs, totalMs: lastPreview.totalMs, naturalness, childIntelligibility, permissionVerified: true })
      setTrials(next); setMessage('试听记录已保存在本机：只含固定文案延迟、候选 ID 和评分，不含孩子语音、试听音频或密钥。')
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '试听记录没有保存。') } finally { setBusy(false) }
  }
  const playEvaluationCandidate = async (candidate: DesktopVoiceEvaluationCandidate): Promise<void> => {
    if (!window.desktop?.readVoiceEvaluationAudio) return
    evaluationAudio.current?.pause(); evaluationAudio.current = null
    setSelectedEvaluationCandidateId(candidate.id); setPlayingCandidate(candidate.id); setMessage(null)
    try {
      const audio = await window.desktop.readVoiceEvaluationAudio(candidate.id)
      const player = new Audio(`data:${audio.mimeType};base64,${audio.base64}`)
      evaluationAudio.current = player
      player.onended = () => {
        setPlayingCandidate(null)
        setMessage(`已播放“${candidate.name}”官方评测试听。请在不记录孩子身份或对话内容的前提下填写盲听表；这不是动态 Agent 语音。`)
      }
      await player.play()
      setMessage(`正在播放“${candidate.name}”官方评测试听。仅用于盲听选择，不会写入孩子档案或打包进产品。`)
    } catch (cause) { setPlayingCandidate(null); setMessage(cause instanceof Error ? cause.message : '评测试听无法播放。') }
  }
  const allListeningScoresEntered = Object.values(listeningScores).every((score) => score >= 1)
  const recordListeningRating = async (): Promise<void> => {
    if (!window.desktop?.recordVoiceListeningRating || !selectedEvaluationCandidateId || !allListeningScoresEntered) return
    setBusy(true); setMessage(null)
    try {
      const next = await window.desktop.recordVoiceListeningRating({ ratingId: crypto.randomUUID(), candidateId: selectedEvaluationCandidateId, createdAt: new Date().toISOString(), evaluatorRole, ...listeningScores, notes: listeningNotes.trim() || undefined })
      setListeningRatings(next); setListeningNotes(''); setMessage('盲听评分已保存在本机；只含候选 ID、维度分数和非身份化备注，不含孩子音频、名字或聊天内容。')
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '盲听评分没有保存。') } finally { setBusy(false) }
  }
  return <section className="model-settings-panel voice-settings-panel">
    <div><span>自然语音</span><h2>{summary?.configured ? '自然语音已配置' : '配置合规自然女声'}</h2><p>孩子原始语音和家庭视频不会进入合成服务。使用参考音色时，参考片段只在注册时上传一次；日常只发送必要回复文本和音色 ID。</p></div>
    <label>服务商通道<select value={voiceProvider} onChange={(event) => { const next = event.target.value as DesktopVoiceProvider; setVoiceProvider(next); if (next === 'qwen_audio_tts') { setVoiceId('longanlingxin'); setCosyModel('qwen-audio-3.0-tts-plus'); setCosyRegion('cn-beijing') } else if (next === 'aliyun_cosyvoice') { setVoiceId('longyingtao_v3'); setCosyModel(COSYVOICE_DEFAULT_MODEL) } else if (next === 'qwen_voice_clone') { setCosyModel('qwen3-tts-vc-2026-01-22'); setCosyRegion('cn-beijing') } }}><option value="qwen_voice_clone">家庭参考女声（一次注册）</option><option value="aliyun_cosyvoice">阿里云 CosyVoice 主声（官方 WebSocket）</option><option value="qwen_audio_tts">Qwen-Audio-TTS 成人女声（临时/故障兜底）</option><option value="relay_pcm">经审核的国产云 TTS relay</option></select></label>
    {voiceProvider === 'qwen_voice_clone' ? <div className="voice-reference-enrollment"><label>本机参考声音<select aria-label="本机参考声音" value={selectedReferenceSampleId} onChange={(event) => setSelectedReferenceSampleId(event.target.value)}>{referenceSamples.map((sample) => <option key={sample.id} value={sample.id}>{sample.recommended ? '推荐 · ' : ''}{sample.name} · {(sample.sizeBytes / 1024 / 1024).toFixed(1)}MB</option>)}</select></label>{referenceSamples.length === 0 && <small>没有在 `~/voice_ref` 找到 WAV、MP3 或 M4A 参考声音。</small>}<small>推荐使用 10–20 秒、单人、无背景音乐的正常语速片段。App 不保存第二份样本，只保存服务商返回的音色 ID。</small></div> : voiceProvider === 'relay_pcm' ? <label>HTTPS relay 地址<input aria-label="语音 relay 地址" value={relayUrl} placeholder="https://voice-gateway.example/pcm" onChange={(event) => setRelayUrl(event.target.value)} /></label> : <><label>百炼工作空间 ID<input aria-label="CosyVoice 工作空间 ID" value={cosyWorkspaceId} onChange={(event) => setCosyWorkspaceId(event.target.value)} /></label><label>区域<input aria-label="CosyVoice 区域" value={cosyRegion} onChange={(event) => setCosyRegion(event.target.value)} /></label><label>模型<input aria-label="CosyVoice 模型" value={cosyModel} onChange={(event) => setCosyModel(event.target.value)} /></label>{voiceProvider === 'qwen_audio_tts' && <small>Qwen-Audio 的音色目录与 CosyVoice 不同；它不会冒充 `longyingtao_v3`，仅作为明确标注的临时/故障兜底。</small>}</>}
    {voiceProvider !== 'qwen_voice_clone' && <label>原创女声音色 ID<input aria-label="自然女声音色 ID" value={voiceId} placeholder="由审核记录提供" onChange={(event) => setVoiceId(event.target.value)} /></label>}
    <label>语音服务密钥<input aria-label="语音服务密钥" type="password" value={apiKey} placeholder={voiceProvider === 'qwen_voice_clone' ? '可留空复用已加密的千问云语音凭据' : summary?.configured ? '重新输入才会覆盖；已保存密钥不显示' : '请输入密钥'} onChange={(event) => setApiKey(event.target.value)} /></label>
    {evaluationCandidates.length > 0 && <div className="voice-evaluation-candidates"><strong>官方评测候选（只读，不是生产声音）</strong><span>主声优先比较龙应桃、龙安温、龙小淳、龙应沐和龙安柔；童声和庆祝声不会被列为主陪伴声。</span>{evaluationCandidates.map((candidate) => <div key={candidate.id}><b>{candidate.name}</b><small>{candidate.trait} · {candidate.allowedUse}</small><button className="secondary-button" disabled={playingCandidate !== null} onClick={() => void playEvaluationCandidate(candidate)}><CirclePlay size={15} />{playingCandidate === candidate.id ? '正在播放' : '播放官方试听'}</button>{candidate.role === 'primary' && <button className="text-button" onClick={() => { setSelectedEvaluationCandidateId(candidate.id); setVoiceId(candidate.id); setMessage(`已将 ${candidate.name} 填入候选音色 ID；仍需完成许可核验、真实云端试听和家长保存。`) }}>作为待评主声</button>}</div>)}
      {selectedEvaluationCandidateId && <div className="voice-listening-scorecard"><p>盲听表：候选 <b>{selectedEvaluationCandidateId}</b>。请分别用固定文本测试低压提醒、严肃纠错、英文和数学读法；30 分钟疲劳度不能用一次短试听代替。</p><label>评分人<select value={evaluatorRole} onChange={(event) => setEvaluatorRole(event.target.value as 'parent' | 'child' | 'joint')}><option value="joint">亲子共同</option><option value="parent">家长</option><option value="child">孩子</option></select></label>{([['naturalness', '自然度'], ['chineseClarity', '中文清晰度'], ['englishMath', '英文/数学读法'], ['lowPressureReminder', '低压提醒'], ['seriousCorrection', '严肃纠错'], ['fatigue30Minutes', '30分钟疲劳度'], ['childAcceptance', '孩子主观接受度']] as const).map(([key, label]) => <label key={key}>{label}<select value={listeningScores[key]} onChange={(event) => setListeningScores((current) => ({ ...current, [key]: Number(event.target.value) }))}><option value={0}>请选择</option>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}/5</option>)}</select></label>)}<label className="voice-score-notes">非身份化备注（可选，不写姓名/对话）<input value={listeningNotes} maxLength={300} onChange={(event) => setListeningNotes(event.target.value)} /></label><button className="secondary-button" disabled={busy || !allListeningScoresEntered} onClick={() => void recordListeningRating()}>保存盲听评分</button></div>}
      {listeningRatings.length > 0 && <small>已记录 {listeningRatings.length} 份本机盲听评分；请比较同一固定文本下的多位候选，不以一次播放定稿。</small>}</div>}
    <label className="toggle-row"><input type="checkbox" checked={licensed} onChange={(event) => setLicensed(event.target.checked)} /><span>{voiceProvider === 'qwen_voice_clone' ? '我确认拥有该参考声音在家庭陪伴场景中的使用授权，并同意将选中的成人声音片段上传一次用于创建专属音色。' : '我已核验此音色为原创或已授权，允许本产品的家庭及儿童场景使用。'}</span></label>
    <div className="model-actions">{voiceProvider === 'qwen_voice_clone' ? <button className="primary-button" disabled={!window.desktop?.enrollReferenceVoice || busy || !canEnrollReference} onClick={() => void enrollReference()}><CirclePlay size={16} />{busy ? '正在注册并试听' : '注册、试听并启用'}</button> : <>{voiceProvider === 'relay_pcm' && <button className="secondary-button" disabled={!window.desktop?.previewVoiceSettings || busy || !ready} onClick={() => void preview()}><CirclePlay size={16} />试听固定文案</button>}<button className="primary-button" disabled={!window.desktop?.saveVoiceSettings || busy || !ready} onClick={() => void save()}>{busy ? '正在处理' : '加密保存并启用'}</button></>}</div>
    {lastPreview && <div className="voice-trial-form"><p>本次固定文案：首包 {lastPreview.firstFrameMs}ms · 完成 {lastPreview.totalMs}ms。请由家长和孩子在知情情况下评分，不让孩子录音或上传声音。</p><label>自然度（1–5）<select value={naturalness} onChange={(event) => setNaturalness(Number(event.target.value))}><option value={0}>请选择</option>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}</select></label><label>儿童可懂度（1–5）<select value={childIntelligibility} onChange={(event) => setChildIntelligibility(Number(event.target.value))}><option value={0}>请选择</option>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}</select></label><button className="secondary-button" disabled={!window.desktop?.recordVoiceTrial || busy || naturalness < 1 || childIntelligibility < 1 || !licensed} onClick={() => void recordTrial()}>记录本次候选</button></div>}
    {trials.length > 0 && <div className="voice-trial-list"><strong>本机候选记录</strong>{trials.slice(0, 5).map((trial) => <span key={trial.trialId}>{trial.voiceId} · 自然度 {trial.naturalness}/5 · 可懂度 {trial.childIntelligibility}/5 · 首包 {trial.firstFrameMs}ms · 完成 {trial.totalMs}ms</span>)}</div>}
    {summary?.configured && !removeArmed && <button className="danger-button" disabled={busy} onClick={() => setRemoveArmed(true)}><Trash2 size={16} />停用并删除语音密钥</button>}
    {summary?.configured && removeArmed && <div className="model-removal-confirmation"><p>删除后自然语音会停用；学习数据与原始语音不受影响。</p><button className="danger-button" disabled={busy} onClick={() => void clear()}>确认删除</button><button className="secondary-button" disabled={busy} onClick={() => setRemoveArmed(false)}>取消</button></div>}
    {!window.desktop && <small>语音密钥只能在桌面应用的安全配置中保存。</small>}
    {message && <div className="tutor-error">{message}</div>}
  </section>
}

export function ModelOperationsPanel({ childId, initialModel }: { childId: string; initialModel: BootstrapState['model'] }): React.JSX.Element {
  const [calls, setCalls] = useState<ModelCallAudit[]>([])
  const [pricing, setPricing] = useState<ModelPricing[]>([])
  const priceReadGeneration = useRef(0)
  const [runtimeStatus, setRuntimeStatus] = useState<ModelRuntimePolicyStatus | null>(null)
  const [provider, setProvider] = useState<'qwen' | 'deepseek'>(initialModel.provider === 'deepseek' ? 'deepseek' : 'qwen')
  const [model, setModel] = useState(initialModel.model || 'qwen3.7-plus')
  const [priceDraft, setPriceDraft] = useState<{ scope: string; input?: string; output?: string } | null>(null)
  const priceScope = JSON.stringify([provider, model.trim()])
  const savedPrice = pricing.find((item) => item.provider === provider && item.model === model.trim())
  const currentDraft = priceDraft?.scope === priceScope ? priceDraft : null
  const inputPrice = currentDraft?.input ?? (savedPrice ? String(savedPrice.input_price_cny_per_million) : '')
  const outputPrice = currentDraft?.output ?? (savedPrice ? String(savedPrice.output_price_cny_per_million) : '')
  const editPrice = (field: 'input' | 'output', value: string): void => {
    setPriceDraft((current) => ({ ...(current?.scope === priceScope ? current : {}), scope: priceScope, [field]: value }))
  }
  const priceSavedMessage = '单价依据已保存。它只用于估算之后的调用，不会改写历史费用。'
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const load = async (): Promise<void> => {
    setLoading(true); setMessage(null)
    const pricingGeneration = priceReadGeneration.current
    try {
      const [nextCalls, nextPricing] = await Promise.all([
        localApi.modelCalls(childId),
        localApi.modelPricing(),
      ])
      setCalls(nextCalls)
      if (pricingGeneration === priceReadGeneration.current) setPricing(nextPricing)
      void localApi.modelRuntimePolicy().then(setRuntimeStatus).catch(() => setRuntimeStatus(null))
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '模型运行记录暂时无法读取。')
    } finally { setLoading(false) }
  }

  useEffect(() => { setCalls([]); setPricing([]); void load() }, [childId])
  const savePricing = async (): Promise<void> => {
    const input = Number(inputPrice)
    const output = Number(outputPrice)
    if (!model.trim() || inputPrice === '' || outputPrice === '' || !Number.isFinite(input) || !Number.isFinite(output) || input < 0 || output < 0) return
    const submittedDraft = priceDraft
    setBusy(true); setMessage(null)
    try {
      const saved = await localApi.saveModelPricing({
        provider, model: model.trim(),
        input_price_cny_per_million: input,
        output_price_cny_per_million: output,
      })
      priceReadGeneration.current += 1
      setPricing((current) => [saved, ...current.filter((item) => item.provider !== saved.provider || item.model !== saved.model)])
      setPriceDraft((current) => current === submittedDraft ? null : current)
      setMessage(priceSavedMessage)
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '单价依据没有保存。')
    } finally { setBusy(false) }
  }

  const saveRuntimePolicy = async (): Promise<void> => {
    if (!runtimeStatus) return
    setBusy(true); setMessage(null)
    try {
      const saved = await localApi.saveModelRuntimePolicy(runtimeStatus.policy)
      setRuntimeStatus(saved)
      setMessage('模型运行策略已保存。超限时只延后后台生成，不中断孩子当前对话和讲题。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '模型运行策略没有保存。')
    } finally { setBusy(false) }
  }

  const completed = calls.filter((item) => item.status === 'completed').length
  const failed = calls.filter((item) => item.status === 'failed').length
  const fallbackCount = calls.filter((item) => Boolean(item.fallback_used)).length
  const pricedCalls = calls.filter((item) => item.estimated_cost_cny !== null)
  const totalCost = pricedCalls.reduce((sum, item) => sum + (item.estimated_cost_cny ?? 0), 0)
  return (
    <section className="model-operations" aria-label="模型调用与费用">
      <div className="model-operations-heading">
        <div><span>模型可观测性</span><h2>调用、降级与估算费用</h2><p>只展示运维字段，不展示孩子的问题、回答或模型上下文。</p></div>
        <button className="icon-button" title="刷新模型记录" disabled={loading || busy} onClick={() => void load()}><RefreshCw size={18} /></button>
      </div>
      <div className="model-pricing-form">
        <label>服务商<select value={provider} onChange={(event) => { setProvider(event.target.value as 'qwen' | 'deepseek'); setPriceDraft(null) }}><option value="qwen">阿里云千问</option><option value="deepseek">DeepSeek</option></select></label>
        <label>模型<input aria-label="计费模型" value={model} maxLength={120} onChange={(event) => { setModel(event.target.value); setPriceDraft(null) }} /></label>
        <label>输入单价<small>元/百万token</small><input aria-label="输入单价" type="number" min="0" step="0.01" value={inputPrice} onChange={(event) => editPrice('input', event.target.value)} /></label>
        <label>输出单价<small>元/百万token</small><input aria-label="输出单价" type="number" min="0" step="0.01" value={outputPrice} onChange={(event) => editPrice('output', event.target.value)} /></label>
        <button className="secondary-button" disabled={busy || !model.trim() || inputPrice === '' || outputPrice === '' || Number(inputPrice) < 0 || Number(outputPrice) < 0} onClick={() => void savePricing()}>保存单价依据</button>
      </div>
      <small className="pricing-disclaimer">估算费用按家长录入的公开单价和API返回token计算，不等同于服务商最终账单；未录入单价时明确标为“未估算”。</small>
      {runtimeStatus && <>
        <div className="model-runtime-policy">
          <label>上限模式<select aria-label="模型上限模式" value={runtimeStatus.policy.enforcement_mode} onChange={(event) => setRuntimeStatus((current) => current && ({ ...current, policy: { ...current.policy, enforcement_mode: event.target.value as 'monitor' | 'enforce' } }))}><option value="monitor">只提醒</option><option value="enforce">延后后台任务</option></select></label>
          <label>每日费用上限<input aria-label="每日模型费用上限" type="number" min="0.1" step="0.1" value={runtimeStatus.policy.daily_cost_limit_cny} onChange={(event) => setRuntimeStatus((current) => current && ({ ...current, policy: { ...current.policy, daily_cost_limit_cny: Number(event.target.value) } }))} /></label>
          <label>每日token上限<input aria-label="每日模型token上限" type="number" min="10000" step="10000" value={runtimeStatus.policy.daily_token_limit} onChange={(event) => setRuntimeStatus((current) => current && ({ ...current, policy: { ...current.policy, daily_token_limit: Number(event.target.value) } }))} /></label>
          <label>连续失败阈值<input aria-label="模型连续失败阈值" type="number" min="1" max="20" value={runtimeStatus.policy.failure_threshold} onChange={(event) => setRuntimeStatus((current) => current && ({ ...current, policy: { ...current.policy, failure_threshold: Number(event.target.value) } }))} /></label>
          <label>冷却秒数<input aria-label="模型降级冷却秒数" type="number" min="30" max="3600" value={runtimeStatus.policy.cooldown_seconds} onChange={(event) => setRuntimeStatus((current) => current && ({ ...current, policy: { ...current.policy, cooldown_seconds: Number(event.target.value) } }))} /></label>
          <button className="secondary-button" disabled={busy} onClick={() => void saveRuntimePolicy()}>保存运行策略</button>
        </div>
        <div className="model-runtime-summary">
          <span>今日 {runtimeStatus.today.calls} 次调用 · {runtimeStatus.today.tokens} token · 估算 ¥{runtimeStatus.today.estimated_cost_cny.toFixed(4)}</span>
          <b>{runtimeStatus.today.cost_limit_reached || runtimeStatus.today.token_limit_reached ? '已达上限' : '额度正常'}</b>
          {runtimeStatus.routes.map((route) => <small key={`${route.provider}-${route.model}`}>{route.provider} / {route.model}：{modelRouteStatusLabel(route.status)}{route.last_error_code ? ` · ${route.last_error_code}` : ''}</small>)}
          {runtimeStatus.purpose_quality.filter((item) => item.state !== 'healthy').map((item) => <small key={item.purpose}>{modelPurposeLabel(item.purpose)}：{item.state === 'blocked' ? '质量门已暂停后台调用' : '质量需观察'} · 近7日异常率 {Math.round(item.bad_result_rate * 100)}%</small>)}
        </div>
      </>}
      <div className="model-call-summary">
        <div><span>最近调用</span><strong>{calls.length}</strong></div>
        <div><span>完成 / 失败</span><strong>{completed} / {failed}</strong></div>
        <div><span>跨服务商降级</span><strong>{fallbackCount}</strong></div>
        <div><span>已估算合计</span><strong>{pricedCalls.length ? `¥${totalCost.toFixed(6)}` : '未估算'}</strong></div>
      </div>
      {loading ? <div className="model-call-empty">正在读取模型运行记录...</div> : calls.length === 0 ? <div className="model-call-empty">当前孩子还没有模型调用。讲题后会在这里出现可追溯记录。</div> : (
        <div className="model-call-list">
          {calls.map((item, index) => <div className={`is-${item.status}`} key={`${item.created_at}-${index}`}>
            <div><strong>{modelPurposeLabel(item.purpose)}</strong><span>{item.provider} / {item.model}{item.fallback_used ? ' · 已降级' : ''}</span></div>
            <div><b>{modelCallStatusLabel(item)}</b><span>{item.latency_ms == null ? '无延迟数据' : `${item.latency_ms}毫秒`}</span></div>
            <div><span>token</span><strong>{(item.prompt_tokens ?? 0) + (item.completion_tokens ?? 0)}</strong></div>
            <div><span>估算费用</span><strong>{formatEstimatedCost(item.estimated_cost_cny)}</strong></div>
            <small>{new Date(item.created_at).toLocaleString()} · 安全{modelAuditLabel(item.safety_status)} · 核验{modelAuditLabel(item.verification_status)}{item.privacy_summary.transmitted === false ? ' · 云发送已阻断' : item.privacy_summary.policy_version ? ` · 脱敏${item.privacy_summary.redaction_count}项 · 同学科回忆${item.privacy_summary.transmitted_recall_count ?? 0}条` : ' · 旧记录无脱敏摘要'}{item.error_code ? ` · ${item.error_code}` : ''}</small>
          </div>)}
        </div>
      )}
      {message && <p className="operation-message">{message === priceSavedMessage && currentDraft
        ? '上一版单价依据已保存；当前修改尚未保存。' : message}</p>}
    </section>
  )
}

function modelRouteStatusLabel(status: ModelRuntimePolicyStatus['routes'][number]['status']): string {
  return ({ healthy: '正常', degraded: '暂时不稳定', degraded_recoverable: '可恢复试探', rate_limited: '限流冷却中', quota_exhausted: '配额已用尽', auth_failed: '凭据失效' } as const)[status]
}

function modelPurposeLabel(purpose: string): string {
  if (purpose.startsWith('tutor_hint_level_')) return `讲题第${purpose.replace('tutor_hint_level_', '')}级提示`
  return ({
    voice_agent_intent: '语音意图识别',
    companion_dialogue: '短陪伴对话',
    tutor_math_verification: '数学完整讲解独立核验',
    tutor_privacy_block: '敏感文本本地阻断',
  } as Record<string, string>)[purpose] ?? '学习模型调用'
}

function modelCallStatusLabel(item: ModelCallAudit): string {
  if (item.status === 'completed') return '已完成'
  if (item.status === 'started') return '调用中'
  return item.error_code === 'APP_RESTART' ? '应用中断' : '失败'
}

function modelAuditLabel(value: string | null): string {
  return ({ passed: '通过', blocked: '已阻断', blocked_replaced: '已安全替换', answer_leak_replaced: '已拦截答案泄露', degraded: '降级', cross_provider_agreed: '交叉核对通过', not_applicable_hint: '不适用', rejected: '未通过' } as Record<string, string>)[value ?? ''] ?? '未记录'
}

function formatEstimatedCost(value: number | null): string {
  if (value === null) return '未估算'
  if (value > 0 && value < 0.000001) return '<¥0.000001'
  return `¥${value.toFixed(6)}`
}

export function WeeklyGoalsPanel({ goals }: { goals: WeeklyGoal[] }): React.JSX.Element | null {
  if (goals.length === 0) return null
  const labels = { pet: 'PET 听说读写', competition_math: '竞赛数学思维' }
  return (
    <section className="weekly-goals" aria-label="本周长期目标">
      <div><span>本周长期目标</span><h2>本周执行量与学习结果</h2><small>{goals[0].week_start} 至 {goals[0].week_end}</small></div>
      <div className="weekly-goal-list">
        {goals.map((goal) => {
          const minutePercent = Math.min(100, Math.round(goal.completed_minutes / Math.max(1, goal.target_minutes) * 100))
          return <article key={goal.weekly_goal_id}>
            <div><strong>{labels[goal.track]}</strong><span className={`weekly-goal-status is-${goal.status}`}>{goalStatusLabel[goal.participation_status ?? goal.status]}</span></div>
            <div className="weekly-goal-meter" aria-label={`${labels[goal.track]}分钟执行量${minutePercent}%`}><i style={{ width: `${minutePercent}%` }} /></div>
            <p>{goal.completed_sessions}/{goal.target_sessions}次 · {goal.completed_minutes}/{goal.target_minutes}分钟</p>
            <GoalEvidenceExplanation goal={goal} />
          </article>
        })}
      </div>
    </section>
  )
}

export function StrategyEvolutionPanel({ childId, initial }: { childId: string; initial: StrategyExperiment[] }): React.JSX.Element {
  const [experiments, setExperiments] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const current = experiments.find((item) => ['draft', 'replay_passed', 'shadow', 'active'].includes(item.status)) ?? experiments[0]
  const statusText: Record<StrategyExperiment['status'], string> = {
    draft: '候选待评估', replay_passed: '历史回放通过', shadow: '影子观察中', active: '已启用', rolled_back: '已恢复基线', rejected: '已拒绝'
  }
  useEffect(() => { setExperiments(initial); setMessage(null) }, [childId, initial])

  const refresh = async (): Promise<void> => setExperiments(await localApi.strategyExperiments(childId))
  const run = async (operation: () => Promise<StrategyExperiment>, success: string): Promise<void> => {
    setBusy(true); setMessage(null)
    try {
      await operation()
      await refresh()
      setMessage(success)
    } catch (cause) {
      await refresh().catch(() => undefined)
      setMessage(cause instanceof ApiError ? cause.message : '讲题策略状态没有更新，请重试。')
    } finally { setBusy(false) }
  }
  const understood = current?.replay_metrics.tutor_understood_rate
  const eligibleShadowTurns = current?.status === 'shadow' ? (current.phase_metrics.eligible_turns ?? 0) : (current?.shadow_metrics.eligible_turns ?? 0)
  return (
    <section className="strategy-evolution" aria-label="讲题提示策略">
      <div className="strategy-heading"><div><span>受控策略进化</span><h2>讲题提示节奏</h2></div><b className={`is-${current?.status ?? 'baseline'}`}>{current ? statusText[current.status] : '基线策略'}</b></div>
      <p className="strategy-summary">候选只尝试缩短前3级提示，先让孩子复述已知条件，再给下一小步。它不能改答案边界、安全、隐私或家庭规则。</p>
      {current && <>
        <div className="strategy-metrics">
          <div><span>历史讲题反馈</span><strong>{current.replay_metrics.tutor_feedback_count ?? 0}次</strong></div>
          <div><span>历史自诉理解率</span><strong>{understood == null ? '待取证' : `${Math.round(understood * 100)}%`}</strong></div>
          <div><span>影子适用回合</span><strong>{eligibleShadowTurns}/3</strong></div>
          <div><span>活跃期理解反馈</span><strong>{current.active_metrics.feedback_count ?? 0}次</strong></div>
        </div>
        <div className="strategy-boundaries"><strong>始终冻结</strong>{current.frozen_boundaries.map((item) => <span key={item}><Check size={14} />{item}</span>)}</div>
        {current.rollback_reason && <p className="strategy-rollback"><RotateCcw size={16} />{current.rollback_reason}</p>}
      </>}
      <div className="strategy-actions">
        {!current || ['rolled_back', 'rejected'].includes(current.status) ? <button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.createStrategyCandidate(childId), '已创建一个低风险候选，孩子仍使用原策略。')}>生成低风险候选</button> : null}
        {current?.status === 'draft' && <button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.replayStrategy(current.experiment_id), '历史回放通过，可以进入不影响孩子的影子观察。')}><History size={16} />运行历史回放</button>}
        {current?.status === 'replay_passed' && <button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.shadowStrategy(current.experiment_id), '已开始影子观察，孩子实际收到的提示不变。')}><Clock size={16} />开始影子观察</button>}
        {current?.status === 'shadow' && <button className="primary-button" disabled={busy || eligibleShadowTurns < 3} onClick={() => void run(() => localApi.activateStrategy(current.experiment_id), '候选策略已启用，系统会持续监测真实反馈。')}><Check size={16} />启用候选</button>}
        {current && ['replay_passed', 'shadow', 'active'].includes(current.status) && <button className="danger-button" disabled={busy} onClick={() => void run(() => localApi.rollbackStrategy(current.experiment_id, '家长手动停止当前候选，恢复基线提示策略。'), '已恢复基线提示策略。')}><RotateCcw size={16} />恢复原策略</button>}
      </div>
      {current?.status === 'shadow' && eligibleShadowTurns < 3 && <small>还需{3 - eligibleShadowTurns}个适用讲题回合才能启用。影子期不改变孩子收到的内容。</small>}
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}

const weaknessStatusText: Record<WeaknessCluster['diagnosis_status'], string> = {
  suspected: '待核实',
  emerging_pattern: '重复出现',
  confirmed_gap: '已确认薄弱点',
  improving: '正在改善',
  stable: '近期稳定'
}

const currentLearningStatusText: Record<string, string> = {
  needs_evidence: '待核实', recheck_needed: '需要重新核实', supported_only: '仍需独立检查',
  independent_observed: '已有独立作答', retention_observed: '已有延迟复测证据'
}

const currentLearningNextText: Record<string, string> = {
  needs_evidence: '当前缺少可核验作答，先核实，不据此判断会或不会。',
  recheck_needed: '最近有效作答仍有错误，先核实卡住的步骤。',
  supported_only: '减少提示后换题检查，不把辅助完成算作独立掌握。',
  independent_observed: '安排跨天复测，继续核实能否独立完成不同题目。',
  retention_observed: '继续核实迁移能力，不凭一次延迟复测宣布全部掌握。'
}

export function MistakeWeaknessPanel({ childId }: { childId: string }): React.JSX.Element {
  const [items, setItems] = useState<WeaknessCluster[]>([])
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = async (): Promise<void> => {
    setBusy(true)
    setError(null)
    try {
      setItems(await localApi.weaknessClusters(childId))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '薄弱点证据暂时无法读取。')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => { void load() }, [childId])

  return (
    <section className="mistake-weakness-panel" aria-label="薄弱点与错题复测">
      <div className="mistake-weakness-heading">
        <div><span>错题证据链</span><h2>薄弱点与复测进展</h2></div>
        <button className="icon-button" title="刷新薄弱点" disabled={busy} onClick={() => void load()}><RefreshCw size={16} /></button>
      </div>
      {busy && items.length === 0 ? <p className="muted-copy">正在汇总错题证据...</p> : error ? (
        <div className="inline-recovery"><p>{error}</p><button className="secondary-button" onClick={() => void load()}>重试</button></div>
      ) : items.length === 0 ? (
        <div className="mistake-weakness-empty"><strong>暂时没有已形成的薄弱点</strong><span>系统只根据真实错题和复测证据更新，不把未作答当作不会。</span></div>
      ) : (
        <div className="mistake-weakness-list">
          {items.map((item) => {
            const current = item.current_evidence
            const mastery = item.mastery_probability == null ? null : Math.round(item.mastery_probability * 100)
            const nextReview = item.next_review_at ? new Date(item.next_review_at) : null
            return <article key={item.cluster_id}>
              <div className="mistake-weakness-title"><div><span>{item.subject}</span><strong>{item.title}</strong></div><b className={`is-${current ? current.status : item.diagnosis_status}`}>{current ? currentLearningStatusText[current.status] : `历史状态：${weaknessStatusText[item.diagnosis_status]}`}</b></div>
              <div className="mistake-weakness-metrics">
                <span><strong>{item.occurrence_count}</strong>次错题记录</span>
                <span><strong>{item.relapse_count}</strong>次历史回退</span>
                <span><strong>{current ? current.independent_correct : mastery == null ? '--' : `${mastery}%`}</strong>{current ? '次有效独立正确' : '历史掌握估计'}</span>
                <span><strong>{current ? current.retention_success_count : item.hint_dependency == null ? '--' : `${Math.round(item.hint_dependency * 100)}%`}</strong>{current ? '次有效延迟复测' : '历史提示依赖'}</span>
              </div>
              <p>{current ? currentLearningNextText[current.status] : nextReview ? `历史计划下次复测：${nextReview.toLocaleString()}，执行前需重新核实。` : '旧记录需重新核实，不能据此决定是否追加练习。'}</p>
            </article>
          })}
        </div>
      )}
      <small>“稳定”需要迁移题和延迟复测共同通过；一次做对不会直接标记为掌握。</small>
    </section>
  )
}

export function LearningOverview({ state }: { state: LearningState }): React.JSX.Element {
  const [openNodeId, setOpenNodeId] = useState<string | null>(null)
  const [historyByNode, setHistoryByNode] = useState<Record<string, MasteryHistoryItem[]>>({})
  const [historyBusy, setHistoryBusy] = useState<string | null>(null)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const tracks = [
    ['school', '校内学习'],
    ['pet', 'PET能力'],
    ['competition_math', '竞赛数学思维']
  ]
  const toggleHistory = async (knowledgeNodeId: string): Promise<void> => {
    if (openNodeId === knowledgeNodeId) {
      setOpenNodeId(null)
      return
    }
    setOpenNodeId(knowledgeNodeId)
    setHistoryError(null)
    if (historyByNode[knowledgeNodeId]) return
    setHistoryBusy(knowledgeNodeId)
    try {
      const items = await localApi.masteryHistory(state.child_id, knowledgeNodeId)
      setHistoryByNode((current) => ({ ...current, [knowledgeNodeId]: items }))
    } catch (cause) {
      setHistoryError(cause instanceof ApiError ? cause.message : '掌握度依据暂时无法读取。')
    } finally {
      setHistoryBusy(null)
    }
  }
  return (
    <section className="learning-overview">
      <div><span>可解释学习状态</span><h2>知识与能力证据</h2></div>
      {state.pet_baseline && <PetBaselineProgress baseline={state.pet_baseline} />}
      <div className="learning-tracks">
        {tracks.map(([track, label]) => {
          const items = state.items.filter((item) => item.track === track)
          if (items.length === 0) return null
          return (
            <div className="learning-track" key={track}>
              <h3>{label}</h3>
              {items.map((item) => {
                const current = item.current_evidence
                const assessed = current ? current.attempt_count > 0 : item.evidence_count !== null && item.evidence_count > 0
                const hasHistory = assessed || (item.evidence_count ?? 0) > 0
                const percent = Math.round((item.mastery_probability ?? 0) * 100)
                const history = historyByNode[item.knowledge_node_id] ?? []
                return <div className="mastery-item" key={item.knowledge_node_id}>
                    <div className={`mastery-row${current ? ' is-current' : ''}`}>
                    <div><strong>{item.title}</strong><span>{current ? currentLearningStatusText[current.status] : assessed ? `历史估计 · ${item.evidence_count}条记录 · 置信度${Math.round((item.status_confidence ?? 0) * 100)}%` : '尚未评估'}</span>{current && <span>{current.independent_correct}次独立正确 · {current.supported_attempt_count}次辅助作答 · {current.retention_success_count}次延迟复测</span>}</div>
                    {!current && <><div className="mastery-meter"><i style={{ width: assessed ? `${percent}%` : '0%' }} /></div><b>{assessed ? `${percent}%` : '--'}</b></>}
                    <button className="icon-button" disabled={!hasHistory || historyBusy === item.knowledge_node_id} title={openNodeId === item.knowledge_node_id ? '收起掌握度依据' : '查看掌握度依据'} onClick={() => void toggleHistory(item.knowledge_node_id)}><History size={15} /></button>
                    </div>
                  {(item.prerequisites.length > 0 || item.next_paths.length > 0) && <div className="mastery-path"><span>{item.prerequisites.length > 0 ? `前置：${item.prerequisites.map((entry) => `${entry.title}${entry.readiness === 'ready' ? '（有核实证据）' : '（待取证）'}`).join('、')}` : '没有已登记的前置能力'}</span>{item.next_paths.length > 0 && <span>后续可核实：{item.next_paths.map((entry) => entry.title).join('、')}</span>}</div>}
                  {openNodeId === item.knowledge_node_id && <div className="mastery-history">
                    {historyBusy === item.knowledge_node_id ? <p>正在读取依据...</p> : historyError ? <p>{historyError}</p> : history.length === 0 ? <p>尚无可追溯更新。</p> : history.map((entry, index) => <div key={`${entry.created_at}-${index}`}><strong>{entry.evidence_summary}</strong><span>{Math.round(entry.old_state.mastery_probability * 100)}% → {Math.round(entry.new_state.mastery_probability * 100)}% · 证据置信度{Math.round(entry.confidence * 100)}%</span><small>{new Date(entry.occurred_at).toLocaleString()} · {entry.independent ? '独立作答' : '辅助证据'} · 下次复测{entry.new_state.next_review_at ? new Date(entry.new_state.next_review_at).toLocaleString() : '待安排'}</small></div>)}</div>}
                </div>
              })}
            </div>
          )
        })}
      </div>
      <small>掌握度只根据可追溯学习证据更新；尚未评估不等于不会。</small>
    </section>
  )
}

export function ReportDetail({ report }: { report: DailyReport }): React.JSX.Element {
  const boundAction = report.parent_action?.binding_status === 'bound' ? report.parent_action : null
  const reviewUnavailable = report.parent_action?.binding_status === 'unknown' && report.parent_action.reason.startsWith('review_')
  const coaching = boundAction?.coaching ?? report.parent_coaching
  return (
    <article className="report-detail">
      <div className="report-title"><span>{report.report_date} · {['local_rules_v1', 'local_rules_v2', 'local_rules_revalidated_v3'].includes(report.source) ? '本地证据规则' : report.source}</span><h2>{report.summary.headline}</h2><p>{report.summary.interpretation}</p></div>
      <section>
        <h3>今天发生了什么</h3>
        <ul>{report.summary.facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
        <small>共{report.summary.evidence_count}条确认记录</small>
        <details className="report-evidence">
          <summary>查看证据明细</summary>
          <div>
            <h4>课堂回想</h4>
            {report.evidence_detail.recalls.length === 0 ? <p>{report.evidence_detail.declarations.no_recall ? '孩子明确表示今天暂时想不起课堂内容；系统没有代为补写。' : '没有已确认的课堂记录。'}</p> : report.evidence_detail.recalls.map((item, index) => <p key={`${item.subject}-${index}`}><b>{item.subject}：</b>{item.summary}<small>{item.confirmation_status === 'child_confirmed' ? '孩子确认' : '待确认候选'}</small></p>)}
            <h4>任务执行</h4>
            {report.evidence_detail.declarations.no_homework && <p>孩子确认今天没有校内作业；下列内容如有记录，均为长期目标或复测任务。</p>}
            {report.evidence_detail.tasks.length === 0 && <p>{report.evidence_detail.declarations.no_homework ? '系统没有生成虚构任务。' : '没有已确认的任务记录。'}</p>}
            {report.evidence_detail.tasks.map((item, index) => <p key={`${item.title}-${index}`}><b>{item.subject} · {item.title}：</b>{item.status === 'completed' ? `记录完成${item.actual_minutes ?? item.expected_minutes}分钟` : item.status === 'rescheduled' ? '已延期' : '未完成'}{item.reschedule_reason ? ` · 原因：${item.reschedule_reason}` : ''}{item.completion_evidence_type ? ` · ${completionEvidenceText(item.completion_evidence_type)}` : ''}{item.completion_checklist?.length ? ` · 已核对${item.completion_checklist.length}项` : ''}{item.completion_artifact_id ? ` · ${item.completion_artifact_raw_stored ? '完成页原图已加密保存' : '完成页已检查，原图未保留'}（${item.completion_artifact_status === 'completed' ? '质量通过' : '有质量提醒'}）` : ''}{item.completion_note ? ` · ${item.completion_note}` : ''}</p>)}
            {report.evidence_detail.reflection && <><h4>孩子复盘</h4>{report.evidence_detail.reflection.skipped ? <p>孩子今天选择跳过，系统不对空白内容做推断。</p> : <><p><b>自述困难：</b>{report.evidence_detail.reflection.difficult_text || '未填写'}</p><p><b>下一步：</b>{report.evidence_detail.reflection.next_action || '未填写'}</p></>}</>}
          </div>
        </details>
      </section>
      <section><h3>建议沟通场景</h3><p>{coaching.scenario}</p><p className="muted-copy">{coaching.why_this_fits}</p>{coaching.evidence_basis?.length ? <div className="coaching-basis"><strong>本次建议依据</strong><ul>{coaching.evidence_basis.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}</section>
      {!reviewUnavailable && coaching.script.length > 0 && <section><h3>{boundAction?.review_binding?.decision === 'custom' ? '家长修改后的话术' : '可以直接这样说'}</h3><ol className="script-list">{coaching.script.map((line, index) => <li key={line}><i>{index + 1}</i><span>{line}</span></li>)}</ol></section>}
      {!reviewUnavailable && coaching.action && <section className="action-band"><Clock size={22} /><div><h3>今天具体行动 · {coaching.action.duration_minutes}分钟</h3><strong>{coaching.action.what}</strong>{coaching.action.steps?.length ? <ol>{coaching.action.steps.map((step) => <li key={step}>{step}</li>)}</ol> : <p>旧版报告未记录分步行动；请只执行上面的简短行动，不自行补加要求。</p>}{coaching.action.stop_condition ? <p>{coaching.action.stop_condition}</p> : null}</div></section>}
      <section><h3>避免事项与替代表达</h3><div className="avoid-list">{coaching.avoid.map((item) => <div key={item.do_not_say}><p><b>不要说：</b>{item.do_not_say}</p><p><b>可以说：</b>{item.say_instead}</p></div>)}</div></section>
      {report.parent_action?.binding_status === 'bound'
        ? <ParentActionFeedbackPanel key={report.parent_action.snapshot_id} snapshot={report.parent_action} />
        : reviewUnavailable ? <p role="status">这份行动的审核或来源已变化，请在报告审核中重新核对采用方式。历史反馈保留，但不用于当前行动效果判断。</p>
          : <><p className="muted-copy">这份历史建议没有冻结行动版本，反馈仅保留为记录，不用于判断行动效果。</p><ReportFeedbackPanel report={report} /></>}
      <HistoricalParentActionFeedback childId={report.child_id} reportId={report.report_id} excludeSnapshotId={boundAction?.snapshot_id} />
    </article>
  )
}

function completionEvidenceText(value: string): string {
  return ({ child_checked: '孩子确认已检查', child_confirmed: '孩子确认完成', voice_confirmed: '语音确认完成', needs_correction: '仍需订正', planned_break: '计划休息', review_evidence: '复测已记录，不等于长期掌握' } as Record<string, string>)[value] ?? '完成记录'
}

function ReportFeedbackPanel({ report }: { report: DailyReport }): React.JSX.Element {
  const [executed, setExecuted] = useState(report.feedback?.executed ?? false)
  const [effectiveness, setEffectiveness] = useState(report.feedback?.effectiveness ?? 3)
  const [stress, setStress] = useState(report.feedback?.stress_rating ?? 3)
  const [notes, setNotes] = useState(report.feedback?.notes_summary ?? '')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => {
    setExecuted(report.feedback?.executed ?? false)
    setEffectiveness(report.feedback?.effectiveness ?? 3)
    setStress(report.feedback?.stress_rating ?? 3)
    setNotes(report.feedback?.notes_summary ?? '')
    setMessage(null)
  }, [report.report_id, report.feedback])
  const save = async (): Promise<void> => {
    setBusy(true); setMessage(null)
    try {
      await localApi.saveParentReportFeedback(report.report_id, {
        executed,
        effectiveness: executed ? effectiveness : null,
        stress_rating: executed ? stress : null,
        notes_summary: notes.trim()
      })
      setMessage('反馈已保存，后续建议会参考效果和沟通压力，但不会据此给孩子贴标签。')
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '反馈没有保存。') } finally { setBusy(false) }
  }
  return (
    <section className="report-feedback">
      <h3>执行反馈</h3>
      <p className="muted-copy">只记录这套建议在你们家是否可用，用于调整下一次话术和行动时长。</p>
      <div className="feedback-mode" role="group" aria-label="是否执行建议">
        <button className={!executed ? 'is-active' : ''} onClick={() => setExecuted(false)}>暂未执行</button>
        <button className={executed ? 'is-active' : ''} onClick={() => setExecuted(true)}>已经执行</button>
      </div>
      {executed && <div className="feedback-ratings">
        <label>实际效果<select value={effectiveness} onChange={(event) => setEffectiveness(Number(event.target.value))}>{[1, 2, 3, 4, 5].map((value) => <option value={value} key={value}>{value} · {value === 1 ? '没有帮助' : value === 5 ? '很有帮助' : '一般'}</option>)}</select></label>
        <label>沟通压力<select value={stress} onChange={(event) => setStress(Number(event.target.value))}>{[1, 2, 3, 4, 5].map((value) => <option value={value} key={value}>{value} · {value === 1 ? '很轻松' : value === 5 ? '压力很大' : '一般'}</option>)}</select></label>
      </div>}
      <label>家庭备注<textarea maxLength={500} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="例如：孩子更接受先听他说完，再一起定时。" /></label>
      <button className="secondary-button" disabled={busy} onClick={() => void save()}>{busy ? '正在保存' : '保存反馈'}</button>
      {message && <p className="operation-message">{message}</p>}
    </section>
  )
}
