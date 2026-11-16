import { useEffect, useState } from 'react'
import { BookOpenCheck, RefreshCw, RotateCcw } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { KnowledgeGraphLearningPath } from './types'

type Props = {
  childId: string
  nodeId: string
  load?: (childId: string, nodeId: string) => Promise<KnowledgeGraphLearningPath>
}

/**
 * Child-facing, deliberately narrow projection of an already host-scored mistake.
 * The server is the approval gate: this component never receives or renders
 * pending/rejected/needs-split candidates as a fallback.
 */
export function KnowledgeGraphDiagnosticGuidance({ childId, nodeId, load = localApi.knowledgeGraphLearningPath }: Props): React.JSX.Element {
  const [path, setPath] = useState<KnowledgeGraphLearningPath | null>(null)
  const [state, setState] = useState<'loading' | 'unavailable' | 'error' | 'ready'>('loading')

  const refresh = async (): Promise<void> => {
    setState('loading')
    try {
      const result = await load(childId, nodeId)
      setPath(result)
      setState('ready')
    } catch (cause) {
      setPath(null)
      setState(cause instanceof ApiError && cause.code === 'KNOWLEDGE_POINT_UNAVAILABLE' ? 'unavailable' : 'error')
    }
  }

  useEffect(() => { void refresh() }, [childId, nodeId]) // Refresh after a durable parent approval/withdrawal.

  if (state === 'loading') return <section className="diagnostic-guidance" aria-label="已批准学习建议"><span>正在核对本机已批准的学习建议…</span></section>
  if (state === 'unavailable') return <section className="diagnostic-guidance is-unavailable" aria-label="已批准学习建议"><strong>暂时没有可用的已批准讲解或复测</strong><span>不会用待审核、已拒绝或需拆分的候选替代。请让家长先完成本机素材审核。</span><button className="secondary-button" onClick={() => void refresh()}><RefreshCw size={16} />刷新已批准建议</button></section>
  if (state === 'error' || !path) return <section className="diagnostic-guidance is-unavailable" aria-label="已批准学习建议"><strong>本机建议暂时无法读取</strong><button className="secondary-button" onClick={() => void refresh()}><RotateCcw size={16} />重试</button></section>

  const materials = path.materials.filter((item) => item.review_status === 'approved')
  const retests = path.diagnosis.retest_items
  return <section className="diagnostic-guidance" aria-label="已批准学习建议">
    <div className="tutor-heading"><BookOpenCheck size={21} /><div><strong>根据这次错题，下一步这样学</strong><span>只显示家长或产品已批准的本机素材；不会展示候选内容。</span></div></div>
    {materials.length > 0 ? <div className="guidance-group"><b>先看讲解</b><ul>{materials.map((item, index) => <li key={`${item.title}-${index}`}><strong>{item.title}</strong><span>{item.preview || '本机讲解片段可用'}</span><small>本机已批准 · {item.license_boundary ?? '使用边界待确认'}</small></li>)}</ul></div> : <p>当前没有可直接讲解的已批准片段；请使用课本或请家长补充材料。</p>}
    {retests.length > 0 ? <div className="guidance-group"><b>再做一道独立复测</b><ul>{retests.map((item, index) => <li key={`${item.title}-${index}`}><strong>{item.title}</strong><span>{item.basis === 'same_knowledge_point' ? '复测同一知识点' : '复测关联知识点'}</span></li>)}</ul></div> : <p>暂时没有已批准的复测题，不把未审核题目当作练习推荐。</p>}
    {path.next_action === 'manual_material_required' && <p>这次错题可以记录，但还需要家长提供或批准合适的讲解材料。</p>}
    <button className="secondary-button" onClick={() => void refresh()}><RefreshCw size={16} />刷新已批准建议</button>
  </section>
}
