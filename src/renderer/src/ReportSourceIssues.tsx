import { useEffect, useRef, useState } from 'react'
import { parentDraftAuthorityEvent, parentDraftAuthorityVersion } from './parentDraftAuthority'
import { parentVisualInvalidationActive, parentVisualInvalidationEvent } from './parentVisualInvalidation'
import { requestParentLocal } from './api'

type SourceIssue = {
  issue_id: string; child_id: string; source_id: string; status: 'open' | 'resolved'
  report_type?: 'daily' | 'weekly' | 'monthly'; report_date?: string
  snapshot_id?: string | null; error_code?: string; message: string; recovery_hint: string
}
type LegacyOriginal = { child_id: string; report_id: string; report_date: string; verification: 'legacy_unverified';
  can_send: false; can_create_trusted_report: false; local_only: true; expires_at: string; source_digest: string;
  summary: Record<string, unknown>; parent_coaching: Record<string, unknown> }
type Props = { childId: string; ready: boolean; refreshVersion: number; onOpen: (id: string) => Promise<void> }
const asText = (value: unknown): string => typeof value === 'string' ? value : ''
const asStrings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

export function ReportSourceIssues(props: Props): React.JSX.Element {
  return <IssueList key={props.childId} {...props} />
}

function IssueList({ childId, ready, refreshVersion, onOpen }: Props): React.JSX.Element {
  const [items, setItems] = useState<SourceIssue[]>([])
  const [error, setError] = useState('')
  const [opening, setOpening] = useState(false)
  const [legacy, setLegacy] = useState<LegacyOriginal | null>(null)
  const [legacyLoading, setLegacyLoading] = useState(false)
  const legacyRequest = useRef<AbortController | null>(null)
  const legacyEpoch = useRef(0)
  useEffect(() => {
    const clear = (): void => { legacyEpoch.current++; legacyRequest.current?.abort(); legacyRequest.current=null; setLegacy(null); setLegacyLoading(false) }
    const visibility = (): void => { if (document.visibilityState !== 'visible') clear() }
    window.addEventListener(parentDraftAuthorityEvent,clear)
    window.addEventListener('hsc-runtime-restarted',clear)
    window.addEventListener(parentVisualInvalidationEvent,clear)
    document.addEventListener('visibilitychange',visibility)
    if (!ready) clear()
    return () => { legacyEpoch.current++; legacyRequest.current?.abort(); legacyRequest.current=null
      window.removeEventListener(parentDraftAuthorityEvent,clear); window.removeEventListener('hsc-runtime-restarted',clear)
      window.removeEventListener(parentVisualInvalidationEvent,clear); document.removeEventListener('visibilitychange',visibility) }
  }, [childId,ready,refreshVersion])
  useEffect(() => {
    if (!legacy) return
    let stopped = false
    let check: AbortController | null = null
    const clear = (): void => { legacyEpoch.current++; legacyRequest.current?.abort(); legacyRequest.current=null; setLegacy(null); setLegacyLoading(false) }
    const timeout = window.setTimeout(clear,Math.max(0,Math.min(2147483647,Date.parse(legacy.expires_at)-Date.now())))
    const interval = window.setInterval(() => {
      if (check) return
      const controller=new AbortController(); check=controller
      const fail=window.setTimeout(() => { controller.abort(); if (!stopped) clear() },3000)
      const statusPath=`/v1/parent/children/${encodeURIComponent(childId)}/legacy-daily-reports/${encodeURIComponent(legacy.report_id)}/original/status`
      void requestParentLocal<{child_id:string;report_id:string;verification:string;source_digest:string;expires_at:string}>(statusPath,
        {signal:controller.signal,cache:'no-store'}).then(value => {
          if (!stopped && (value.child_id!==childId || value.report_id!==legacy.report_id || value.verification!=='legacy_unverified'
              || value.source_digest!==legacy.source_digest || Date.parse(value.expires_at)!==Date.parse(legacy.expires_at)
              || Date.parse(value.expires_at)<=Date.now())) clear()
        }).catch(() => { if (!stopped) clear() }).finally(() => { window.clearTimeout(fail); if (check===controller) check=null })
    },5000)
    return () => { stopped=true; check?.abort(); window.clearTimeout(timeout); window.clearInterval(interval) }
  }, [childId,legacy])
  useEffect(() => {
    if (!ready) return
    let current = true
    void requestParentLocal<{ child_id: string; items: SourceIssue[] }>(
      `/v1/parent/children/${encodeURIComponent(childId)}/report-source-issues`, { cache: 'no-store' },
    ).then(result => {
      if (!current) return
      if (result.child_id !== childId || !Array.isArray(result.items) || result.items.some(item => item.child_id !== childId)) {
        throw new Error('报告来源归属未能核对。')
      }
      setItems(result.items.filter(item => item.status === 'open'))
      setError('')
    }).catch(() => {
      if (current) setError('历史报告来源问题尚未读取，请刷新报告后重试；不能据此认为没有问题。')
    })
    return () => { current = false }
  }, [childId, ready, refreshVersion])
  const openLegacy = async (item: SourceIssue): Promise<void> => {
    if (!ready || document.visibilityState !== 'visible' || parentVisualInvalidationActive() || item.child_id !== childId || item.error_code !== 'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED') return
    legacyRequest.current?.abort(); const controller=new AbortController(); legacyRequest.current=controller
    const epoch=++legacyEpoch.current; const authority=parentDraftAuthorityVersion()
    setLegacy(null); setLegacyLoading(true); setError('')
    try {
      const path=`/v1/parent/children/${encodeURIComponent(childId)}/legacy-daily-reports/${encodeURIComponent(item.source_id)}/original`
      const data=await requestParentLocal<LegacyOriginal>(path,{signal:controller.signal,cache:'no-store'})
      if (controller.signal.aborted || epoch!==legacyEpoch.current || authority!==parentDraftAuthorityVersion() || document.visibilityState !== 'visible' || parentVisualInvalidationActive()) return
      if (data.child_id!==childId || data.report_id!==item.source_id || data.verification!=='legacy_unverified'
          || data.can_send!==false || data.can_create_trusted_report!==false || data.local_only!==true
          || typeof data.source_digest!=='string' || !/^[0-9a-f]{64}$/.test(data.source_digest)
          || !Number.isFinite(Date.parse(data.expires_at)) || Date.parse(data.expires_at)<=Date.now()
          || !data.summary || typeof data.summary!=='object' || Array.isArray(data.summary)
          || !data.parent_coaching || typeof data.parent_coaching!=='object' || Array.isArray(data.parent_coaching)) throw new Error('LEGACY_ORIGINAL_INVALID')
      setLegacy(data)
    } catch {
      if (!controller.signal.aborted && epoch===legacyEpoch.current) setError('旧日报原稿暂时无法安全读取；原记录仍保留，请重新解锁家长中心后再试。')
    } finally { if (epoch===legacyEpoch.current) { legacyRequest.current=null; setLegacyLoading(false) } }
  }
  const open = async (id: string): Promise<void> => {
    setOpening(true)
    try { await onOpen(id); setError('') }
    catch { setError('原报告暂不能打开，问题记录仍保留。请刷新后核对。') }
    finally { setOpening(false) }
  }
  return <>{error && <p role="status">{error}</p>}{items.length > 0 && <section aria-label="历史报告来源问题">
    <h3>历史报告需要核对</h3>
    <ul>{items.map(item => <li key={item.issue_id} data-work-object={item.source_id}>
      <strong>{item.report_date ?? '历史记录'} · {{ daily: '日报', weekly: '周报', monthly: '月报' }[item.report_type ?? 'daily']}</strong>
      <p>{item.message}</p><p>{item.recovery_hint}</p>
      {item.snapshot_id && <button type="button" disabled={opening} onClick={() => { void open(item.snapshot_id!) }}>查看保留的原报告</button>}
      {item.report_type === 'daily' && item.error_code === 'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED' && <button type="button" disabled={legacyLoading} onClick={() => { void openLegacy(item) }}>查看旧日报原稿</button>}
    </li>)}</ul>
    {legacyLoading && <p role="status">正在读取旧日报原稿…</p>}
    {legacy && <LegacyOriginalView report={legacy} onClose={() => { legacyEpoch.current++; setLegacy(null) }} />}
  </section>}</>
}

