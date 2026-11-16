import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { ExecutionTraceDetail, ExecutionTraceTurn } from './executionTrace'
import './executionTrace.css'

const stages: Record<string, string> = {
  request_received: '收到交互', intent_selected: '理解意图', action_scheduled: '安排操作', action_result: '操作结果',
  device_result: '设备结果', learning_result: '学习记录', plan_written: '计划保存', report_written: '报告保存'
}
const statuses: Record<string, string> = {
  pending: '待处理', planned: '已安排', running: '进行中', active: '进行中', paused: '已暂停', waiting_child: '等待回答',
  completed: '执行已结束', failed: '执行失败', unknown: '结果未确认', cancelled: '已取消', ready: '已准备',
  rescheduled: '已延期', accepted: '已接受', recorded: '已记录', withdrawn: '已撤回', revoked: '已撤销'
}
const capabilities: Record<string, string> = {
  'task.start': '开始学习任务', 'task.pause': '暂停学习任务', 'task.resume': '恢复原学习任务', 'task.complete': '完成学习任务',
  'plan.generate': '制定学习计划', 'learning.evaluate_response': '核对学习回答', 'diagnostic.submit': '保存作答',
  'mistake.open': '查看错题', 'mistake.dialogue': '讨论错题', 'break.begin': '开始休息', 'break.resume': '继续学习'
}
const errors: Record<string, string> = {
  MODEL_TIMEOUT: '模型响应超时', MODEL_UNAVAILABLE: '模型暂时不可用', MODEL_RESPONSE_INVALID: '模型返回无法执行',
  APP_RESTARTED: '应用运行曾中断', DEVICE_NOT_BOUND: '设备尚未绑定', DEVICE_COMMAND_UNKNOWN: '设备结果未确认',
  DEVICE_COMMAND_FAILED: '设备操作失败', DEVICE_COMMAND_TIMEOUT: '设备响应超时', LOCAL_SESSION_REQUIRED: '没有当前学习会话',
  PARENT_AUTH_REQUIRED: '需要家长确认', PLAN_INPUT_REQUIRED: '学习安排还缺必要信息', SOURCE_CHANGED: '原来源已变化',
  VERSION_CONFLICT: '记录版本已变化'
}
const stamp = (value: string): string => Number.isNaN(Date.parse(value)) ? '时间未记录' : new Date(value).toLocaleString('zh-CN')
const status = (value: string): string => statuses[value] ?? '结果未确认'

