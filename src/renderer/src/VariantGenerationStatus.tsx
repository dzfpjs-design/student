import { useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { ApiError, localApi, requestParentLocal } from './api'
import type { VariantGenerationJob } from './types'
import { PaperVariantAuthorizationPanel } from './PaperVariantAuthorizationPanel'
import type { PaperParentRequest } from './paperVariantAuthorizationApi'

function paperMistake(job: VariantGenerationJob, childId: string): string | null {
  const owned = job as VariantGenerationJob & { child_id?: unknown; mistake_id?: unknown; source_snapshot_json?: unknown }
  if (owned.child_id !== childId || typeof owned.mistake_id !== 'string' || !owned.mistake_id) return null
  if (job.error_code?.startsWith('PAPER_VARIANT_') || job.error_code?.startsWith('SUPPLY_PAPER_')) return owned.mistake_id
  try {
    const source = typeof owned.source_snapshot_json === 'string' ? JSON.parse(owned.source_snapshot_json) : null
    return typeof source?.source_ref === 'string' && source.source_ref.startsWith('confirmed_paper_question_v1:') ? owned.mistake_id : null
  } catch { return null }
}

const statusLabel = { pending: '等待准备', processing: '正在准备', completed: '准备结束', blocked: '需要处理', failed: '准备失败' }
function reason(job: VariantGenerationJob): string {
  if (job.error_code?.startsWith('PAPER_VARIANT_')) return '纸面原题的变式用途需要家长核对，旧任务不会通过重新批准而复活。'
  if (job.error_code?.startsWith('SUPPLY_')) return '来源题目需要重新核对，当前题源不能继续生成。'
  if (job.error_code === 'MODEL_NOT_CONFIGURED') return '尚未配置教学模型。'
  if (job.error_code === 'GENERATION_OUTCOME_UNKNOWN') return '上次模型请求结果不确定，重新准备可能产生一次新的模型费用。'
  if (job.error_code) return '模型响应未能形成合格候选，重试会发起新的模型请求。'
  return `${job.candidate_count} 道候选`
}

type VariantGenerationStatusProps = {
  childId: string; onUpdated: () => void | Promise<void>; request?: PaperParentRequest; onRequireParentAuth?: () => void
}
export function VariantGenerationStatus(props: VariantGenerationStatusProps): React.JSX.Element {
  return <GenerationStatus key={props.childId} {...props} />
}
function GenerationStatus({ childId, onUpdated, request = requestParentLocal, onRequireParentAuth }: VariantGenerationStatusProps): React.JSX.Element | null {
  const [jobs, setJobs] = useState<VariantGenerationJob[]>([])
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const generation = useRef(0)
  const alive = useRef(false), writing = useRef(false)
  const load = async (propagate = false): Promise<boolean> => {
    if (!alive.current) return false
    const version = ++generation.current
    try {
      const result = await localApi.variantGenerationJobs(childId)
      if (alive.current && version === generation.current) {
        setJobs(result.filter(job => job.generation_mode === 'model' && (job.status !== 'completed' || paperMistake(job, childId))))
        setMessage(null); return true
      }
    } catch (error) {
      if (alive.current && version === generation.current) {
        setMessage('题目准备状态未能读取。')
        if (propagate) throw error
      }
    }
    return false
  }
  useEffect(() => { alive.current = true; void load(); return () => { alive.current = false; generation.current++ } }, [childId])
  const retry = async (job: VariantGenerationJob): Promise<void> => {
    if (!alive.current || writing.current) return
    writing.current = true; setBusy(true); setMessage(null); generation.current++
    const key = `hsc-variant-retry:${childId}:${job.job_id}`
    let accepted = false, commandSaved = false
    try {
      const action = localStorage.getItem(key) ?? crypto.randomUUID()
      localStorage.setItem(key, action)
      commandSaved = true
      const result = await localApi.retryVariantGeneration(childId, job.job_id, action)
      accepted = true
      if (localStorage.getItem(key) === action) localStorage.removeItem(key)
      if (alive.current) {
        generation.current++
        setJobs(current => current.map(item => item.job_id === job.job_id ? result : item))
        setMessage(result.status === 'blocked' ? '原重试回执已核对；任务当前仍需处理，不会据旧回执自动继续。'
          : result.status === 'failed' ? '原重试回执已核对；任务当前准备失败，请先核对原因。'
            : result.status === 'completed' ? '原重试回执已核对；准备已结束，候选仍须审核。'
              : '重试已保存，后台将继续准备题目。')
        try { await onUpdated() }
        catch { if (alive.current) setMessage(['pending', 'processing'].includes(result.status)
          ? '重试已保存，但候选列表未刷新；请刷新查看，不要重复提交重试。'
          : `原重试回执已核对，但候选列表未刷新；任务当前${statusLabel[result.status]}，请刷新查看。`) }
      }
    } catch (error) {
      if (alive.current) setMessage(accepted ? '重试已保存，但本机恢复指令未能清理；请核对原请求，不要新建重试。'
        : !commandSaved ? '本机无法保存重试指令，未发送请求。'
          : error instanceof ApiError ? error.message : '未确认重试结果，原重试指令已保留。')
    } finally { writing.current = false; if (alive.current) setBusy(false) }
  }
  const refresh = async (): Promise<void> => {
    if (!alive.current || writing.current) return
    const version = generation.current + 1
    try { if (await load(true) && alive.current) await onUpdated() }
    catch { if (alive.current && generation.current === version) setMessage('准备状态或候选列表未刷新，请重试读取；没有重新准备题目。') }
  }
  if (!jobs.length && !message) return null
  return <div className="variant-generation-status">
    <div className="panel-heading"><h3>题目准备进度</h3><button disabled={busy} onClick={() => { void refresh() }}><RefreshCw size={16} />刷新准备状态</button></div>
    {message && <p role="status">{message}</p>}
    {jobs.map((job, index) => <div key={job.job_id} data-work-object={job.job_id}>
      <p><strong>准备任务 {index + 1} · {statusLabel[job.status]}</strong>　{reason(job)}</p>
      {['blocked', 'failed'].includes(job.status) && !job.error_code?.startsWith('SUPPLY_') && !job.error_code?.startsWith('PAPER_VARIANT_') && <button disabled={busy} onClick={() => void retry(job)}><RefreshCw size={16} />重新准备候选题</button>}
    </div>)}
    {[...new Set(jobs.map(job => paperMistake(job, childId)).filter((id): id is string => !!id))].map(mistakeId =>
      <PaperVariantAuthorizationPanel key={`${childId}:${mistakeId}`} childId={childId} mistakeId={mistakeId} request={request}
        onRequireParentAuth={onRequireParentAuth} onChanged={async () => { if (await load(true) && alive.current) await onUpdated() }} />)}
  </div>
}