function LegacyOriginalView({report,onClose}:{report:LegacyOriginal;onClose:()=>void}): React.JSX.Element {
  const summary=report.summary, coaching=report.parent_coaching, action=asRecord(coaching.action)
  const avoid=Array.isArray(coaching.avoid) ? coaching.avoid.map(asRecord) : []
  return <article aria-label="旧日报原稿">
    <p role="alert">历史内容未经核验，只供查看；不能发送或作为已核实的当前学习依据。此处展示保留的摘要和沟通建议，旧日报未冻结当时的独立证据明细。</p>
    <p>{report.report_date} · 旧日报原稿</p>
    {asText(summary.headline) && <h4>{asText(summary.headline)}</h4>}
    {asText(summary.interpretation) && <p>{asText(summary.interpretation)}</p>}
    {asStrings(summary.facts).length > 0 && <section><h5>原稿中的记录</h5><ul>{asStrings(summary.facts).map((text,index)=><li key={index}>{text}</li>)}</ul></section>}
    {typeof summary.evidence_count==='number' && Number.isInteger(summary.evidence_count) && summary.evidence_count>=0 && <p>原稿标注的记录数：{summary.evidence_count}（未核验）</p>}
    {(asText(coaching.scenario) || asText(coaching.why_this_fits) || asStrings(coaching.evidence_basis).length>0) && <section><h5>原稿中的沟通场景</h5>
      {asText(coaching.scenario) && <p>{asText(coaching.scenario)}</p>}{asText(coaching.why_this_fits) && <p>{asText(coaching.why_this_fits)}</p>}
      {asStrings(coaching.evidence_basis).length>0 && <ul>{asStrings(coaching.evidence_basis).map((text,index)=><li key={index}>{text}</li>)}</ul>}</section>}
    {asStrings(coaching.script).length>0 && <section><h5>原稿中的建议话语</h5><ol>{asStrings(coaching.script).map((text,index)=><li key={index}>{text}</li>)}</ol></section>}
    {(asText(action.what) || asStrings(action.steps).length>0) && <section><h5>原稿中的行动建议</h5>
      {asText(action.what) && <p>{asText(action.what)}</p>}{asText(action.when) && <p>{asText(action.when)}</p>}
      {typeof action.duration_minutes==='number' && Number.isFinite(action.duration_minutes) && <p>原稿标注时长：{action.duration_minutes} 分钟</p>}
      {asStrings(action.steps).length>0 && <ol>{asStrings(action.steps).map((text,index)=><li key={index}>{text}</li>)}</ol>}
      {asText(action.stop_condition) && <p>{asText(action.stop_condition)}</p>}</section>}
    {avoid.length>0 && <section><h5>原稿中的避免事项</h5>{avoid.map((entry,index)=><div key={index}>
      {asText(entry.do_not_say) && <p>避免：{asText(entry.do_not_say)}</p>}{asText(entry.say_instead) && <p>可换成：{asText(entry.say_instead)}</p>}</div>)}</section>}
    <button type="button" onClick={onClose}>收起原稿</button>
  </article>
}
