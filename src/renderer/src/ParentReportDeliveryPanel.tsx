import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { ApiError, localApi, requestParentLocal } from './api'
import type { DailyReport, ParentReportSnapshot, ReportDelivery, ReportDeliveryRecipient } from './types'
import { ReportFeedbackHistory } from './ReportFeedbackHistory'
import { ReportPlanAdjustments } from './ReportPlanAdjustments'
import { PeriodReportRebinding } from './PeriodReportRebinding'
import { clearPeriodRebindingLocalState } from './reportRebindingContract'
import { HistoricalParentActionFeedback, ParentActionFeedbackPanel } from './ParentActionFeedbackPanel'
import { ReportSourceIssues } from './ReportSourceIssues'
import { PeriodSupplyFollowup } from './PeriodSupplyFollowup'
import { useParentWorkFocus, type ParentWorkFocusProps } from './parentWorkFocus'
import './ParentReportDeliveryPanel.css'

type Props = { childId: string; refreshToken?: number; onReviewChanged?: () => Promise<void>; onOpenSupply?: (rootId: string) => Promise<void> } & ParentWorkFocusProps
type PendingFeedbackSave = { client_action_id: string; helpfulness: number | null; stress_rating: number | null; notes: string | null }
type PendingPeriodAction = { kind: 'retract' | 'restore'; evidence_ref: string; note: string; client_action_id: string }
const periodActionStorageKey = (childId: string): string => `hsc:period-evidence-pending:${encodeURIComponent(childId)}`

export function clearReportPeriodEvidenceLocalState(childId?: string): void {
  clearPeriodRebindingLocalState(childId)
  if (childId !== undefined) {
    window.localStorage.removeItem(periodActionStorageKey(childId))
    return
  }
  for (const key of Object.keys(window.localStorage)) {
    if (key.startsWith('hsc:period-evidence-pending:')) window.localStorage.removeItem(key)
  }
}

function readPendingPeriodAction(childId: string): PendingPeriodAction | null {
  try {
    const value = JSON.parse(window.localStorage.getItem(periodActionStorageKey(childId)) ?? 'null')
    return value && ['retract', 'restore'].includes(value.kind) && typeof value.evidence_ref === 'string'
      && typeof value.note === 'string' && typeof value.client_action_id === 'string' ? value : null
  } catch { return null }
}
type ChannelTest = { channel: 'email' | 'wechat_work_bot'; status: 'passed' | 'failed' | 'blocked' | 'processing' | 'unknown' | null; message: string; tested_at: string | null; client_action_id?: string; recipient_id?: string | null; verified_recipient_ids?: string[]; kept_without_resend?: boolean }

/**
 * An intentionally standalone panel: ParentCenter can embed it without taking
 * ownership of delivery state or exposing internal identifiers in its main UI.
 */
type PendingSavedEditCarry = { child_id: string; target_snapshot_id: string; source_snapshot_id: string; source_edit_version: string; target_edit_version: string; client_action_id: string }
const savedEditCarryKey = (childId: string): string => `hsc:report-saved-edit-carry:${childId}`
const validEditVersion = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const reportSourceRoot = (sourceId: string): string => sourceId.includes('@') ? sourceId.slice(0, sourceId.lastIndexOf('@')) : sourceId
function readSavedEditCarry(childId: string): PendingSavedEditCarry | null {
  try {
    const raw = window.localStorage.getItem(savedEditCarryKey(childId))
    if (!raw || raw.length > 2048) return null
    const value = JSON.parse(raw) as PendingSavedEditCarry
    if (Object.keys(value).sort().join(',') !== 'child_id,client_action_id,source_edit_version,source_snapshot_id,target_edit_version,target_snapshot_id' || value.child_id !== childId ||
        !validEditVersion(value.source_edit_version) || !validEditVersion(value.target_edit_version) ||
        ![value.child_id, value.source_snapshot_id, value.target_snapshot_id, value.client_action_id].every(item => typeof item === 'string' && item.length > 0 && item.length <= 128)) return null
    return value
  } catch { return null }
}

export function ParentReportDeliveryPanel({ childId, ...props }: Props): React.JSX.Element {
  return <ReportDeliveryContent key={childId} childId={childId} {...props} />
}

