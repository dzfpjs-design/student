import { describe, expect, it } from 'vitest'
import { DocumentPageCandidates, type PageFrameObservation } from './documentPageCandidates'

const left = { x: 0, y: 0, width: .5, height: 1 }
const right = { x: .5, y: 0, width: .5, height: 1 }
const leftLine = { x: .1, y: .2, width: .2, height: .1 }
const rightLine = { x: .6, y: .2, width: .2, height: .1 }
const scope = { childId: 'child-a', sessionId: 'session-a', monitorGeneration: 3 }
const frame = (n: number, epoch: string, visible: typeof left[], fragments: PageFrameObservation['fragments']): PageFrameObservation =>
  ({ ...scope, pageEpoch: epoch, frameId: `frame-${n}`, observedAt: n, visible, fragments })

describe('provisional page observations', () => {
  it('combines uncovered regions with per-frame provenance but never marks history as current or gradeable', () => {
    const pages = new DocumentPageCandidates(scope)
    pages.observe(frame(1, 'verified-page-a', [left], [{ rect: leftLine, text: '第一题' }]))
    const result = pages.observe(frame(2, 'verified-page-a', [right], [{ rect: rightLine, text: '第二题' }]))
    expect(result.fragments).toEqual([
      expect.objectContaining({ text: '第一题', frameId: 'frame-1', freshness: 'historical_unconfirmed' }),
      expect.objectContaining({ text: '第二题', frameId: 'frame-2', freshness: 'current' }),
    ])
    expect(result).toMatchObject({ reviewRequired: true, mayUpdateLearningRecord: false })
  })

  it('replaces or removes old answers once their region is visible again', () => {
    const pages = new DocumentPageCandidates(scope)
    pages.observe(frame(1, 'p', [left], [{ rect: leftLine, text: '旧答案' }]))
    pages.observe(frame(2, 'p', [right], []))
    const changed = pages.observe(frame(3, 'p', [left], [{ rect: leftLine, text: '新答案' }]))
    expect(changed.fragments.map(f => f.text)).toEqual(['新答案'])
    const missed = pages.observe(frame(4, 'p', [left], []))
    expect(missed.fragments[0]).toMatchObject({ text: '新答案', freshness: 'historical_unconfirmed' })
    const erased = pages.observe({ ...frame(5, 'p', [left], []), verifiedEmpty: [left] })
    expect(erased.fragments).toEqual([])
  })

  it('discards every old fragment when the verified page epoch changes', () => {
    const pages = new DocumentPageCandidates(scope)
    pages.observe(frame(1, 'page-1', [left], [{ rect: leftLine, text: '上一页' }]))
    const result = pages.observe(frame(2, 'page-2', [right], [{ rect: rightLine, text: '下一页' }]))
    expect(result.fragments.map(f => f.text)).toEqual(['下一页'])
    expect(() => pages.observe(frame(1, 'page-1', [left], [{ rect: leftLine, text: '迟到的旧页' }])))
      .toThrow('DOCUMENT_PAGE_FRAME_STALE')
    expect(pages.observe({ ...frame(3, 'page-2', [right], []), verifiedEmpty: [right] }).fragments).toEqual([])
  })

  it('refuses fragments from obscured areas and stale or repeated frames', () => {
    const pages = new DocumentPageCandidates(scope)
    expect(() => pages.observe(frame(1, 'p', [left], [{ rect: rightLine, text: '手下的字' }])))
      .toThrow('DOCUMENT_PAGE_CANDIDATE_INVALID')
    pages.observe(frame(2, 'p', [left], []))
    expect(() => pages.observe(frame(2, 'p', [right], []))).toThrow('DOCUMENT_PAGE_FRAME_STALE')
    pages.clear()
    expect(pages.observe(frame(3, 'p', [right], [])).fragments).toEqual([])
  })

  it('clears buffered observations on a child, session, or monitor-generation change', () => {
    const pages = new DocumentPageCandidates(scope)
    pages.observe(frame(1, 'p', [left], [{ rect: leftLine, text: '本次页面' }]))
    expect(() => pages.observe({ ...frame(2, 'p', [right], []), childId: 'child-b' })).toThrow('DOCUMENT_PAGE_SCOPE_CHANGED')
    expect(() => pages.observe(frame(3, 'p', [right], []))).toThrow('DOCUMENT_PAGE_SCOPE_CHANGED')
    const resumed = new DocumentPageCandidates(scope)
    expect(resumed.observe(frame(3, 'p', [right], [])).fragments).toEqual([])
    expect(() => resumed.observe({ ...frame(4, 'p', [right], []), monitorGeneration: 4 })).toThrow('DOCUMENT_PAGE_SCOPE_CHANGED')
  })
})
