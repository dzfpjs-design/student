import { useEffect, useRef, useState } from 'react'
import { ApiError, localApi, requestParentLocal } from './api'
import { parentDraftAuthorityEvent } from './parentDraftAuthority'
import { captureDeletionUnconfirmed, captureDeletionIntent, confirmCaptureDeletion, parentVisualInvalidationActive, parentVisualInvalidationEvent } from './parentVisualInvalidation'

type Page = { text: string; source?: 'local_ocr' | 'cloud_page'; observations: { revision_id: string; confidence: number; review_required: boolean; observed_at: string }[] }
type Preview = { child_id: string; learning_date: string; local_only: boolean; is_formal_report: boolean;
  document_observation: { pages: Page[] }; page_observation_time: { status: string; pages?: { revision_id: string; estimated_visible_ms: number }[] };
  recorded_task_time?: { tasks: { subject: string; title: string; recorded_active_seconds: number; reported_minutes: number | null }[]; omitted_count: number };
  camera_and_document_samples?: { sources: { camera?: { eligible_states?: Record<string, number> } } } }
type PreparationStatus = 'needs_authorization' | 'not_prepared' | 'prepared' | 'source_changed' | 'unknown' | 'already_sent' | 'blocked'
type Preparation = { child_id: string; day: string; delivery_enabled: false; recipients: { recipient_id: string; label: string; address: string; status: PreparationStatus; source_id?: string }[] }
const preparationLabels: Record<PreparationStatus, string> = { needs_authorization: '需要家长授权', not_prepared: '尚未准备', prepared: '已准备，尚未发送', source_changed: '记录已变化，需重新核对', unknown: '历史发送结果未知，请先核对，勿重复发送', already_sent: '已有发送记录', blocked: '暂时无法准备' }
const sendKey = (child: string, day: string, recipient: string): string => `hsc-visual-send-v1:${encodeURIComponent(child)}:${encodeURIComponent(day)}:${encodeURIComponent(recipient)}`
function sendBlocked(key: string): boolean { try { return localStorage.getItem(key) !== null } catch { return true } }

function chinaToday(): string {
  const fields = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date())
  const find = (name: string): string => fields.find(field => field.type === name)?.value ?? ''
  return `${find('year')}-${find('month')}-${find('day')}`
}