function ReportDeliveryContent({ childId, refreshToken = 0, onReviewChanged, focusRequest, onFocusResolved, onOpenSupply }: Props): React.JSX.Element {
  const focusRoot = useRef<HTMLElement>(null)
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [sourceRefreshVersion, setSourceRefreshVersion] = useState(0)
  const [focusedNonce, setFocusedNonce] = useState('')
  const focusAttempt = useRef('')
  const focusCallback = useRef(onFocusResolved)
  focusCallback.current = onFocusResolved
  const [snapshots, setSnapshots] = useState<ParentReportSnapshot[]>([])
  const [recipients, setRecipients] = useState<ReportDeliveryRecipient[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [deliveries, setDeliveries] = useState<ReportDelivery[]>([])
  const [deliveriesFor, setDeliveriesFor] = useState('')
  const [deliveryTotals, setDeliveryTotals] = useState<{ scheduled: number; processing: number; failed: number; unknown: number; recovered: number } | null>(null)
  const [intro, setIntro] = useState('')
  const [note, setNote] = useState('')
  const [coachingDecision, setCoachingDecision] = useState<'adopt' | 'custom' | 'skip'>('adopt')
  const [coachingScriptEdit, setCoachingScriptEdit] = useState('')
  const [coachingActionEdit, setCoachingActionEdit] = useState('')
  const [coachingDuration, setCoachingDuration] = useState(0)
  const [coachingObservation, setCoachingObservation] = useState('')
  const [address, setAddress] = useState('')
  const [label, setLabel] = useState('')
  const [channel, setChannel] = useState<'email' | 'wechat_work_bot'>('email')
  const [recipientConsent, setRecipientConsent] = useState(false)
  const [channelStatus, setChannelStatus] = useState<Array<{ channel: 'email' | 'wechat_work_bot'; configured: boolean; credential_updated_at: string | null; requires_encryption_key: boolean }>>([])
  const [secretStatus, setSecretStatus] = useState<DesktopReportDeliverySecretStatus | null>(null)
  const [channelTests, setChannelTests] = useState<ChannelTest[]>([])
  const [channelTestActions, setChannelTestActions] = useState<Record<string, string>>({})
  const [selectedEmailRecipientId, setSelectedEmailRecipientId] = useState('')
  const [reauthorizedEmail, setReauthorizedEmail] = useState<{ previousId: string; recipientId: string; address: string } | null>(null)
  const [quietStart, setQuietStart] = useState('21:00')
  const [quietEnd, setQuietEnd] = useState('07:00')
  const [retentionDays, setRetentionDays] = useState(90)
  const [redactionLevel, setRedactionLevel] = useState<'standard' | 'minimal'>('standard')
  const [smtpHost, setSmtpHost] = useState('')
  const [smtpPort, setSmtpPort] = useState('587')
  const [smtpUsername, setSmtpUsername] = useState('')
  const [smtpPassword, setSmtpPassword] = useState('')
  const [smtpFrom, setSmtpFrom] = useState('')
  const [smtpSecurity, setSmtpSecurity] = useState<'starttls' | 'ssl' | 'none'>('starttls')
  const [workWebhook, setWorkWebhook] = useState('')
  const [scheduledAt, setScheduledAt] = useState('')
  const [helpfulness, setHelpfulness] = useState<number | null>(null)
  const [stressRating, setStressRating] = useState<number | null>(null)
  const [feedbackNotes, setFeedbackNotes] = useState('')
  const [pendingFeedbackSave, setPendingFeedbackSave] = useState<PendingFeedbackSave | null>(null)
  const [retractedEvidence, setRetractedEvidence] = useState<RetractedPeriodEvidence[]>([])
  const [pendingPeriodAction, setPendingPeriodAction] = useState<PendingPeriodAction | null>(() => readPendingPeriodAction(childId))
  const [restoreNote, setRestoreNote] = useState('家长核对后恢复这条学习记录。')
  const [correctionNote, setCorrectionNote] = useState('')
  const [pendingUnknownReissue, setPendingUnknownReissue] = useState<ReportDelivery | null>(null)
  const [carrySource, setCarrySource] = useState<ParentReportSnapshot | null>(null)
  const [carryPending, setCarryPending] = useState<PendingSavedEditCarry | null>(() => readSavedEditCarry(childId))
  const currentCarryChild = useRef(childId)
  currentCarryChild.current = childId
  useEffect(() => { setCarrySource(null); setCarryPending(readSavedEditCarry(childId)) }, [childId])
  const [busy, setBusy] = useState(false)
  const [reviewRefreshFailed, setReviewRefreshFailed] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [readFailed, setReadFailed] = useState(false)
  const [deliveriesReady, setDeliveriesReady] = useState(false)
  const loadGeneration = useRef(0)
  const reportNavigation = useRef<HTMLElement | null>(null)
  const selectedIdRef = useRef(selectedId)
  selectedIdRef.current = selectedId

  const selected = snapshots.find((item) => item.snapshot_id === selectedId) ?? snapshots[0] ?? null
  const selectedEditVersion = JSON.stringify(selected?.parent_edit)
  const channelTestsProcessing = channelTests.some((item) => item.status === 'processing')
  const reload = async (): Promise<void> => {
    const generation = ++loadGeneration.current
    setRefreshing(true)
    try {
      const [loaded, recipientResult, channels, nextSecretStatus] = await Promise.all([localApi.reportSnapshots(childId), localApi.reportRecipients(childId), localApi.reportDeliveryChannels(), window.desktop?.getReportDeliverySecretStatus?.().catch(() => null) ?? Promise.resolve(null)])
      const nextSnapshots = loaded.filter((item) => item.child_id === childId)
      const historicalId = selectedIdRef.current
      let historicalDeleted = false
      let historicalUnreadable = false
      if (historicalId && !nextSnapshots.some((item) => item.snapshot_id === historicalId)) {
        try {
          const historical = await requestParentLocal<ParentReportSnapshot>(`/v1/parent/report-snapshots/${encodeURIComponent(historicalId)}`)
          if (historical.child_id !== childId || historical.snapshot_id !== historicalId) throw new Error('历史报告不属于当前孩子，未打开。')
          nextSnapshots.push(historical)
        } catch (error) {
          if (!(error instanceof ApiError) || (error.status !== 404 && error.status !== 409)) throw error
          historicalDeleted = error.status === 404
          historicalUnreadable = error.status === 409
        }
      }
      const totals = await localApi.reportDeliveryStatus(childId).catch(() => null)
      const tests = channels.some((item) => item.configured) ? await localApi.reportDeliveryChannelTests(childId).catch(() => []) : []
      const retracted = nextSnapshots.some((item) => item.report_type !== 'daily') ? await localApi.retractedPeriodEvidence(childId).catch(() => []) : []
      if (loadGeneration.current !== generation) return
      setDeliveryTotals(totals); setChannelTests(tests); setRetractedEvidence(retracted)
      setSnapshots(nextSnapshots); setRecipients(recipientResult.items.filter((item) => item.child_id === childId)); setChannelStatus(channels); setSecretStatus(nextSecretStatus)
      if (nextSecretStatus && ((channels.some((item) => item.channel === 'email' && item.configured) && !nextSecretStatus.smtpPasswordConfigured) || (channels.some((item) => item.channel === 'wechat_work_bot' && item.configured) && !nextSecretStatus.wecomWebhookConfigured))) {
        setMessage(nextSecretStatus.state === 'unavailable' ? '系统安全存储不可用；不会尝试外发。请恢复系统安全存储后重新输入通道凭据。' : '已保存的通道凭据不可用或损坏；不会尝试外发。请重新输入凭据，或删除对应通道配置。')
      }
      setQuietStart(recipientResult.preferences.quiet_start); setQuietEnd(recipientResult.preferences.quiet_end); setRetentionDays(recipientResult.preferences.retention_days); setRedactionLevel(recipientResult.preferences.redaction_level)
      if (historicalDeleted) setMessage('已选历史报告已删除或不可访问，已返回当前报告列表。')
      if (historicalUnreadable) setMessage('已选历史快照暂不能读取，旧稿仍保留；请核对下方来源问题。')
      setSelectedId((current) => nextSnapshots.some((item) => item.snapshot_id === current) ? current : nextSnapshots[0]?.snapshot_id ?? null)
      setReadFailed(false)
      setLoadedOnce(true)
      setSourceRefreshVersion(value => value + 1)
    } catch (error) {
      if (loadGeneration.current === generation) { setReadFailed(true); throw error }
    } finally {
      if (loadGeneration.current === generation) setRefreshing(false)
    }
  }
  useEffect(() => { void reload().catch(showError); return () => { loadGeneration.current += 1 } }, [childId, refreshToken]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!focusRequest || refreshing || focusAttempt.current === focusRequest.nonce) return
    if (readFailed) { focusAttempt.current = focusRequest.nonce; focusCallback.current?.(focusRequest.nonce, '报告来源未能读取，请刷新后重试。'); return }
    if (!loadedOnce) return
    focusAttempt.current = focusRequest.nonce
    let current = true
    const target = focusRequest.relatedId || focusRequest.objectId
    void requestParentLocal<ParentReportSnapshot>(`/v1/parent/report-snapshots/${encodeURIComponent(target)}`).then(value => {
      if (!current) return
      if (value.child_id !== childId || value.snapshot_id !== target) throw new Error('报告对象归属不一致')
      setDeliveriesReady(false)
      setSnapshots(items => [...items.filter(item => item.snapshot_id !== target), value])
      setSelectedId(target); setFocusedNonce(focusRequest.nonce)
    }).catch(() => { if (current) focusCallback.current?.(focusRequest.nonce, '指定报告已删除、不可访问或未能读取；没有以首条报告替代。') })
    return () => { current = false }
  }, [focusRequest?.nonce, childId, loadedOnce, readFailed, refreshing]) // eslint-disable-line react-hooks/exhaustive-deps
  useParentWorkFocus(focusRequest, focusedNonce === focusRequest?.nonce && !refreshing &&
    (focusRequest?.relatedId ? deliveriesReady && deliveriesFor === selected?.snapshot_id : selected?.snapshot_id === focusRequest?.objectId),
    focusRoot, onFocusResolved)
  useEffect(() => {
    const nav = reportNavigation.current
    const active = nav?.querySelector('[aria-current="true"]')
    if (!nav || !active) return
    const bounds = nav.getBoundingClientRect()
    const item = active.getBoundingClientRect()
    // Reveal the selection inside its list without scrolling the report page.
    if (item.top < bounds.top) nav.scrollTop += item.top - bounds.top
    else if (item.bottom > bounds.bottom) nav.scrollTop += item.bottom - bounds.bottom
  }, [selected?.snapshot_id, snapshots])
  const loadSavedEdit = (report: ParentReportSnapshot): void => {
    setIntro(report.parent_edit.delivery_intro); setNote(report.parent_edit.parent_note)
    setCoachingDecision(report.parent_edit.coaching_decision ?? 'adopt'); setCoachingScriptEdit(report.parent_edit.coaching_script ?? '')
    setCoachingActionEdit(report.parent_edit.coaching_action ?? ''); setCoachingDuration(report.parent_edit.coaching_duration_minutes ?? report.content.parent_coaching.action?.duration_minutes ?? 0); setCoachingObservation(report.parent_edit.coaching_observation ?? '')
  }
  // Bind editable fields before the new snapshot becomes interactive.
  useLayoutEffect(() => {
    if (!selected) return
    loadSavedEdit(selected)
  }, [selected?.snapshot_id, selected?.status, selectedEditVersion]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let current = true
    setDeliveries([]); setDeliveriesReady(false); setPendingUnknownReissue(null)
    if (selected) void localApi.reportDeliveries(selected.snapshot_id).then((items) => {
      if (current) { setDeliveries(items.filter((item) => item.snapshot_id === selected.snapshot_id)); setDeliveriesFor(selected.snapshot_id); setDeliveriesReady(true) }
    }).catch(() => { if (current) { setMessage('当前报告的送达记录暂不可用；可重试状态查询。'); if (focusRequest?.relatedId && focusedNonce === focusRequest.nonce) focusCallback.current?.(focusRequest.nonce, '指定报告的送达证据未能读取。') } })
    return () => { current = false }
  }, [selected?.snapshot_id, snapshots]) // eslint-disable-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    if (!selected) return
    const latestFeedback = selected.feedback_current_status === 'active' ? selected.feedback_current : null
    setHelpfulness(latestFeedback?.helpfulness ?? null); setStressRating(latestFeedback?.stress_rating ?? null); setFeedbackNotes(latestFeedback?.notes ?? '')
    setPendingFeedbackSave(null)
  }, [selected?.snapshot_id, selected?.feedback_current_revision, selected?.feedback_current_status]) // eslint-disable-line react-hooks/exhaustive-deps
  const showError = (error: unknown): void => {
    if (error instanceof ApiError && error.status === 409) { setReadFailed(true); setPendingUnknownReissue(null) }
    setMessage(error instanceof Error ? error.message : '操作未完成，请稍后重试。')
  }
  const run = (action: () => Promise<void>): void => { setBusy(true); setMessage(null); void action().catch(showError).finally(() => setBusy(false)) }
  const sourceEligible = selected?.status === 'approved' && selected.correction_status?.source_current !== false
  const correctionSnapshot = selected?.correction_status?.latest_replacement_id !== undefined ? selected
    : snapshots.find((item) => item.correction_status?.child_id === childId && item.correction_status.replacements.some((link) => link.replacement_snapshot_id === selected?.snapshot_id)) ?? selected
  const sendingBlocked = busy || refreshing || readFailed || !deliveriesReady || deliveriesFor !== selected?.snapshot_id || !sourceEligible
  const openCorrection = async (snapshotId: string): Promise<void> => {
    let target = snapshots.find((item) => item.snapshot_id === snapshotId)
    if (!target) {
      target = await requestParentLocal<ParentReportSnapshot>(`/v1/parent/report-snapshots/${encodeURIComponent(snapshotId)}`)
      if (target.child_id !== childId || target.snapshot_id !== snapshotId) throw new Error('更正报告不属于当前孩子，未打开。')
      setSnapshots((items) => [...items.filter((item) => item.snapshot_id !== snapshotId), target!])
    }
    setSelectedId(target.snapshot_id)
  }
  const viewReplacementWithSavedEdits = async (): Promise<void> => {
    if (!selected || carryPending) return
    if (selectedEditDirty) throw new Error('请先保存当前修改或恢复已保存内容；没有刷新或切换报告。')
    const sourceId = selected.snapshot_id
    const ownerChild = childId
    // The existing list refresh builds current replacement drafts. It never approves.
    const offered = await localApi.reportSnapshots(ownerChild)
    const old = await requestParentLocal<ParentReportSnapshot>(`/v1/parent/report-snapshots/${encodeURIComponent(sourceId)}`)
    const nextId = old.correction_status?.latest_replacement_id
    if (old.child_id !== ownerChild || old.snapshot_id !== sourceId || old.correction_status?.source_current !== false ||
        old.correction_status.latest_replacement_availability !== 'available' || !nextId || nextId === sourceId) {
      throw new Error('同源新版尚未就绪；已保存修改仍保留在旧稿，请刷新后核对。')
    }
    const next = await requestParentLocal<ParentReportSnapshot>(`/v1/parent/report-snapshots/${encodeURIComponent(nextId)}`)
    if (next.snapshot_id !== nextId || next.child_id !== ownerChild || next.source_kind !== old.source_kind ||
        next.report_type !== old.report_type || reportSourceRoot(next.source_id) !== reportSourceRoot(old.source_id) ||
        next.content.report_date !== old.content.report_date || next.status !== 'draft' ||
        next.correction_status?.source_current !== true || !validEditVersion(old.parent_edit_version) || !validEditVersion(next.parent_edit_version)) {
      throw new Error('新版归属、周期或当前证据未能核对；没有沿用或覆盖任何修改。')
    }
    if (currentCarryChild.current !== ownerChild) return
    setSnapshots([...offered.filter(item => item.child_id === ownerChild && item.snapshot_id !== sourceId && item.snapshot_id !== nextId), old, next])
    setCarrySource(old); setSelectedId(nextId)
    setMessage('请先核对新版依据与下方旧稿修改，再明确选择是否沿用。尚未批准。')
  }
  const carrySavedEdits = async (pending?: PendingSavedEditCarry): Promise<void> => {
    const ownerChild = childId
    if (selectedEditDirty) throw new Error('请先保存当前修改或恢复已保存内容；没有沿用或覆盖输入。')
    let command = pending ?? carryPending
    if (!command) {
      if (!selected || !carrySource || selected.child_id !== ownerChild || carrySource.child_id !== ownerChild ||
          !validEditVersion(selected.parent_edit_version) || !validEditVersion(carrySource.parent_edit_version)) {
        throw new Error('请先重新核对两份同源报告。')
      }
      command = { child_id: ownerChild, target_snapshot_id: selected.snapshot_id, source_snapshot_id: carrySource.snapshot_id,
        source_edit_version: carrySource.parent_edit_version!, target_edit_version: selected.parent_edit_version!, client_action_id: crypto.randomUUID() }
      // Persist only identities/CAS tokens before sending. Unknown results reuse this one command.
      window.localStorage.setItem(savedEditCarryKey(ownerChild), JSON.stringify(command))
      setCarryPending(command)
    }
    if (command.child_id !== ownerChild) throw new Error('待核对修改不属于当前孩子。')
    const { child_id: _child, target_snapshot_id: targetId, ...request } = command
    let updated: ParentReportSnapshot
    try { updated = await localApi.carryReportSavedEdits(targetId, request) }
    catch (error) {
      // This closed code is raised only before the transfer writes. A generic
      // timeout/409 may follow a commit and must retain the original command.
      if (error instanceof ApiError && error.code === 'REPORT_EDIT_CARRY_NOT_APPLIED') {
        window.localStorage.removeItem(savedEditCarryKey(ownerChild))
        if (currentCarryChild.current === ownerChild) { setCarryPending(null); setCarrySource(null); setSelectedId(command.source_snapshot_id) }
      }
      throw error
    }
    const receipt = updated.edit_transfer
    if (updated.child_id !== ownerChild || updated.snapshot_id !== targetId || !receipt ||
        receipt.target_snapshot_id !== targetId || Object.entries(request).some(([key, value]) => receipt[key as keyof typeof receipt] !== value) ||
        !validEditVersion(receipt.saved_edit_version) || typeof receipt.replayed !== 'boolean') {
      throw new Error('沿用结果暂不能核对；原请求已保留，不会另发新的转接。')
    }
    window.localStorage.removeItem(savedEditCarryKey(ownerChild))
    if (currentCarryChild.current !== ownerChild) return
    setCarryPending(null); setCarrySource(null)
    setSnapshots(items => [...items.filter(item => item.snapshot_id !== targetId), updated]); setSelectedId(targetId)
    setMessage('修改已保存到新版。请核对新版证据和修改内容，再单独确认并批准。')
  }
  const refreshChannelTests = async (): Promise<void> => {
    const next = await localApi.reportDeliveryChannelTests(childId)
    setChannelTests(next)
  }
  const applyEmailTestResult = async (result: Awaited<ReturnType<typeof localApi.testReportDeliveryChannel>>, recipientId: string, messagePrefix = ''): Promise<void> => {
    if (result.historical_only === true || result.current_verified === false) {
      // A replayed pass describes an earlier configuration. Clear the local
      // claim immediately, even if the authoritative status read fails.
      setChannelTests(items => [...items.filter(item => item.channel !== 'email'), { ...result, status: 'blocked', verified_recipient_ids: [] }])
      setMessage(result.message)
      await refreshChannelTests()
      return
    }
    setChannelTests(items => [...items.filter(item => item.channel !== 'email'), result.status === 'passed' && result.recipient_id === recipientId
      ? { ...result, verified_recipient_ids: Array.from(new Set([...(items.find(item => item.channel === 'email')?.verified_recipient_ids ?? []), recipientId])) }
      : result])
    setMessage(messagePrefix + result.message)
  }
  useEffect(() => {
    if (!channelTestsProcessing) return
    let cancelled = false
    let attempts = 0
    let timer: number | undefined
    const poll = async (): Promise<void> => {
      attempts += 1
      try { if (!cancelled) await refreshChannelTests() } catch { /* Reports remain readable if status refresh is offline. */ }
      if (!cancelled && attempts < 12) timer = window.setTimeout(() => { void poll() }, 3000)
    }
    timer = window.setTimeout(() => { void poll() }, 3000)
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer) }
  }, [childId, channelTestsProcessing]) // eslint-disable-line react-hooks/exhaustive-deps
  const changePeriodEvidence = async (kind: 'retract' | 'restore', evidenceRef: string, note: string): Promise<void> => {
    const existing = readPendingPeriodAction(childId)
    if (existing && (existing.kind !== kind || existing.evidence_ref !== evidenceRef || existing.note !== note)) {
      throw new Error('上次操作结果尚未确认，请先继续核对原操作；本次修改没有提交。')
    }
    const pending = existing ?? { kind, evidence_ref: evidenceRef, note, client_action_id: crypto.randomUUID() }
    // Persist the original payload before dispatch; retries must not adopt edited text.
    window.localStorage.setItem(periodActionStorageKey(childId), JSON.stringify(pending))
    setPendingPeriodAction(pending)
    const result = kind === 'retract'
      ? await localApi.retractPeriodEvidence(childId, evidenceRef, pending.note, pending.client_action_id)
      : await localApi.restorePeriodEvidence(childId, evidenceRef, pending.note, pending.client_action_id)
    clearReportPeriodEvidenceLocalState(childId)
    setPendingPeriodAction(null)
    setMessage(result.failures.length > 0
      ? '证据操作已记录，但部分报告尚未完成更正。请核对最新报告状态，不要为重建报告再次撤回或恢复。'
      : '证据操作已记录；请以刷新后的当前记录为准。原报告保持冻结，不会自动发送。')
    // The write receipt is final even when its subsequent read fails.
    await reload().catch(() => undefined)
  }
  const refreshReviewedReports = async (): Promise<void> => {
    let failed = false
    try { await reload() } catch { failed = true }
    try { await onReviewChanged?.() } catch { failed = true }
    setReviewRefreshFailed(failed)
  }
  const changeReview = async (operation: 'approve' | 'revoke'): Promise<void> => {
    if (!selected) return
    if (operation === 'approve' && (selected.correction_status?.source_current === false || carryPending)) throw new Error('请先核对新版或上次沿用结果，旧版不能批准。')
    const updated = operation === 'approve'
      ? await localApi.approveReportSnapshot(selected.snapshot_id, coachingEditPayload())
      : await localApi.revokeReportSnapshot(selected.snapshot_id, '家长撤销报告；停止尚未发送的送达任务')
    setPendingFeedbackSave(null)
    if (updated?.child_id === childId && updated.snapshot_id === selected.snapshot_id) {
      // Apply the frozen receipt together with its status, before asynchronous refresh.
      loadSavedEdit(updated)
      setSnapshots((current) => current.map((item) => item.snapshot_id === updated.snapshot_id ? updated : item))
    }
    setMessage(operation === 'approve' ? '批准已保存。' : '撤销已保存；不会再发送此报告的待发送任务。')
    await refreshReviewedReports()
  }
  const channelTestActionId = (testedChannel: 'email' | 'wechat_work_bot', recipientId?: string): string => {
    const key = channelTestStorageKey(childId, testedChannel, recipientId)
    const existing = channelTestActions[key] ?? window.localStorage.getItem(key)
    if (existing) return existing
    const created = crypto.randomUUID()
    window.localStorage.setItem(key, created)
    setChannelTestActions((current) => ({ ...current, [key]: created }))
    return created
  }
  const replaceChannelTestActionId = (testedChannel: 'email' | 'wechat_work_bot', recipientId?: string): string => {
    const key = channelTestStorageKey(childId, testedChannel, recipientId)
    const created = crypto.randomUUID()
    window.localStorage.setItem(key, created)
    setChannelTestActions((current) => ({ ...current, [key]: created }))
    return created
  }
  const clearChannelTestAction = (testedChannel: 'email' | 'wechat_work_bot'): void => {
    const prefix = channelTestStorageKey(childId, testedChannel)
    const unknown = channelTests.find(item => item.channel === testedChannel && item.status === 'unknown')?.client_action_id
    for (const key of Object.keys(window.localStorage)) {
      if ((key === prefix || key.startsWith(prefix + ':')) && window.localStorage.getItem(key) !== unknown) window.localStorage.removeItem(key)
    }
    setChannelTestActions((current) => Object.fromEntries(Object.entries(current).filter(([key, value]) =>
      !(key === prefix || key.startsWith(prefix + ':')) || value === unknown)))
  }
  const channelTestReissueActionId = (previousId: string, recipientId: string): string => {
    // A lost reissue response must retry the original replacement identity.
    const key = `hsc:report-delivery-test-reissue:${encodeURIComponent(childId)}:${encodeURIComponent(recipientId)}:${encodeURIComponent(previousId)}`
    const existing = window.localStorage.getItem(key)
    if (existing) return existing
    const created = crypto.randomUUID()
    window.localStorage.setItem(key, created)
    return created
  }
  const unknownDeliveryReissueActionId = (deliveryId: string): string => {
    const key = `hsc:report-delivery-reissue:${childId}:${deliveryId}`
    const existing = window.localStorage.getItem(key)
    if (existing) return existing
    const created = crypto.randomUUID()
    window.localStorage.setItem(key, created)
    return created
  }
  const summaryFacts = Array.isArray((selected?.content.summary as { facts?: unknown[] } | undefined)?.facts) ? (selected?.content.summary as { facts: string[] }).facts : []
  const coverage = selected?.facts.coverage ?? {}
  const periodLearning = selected?.facts.learning_summary as PeriodLearningSummary | undefined
  const periodFacts = [
    typeof coverage.recorded_days === 'number' ? `本期已记录${coverage.recorded_days}天、${typeof coverage.recorded_sessions === 'number' ? coverage.recorded_sessions : 0}次学习会话，共${typeof coverage.evidence_count === 'number' ? coverage.evidence_count : 0}条证据。` : null,
    typeof coverage.excluded_daily_reports === 'number' && coverage.excluded_daily_reports > 0 ? `另有${coverage.excluded_daily_reports}份日报来源暂不能核对，未计入本期汇总；原稿仍保留。` : null,
    periodLearning?.coverage ? `其中${periodLearning.coverage.eligible_evidence_count ?? 0}条为可追溯、未撤回的知识证据，覆盖${periodLearning.coverage.knowledge_nodes_covered ?? 0}个知识点。` : null,
    (periodLearning?.coverage?.qualified_observation_count ?? 0) > 0 ? `另有${periodLearning!.coverage!.qualified_observation_count}份原作答已可靠核对，单独列出，不计为掌握。` : null,
    ...(selected?.facts.daily_signals ?? []).map((item) => `${typeof item.date === 'string' ? item.date : '记录日'}：${typeof item.headline === 'string' ? item.headline : '已记录学习信号'}`),
  ].filter((item): item is string => Boolean(item))
  const facts = summaryFacts.length > 0 ? summaryFacts : periodFacts
  const evidenceInsufficient = selected?.confidence.overall === 'limited' || facts.length === 0
  const coaching = selected?.content.parent_coaching
  const baseAction = coaching?.action
  const baseScript = coaching?.script ?? []
  const action = coachingDecision === 'skip' ? { what: '家长本次选择不执行额外行动', duration_minutes: 0, steps: ['本次不追加对话或行动'], stop_condition: '本次不安排额外行动。' } : coachingDecision === 'custom' ? { ...(baseAction ?? { what: '', duration_minutes: 0, steps: [], stop_condition: '' }), what: coachingActionEdit || baseAction?.what || '', duration_minutes: coachingDuration } : baseAction
  const script = coachingDecision === 'skip' ? ['这次先不追加对话或行动；明天根据孩子愿意确认的事实再调整。'] : coachingDecision === 'custom' && coachingScriptEdit.trim() ? [coachingScriptEdit] : baseScript
  const avoid = coaching?.avoid ?? []
  const coachingBasis = coaching?.evidence_basis ?? []
  const coachingSources = coaching?.evidence_sources ?? []
  const personalization = coaching?.personalization
  const adaptation = coaching?.adaptation
  const scriptPhases = coachingDecision === 'adopt' ? (coaching?.script_phases ?? script.map((text, index) => ({ phase: ['开场', '倾听', '共情', '共同选择', '收尾'][Math.min(index, 4)] as '开场' | '倾听' | '共情' | '共同选择' | '收尾', text }))) : script.map((text, index) => ({ phase: ['开场', '倾听', '共情', '共同选择', '收尾'][Math.min(index, 4)] as '开场' | '倾听' | '共情' | '共同选择' | '收尾', text }))
  const evidenceDetail = selected?.content.evidence_detail as Partial<DailyReport['evidence_detail']> | undefined
  const recalls = evidenceDetail?.recalls ?? []
  const tasks = evidenceDetail?.tasks ?? []
  const knowledgeEvidence = evidenceDetail?.knowledge_evidence ?? []
  const mistakes = evidenceDetail?.mistakes ?? []
  const reflection = evidenceDetail?.reflection ?? null
  const drafts = snapshots.filter((item) => item.status === 'draft')
  const approved = snapshots.filter((item) => item.status === 'approved')
  const waiting = deliveryTotals ? deliveryTotals.scheduled + deliveryTotals.processing : undefined
  const failed = deliveryTotals?.failed
  const delayed = deliveryTotals?.recovered
  const emailConfigured = channelStatus.find((item) => item.channel === 'email')?.configured === true
  const localSmtpSink = ['127.0.0.1', 'localhost', '::1'].includes(smtpHost.trim().toLowerCase().replace(/^\[|\]$/g, ''))
  const emailTest = channelTests.find((item) => item.channel === 'email')
  const wecomTest = channelTests.find((item) => item.channel === 'wechat_work_bot')
  const authorizedEmails = recipients.filter(item => item.channel === 'email' && item.consent_status === 'consented')
  const emailRecipient = authorizedEmails.find(item => item.recipient_id === selectedEmailRecipientId) ?? authorizedEmails[0]
  const emailTestRecipientMatches = !!emailRecipient && emailTest?.recipient_id === emailRecipient.recipient_id
  const selectedEmailVerified = !!emailRecipient && !!emailTest?.verified_recipient_ids?.includes(emailRecipient.recipient_id)
  const feedbackBlank = helpfulness === null && stressRating === null && !feedbackNotes.trim()
  // Match loadSavedEdit exactly, including untouched duration defaults. Every
  // editable human field participates even when its mode currently hides it.
  const selectedEditDirty = !!selected && (
    intro !== (selected.parent_edit.delivery_intro ?? '') ||
    note !== (selected.parent_edit.parent_note ?? '') ||
    coachingDecision !== (selected.parent_edit.coaching_decision ?? 'adopt') ||
    coachingScriptEdit !== (selected.parent_edit.coaching_script ?? '') ||
    coachingActionEdit !== (selected.parent_edit.coaching_action ?? '') ||
    coachingDuration !== (selected.parent_edit.coaching_duration_minutes ?? selected.content.parent_coaching.action?.duration_minutes ?? 0) ||
    coachingObservation !== (selected.parent_edit.coaching_observation ?? '')
  )
  const effectiveObservation = coachingDecision === 'skip' ? '' : coachingObservation
  const coachingEditPayload = (): { delivery_intro: string; parent_note: string; coaching_decision: 'adopt' | 'custom' | 'skip'; coaching_script: string; coaching_action: string; coaching_duration_minutes?: number; coaching_observation: string } => ({
    delivery_intro: intro, parent_note: note, coaching_decision: coachingDecision, coaching_script: coachingScriptEdit,
    coaching_action: coachingActionEdit, ...(coachingDecision === 'skip' ? { coaching_duration_minutes: 0 } : coachingDecision === 'custom' ? { coaching_duration_minutes: coachingDuration } : {}),
    coaching_observation: effectiveObservation,
  })

  return <section ref={focusRoot} className="report-delivery-panel" aria-label="报告送达">
    <header><div><h2>报告审核与送达</h2><p>先核对证据，再批准；批准后的内容会冻结，不会被后续学习记录悄悄改写。</p></div><button type="button" aria-label="刷新报告状态" title="刷新报告状态（不会发送）" disabled={busy || refreshing} onClick={() => run(reload)}><RefreshCw size={16} aria-hidden="true" /></button></header>
    {message && <p role="alert" className="parent-error">{message}</p>}
    {refreshing && <p role="status">正在核对报告状态。</p>}
    {readFailed && <p role="alert">报告状态未能更新；当前显示上次读取的记录，恢复连接后请重新核对。</p>}
    {carryPending && carryPending.child_id === childId && <section aria-label="待核对的修改沿用"><p>上次沿用修改的结果尚未核对。两份原稿保留；以下只核对同一次请求，不会自动批准。</p><button disabled={busy || refreshing} onClick={() => run(() => carrySavedEdits(carryPending))}>核对上次沿用结果</button></section>}
    <ReportSourceIssues childId={childId} ready={loadedOnce} refreshVersion={sourceRefreshVersion} onOpen={openCorrection} />
    {reviewRefreshFailed && <p role="alert">审核已保存，但关联报告未能全部刷新。<button disabled={busy || refreshing} onClick={() => run(refreshReviewedReports)}>重新读取审核结果</button></p>}
    {pendingPeriodAction && <section aria-label="待确认的证据操作"><p>上次{pendingPeriodAction.kind === 'retract' ? '撤回' : '恢复'}结果尚未确认。继续核对会使用原说明，不会重复记为新操作。</p><p>{pendingPeriodAction.note}</p><button disabled={busy || refreshing} onClick={() => run(() => changePeriodEvidence(pendingPeriodAction.kind, pendingPeriodAction.evidence_ref, pendingPeriodAction.note))}>继续核对上次操作</button></section>}
    <section className="report-delivery-schedule" aria-label="报告节奏"><h3>报告节奏</h3>{snapshots.length === 0 ? <p>当前没有可读取的报告。若历史报告来源有问题，原稿仍保留，需先核对。</p> : <><p>日报：完成复盘后生成草稿；周报/月报：本机维护周期会补齐本期有证据的草稿。生成不会自动外发。</p>{deliveryTotals ? <ul><li>待家长审核：{drafts.length}份</li><li>已批准：{approved.length}份</li><li>待发送：{waiting}份{delayed && delayed > 0 ? '（含重启后已恢复的任务）' : ''}</li><li>结果未知：{deliveryTotals.unknown}份{deliveryTotals.unknown > 0 ? '，可能已发送；系统不会自动重发。' : ''}</li><li>发送需处理：{failed}份{failed && failed > 0 ? '，请在对应报告的送达记录中修复后重试。' : ''}</li></ul> : <p role="status" className="report-uncertainty">送达汇总状态暂不可用；报告内容仍可查看。<button disabled={busy} onClick={() => run(reload)}>重试状态查询</button></p>}{!emailConfigured && <p className="report-uncertainty">外部通道未配置：报告仅保存在本机家长中心，不会发送到邮箱或企业微信。</p>}</>}</section>
    {snapshots.length === 0 ? <p className="parent-empty">尚无可送达报告。孩子完成一次有证据的学习复盘后会在这里生成草稿。</p> : <div className="report-delivery-workspace">
      <nav ref={reportNavigation} aria-label="选择报告">{snapshots.map((item) => <button key={item.snapshot_id} disabled={busy || refreshing} aria-current={item.snapshot_id === selected?.snapshot_id ? 'true' : undefined} className={item.snapshot_id === selected?.snapshot_id ? 'is-active' : ''} onClick={() => setSelectedId(item.snapshot_id)}>{item.content.report_date ?? '阶段报告'} · {item.content.headline}<small>{item.revoke_reason === '后续证据已更新；请审核新版草稿' ? '后续证据已更新' : statusText(item.status)}</small></button>)}</nav>
      {selected && <article className="report-delivery-preview" data-work-object={selected.snapshot_id}>
        <h3>{selected.content.headline}</h3>
        {correctionSnapshot?.correction_status?.child_id === childId && <ReportCorrectionReview snapshot={correctionSnapshot} selectedId={selected.snapshot_id} snapshots={snapshots} recipients={recipients}
          busy={busy || refreshing || readFailed} onOpen={(id) => run(() => openCorrection(id))} />}
        {selectedEditDirty && (carrySource !== null || selected.correction_status?.source_current === false) && <section aria-label="未保存的报告修改"><p role="status">当前有未保存的修改。请先保存采用方式或恢复已保存内容，再查看新版或沿用旧稿。</p><button disabled={busy || carryPending !== null} onClick={() => loadSavedEdit(selected)}>恢复已保存内容</button></section>}
        {selected.correction_status?.source_current === false && <section aria-label="旧版审核需更新"><p role="status">这份报告的依据已变化，不能批准旧版。已保存的采用方式和备注仍保留在原稿。</p><button disabled={busy || refreshing || carryPending !== null || selectedEditDirty} onClick={() => run(viewReplacementWithSavedEdits)}>查看新版并沿用修改</button></section>}
        {carrySource && carrySource.child_id === childId && carrySource.snapshot_id !== selected.snapshot_id && <section aria-label="核对旧稿已保存修改"><h4>旧稿已保存的修改</h4><p>新版证据显示在本页。沿用只保存人工修改，不改变证据，也不会批准；如果任一稿已被其他窗口修改，会停止并保留两稿。</p><p>采用方式：{carrySource.parent_edit.coaching_decision === 'skip' ? '本次跳过' : carrySource.parent_edit.coaching_decision === 'custom' ? '修改后采用' : '采用建议'}</p><p>{carrySource.parent_edit.coaching_script}</p><p>{carrySource.parent_edit.coaching_action}</p><p>{carrySource.parent_edit.parent_note}</p><p>{carrySource.parent_edit.coaching_observation}</p><button disabled={busy || refreshing || carryPending !== null || selectedEditDirty || selected.status !== 'draft' || selected.correction_status?.source_current !== true} onClick={() => run(() => carrySavedEdits())}>沿用已保存修改到这份新版</button></section>}
        <section><h4>依据的事实</h4>{facts.length > 0 ? <ul>{facts.map((fact) => <li key={fact}>{fact}</li>)}</ul> : <p>该报告没有可外发的学习事实；系统不会补造结论。</p>}{evidenceInsufficient && <p className="report-uncertainty"><strong>证据不足：</strong>这份报告只反映已确认的少量记录，不据此推断掌握程度、能力、态度或趋势。</p>}<p className="report-uncertainty">{selected.confidence.uncertainty}</p></section>
        <section><h4>基于事实的解读</h4><p>{selected.inferences.interpretation}</p></section>
        <details className="report-delivery-evidence"><summary>查看学习证据明细</summary>{selected.report_type !== 'daily' ? <PeriodLearningEvidence summary={periodLearning} retracted={retractedEvidence} restoreNote={restoreNote} onRestoreNoteChange={setRestoreNote} busy={busy || refreshing || readFailed || pendingPeriodAction !== null} onRetract={(item) => run(() => changePeriodEvidence('retract', item.evidence_ref, '家长请求撤回本期汇总中的这条记录，待核对。'))} onRestore={(item) => run(() => changePeriodEvidence('restore', item.evidence_ref, restoreNote.trim()))} /> : <div>{recalls.length === 0 && tasks.length === 0 && knowledgeEvidence.length === 0 && mistakes.length === 0 && !reflection ? <p>这份日报没有可继续下钻的证据条目；系统不会补造细节。</p> : <>{recalls.length > 0 && <section><h5>课堂回想</h5><ul>{recalls.map((item, index) => <li key={`${item.subject}-${index}`}>{item.subject}：{item.summary}（{item.confirmation_status === 'child_confirmed' ? '孩子确认' : '待确认'}）</li>)}</ul></section>}{tasks.length > 0 && <section><h5>计划完成</h5><ul>{tasks.map((item, index) => <li key={`${item.title}-${index}`}>{item.subject} · {item.title}：{item.status === 'completed' ? `记录完成${item.actual_minutes ?? item.expected_minutes}分钟` : item.status === 'rescheduled' ? '已延期' : '未完成'}</li>)}</ul></section>}{knowledgeEvidence.length > 0 && <section><h5>作答证据</h5><ul>{knowledgeEvidence.map((item, index) => <li key={`${item.knowledge_title}-${index}`}>{item.subject} · {item.knowledge_title}：{item.score_status === 'verified_local' ? (item.independent ? '已核验独立作答' : '已核验辅助作答') : '历史评分，未重新核验'}{item.correctness == null ? '' : ` · 正确率${Math.round(item.correctness * 100)}%`}{item.mastery_probability == null ? '' : ` · 历史掌握估计${Math.round(item.mastery_probability * 100)}%`}</li>)}</ul></section>}{mistakes.length > 0 && <section><h5>关联错题</h5><ul>{mistakes.map((item, index) => <li key={`${item.title}-${index}`}>{item.subject} · {item.title}：{item.status === 'mastered' ? '已掌握' : item.status === 'excluded' ? '已排除' : '待继续处理'}；证据置信度{Math.round(item.confidence * 100)}%</li>)}</ul></section>}{reflection && <section><h5>孩子复盘</h5><p>{reflection.skipped ? '孩子本次选择跳过复盘，系统不对空白内容推断。' : `自述困难：${reflection.difficult_text || '未填写'}；下一步：${reflection.next_action || '未填写'}`}</p></section>}</>}</div>}</details>
        <section className="report-parent-guidance"><h4>为什么适合这次沟通</h4>{coaching?.scenario && <p>{coaching.scenario}</p>}{coaching?.why_this_fits && <p className="muted-copy">{coaching.why_this_fits}</p>}{coachingBasis.length > 0 && <div className="coaching-basis"><strong>本次建议依据</strong><ul>{coachingBasis.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></div>}{coachingSources.length > 0 && <div className="coaching-basis"><strong>依据来源</strong><ul>{coachingSources.map((item) => <li key={item.evidence_ref}>{item.source}{item.occurred_at ? ` · ${item.occurred_at.slice(0, 16).replace('T', ' ')}` : ''}</li>)}</ul><p className="muted-copy">可在上方“查看学习证据明细”继续核对已记录内容。</p></div>}{coaching?.uncertainty && <p className="report-uncertainty">不确定性：{coaching.uncertainty}</p>}{personalization?.goal_context && <p className="muted-copy">本次考虑的年龄/目标：{personalization.goal_context}</p>}{personalization?.recent_confirmed_facts.length ? <p className="muted-copy">近期确认记录仅用于安排本次对话，不用于给孩子贴标签。</p> : null}{adaptation?.basis === 'parent_feedback' && <p className="muted-copy">已参考最近{adaptation.feedback_count}次家长反馈：{adaptation.note}</p>}<h4>今晚做什么</h4>{action ? <><p><strong>{action.what}（约{action.duration_minutes}分钟）</strong></p><ol>{action.steps.map((step, index) => <li key={`${index}-${step}`}>{step}</li>)}</ol>{effectiveObservation && <p className="muted-copy">观察：{effectiveObservation}</p>}{action.stop_condition && <p className="muted-copy">停止条件：{action.stop_condition}</p>}</> : <p className="muted-copy">当前阶段报告没有额外的当日行动；请以已记录的下一步为准。</p>}<h4>分阶段可以这样说</h4><ol className="script-list">{scriptPhases.map((item, index) => <li key={`${index}-${item.phase}`}><i>{item.phase}</i><span>{item.text}</span></li>)}</ol>{avoid.length > 0 && <><h4>不要这样说</h4><ul>{avoid.map((item, index) => <li key={`${index}-${item.do_not_say}`}><s>{item.do_not_say}</s><br />可以改说：{item.say_instead}</li>)}</ul></>}</section>
        <section><h4>家长如何采用这套建议</h4><p className="muted-copy">你可以采用、按家庭语言修改，或本次跳过。修改只改变本份报告的执行方式，事实依据和停止边界仍保留。</p><div className="feedback-mode" role="group" aria-label="采用建议方式"><button disabled={selected.status !== 'draft' || busy || carryPending !== null} className={coachingDecision === 'adopt' ? 'is-active' : ''} onClick={() => setCoachingDecision('adopt')}>采用建议</button><button disabled={selected.status !== 'draft' || busy || carryPending !== null} className={coachingDecision === 'custom' ? 'is-active' : ''} onClick={() => setCoachingDecision('custom')}>修改后采用</button><button disabled={selected.status !== 'draft' || busy || carryPending !== null} className={coachingDecision === 'skip' ? 'is-active' : ''} onClick={() => setCoachingDecision('skip')}>本次跳过</button></div>{coachingDecision === 'custom' && <><label>改成你会说的话<textarea aria-label="修改沟通话术" disabled={selected.status !== 'draft' || busy || carryPending !== null} value={coachingScriptEdit} maxLength={1000} onChange={(event) => setCoachingScriptEdit(event.target.value)} placeholder="例如：我先听你说完，再一起选一个最小步骤。" /></label><label>改成今天的行动<textarea aria-label="修改今天行动" disabled={selected.status !== 'draft' || busy || carryPending !== null} value={coachingActionEdit} maxLength={500} onChange={(event) => setCoachingActionEdit(event.target.value)} placeholder="例如：只一起圈出第一处不确定。" /></label><label>行动时长（0–30分钟）<input aria-label="修改行动时长" disabled={selected.status !== 'draft' || busy || carryPending !== null} type="number" min="0" max="30" value={coachingDuration} onChange={(event) => setCoachingDuration(Math.max(0, Math.min(30, Number(event.target.value) || 0)))} /></label></>}<label>观察与明天调整<textarea aria-label="观察与明天调整" disabled={selected.status !== 'draft' || busy || carryPending !== null} value={coachingObservation} maxLength={500} onChange={(event) => setCoachingObservation(event.target.value)} placeholder="例如：若孩子拒绝，明天只邀请他说一件具体事实，不追加练习。" /></label><label>发送前开场白<textarea aria-label="开场白" disabled={selected.status !== 'draft' || busy || carryPending !== null} value={intro} maxLength={500} onChange={(event) => setIntro(event.target.value)} placeholder="例如：今晚我们一起看这份记录，不要求立刻解释。" /></label><label>补充备注<textarea disabled={selected.status !== 'draft' || busy || carryPending !== null} value={note} maxLength={1000} onChange={(event) => setNote(event.target.value)} /></label>
          {selected.status === 'draft' && <div><button disabled={busy || carryPending !== null || (coachingDecision === 'custom' && !coachingScriptEdit.trim() && !coachingActionEdit.trim())} onClick={() => run(async () => { await localApi.editReportSnapshot(selected.snapshot_id, coachingEditPayload()); await reload() })}>保存采用方式</button><button className="primary-button" disabled={busy || selected.correction_status?.source_current === false || carryPending !== null || (coachingDecision === 'custom' && !coachingScriptEdit.trim() && !coachingActionEdit.trim())} onClick={() => run(() => changeReview('approve'))}>确认并批准</button></div>}
          {selected.status === 'approved' && <><p role="status">已批准。以下发送均引用已冻结的证据版本；后续学习记录不会改写这份报告。</p><button disabled={busy} onClick={() => run(() => changeReview('revoke'))}>撤销此报告并停止待发送任务</button></>}
        </section>
        <section><h4>发现事实需要修正？</h4><p>这不会改写这份冻结报告；系统会停止未发送任务并保留你的更正说明，之后请在学习记录中核对和更正证据。</p>{(selected.corrections ?? []).map((item, index) => <p key={`${item.created_at}-${index}`} className="report-uncertainty">已标记待修正：{item.note}</p>)}{selected.status !== 'revoked' && <><label>修正说明<textarea aria-label="修正说明" value={correctionNote} maxLength={1000} onChange={(event) => setCorrectionNote(event.target.value)} placeholder="例如：这项任务实际未完成，需先核对孩子的完成记录。" /></label><button disabled={busy || correctionNote.trim().length < 2} onClick={() => run(async () => { await localApi.requestReportSnapshotCorrection(selected.snapshot_id, correctionNote); setCorrectionNote(''); await reload() })}>标记待修正并停止外发</button></>}</section>
        <section><h4>已授权收件人</h4><p>收件人必须逐一确认授权；撤销后不会再进入发送队列。</p><label>计划发送时间（留空则立即进入队列）<input aria-label="计划发送时间" type="datetime-local" value={scheduledAt} onChange={(event) => setScheduledAt(event.target.value)} /></label><div className="recipient-list">{recipients.map((recipient) => <div key={recipient.recipient_id}><span>{recipient.label} · {recipient.channel === 'email' ? '邮件' : '企业微信群机器人'} · {recipient.consent_status === 'consented' ? '已授权' : '未授权/已撤销'}</span>{recipient.consent_status === 'consented' && selected.status === 'approved' && <button disabled={sendingBlocked || deliveries.some((item) => item.recipient_id === recipient.recipient_id && ['sent', 'processing', 'unknown', 'unknown_acknowledged', 'superseded', 'scheduled'].includes(item.status))} onClick={() => run(async () => { await localApi.scheduleReportDelivery(selected.snapshot_id, recipient.recipient_id, scheduledAt ? new Date(scheduledAt).toISOString() : undefined); await reload() })}>{scheduledAt ? '定时排队' : '立即发送/排队'}</button>}{recipient.consent_status === 'consented' && <button disabled={busy} onClick={() => run(async () => { await localApi.revokeReportRecipient(recipient.recipient_id); await reload() })}>撤销授权</button>}</div>)}</div>
          <div className="recipient-add"><label>通道<select value={channel} onChange={(event) => setChannel(event.target.value as typeof channel)}><option value="email">邮件</option><option value="wechat_work_bot">企业微信群机器人</option></select></label><label>称呼<input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="妈妈" /></label><label>{channel === 'email' ? '邮箱' : '本地群组标签'}<input value={address} onChange={(event) => setAddress(event.target.value)} placeholder={channel === 'email' ? 'parent@example.com' : '例如：家庭学习群'} /></label><label className="recipient-consent"><input aria-label="确认收件人授权" type="checkbox" checked={recipientConsent} onChange={(event) => setRecipientConsent(event.target.checked)} /><span>我确认该成年人/群内所有成员已同意接收此孩子的报告。</span></label><button disabled={busy || !recipientConsent || !label.trim() || !address.trim()} onClick={() => run(async () => { await localApi.addReportRecipient({ child_id: childId, channel, address, label, consented: true }); setAddress(''); setLabel(''); setRecipientConsent(false); await reload() })}>确认授权并添加</button></div>
          <details className="report-delivery-settings"><summary>送达设置（本机受保护配置）</summary><p>邮件与企业微信均须明确允许向外部服务提交报告；企业微信机器人会向群内所有成员可见。</p>{channelStatus.some((item) => item.requires_encryption_key) && <p role="alert" className="parent-error">本机尚未配置数据加密密钥，不能保存外发配置；报告仍可在本机审核。</p>}<div className="delivery-settings-grid"><label>安静开始<input aria-label="安静开始" type="time" value={quietStart} onChange={(event) => setQuietStart(event.target.value)} /></label><label>安静结束<input aria-label="安静结束" type="time" value={quietEnd} onChange={(event) => setQuietEnd(event.target.value)} /></label><label>送达记录保留天数<input aria-label="送达记录保留天数" type="number" min="7" max="3650" value={retentionDays} onChange={(event) => setRetentionDays(Number(event.target.value))} /></label><label>送达内容<select aria-label="送达内容" value={redactionLevel} onChange={(event) => setRedactionLevel(event.target.value as 'standard' | 'minimal')}><option value="standard">标准：事实、行动和沟通建议</option><option value="minimal">最小披露：仅标题和本机查看提示</option></select></label><button disabled={busy} onClick={() => run(async () => { await localApi.saveReportDeliveryPreferences(childId, { quiet_start: quietStart, quiet_end: quietEnd, retention_days: retentionDays, redaction_level: redactionLevel }); await reload() })}>保存时段、隐私与保留期</button></div>
            <fieldset><legend>应用内置邮件 SMTP {channelStatus.find((item) => item.channel === 'email')?.configured ? '· 已配置' : '· 未配置'}</legend><p className="report-uncertainty">不需要安装或保持 Foxmail、Outlook 等邮件客户端。应用专用密码不会写入本机数据库或日志，而是交给系统安全存储，服务重启时自动恢复；本机 sink 不需要密码。</p><div className="delivery-settings-grid"><label>主机<input aria-label="SMTP主机" value={smtpHost} onChange={(event) => setSmtpHost(event.target.value)} placeholder="smtp.example.com" /></label><label>端口<input aria-label="SMTP端口" value={smtpPort} onChange={(event) => setSmtpPort(event.target.value)} /></label><label>加密方式<select aria-label="SMTP加密方式" value={smtpSecurity} onChange={(event) => { const security = event.target.value as 'starttls' | 'ssl' | 'none'; setSmtpSecurity(security); setSmtpPort((current) => security === 'ssl' && current === '587' ? '465' : security === 'starttls' && current === '465' ? '587' : current) }}><option value="starttls">STARTTLS（常用端口 587）</option><option value="ssl">SSL/TLS（常用端口 465）</option><option value="none">无加密（仅本机测试）</option></select></label><label>用户名<input aria-label="SMTP用户名" value={smtpUsername} onChange={(event) => setSmtpUsername(event.target.value)} /></label><label>应用专用密码<input aria-label="SMTP密码" type="password" value={smtpPassword} onChange={(event) => setSmtpPassword(event.target.value)} /></label><label>发件地址<input aria-label="SMTP发件地址" value={smtpFrom} onChange={(event) => setSmtpFrom(event.target.value)} /></label>{smtpSecurity === 'none' && !localSmtpSink && <p role="alert" className="parent-error">为保护孩子数据，外部 SMTP 必须使用 SSL/TLS 或 STARTTLS；无加密仅可用于本机 sink。</p>}<button disabled={busy || !smtpHost || !smtpFrom || (smtpSecurity === 'none' && !localSmtpSink)} onClick={() => run(async () => { const saveSecrets = window.desktop?.saveReportDeliverySecrets; if (smtpPassword && !saveSecrets) throw new Error('外部邮箱密码只能在桌面应用的系统安全存储中保存。'); if (smtpPassword && saveSecrets) await saveSecrets({ smtpPassword }); await localApi.saveReportDeliveryChannel('email', { host: smtpHost, port: Number(smtpPort), username: smtpUsername, password: smtpPassword, from_address: smtpFrom, use_ssl: smtpSecurity === 'ssl', starttls: smtpSecurity === 'starttls', allow_external_delivery: true }); setSmtpPassword(''); clearChannelTestAction('email'); await reload() })}>保存配置并允许邮件发送</button>{emailConfigured && <button disabled={busy} onClick={() => run(async () => { const result = await localApi.clearReportDeliveryChannel('email'); setSmtpPassword(''); clearChannelTestAction('email'); if (!result.runtimeRestarted) { setMessage('已删除邮件通道配置和桌面安全存储中的应用专用密码；报告仅保存在本机。'); await reload() } })}>删除邮件配置与密码</button>}</div><label>测试收件邮箱<select aria-label="测试收件邮箱" value={emailRecipient?.recipient_id ?? ''} disabled={busy || emailTest?.status === 'processing'} onChange={event => setSelectedEmailRecipientId(event.target.value)}>{authorizedEmails.length === 0 && <option value="">请先添加并授权收件人</option>}{authorizedEmails.map(item => <option key={item.recipient_id} value={item.recipient_id}>{item.label} · {item.address}</option>)}</select></label><p>{selectedEmailVerified ? '所选邮箱已通过当前配置的测试。' : '所选邮箱尚未通过当前配置的测试。'}</p><p className={emailTest?.status === 'failed' ? 'parent-error' : 'report-uncertainty'}>邮件发送测试：{emailTest?.status === 'passed' && !selectedEmailVerified ? '所选邮箱尚未核验。' : channelTestText(emailTest)}</p><button disabled={busy} onClick={() => run(refreshChannelTests)}>刷新测试状态</button>{emailTest?.status === 'unknown' && emailTest.client_action_id && <div className="report-uncertainty"><p>{emailTest.kept_without_resend ? '已选择保持不重发。' : '上次测试结果未知，可能已发出。'}{!emailTestRecipientMatches && '请切回原收件邮箱；若原收件人无法确认，请联系维护者核对原操作。'}</p><button disabled={busy} onClick={() => run(async () => { const result = await localApi.keepUnknownReportDeliveryChannelTest(childId, 'email', emailTest.client_action_id!); setChannelTests((items) => [...items.filter((item) => item.channel !== 'email'), { ...result, recipient_id: result.recipient_id === undefined ? emailTest.recipient_id : result.recipient_id }]); setMessage(result.message) })}>保持不重发</button><button disabled={busy || !emailTestRecipientMatches} onClick={() => run(async () => { if (!emailRecipient || !emailTestRecipientMatches) return; const nextActionId = channelTestReissueActionId(emailTest.client_action_id!, emailRecipient.recipient_id); const result = await localApi.reissueReportDeliveryChannelTest(childId, 'email', emailTest.client_action_id!, nextActionId, emailRecipient.recipient_id); await applyEmailTestResult(result, emailRecipient.recipient_id, '已明确发起新的测试；可能会收到第二封固定的非隐私测试邮件。') })}>明确发起新的测试</button></div>}<button disabled={busy || !emailRecipient || emailTest?.status === 'processing' || (emailTest?.status === 'unknown' && !emailTestRecipientMatches)} onClick={() => run(async () => { if (!emailRecipient) return; const actionId = emailTest?.status === 'unknown' && emailTest.client_action_id ? emailTest.client_action_id : selectedEmailVerified ? replaceChannelTestActionId('email', emailRecipient.recipient_id) : channelTestActionId('email', emailRecipient.recipient_id); const result = await localApi.testReportDeliveryChannel(childId, 'email', actionId, emailRecipient.recipient_id); if (result.historical_only === true || result.current_verified === false) { const key = channelTestStorageKey(childId, 'email', emailRecipient.recipient_id); if (window.localStorage.getItem(key) === actionId) window.localStorage.removeItem(key); setChannelTestActions(current => { if (current[key] !== actionId) return current; const next = { ...current }; delete next[key]; return next }) }; await applyEmailTestResult(result, emailRecipient.recipient_id) })}>{emailTest?.status === 'processing' ? '测试处理中（请稍候）' : emailTest?.status === 'unknown' ? '普通重试（不会再次发送）' : selectedEmailVerified ? '重新发送一封测试邮件' : '发送不含孩子数据的测试邮件'}</button></fieldset>
            {emailTest?.status === 'unknown' && emailTest.client_action_id && emailTestRecipientMatches && emailRecipient && <section aria-label="重新授权邮箱的测试恢复">
              <button disabled={busy} onClick={() => setReauthorizedEmail({previousId:emailTest.client_action_id!,recipientId:emailRecipient.recipient_id,address:emailRecipient.address})}>核对重新授权的收件邮箱</button>
              {reauthorizedEmail && <div role="alert"><p>原测试结果未知，邮件可能已送达。确认后将向当前重新授权的 {reauthorizedEmail.address} 再发送一封不含孩子数据的固定测试邮件。</p>
                <button disabled={busy} onClick={() => setReauthorizedEmail(null)}>取消当前邮箱重测</button>
                <button disabled={busy} onClick={() => run(async () => {
                  const {previousId,recipientId,address:confirmedAddress}=reauthorizedEmail
                  if (emailTest?.status !== 'unknown' || emailTest.client_action_id !== previousId || emailRecipient?.recipient_id !== recipientId || emailRecipient.address !== confirmedAddress) throw new Error('收件邮箱或测试状态已变化，请重新核对。')
                  const actionId=channelTestReissueActionId(previousId,recipientId)
                  const result=await localApi.reissueReauthorizedEmailTest(childId,previousId,actionId,recipientId,confirmedAddress)
                  await applyEmailTestResult(result,recipientId)
                  if (result.status === 'passed' && result.historical_only !== true && result.current_verified !== false) setReauthorizedEmail(null)
                })}>确认当前邮箱并发送新测试</button>
              </div>}
            </section>}
            <fieldset><legend>企业微信消息推送 {channelStatus.find((item) => item.channel === 'wechat_work_bot')?.configured ? '· 已配置' : '· 未配置'}</legend><p className="report-uncertainty">仅支持企业微信群机器人，不是个人微信。未提供企业组织账号/Webhook 时无法真实验证，系统不会伪造成功或用界面自动化发送。</p><label>Webhook 地址<input aria-label="企业微信Webhook" type="password" value={workWebhook} onChange={(event) => setWorkWebhook(event.target.value)} placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." /></label><button disabled={busy || !workWebhook.startsWith('https://')} onClick={() => run(async () => { if (!window.desktop?.saveReportDeliverySecrets) throw new Error('企业微信 Webhook 只能在桌面应用的系统安全存储中保存。'); await window.desktop.saveReportDeliverySecrets({ wecomWebhook: workWebhook }); await localApi.saveReportDeliveryChannel('wechat_work_bot', { webhook_url: workWebhook, allow_external_delivery: true }); setWorkWebhook(''); clearChannelTestAction('wechat_work_bot'); await reload() })}>保存并允许企业微信发送</button>{channelStatus.find((item) => item.channel === 'wechat_work_bot')?.configured && <button disabled={busy} onClick={() => run(async () => { const result = await localApi.clearReportDeliveryChannel('wechat_work_bot'); setWorkWebhook(''); clearChannelTestAction('wechat_work_bot'); if (!result.runtimeRestarted) { setMessage('已删除企业微信配置和桌面安全存储中的 Webhook；不会发送。'); await reload() } })}>删除企业微信配置与 Webhook</button>}<p className="report-uncertainty">企业微信校验：{channelTestText(wecomTest) || '尚未校验。'}</p><button disabled={busy} onClick={() => run(async () => { const result = await localApi.testReportDeliveryChannel(childId, 'wechat_work_bot', channelTestActionId('wechat_work_bot')); setChannelTests((items) => [...items.filter((item) => item.channel !== 'wechat_work_bot'), result]); setMessage(result.message) })}>检查企业微信配置限制</button></fieldset>
          </details>
        </section>
        <section><h4>送达记录</h4>{!sourceEligible && selected.correction_status?.source_current === false && <p className="report-uncertainty">旧版来源已变化，请先审核更正报告，不能重发旧版。</p>}<button disabled={busy} onClick={() => run(async () => { await localApi.processReportDeliveries(); await reload() })}>立即检查到期发送</button>{!deliveriesReady ? <p>送达记录尚未核对，暂不开放发送；可刷新报告状态重试。</p> : deliveries.length === 0 ? <p>尚未排队发送。邮件/企业微信配置完成且家长明确确认后才会外发。</p> : <ul>{deliveries.map((delivery) => <li key={delivery.delivery_id} data-work-object={delivery.delivery_id}>{delivery.channel === 'email' ? '邮件' : '企业微信'} · {deliveryStatus(delivery)}{deliveryIssue(delivery) && <span className="report-uncertainty"> · {deliveryIssue(delivery)}</span>} {delivery.status === 'failed' && <button disabled={sendingBlocked} onClick={() => run(async () => { await localApi.retryReportDelivery(delivery.delivery_id); await reload() })}>修复后重试</button>} {['unknown', 'unknown_acknowledged'].includes(delivery.status) && <span className="report-uncertainty"> 为避免重复发送，普通重试不会再次投递。{delivery.status === 'unknown' && <button disabled={busy} onClick={() => run(async () => { await localApi.keepUnknownReportDelivery(delivery.delivery_id); await reload() })}>保持不重发</button>} {delivery.status === 'unknown_acknowledged' && <span>你之后仍可改变决定并明确创建新的重发。</span>}<button disabled={sendingBlocked} onClick={() => setPendingUnknownReissue(delivery)}>明确创建新的重发</button></span>} {['scheduled', 'processing'].includes(delivery.status) && <button disabled={busy} onClick={() => run(async () => { await localApi.cancelReportDelivery(delivery.delivery_id, '家长取消发送'); await reload() })}>取消</button>}</li>)}</ul>}{pendingUnknownReissue && <div role="alert" className="parent-error"><p>原邮件可能已经送达。确认后会创建一项新的发送并再发一封完整报告；这不是普通重试。</p><button disabled={busy} onClick={() => setPendingUnknownReissue(null)}>取消，不再发送</button><button disabled={sendingBlocked} onClick={() => run(async () => { await localApi.reissueUnknownReportDelivery(pendingUnknownReissue.delivery_id, unknownDeliveryReissueActionId(pendingUnknownReissue.delivery_id)); setPendingUnknownReissue(null); await reload() })}>确认再发一封</button></div>}</section>
        {selected.report_type !== 'daily' && <PeriodSupplyFollowup key={`supply-${selected.snapshot_id}`} childId={childId} snapshotId={selected.snapshot_id} onOpenSupply={onOpenSupply} />}
        {selected.report_type !== 'daily' && (selected.parent_action?.binding_status === 'bound' ? <ParentActionFeedbackPanel key={selected.parent_action.snapshot_id} snapshot={selected.parent_action} /> : <p role="status">{selected.status === 'draft' ? '本期行动尚待家长批准，当前不能填写执行效果。' : '本期行动的审核或来源已变化，当前不可执行；历史反馈保留，请重新核对。'}</p>)}
        <HistoricalParentActionFeedback childId={childId} actions={selected.historical_parent_actions} excludeSnapshotId={selected.parent_action?.binding_status === 'bound' ? selected.parent_action.snapshot_id : undefined} />
        <section><h4>家长反馈</h4><p>这会用于调整后续沟通建议，不会改写已批准报告的事实。只写备注会保留为审计说明，不计入帮助程度或沟通压力统计。</p><div className="delivery-settings-grid"><label>建议帮助程度<select aria-label="建议帮助程度" value={helpfulness ?? ''} onChange={(event) => setHelpfulness(event.target.value ? Number(event.target.value) : null)}><option value="">暂不评价</option>{[1, 2, 3, 4, 5].map((value) => <option value={value} key={value}>{value}</option>)}</select></label><label>沟通压力<select aria-label="沟通压力" value={stressRating ?? ''} onChange={(event) => setStressRating(event.target.value ? Number(event.target.value) : null)}><option value="">暂不评价</option>{[1, 2, 3, 4, 5].map((value) => <option value={value} key={value}>{value}</option>)}</select></label><label>补充备注<textarea aria-label="报告反馈备注" value={feedbackNotes} maxLength={1000} onChange={(event) => setFeedbackNotes(event.target.value)} /></label><button disabled={busy || feedbackBlank} onClick={() => run(async () => { const notes = feedbackNotes.trim() || null; const current = { helpfulness, stress_rating: stressRating, notes }; const replay = pendingFeedbackSave && pendingFeedbackSave.helpfulness === current.helpfulness && pendingFeedbackSave.stress_rating === current.stress_rating && pendingFeedbackSave.notes === current.notes ? pendingFeedbackSave : { ...current, client_action_id: crypto.randomUUID() }; setPendingFeedbackSave(replay); await localApi.saveReportSnapshotFeedback(selected.snapshot_id, replay); await reload(); setPendingFeedbackSave(null) })}>保存反馈</button></div></section>
        <ReportFeedbackHistory key={`${childId}:${selected.snapshot_id}:${selected.feedback_current_revision ?? 0}`} childId={childId} snapshotId={selected.snapshot_id} onChanged={reload} />
        {selected.report_type === 'daily'
          ? <ReportPlanAdjustments childId={childId} snapshotId={selected.snapshot_id} feedbackRevision={selected.feedback_current_status === 'active' ? selected.feedback_current_revision ?? null : null} />
          : <PeriodReportRebinding childId={childId} snapshotId={selected.snapshot_id} onRefreshReports={reload} onOpenReport={setSelectedId} />}
      </article>}
    </div>}
  </section>
}

