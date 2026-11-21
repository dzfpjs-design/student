import type { DocumentPreviewPageDraft } from './types'

export function DocumentPageDraft({ draft }: { draft: DocumentPreviewPageDraft }) {
  const historical = draft.lines.filter(line => line.status === 'historical').length
  const conflicts = draft.lines.filter(line => line.conflict).length
  return <section aria-label="累计纸面文字草稿">
    <strong>{draft.merged ? '累计纸面文字，待核对' : '本次纸面文字，待核对'}</strong>
    <small>{draft.frame_count} 次采样 · {draft.lines.length} 行文字{historical > 0 ? ` · ${historical} 行来自先前画面` : ''}{conflicts > 0 ? ` · ${conflicts} 行有不同读值` : ''}</small>
    <small>{draft.merged
      ? '只累计实际读到的文字。先前画面的内容可能已经修改；不同读值会一起保留，请对照纸面核对。'
      : '尚未确认与先前画面是同一页，先单独保留这次读到的内容。'}</small>
    <ul style={{ paddingInlineStart: '1.4em', overflowWrap: 'anywhere' }}>
      {draft.lines.map((line, index) => <li key={`${line.preview_id}:${index}`}>
        {line.conflict && <b>不同读值 · </b>}
        {line.status === 'historical' && <span>先前画面 · </span>}
        <span>{line.text}</span>
      </li>)}
    </ul>
    <small>未读到的区域仍未知。草稿不会自动判错或计入学习报告。</small>
  </section>
}