export function ParentDailyVisualPreview({ childId }: { childId: string }): React.JSX.Element {
  const [dailyStatus, setDailyStatus] = useState<Awaited<ReturnType<typeof localApi.combinedDailyStatus>> | null>(null)
  const [dailyStatusError, setDailyStatusError] = useState(false)
  useEffect(() => {
    let stopped = false
    let controller: AbortController | null = null
    const loadStatus = (): void => {
      controller?.abort(); controller = new AbortController()
      const request = controller
      void localApi.combinedDailyStatus(childId, request.signal).then(value => {
        if (!stopped && !request.signal.aborted) { setDailyStatus(value); setDailyStatusError(false) }
      }).catch(() => { if (!stopped && !request.signal.aborted) { setDailyStatus(null); setDailyStatusError(true) } })
    }
    setDailyStatus(null); setDailyStatusError(false); loadStatus()
    const timer = window.setInterval(loadStatus, 30000)
    return () => { stopped = true; controller?.abort(); window.clearInterval(timer) }
  }, [childId])
  const [day, setDay] = useState(chinaToday)
  const [state, setState] = useState<{ kind: 'idle' | 'loading' | 'error' } | { kind: 'ready'; data: Preview }>({ kind: 'idle' })
  const [invalidating, setInvalidating] = useState(parentVisualInvalidationActive)
  const [recoveryMessage, setRecoveryMessage] = useState('')
  const [recovering, setRecovering] = useState(false)
  const [preparation, setPreparation] = useState<{kind: 'idle' | 'loading' | 'error' | 'uncertain'} | {kind:'ready'; data: Preparation}>({kind:'idle'})
  const [sending, setSending] = useState(false)
  const [sendMessage, setSendMessage] = useState('')
  const sendRequest = useRef<AbortController | null>(null)
  const preparationRequest = useRef<AbortController | null>(null)
  const current = useRef<AbortController | null>(null)
  const recovery = useRef<AbortController | null>(null)
  const expires = useRef(0)
  useEffect(() => {
    setState({ kind: 'idle' })
    setRecovering(false); setRecoveryMessage('')
    const clear = (): void => { sendRequest.current?.abort(); sendRequest.current = null; setSending(false); setSendMessage(''); current.current?.abort(); current.current = null; recovery.current?.abort(); recovery.current = null; preparationRequest.current?.abort(); preparationRequest.current = null; setPreparation({kind:'idle'}); setRecovering(false); setState({ kind: 'idle' }) }
    const changed = (): void => { setInvalidating(parentVisualInvalidationActive()); clear() }
    window.addEventListener('hsc-privacy-pause', clear)
    window.addEventListener(parentDraftAuthorityEvent, clear)
    window.addEventListener('hsc-runtime-restarted', clear)
    window.addEventListener(parentVisualInvalidationEvent, changed)
    return () => { window.removeEventListener('hsc-privacy-pause', clear); window.removeEventListener(parentDraftAuthorityEvent, clear); window.removeEventListener('hsc-runtime-restarted', clear); window.removeEventListener(parentVisualInvalidationEvent, changed); sendRequest.current?.abort(); sendRequest.current = null; current.current?.abort(); current.current = null; recovery.current?.abort(); recovery.current = null; preparationRequest.current?.abort(); preparationRequest.current = null }
  }, [childId])
  const sessionPath = `/api/parent/children/${encodeURIComponent(childId)}/daily-visual-preview/session`
  useEffect(() => {
    if (state.kind !== 'ready') return
    let stopped = false
    let pending: AbortController | null = null
    const clear = (): void => { sendRequest.current?.abort(); sendRequest.current = null; setSending(false); setSendMessage(''); current.current?.abort(); current.current = null; preparationRequest.current?.abort(); preparationRequest.current = null; setPreparation({kind:'idle'}); setState({ kind: 'idle' }) }
    const timeout = window.setTimeout(clear, Math.max(0, expires.current - Date.now()))
    const poll = window.setInterval(() => {
      if (pending) return
      const controller = new AbortController(); pending = controller
      const fail = window.setTimeout(() => { controller.abort(); if (!stopped) clear() }, 3000)
      void requestParentLocal<{expires_at: string}>(sessionPath, { signal: controller.signal, cache: 'no-store' }).then(value => {
        if (!stopped && (!Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now() || Date.parse(value.expires_at) !== expires.current)) clear()
      }).catch(() => { if (!stopped) clear() }).finally(() => { window.clearTimeout(fail); if (pending === controller) pending = null })
    }, 5000)
    const visibility = (): void => { if (document.visibilityState !== 'visible') clear() }
    document.addEventListener('visibilitychange', visibility)
    return () => { stopped = true; pending?.abort(); window.clearTimeout(timeout); window.clearInterval(poll); document.removeEventListener('visibilitychange', visibility) }
  }, [state.kind, sessionPath])
  const load = async (): Promise<void> => {
    if (parentVisualInvalidationActive()) return
    preparationRequest.current?.abort(); preparationRequest.current = null; setPreparation({kind:'idle'})
    current.current?.abort()
    const controller = new AbortController()
    current.current = controller
    setState({ kind: 'loading' })
    try {
      const data = await requestParentLocal<Preview>(`/api/parent/children/${encodeURIComponent(childId)}/daily-visual-preview/${encodeURIComponent(day)}`, { signal: controller.signal, cache: 'no-store' })
      if (controller.signal.aborted || current.current !== controller) return
      if (data.child_id !== childId || data.learning_date !== day || data.is_formal_report !== false || data.local_only !== true) throw new Error('PREVIEW_MISMATCH')
      const session = await requestParentLocal<{expires_at: string}>(sessionPath, { signal: controller.signal, cache: 'no-store' })
      if (controller.signal.aborted || current.current !== controller) return
      expires.current = Date.parse(session.expires_at)
      if (!Number.isFinite(expires.current) || expires.current <= Date.now()) throw new Error('PARENT_SESSION_EXPIRED')
      setState({ kind: 'ready', data })
    } catch {
      if (!controller.signal.aborted && current.current === controller) setState({ kind: 'error' })
    }
  }
  const changeDay = (value: string): void => { sendRequest.current?.abort(); sendRequest.current = null; setSending(false); setSendMessage(''); current.current?.abort(); current.current = null; preparationRequest.current?.abort(); preparationRequest.current = null; setPreparation({kind:'idle'}); setState({ kind: 'idle' }); setDay(value) }
  const loadPreparation = async (recipientId?: string, expectedSourceId?: string): Promise<void> => {
    if (parentVisualInvalidationActive() || state.kind !== 'ready' || preparation.kind === 'loading') return
    const controller = new AbortController(); preparationRequest.current?.abort(); preparationRequest.current = controller
    const valid = (): boolean => !controller.signal.aborted && preparationRequest.current === controller && !parentVisualInvalidationActive() && expires.current > Date.now()
    const verifySession = async (): Promise<void> => {
      const session = await requestParentLocal<{expires_at:string}>(sessionPath, {signal:controller.signal,cache:'no-store'})
      if (!valid() || Date.parse(session.expires_at) !== expires.current) throw new Error('PARENT_SESSION_EXPIRED')
    }
    setPreparation({kind:'loading'})
    let posted = false
    try {
      await verifySession()
      const path = `/api/parent/children/${encodeURIComponent(childId)}/daily-visual-preview/${encodeURIComponent(day)}/delivery-preparation`
      if (recipientId) {
        posted = true
        await requestParentLocal(path, {method:'POST',body:JSON.stringify({recipient_id:recipientId,...(expectedSourceId ? {expected_source_id:expectedSourceId} : {})}),signal:controller.signal,cache:'no-store'})
        if (!valid()) return
      }
      const data = await requestParentLocal<Preparation>(path, {signal:controller.signal,cache:'no-store'})
      if (!valid()) return
      if (data.child_id !== childId || data.day !== day || data.delivery_enabled !== false || !Array.isArray(data.recipients) || data.recipients.some(item => !item || typeof item.recipient_id !== 'string' || typeof item.label !== 'string' || typeof item.address !== 'string' || !Object.prototype.hasOwnProperty.call(preparationLabels,item.status))) throw new Error('PREPARATION_MISMATCH')
      await verifySession()
      if (valid()) setPreparation({kind:'ready',data})
    } catch (error) {
      if (controller.signal.aborted || preparationRequest.current !== controller) return
      if ((error instanceof ApiError && (error.status === 401 || error.status === 403)) || (error instanceof Error && error.message === 'PARENT_SESSION_EXPIRED') || expires.current <= Date.now()) {
        preparationRequest.current = null; setPreparation({kind:'idle'}); setState({kind:'idle'}); return
      }
      setPreparation({kind:posted ? 'uncertain' : 'error'})
    }
  }
  const sendReport = async (recipient: Preparation['recipients'][number]): Promise<void> => {
    if (sendRequest.current || parentVisualInvalidationActive() || state.kind !== 'ready' || recipient.status !== 'prepared' || !recipient.source_id) return
    const key = sendKey(childId, day, recipient.recipient_id)
    if (sendBlocked(key)) return
    const controller = new AbortController(); sendRequest.current = controller; setSending(true); setSendMessage('')
    const valid = (): boolean => !controller.signal.aborted && sendRequest.current === controller && !parentVisualInvalidationActive() && expires.current > Date.now()
    let posted = false
    try {
      const session = await requestParentLocal<{expires_at:string}>(sessionPath, {signal:controller.signal,cache:'no-store'})
      if (!valid() || Date.parse(session.expires_at) !== expires.current) throw new Error('PARENT_SESSION_EXPIRED')
      // Persist before POST. A lost response or navigation cannot authorize a duplicate.
      localStorage.setItem(key, 'pending')
      if (localStorage.getItem(key) !== 'pending') throw new Error('SEND_STATE_NOT_DURABLE')
      posted = true
      const path = `/api/parent/children/${encodeURIComponent(childId)}/daily-visual-preview/${encodeURIComponent(day)}/send`
      const result = await requestParentLocal<{status:string}>(path, {method:'POST',body:JSON.stringify({recipient_id:recipient.recipient_id,source_id:recipient.source_id}),signal:controller.signal,cache:'no-store'})
      if (!valid()) return
      if (!['sent','unknown','ready'].includes(result.status)) throw new Error('SEND_RESULT_INVALID')
      const sessionAfter = await requestParentLocal<{expires_at:string}>(sessionPath, {signal:controller.signal,cache:'no-store'})
      if (!valid() || Date.parse(sessionAfter.expires_at) !== expires.current) throw new Error('PARENT_SESSION_EXPIRED')
      if (result.status === 'ready') localStorage.removeItem(key)
      else localStorage.setItem(key, result.status)
      setPreparation(previous => previous.kind !== 'ready' ? previous : {kind:'ready',data:{...previous.data,recipients:previous.data.recipients.map(item => item.recipient_id === recipient.recipient_id ? {...item,status:result.status === 'sent' ? 'already_sent' : result.status === 'unknown' ? 'unknown' : 'prepared'} : item)}})
      setSendMessage(result.status === 'sent' ? '邮件服务器已接收学习观察日报。' : result.status === 'unknown' ? '发送结果未知，请先核对邮箱，系统不会自动重复发送。' : '本次尚未发送，请稍后重试。')
    } catch (error) {
      if (!controller.signal.aborted && sendRequest.current === controller) {
        if ((error instanceof ApiError && (error.status === 401 || error.status === 403)) || expires.current <= Date.now()) { setState({kind:'idle'}); setPreparation({kind:'idle'}) }
        else setSendMessage(posted ? '发送结果尚未确认，请核对邮箱；不会自动重复发送。' : '无法开始发送，请检查家长登录状态。')
      }
    } finally { if (sendRequest.current === controller) { sendRequest.current = null; setSending(false) } }
  }
  const checkDeletion = async (): Promise<void> => {
    const intent = captureDeletionIntent()
    if (!intent || intent.childId !== childId || recovering) return
    const controller = new AbortController(); recovery.current = controller; setRecovering(true); setRecoveryMessage('')
    try {
      const path = `/api/parent/children/${encodeURIComponent(childId)}/daily-visual-preview/deletion-receipt/${encodeURIComponent(intent.artifactId)}/${encodeURIComponent(intent.actionId)}`
      const receipt = await requestParentLocal<{verified: boolean; child_id?: string; artifact_id?: string; action_id?: string}>(path, {signal:controller.signal, cache:'no-store'})
      if (controller.signal.aborted || recovery.current !== controller) return
      if (receipt.verified !== true || receipt.child_id !== intent.childId || receipt.artifact_id !== intent.artifactId || receipt.action_id !== intent.actionId) {
        setRecoveryMessage('原操作尚未完成核验，请稍后再核对；没有再次发送删除。'); return
      }
      const session = await requestParentLocal<{expires_at: string}>(sessionPath, { signal: controller.signal, cache: 'no-store' })
      if (controller.signal.aborted || recovery.current !== controller || !Number.isFinite(Date.parse(session.expires_at)) || Date.parse(session.expires_at) <= Date.now()) return
      confirmCaptureDeletion(intent)
      setRecoveryMessage('原删除已确认完成，可以重新查看当前记录。')
    } catch { if (!controller.signal.aborted && recovery.current === controller) setRecoveryMessage('暂时无法核对原删除，请稍后重试；没有再次发送删除。') }
    finally { if (recovery.current === controller) { recovery.current = null; setRecovering(false) } }
  }
  return <section aria-label="高拍仪学习记录" className="report-delivery-panel">
    <h3>高拍仪学习记录（本机查看）</h3>
    <p>查看本机预览没有发送邮件。</p>
    <section aria-label="综合日报发送状态">
      <h4>综合日报发送状态</h4>
      {dailyStatusError ? <p role="alert">暂时无法读取日报发送状态，请稍后重试或查看邮箱。</p>
        : !dailyStatus ? <p role="status">正在读取日报发送状态…</p>
        : dailyStatus.result_unknown ? <p role="alert">上次邮件发送结果未知，请先查看邮箱；系统不会自动重复发送。</p>
        : dailyStatus.mode === 'blocked' ? <p role="alert">{dailyStatus.last_check.message || '恢复后的发送状态需要核对，日报已暂停。'}</p>
        : dailyStatus.mode === 'legacy' && !dailyStatus.sending_safety_ready ? <p>综合日报的发送保护尚未就绪，暂未启用自动综合日报。原有日志邮件状态请查看家长中心。</p>
        : dailyStatus.last_check.status === 'blocked' ? <p role="alert">{dailyStatus.last_check.message || '日报暂未发送，正在等待设置或数据恢复。'}</p>
        : dailyStatus.last_check.status === 'sent' ? <p>本次综合日报已发送，正在更新下次发送进度。</p>
        : dailyStatus.last_sent_day ? <p>已发送至 {dailyStatus.last_sent_day} 的综合日报。{dailyStatus.next_day && ` 下一份：${dailyStatus.next_day}。`}</p>
        : <p>等待发送{dailyStatus.next_day ? ` ${dailyStatus.next_day} 的` : ''}综合日报。</p>}
    </section>
    <label>日期（北京时间） <input aria-label="学习记录日期" type="date" value={day} onChange={event => changeDay(event.target.value)} /></label>{' '}
    <button type="button" disabled={!day || invalidating || sending || state.kind === 'loading'} onClick={() => void load()}>查看记录</button>
    {invalidating && <p role="status">{captureDeletionUnconfirmed() ? '采集记录的删除结果尚未确认，暂时无法查看或再次删除。请联系维护者核对原操作，不要重复删除。' : '正在处理删除或恢复，记录暂不可查看。'}</p>}
    {invalidating && captureDeletionIntent()?.childId === childId && <button type="button" disabled={recovering} onClick={() => void checkDeletion()}>核对原删除</button>}
    {recoveryMessage && <p role="status">{recoveryMessage}</p>}
    {state.kind === 'loading' && <p role="status">正在读取记录…</p>}
    {state.kind === 'error' && <p role="alert">记录暂时无法读取，可能正在删除或需要重新解锁家长中心，请稍后重试。</p>}
    {state.kind === 'ready' && <div>
      <p>这是读取时的记录；数据更改后请重新查看。OCR 可能有误；看到页面不代表孩子已经学习或掌握。可见时间是采样估计，不是做题、背书或专注时长。</p>
      {state.data.recorded_task_time && <section aria-label="任务记录计时">
        <h4>已完成任务的记录计时</h4>
        {state.data.recorded_task_time.tasks.length === 0 ? <p>当天没有可归属到当天的已完成任务计时。</p> : <ul>{state.data.recorded_task_time.tasks.map((task, index) => <li key={index}>{task.subject}：{task.title}；计时 {(task.recorded_active_seconds / 60).toFixed(1)} 分钟{task.reported_minutes !== null && `；填写 ${task.reported_minutes} 分钟`}</li>)}</ul>}
        {state.data.recorded_task_time.omitted_count > 0 && <p>另有 {state.data.recorded_task_time.omitted_count} 项时间跨日或记录不完整，未计入。</p>}
        <p>任务计时和填写时长不等于实际专注或学习时长。</p>
      </section>}
      {state.data.camera_and_document_samples?.sources.camera?.eligible_states && Object.keys(state.data.camera_and_document_samples.sources.camera.eligible_states).length > 0 && <section aria-label="历史观察摄像头样本">
        <h4>历史观察摄像头样本</h4>
        <ul>{([['present', '检测到正面人脸'], ['writing_like', '画面出现变化，原因不确定'], ['paused_like', '画面变化较少，原因不确定'], ['away', '未检测到正面人脸，也可能是遮挡']] as const).map(([key, label]) => {
          const count = state.data.camera_and_document_samples?.sources.camera?.eligible_states?.[key] ?? 0
          return count > 0 ? <li key={key}>{label}：{count} 次</li> : null
        })}</ul>
        <p>这些样本不能判断是否做题、背书或持续专注。</p>
      </section>}
      <section aria-label="日报准备状态">
        <h4>日报准备状态</h4><p>这里显示所选日期的本机草稿状态；自动发送进度以综合日报发送状态为准。</p>
        {preparation.kind === 'ready' && preparation.data.recipients.every(item => item.status !== 'already_sent' && item.status !== 'unknown' && !sendBlocked(sendKey(childId,day,item.recipient_id))) && <p>这里只准备本机草稿，尚未发送邮件。</p>}
        <button type="button" disabled={preparation.kind === 'loading' || sending} onClick={() => void loadPreparation()}>查看日报准备状态</button>
        {sendMessage && <p role="status">{sendMessage}</p>}
        {preparation.kind === 'loading' && <p role="status">正在核对日报准备状态…</p>}
        {preparation.kind === 'error' && <p role="alert">无法读取日报准备状态，请检查家长授权或稍后重试。</p>}
        {preparation.kind === 'uncertain' && <p role="alert">本机草稿准备结果尚未确认，请重新查看状态；没有发送邮件。</p>}
        {preparation.kind === 'ready' && (preparation.data.recipients.length === 0 ? <p>还没有设置日报收件人。</p> : <ul>{preparation.data.recipients.map(recipient => <li key={recipient.recipient_id}>
          <span>{recipient.label}（{recipient.address}）：{recipient.status === 'prepared' && sendBlocked(sendKey(childId, day, recipient.recipient_id)) ? '发送结果需核对' : preparationLabels[recipient.status]}</span>{' '}
          {recipient.status === 'not_prepared' && <button type="button" disabled={sending} onClick={() => void loadPreparation(recipient.recipient_id)}>准备本机草稿</button>}
          {recipient.status === 'source_changed' && typeof recipient.source_id === 'string' && recipient.source_id.length > 0 && <button type="button" disabled={sending} onClick={() => void loadPreparation(recipient.recipient_id,recipient.source_id)}>更新本机草稿</button>}
          {recipient.status === 'prepared' && recipient.source_id && (sendBlocked(sendKey(childId, day, recipient.recipient_id)) ? <span>发送结果需核对，暂不能再次发送。</span> : <button type="button" disabled onClick={() => void sendReport(recipient)}>手动发送（未开放）</button>)}
        </li>)}</ul>)}
      </section>
      {state.data.document_observation.pages.length === 0 ? <p>这一天没有可核实的高拍页面记录。</p> : state.data.document_observation.pages.map((page, index) => {
        const revisions = new Set(page.observations.map(observation => observation.revision_id))
        const time = (state.data.page_observation_time.pages ?? []).filter(item => revisions.has(item.revision_id)).reduce((sum, item) => sum + item.estimated_visible_ms, 0)
        return <article key={`${index}-${page.observations[0]?.revision_id ?? 'page'}`}>
          <h4>页面 {index + 1}</h4><p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{page.text}</p>
          <p>{page.source === 'cloud_page' ? '云端页面识别候选，尚未核对，可能有误。' : page.observations.some(observation => observation.review_required) ? '部分识别置信度较低，需要核对。' : '识别内容仍需核对。'}</p>
          {page.source !== 'cloud_page' && <p>识别置信度：{page.observations.map(observation => `${Math.round(observation.confidence * 100)}%`).join('、')}。</p>}
          <p>{time > 0 ? `符合采样条件的估计可见时间：${Math.round(time / 1000)} 秒；可能存在未采样或遮挡缺口。` : '没有足够的页面可见时间记录。'}</p>
        </article>
      })}
    </div>}
  </section>
}
