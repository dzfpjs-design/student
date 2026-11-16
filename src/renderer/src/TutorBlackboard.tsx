import { useEffect, useMemo, useRef } from 'react'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import { ArrowRight } from 'lucide-react'
import type { TutorBlackboardBlock, TutorBlackboardPresentation, TutorTurn } from './types'

export function tutorBlackboardFromTurn(turn: TutorTurn | null): TutorBlackboardPresentation | null {
  const candidate = turn?.output_envelope?.result?.blackboard
  if (!candidate || typeof candidate !== 'object') return null
  const board = candidate as Partial<TutorBlackboardPresentation>
  if (board.version !== 'tutor_blackboard_v1' || !Array.isArray(board.segments) || board.segments.length < 1) return null
  if (board.segments.some((segment) => !segment || typeof segment.narration !== 'string' || !Array.isArray(segment.blocks))) return null
  return board as TutorBlackboardPresentation
}

export function tutorExampleNotice(turn: TutorTurn | null, playbackTurnId: string | null): string | null {
  if (!playbackTurnId || turn?.turn_id !== playbackTurnId) return null
  const status = turn.output_envelope?.example_quality?.status
  if (status === 'missing') return '这次还没有找到合适的例题，你可以让我换一个例子。'
  if (status === 'unavailable') return '例题暂时没能核对，你可以让我换一个例子。'
  return null
}

export function tutorSegmentStartDelays(presentation: TutorBlackboardPresentation): number[] {
  let elapsed = 0
  return presentation.segments.map((segment) => {
    const delay = elapsed
    elapsed += Math.max(1_200, Math.min(16_000, segment.duration_hint_ms || estimatedNarrationDuration(segment.narration)))
    return delay
  })
}

function estimatedNarrationDuration(text: string): number {
  const punctuation = (text.match(/[，。？！；：]/g) ?? []).length
  return Math.max(1_400, Math.min(16_000, text.length * 235 + punctuation * 180))
}

function Formula({ latex, label }: { latex: string; label?: string }): React.JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ref.current) return
    try {
      katex.render(latex, ref.current, { displayMode: true, throwOnError: false, strict: 'ignore', trust: false })
    } catch {
      ref.current.textContent = label || latex
    }
  }, [label, latex])
  return <div className="blackboard-formula" ref={ref} aria-label={label || latex} />
}

function BoardBlock({ block }: { block: TutorBlackboardBlock }): React.JSX.Element {
  const className = `blackboard-block is-${block.kind} tone-${block.tone}`
  if (block.kind === 'formula') return <div className={className}>{block.text && <span>{block.text}</span>}<Formula latex={block.latex ?? ''} label={block.text} /></div>
  if (block.kind === 'steps') return <ol className={className}>{(block.items ?? []).map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol>
  if (block.kind === 'table') return (
    <div className={`${className} blackboard-table-wrap`}>
      <table><thead><tr>{(block.columns ?? []).map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{(block.rows ?? []).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={`${cellIndex}-${cell}`}>{cell}</td>)}</tr>)}</tbody></table>
    </div>
  )
  if (block.kind === 'relation') return (
    <div className={className}>{(block.nodes ?? []).map((node, index) => <span className="blackboard-relation-node" key={`${index}-${node}`}>{node}{index < (block.nodes?.length ?? 0) - 1 && <ArrowRight aria-hidden="true" size={22} />}</span>)}</div>
  )
  if (block.kind === 'heading') return <h3 className={className}>{block.text}</h3>
  return <div className={className}>{block.text}</div>
}

export function TutorBlackboard({ presentation, revealedSegments }: {
  presentation: TutorBlackboardPresentation
  revealedSegments: number
}): React.JSX.Element {
  const sectionRef = useRef<HTMLElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const visible = useMemo(() => presentation.segments.slice(0, Math.max(0, revealedSegments)), [presentation, revealedSegments])
  useEffect(() => {
    const node = scrollRef.current
    if (node && typeof node.scrollTo === 'function') node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' })
  }, [visible.length])
  useEffect(() => {
    const node = sectionRef.current
    if (visible.length > 0 && node && typeof node.scrollIntoView === 'function') {
      node.scrollIntoView({ block: 'center', behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
    }
  }, [presentation.title, visible.length > 0])
  const activeId = visible.at(-1)?.segment_id
  return (
    <section className="tutor-blackboard" aria-label={`${presentation.subject}讲解黑板`} ref={sectionRef}>
      <header><span>{presentation.subject}</span><strong>{presentation.title}</strong></header>
      {presentation.source === 'local_degraded' && <p className="blackboard-fallback-notice" role="status">板书暂时没有整理好，先显示讲解文字。</p>}
      <div className="tutor-blackboard-surface" ref={scrollRef} aria-live="polite">
        {visible.map((segment) => (
          <div className={`blackboard-segment${segment.segment_id === activeId ? ' is-active' : ''}`} key={segment.segment_id}>
            {segment.blocks.map((block, index) => <BoardBlock block={block} key={`${segment.segment_id}-${index}`} />)}
          </div>
        ))}
        {visible.length === 0 && <div className="blackboard-writing-cursor" aria-label="准备板书" />}
      </div>
    </section>
  )
}