export function ExecutionTracePanel({ childId }: { childId: string }): React.JSX.Element {
  const [opened, setOpened] = useState(false)
  const [items, setItems] = useState<ExecutionTraceTurn[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<ExecutionTraceDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const generation = useRef(0)
  const detailGeneration = useRef(0)
  useEffect(() => {
    generation.current += 1; detailGeneration.current += 1
    setOpened(false); setItems(null); setCursor(null); setSelected(null); setDetail(null)
    setLoading(false); setDetailLoading(false); setError(''); setDetailError('')
    return () => { generation.current += 1; detailGeneration.current += 1 }
  }, [childId])

  async function load(more = false): Promise<void> {
    const id = ++generation.current
    setLoading(true); setError('')
    if (!more) {
      detailGeneration.current += 1
      setSelected(null); setDetail(null); setDetailError(''); setDetailLoading(false)
    }
    try {
      const result = await localApi.executionTraces(childId, more ? cursor : null)
      if (id !== generation.current) return
      setItems(previous => more ? [...(previous ?? []), ...result.items.filter(row => !previous?.some(old => old.turn_id === row.turn_id))] : result.items)
      setCursor(result.next_cursor)
    } catch (cause) {
      if (id === generation.current) setError(cause instanceof ApiError && cause.status === 401 ? '家长验证已过期，请重新打开家长中心。' : '暂时无法读取交互记录，请刷新重试。')
    } finally { if (id === generation.current) setLoading(false) }
  }

  async function inspect(turnId: string): Promise<void> {
    const id = ++detailGeneration.current
    setSelected(turnId); setDetail(null); setDetailError(''); setDetailLoading(true)
    try {
      const result = await localApi.executionTrace(childId, turnId)
      if (id === detailGeneration.current) {
        if (result.child_id === childId && result.turn_id === turnId) setDetail(result)
        else setDetailError('返回记录与当前交互不一致，已停止展示。请刷新核对。')
      }
    } catch (cause) {
      if (id === detailGeneration.current) setDetailError(cause instanceof ApiError && cause.status === 404 ? '这条交互记录已不可用，可能已被删除。' : '暂时无法读取执行过程，请重试查询。')
    } finally { if (id === detailGeneration.current) setDetailLoading(false) }
  }

  return <section className="execution-trace-panel">
    <details open={opened} onToggle={event => {
      const next = event.currentTarget.open
      setOpened(next)
      if (next && items === null && !loading) void load()
    }}>
      <summary>交互与执行记录</summary>
      <header><h3>最近交互</h3><button aria-label="刷新交互记录" title="刷新交互记录" disabled={loading} onClick={() => void load()}><RefreshCw size={16} /></button></header>
      {loading && <p role="status">正在读取已有记录…</p>}
      {error && <p role="alert">{error}</p>}
      {items?.length === 0 && !loading && <p>该孩子还没有交互记录。</p>}
      <ul className="execution-trace-turns">{items?.map(turn => <li key={turn.turn_id}>
        <button aria-expanded={selected === turn.turn_id} onClick={() => void inspect(turn.turn_id)}>
          <span><strong>{capabilities[turn.capability_name ?? ''] ?? '语音交互'}</strong><time>{stamp(turn.created_at)}</time></span>
          <span>{status(turn.status)}{turn.trace_event_count === 0 && <small>过程记录缺失</small>}</span><ChevronRight size={16} />
        </button>
        {turn.error_code && <p>{errors[turn.error_code] ?? '执行异常，需要核对原结果'}</p>}
        {selected === turn.turn_id && <div className="execution-trace-detail" aria-label="执行过程">
          {detailLoading && <p role="status">正在核对原记录…</p>}
          {detailError && <p role="alert">{detailError}<button aria-label="重试查询执行过程" title="重试查询执行过程" onClick={() => void inspect(turn.turn_id)}><RefreshCw size={16} /></button></p>}
          {detail && <>
            {detail.spans.length === 0 && <p>没有可用的过程记录，不能据此判断哪一步成功。</p>}
            <ol>{detail.spans.map(span => <li key={span.event_id}>
              <strong>{stages[span.event_type] ?? '运行记录'}</strong><span>记录当时：{status(span.source_status)} · {span.elapsed_ms} 毫秒</span>
              {span.error_code && <p>{errors[span.error_code] ?? '执行异常，需要核对原结果'}</p>}
              {!span.source_current && <small>这条记录之后发生过变化。</small>}
              {!span.parent_link_current && <small>前序记录已不可用。</small>}
              {span.source_current && span.recovery_action === 'check_receipt_before_retry' && <p>先核对已有结果，避免重复执行。</p>}
            </li>)}</ol>
            {detail.existing_steps.length > 0 && <p>当前操作状态：{detail.existing_steps.map(step => status(step.status)).join('、')}。</p>}
            {detail.missing_stages.length > 0 && <p>本次未记录：{detail.missing_stages.map(stage => stages[stage] ?? '其他阶段').join('、')}。这些阶段不一定都适用于本次交互。</p>}
            <p>执行记录不代表孩子已掌握知识，也不能单独证明学习改善。</p>
          </>}
        </div>}
      </li>)}</ul>
      {cursor && <button disabled={loading} onClick={() => void load(true)}><ChevronDown size={16} />较早记录</button>}
    </details>
  </section>
}