function ReportCorrectionReview({ snapshot, selectedId, snapshots, recipients, busy, onOpen }: {
  snapshot: ParentReportSnapshot; snapshots: ParentReportSnapshot[]; recipients: ReportDeliveryRecipient[]
  selectedId: string
  busy: boolean; onOpen: (snapshotId: string) => void
}): React.JSX.Element | null {
  const state = snapshot.correction_status
  if (!state || (state.source_current && snapshot.status !== 'revoked' && state.replacements.length === 0 && !state.latest_replacement_id && !state.correction_notification_required)) return null
  const link = state.replacements[state.replacements.length - 1]
  const latestId = state.latest_replacement_id === undefined ? link?.replacement_snapshot_id : state.latest_replacement_id
  const replacement = snapshots.find((item) => item.snapshot_id === latestId && item.child_id === snapshot.child_id)
  const replacementStatus = state.latest_replacement_status === undefined ? replacement?.status : state.latest_replacement_status
  const recipientIds = [...new Set([...state.prior_submissions.map((item) => item.recipient_id), ...state.recipient_ids_requiring_correction])]
  const accepted = state.replacement_submissions.some((item) => item.snapshot_id === latestId && item.source_current && item.channel_acceptance === 'accepted')
  return <section aria-label="报告更正核对" style={{ overflowWrap: 'anywhere' }}>
    <h4>{state.correction_notification_required ? '旧报告需要更正' : accepted && state.recipient_ids_requiring_correction.length === 0 ? '更正报告已交给通道' : '旧报告已停止使用'}</h4>
    <p>{snapshot.snapshot_id === latestId ? '这份更正报告关联的旧版内容与发送记录保留，不会改写成新版。' : `${snapshot.content.report_date ?? '阶段报告'} · ${snapshot.content.headline}：原内容与发送记录保留，不会改写成新版。`}</p>
    <p className="report-uncertainty">通道接受不等于已读。结果未知时可能已经发送，不会自动重发。</p>
    {latestId ? <div><p>{replacementStatus === 'draft' ? '新版待审核' : replacementStatus === 'approved' ? '新版已批准' : replacementStatus === 'revoked' ? '更正版本也已停止使用，需重新核对。' : '更正报告尚未载入。'}</p>
      {latestId === selectedId ? <p>当前查看的是更正版本。</p> : <button type="button" disabled={busy} onClick={() => onOpen(latestId)}>查看更正报告</button>}</div>
      : <p>{state.latest_replacement_availability === 'deleted' ? '最新更正报告已删除，旧版本不能代替它。' : state.latest_replacement_availability === 'ambiguous' ? '更正版本关系需要核对，暂不选择替代版本。' : '新版尚未就绪；旧报告不会自动重新发送。'}</p>}
    <ul>{recipientIds.map((id) => {
      const name = recipients.find((item) => item.recipient_id === id)?.label ?? '原收件人'
      const prior = state.prior_submissions.filter((item) => item.recipient_id === id)
      const submissions = state.replacement_submissions.filter((item) => item.recipient_id === id && item.snapshot_id === latestId)
      const current = [...submissions].reverse().find((item) => item.status !== 'superseded')
      const priorUnknown = prior.some((item) => ['processing', 'unknown', 'unknown_acknowledged', 'superseded'].includes(item.status))
      return <li key={id}>
        {state.recipient_ids_blocked_by_consent?.includes(id) && <p>{name}：接收授权已撤销或不可用，不能发送更正报告。</p>}
        <p>{name}：{priorUnknown ? '旧报告结果未知，可能已发送；不会自动重发。' : prior.some((item) => item.status === 'sent') ? '旧报告已交给通道，不等于已读。' : '旧报告尚未确认送达。'}</p>
        {current && <p>{name}：{!current.source_current ? '更正报告的来源已变化，需要重新核对。' : current.channel_acceptance === 'accepted' ? '更正报告已交给通道，不等于已读。' : current.channel_acceptance === 'unknown' ? '更正报告结果未知，可能已发送；先核对收件情况。' : current.status === 'failed' ? '更正报告发送失败，尚未获得通道接受。' : '更正报告尚未获得通道接受。'}</p>}
      </li>
    })}</ul>
  </section>
}

