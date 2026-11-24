import type { DocumentPreviewPageDraft } from './types'

export function DocumentPageDraft({ draft }: { draft: DocumentPreviewPageDraft }) {
  const historical = draft.lines.filter(line => line.status === 'historical').length
  const conflicts = draft.lines.filter(line => line.conflict).length
  const atCapacity = ['source_limit', 'line_limit'].includes(draft.reason)
  const unmapped = atCapacity ? [] : draft.unmapped_lines ?? []
  return <section aria-label="累计纸面文字草稿">
    <strong>{atCapacity ? '已保存的纸面草稿' : draft.merged ? '累计纸面文字，待核对' : '本次纸面文字，待核对'}</strong>
    <small>{draft.frame_count} 次采样 · {draft.lines.length} 行文字{historical > 0 ? ` · ${historical} 行来自先前画面` : ''}{conflicts > 0 ? ` · ${conflicts} 行有不同读值` : ''}</small>
    <small>{atCapacity ? '本次新读值未加入这份草稿，仍单独保存。' : draft.merged
      ? '只累计实际读到的文字。先前画面的内容可能已经修改；不同读值会一起保留，请对照纸面核对。'
      : '尚未确认与先前画面是同一页，先单独保留这次读到的内容。'}</small>
    <ul aria-label="已对齐的草稿文字" style={{ paddingInlineStart: '1.4em', overflowWrap: 'anywhere' }}>
      {draft.lines.map((line, index) => <li key={`${line.preview_id}:${index}`}>
        {line.conflict && <b>不同读值 · </b>}
        {line.status === 'historical' && <span>先前画面 · </span>}
        <span>{line.text}</span>
      </li>)}
    </ul>
    {unmapped.length > 0 && <section aria-label="本次单独保留的文字">
      <strong>本次单独保留的文字</strong>
      <small>{unmapped.length} 行文字尚未对齐，已单独保存，没有并入上面的草稿。</small>
      <ul style={{ paddingInlineStart: '1.4em', overflowWrap: 'anywhere' }}>
        {unmapped.map((line, index) => <li key={`${line.preview_id}:unmapped:${index}`}>{line.text}</li>)}
      </ul>
    </section>}
    <small>未读到的区域仍未知。草稿不会自动判错或计入学习报告。</small>
  </section>
}