type PeriodEvidence = { subject: string; title?: string; knowledge_title?: string; date: string; summary: string; stage?: string; independent?: boolean; correctness?: number | null; evidence_ref: string }
type RetractedPeriodEvidence = { evidence_ref: string; subject: string; knowledge_title: string; summary: string; date: string; note: string }
type PeriodLearningSummary = {
  new_weaknesses?: PeriodEvidence[]; independent_variant_retests?: PeriodEvidence[]
  pending_confirmation?: PeriodEvidence[]; evidence?: PeriodEvidence[]; qualified_observations?: PeriodEvidence[]
  knowledge_support?: Array<{ subject: string; knowledge_title: string; evidence_count: number; support: string }>
  coverage?: { eligible_evidence_count?: number; knowledge_nodes_covered?: number; qualified_observation_count?: number }
  uncertainty?: string
}

function PeriodLearningEvidence({ summary, retracted, restoreNote, onRestoreNoteChange, busy, onRetract, onRestore }: { summary: PeriodLearningSummary | undefined; retracted: RetractedPeriodEvidence[]; restoreNote: string; onRestoreNoteChange: (value: string) => void; busy: boolean; onRetract: (item: PeriodEvidence) => void; onRestore: (item: RetractedPeriodEvidence) => void }): React.JSX.Element {
  if (!summary) return <p>本期没有可继续下钻的知识证据；系统不会把未关联记录补入结论。</p>
  const rows = (items: PeriodEvidence[] | undefined, label: string, description: (item: PeriodEvidence) => string) => items?.length ? <section><h5>{label}</h5><ul>{items.map((item, index) => <li key={`${item.evidence_ref}-${index}`}>{description(item)} <button disabled={busy} onClick={() => onRetract(item)}>这条记录需撤回核对</button></li>)}</ul></section> : null
  return <div>{summary.uncertainty && <p className="report-uncertainty">{summary.uncertainty}</p>}
    {rows(summary.qualified_observations, '可靠的原作答核对（不等同掌握）', item => `${item.date} · ${item.subject} · ${item.knowledge_title}：${item.summary}`)}
    {summary.knowledge_support?.length ? <section><h5>本期知识证据支持程度</h5><ul>{summary.knowledge_support.map((item) => <li key={`${item.subject}-${item.knowledge_title}`}>{item.subject} · {item.knowledge_title}：{item.support}（本期{item.evidence_count}条记录）</li>)}</ul></section> : null}{rows(summary.new_weaknesses, '本期新发现、需继续核对的错题', (item) => `${item.date} · ${item.subject} · ${item.title}：${item.summary}`)}{rows(summary.independent_variant_retests, '已通过独立变式复测', (item) => `${item.date} · ${item.subject} · ${item.title}：本次独立变式通过（不等同稳定掌握）`)}{rows(summary.pending_confirmation, '仍待确认', (item) => `${item.date} · ${item.subject} · ${item.title}：${item.summary}`)}{rows(summary.evidence, '可追溯知识记录', (item) => `${item.date} · ${item.subject} · ${item.knowledge_title}：${item.independent ? '独立作答' : '有提示记录'}${item.correctness == null ? '；未记录正误，不作负面推断' : item.correctness >= 1 ? '；本次作答正确' : '；本次作答未完全正确'}；${item.summary}`)}{retracted.length > 0 && <section><h5>本期已撤回记录</h5><p className="report-uncertainty">撤回后不会纳入新版报告；核对无误可恢复，并生成新的待审核草稿。</p><label>恢复说明<textarea aria-label="恢复说明" value={restoreNote} maxLength={500} onChange={(event) => onRestoreNoteChange(event.target.value)} /></label><ul>{retracted.map((item) => <li key={item.evidence_ref}>{item.date} · {item.subject} · {item.knowledge_title}：{item.summary}（撤回说明：{item.note}） <button disabled={busy || restoreNote.trim().length < 2} onClick={() => onRestore(item)}>恢复这条记录</button></li>)}</ul></section>}</div>
}

function statusText(status: ParentReportSnapshot['status']): string { return { draft: '待审核', approved: '已批准', revoked: '已撤销' }[status] }
function channelTestStorageKey(childId: string, channel: 'email' | 'wechat_work_bot', recipientId?: string): string {
  return `hsc:report-delivery-test:${childId}:${channel}${recipientId ? `:${encodeURIComponent(recipientId)}` : ''}`
}
function channelTestText(test: ChannelTest | undefined): string {
  if (!test) return '尚未测试。'
  const prefix = test.status === 'passed' ? '已通过。' : test.status === 'failed' ? '未通过。' : test.status === 'processing' ? '处理中。' : test.status === 'unknown' ? '结果未知。' : '受阻。'
  return `${prefix}${test.message}`
}
function deliveryStatus(delivery: ReportDelivery): string { return { scheduled: '已排队', processing: '正在提交', sent: '通道已接受', failed: '发送失败', cancelled: '已取消', unknown: '结果未知，可能已发送', unknown_acknowledged: '已选择保持不重发', superseded: '已明确新建一项重发' }[delivery.status] }
function deliveryIssue(delivery: ReportDelivery): string | null {
  if (delivery.status === 'cancelled') {
    const reasons: Record<string, string> = {
      recipient_snapshot_scope_mismatch: '收件人与报告的孩子或发送渠道不一致；请核对接收人设置后重新安排发送。',
      delivery_envelope_changed: '收件信息在发送前发生变化；请核对当前接收人后重新安排发送。',
      channel_configuration_changed: '发送设置在提交前发生变化；请核对通道设置后重新安排发送。',
      report_disclosure_changed: '报告分享范围已改变；请按当前分享范围重新安排发送。',
      snapshot_source_changed: '学习记录已变化，旧报告未发送；请重新生成并审核报告。',
      snapshot_not_approved: '报告尚未批准或已撤回；请核对并审核当前报告。',
      recipient_consent_revoked: '接收授权已撤销；只有重新确认接收授权后才能安排发送。',
      recipient_email_test_required: '该收件邮箱的发送测试已失效，请先为这个邮箱重新测试。',
      invalid_reissue_provenance: '无法核实这次重发与原未知记录的授权关系，请先核对发送记录。',
      prior_delivery_unresolved: '之前的发送结果尚未确认；请先核对收件情况，避免重复发送。',
    }
    return reasons[delivery.cancel_reason || ''] || '本次发送已取消；请核对当前报告与接收设置后再安排发送。'
  }
  const code = delivery.last_error_code
  if (!code) return null
  if (code === 'WAITING_QUIET_HOURS') return '已进入静默时段，将在静默结束后再检查发送。'
  if (code === 'WAITING_DELETION') return '正在等待该孩子的删除操作结束，暂不发送。'
  if (code === 'UNKNOWN_AFTER_RESTART') return '应用或本地服务重启时无法确认通道是否已接受；系统不会自动重发。'
  if (code === 'CHANNEL_NOT_CONFIGURED') return '外发通道尚未配置，完整报告仍只保存在本机家长中心。'
  if (code === 'EXTERNAL_DELIVERY_NOT_AUTHORIZED') return '未取得外发授权，系统没有发送。'
  if (code === 'SMTP_CREDENTIALS_INVALID') return '邮箱授权信息无效；更新应用专用密码后可重试。'
  if (code === 'CREDENTIAL_KEY_UNAVAILABLE' || code === 'CREDENTIAL_DECRYPT_FAILED') return '本机无法读取加密发送设置；请由家长重新配置。'
  return delivery.last_error_message || '发送未完成；请检查通道设置或网络后重试。'
}
