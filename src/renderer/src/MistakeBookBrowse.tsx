import { useEffect, useId, useState } from 'react'
import { Search } from 'lucide-react'
import type { MistakeLearningState } from './mistakeLearningTypes'

type Period = '' | 'today' | 'week' | 'month' | 'previous_week' | 'custom'
export type BookFilters = { query: string; subject: string; cause: string; date: Period; from: string; to: string }
export const emptyBookFilters: BookFilters = { query: '', subject: '', cause: '', date: '', from: '', to: '' }

const gradeNumbers = '一二三四五六七八九'
const commonSubjects = ['语文', '数学', '英语', '道德与法治']
const juniorSubjects = [...commonSubjects, '历史', '地理', '生物']
const seniorSubjects = ['语文', '数学', '英语', '物理', '化学', '生物', '思想政治', '历史', '地理', '信息技术', '通用技术']
const dateWords: Array<[RegExp, Period]> = [[/最近一个月|本月/, 'month'], [/近\s*[7七]\s*天|最近一周|最近的?|这周/, 'week'], [/上周|上星期/, 'previous_week'], [/今天|今日/, 'today']]
const synonyms = [['分数除法', '分数相除', '除以分数'], ['倒数', '颠倒分子分母'], ['热传递', '传热', '热量传递'], ['一般过去时', '过去式', '过去时'], ['现在完成时', '完成时'], ['运算规则', '运算法则', '算错'], ['审题遗漏', '漏看条件', '看漏条件'], ['英语', '英文']]

export function bookSubjects(grade: string, items: MistakeLearningState[]): string[] {
  const numeric = grade.match(/(?:^|[^\d])([1-9]|1[0-2])(?:年级|$)/)
  const chinese = grade.match(/([一二三四五六七八九])年级/)
  const junior = grade.match(/(?:初|初中)([一二三])(?:年级)?/)
  const year = numeric ? Number(numeric[1]) : junior ? 7 + gradeNumbers.indexOf(junior[1]) : chinese ? 1 + gradeNumbers.indexOf(chinese[1]) : 0
  const enrolled = /^高(?:中)?[一二三]/.test(grade) || year >= 10 ? seniorSubjects : year === 9 ? [...juniorSubjects, '物理', '化学'] : year === 8 ? [...juniorSubjects, '物理'] : year === 7 ? juniorSubjects : year > 0 ? [...commonSubjects, '科学'] : []
  return [...new Set([...enrolled, ...items.map(item => item.source.subject.trim()).filter(Boolean)])]
}

export function bookCauses(item: MistakeLearningState): string[] {
  return [...new Set(item.analysis?.wrong_causes.map(cause => cause.label.trim()).filter(Boolean) ?? [])]
}

function normalize(text: string): string { return text.normalize('NFKC').trim().toLocaleLowerCase('zh-CN') }

function dateFilter(date: string | undefined, period: Period, from = '', to = ''): boolean {
  if (!period || period === 'custom' && !from && !to) return true
  const value = date ? new Date(date) : null
  if (!value || !Number.isFinite(value.getTime())) return false
  const start = new Date();start.setHours(0, 0, 0, 0)
  const end = new Date(start);end.setDate(end.getDate() + 1)
  if (period === 'week') start.setDate(start.getDate() - 6)
  if (period === 'month') start.setDate(1)
  if (period === 'previous_week') {
    start.setDate(start.getDate() - (start.getDay() + 6) % 7)
    end.setTime(start.getTime());start.setDate(start.getDate() - 7)
  }
  if (period !== 'custom') return value >= start && value < end
  return (!from || value >= new Date(`${from}T00:00:00`)) && (!to || value <= new Date(`${to}T23:59:59.999`))
}

function parseQuery(raw: string, subjects: string[]): { words: string[]; period: Period } {
  let query = normalize(raw).replace(/^(?:小知[，,\s]*)?(?:(?:请)?(?:帮我|给我)?(?:找一下|查一下|搜一下|找找|看看|搜索|查找|查|找))?/, '')
  const period = dateWords.find(([word]) => word.test(query))?.[1] ?? ''
  for (const [word] of dateWords) query = query.replace(word, ' ')
  query = query.replace(/错题|题目|那道题|这道题|的题|做错的|错过的/g, ' ')
  for (const subject of [...subjects, '英文']) query = query.replaceAll(normalize(subject), ` ${normalize(subject)} `)
  const words = query.split(/[\s、,，。；;·:：!?！？]+/).map(word => word.replace(/^的|的$/g, '').replace(/^(.{2,})题$/, '$1')).filter(Boolean)
  return { words, period }
}

export type BookSearchExtras = (item: MistakeLearningState) => { causes: string[]; fields: string[] }
export function filteredMistakes(items: MistakeLearningState[], filters: BookFilters, subjects: string[], searchExtras?: BookSearchExtras): MistakeLearningState[] {
  const { words, period } = parseQuery(filters.query, subjects)
  return items.filter(item => {
    const extra = searchExtras?.(item)
    if (filters.subject && item.source.subject !== filters.subject || filters.cause && ![...bookCauses(item), ...extra?.causes ?? []].includes(filters.cause)) return false
    if (!dateFilter(item.source.created_at, filters.date, filters.from, filters.to) || !dateFilter(item.source.created_at, period)) return false
    const fields = [item.source.title, item.source.question, item.source.subject, ...bookCauses(item), ...item.analysis?.knowledge_points.map(point => point.label) ?? [], ...extra?.fields ?? [], ...extra?.causes ?? []].map(normalize)
    return fields.includes(normalize(filters.query)) || words.every(word => (synonyms.find(group => group.includes(word)) ?? [word]).some(alias => fields.some(field => field.includes(alias))))
  })
}

type Suggestion = { kind: '题目' | '知识点' | '错因' | '整句搜索'; label: string; query: string; id?: string }
type FilterProps = { items: MistakeLearningState[]; subjects: string[]; causes: string[]; value: BookFilters; onChange: (next: BookFilters) => void; onOpen: (mistakeId: string) => void; searchExtras?: BookSearchExtras }

export function MistakeBookFilters({ items, subjects, causes, value, onChange, onOpen, searchExtras }: FilterProps): React.JSX.Element {
  const inputId = useId()
  const listId = `${inputId}-choices`
  const [draft, setDraft] = useState(value.query)
  const [open, setOpen] = useState(false)
  const [index, setIndex] = useState(-1)
  useEffect(() => { setDraft(value.query);setOpen(false);setIndex(-1) }, [value.query])
  useEffect(() => {
    if (!open || index < 0) return
    const list = document.getElementById(listId)
    const option = document.getElementById(`${listId}-${index}`)
    if (!list || !option) return
    const bounds = list.getBoundingClientRect()
    const row = option.getBoundingClientRect()
    if (row.top < bounds.top) list.scrollTop -= bounds.top - row.top
    else if (row.bottom > bounds.bottom) list.scrollTop += row.bottom - bounds.bottom
  }, [index, listId, open])
  const matched = draft.trim() ? filteredMistakes(items, { ...value, query: draft }, subjects, searchExtras) : []
  const byLabel = (labels: string[], kind: '错因' | '知识点'): Suggestion[] => [...new Set(labels)].filter(Boolean).slice(0, 2).map(label => ({ kind, label, query: label }))
  const period = dateWords.find(([word]) => word.test(draft))?.[0].exec(draft)?.[0]
  const choices: Suggestion[] = draft.trim() ? [
    ...matched.slice(0, 3).map(item => ({ kind: '题目' as const, label: item.source.title || item.source.question, query: item.source.title || item.source.question, id: item.mistake_id })),
    ...byLabel(matched.flatMap(item => item.analysis?.knowledge_points.map(point => point.label) ?? []), '知识点'),
    ...byLabel(matched.flatMap(item => [...bookCauses(item), ...searchExtras?.(item).causes ?? []]), '错因'),
    { kind: '整句搜索', label: `查找「${draft.trim()}」`, query: draft.trim() }
  ] : []
  function close(): void { setOpen(false);setIndex(-1) }
  function commit(choice?: Suggestion): void {
    const query = choice?.kind === '整句搜索' || !period ? choice?.query ?? draft.trim() : choice ? `${period} ${choice.query}` : draft.trim()
    setDraft(query);close()
    onChange({ ...value, query })
    if (choice?.id) onOpen(choice.id)
  }
  const showChoices = open && choices.length > 0
  return <div className="cloud-book-filters" aria-label="错题筛选">
    <form className="cloud-book-search" role="search" onSubmit={event => { event.preventDefault();commit(choices[index]) }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close() }}>
      <label htmlFor={inputId}><Search size={17}/><span className="cloud-book-visually-hidden">搜索错题</span></label>
      <input id={inputId} role="combobox" aria-expanded={showChoices} aria-autocomplete="list" aria-controls={listId} aria-activedescendant={index >= 0 ? `${listId}-${index}` : undefined} autoComplete="off" maxLength={120} value={draft} placeholder="如：找最近做错的分数题"
        onFocus={() => setOpen(true)} onChange={event => { setDraft(event.target.value);setIndex(-1);setOpen(true) }} onKeyDown={event => {
          if (event.key === 'Escape') { event.preventDefault();close() }
          if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && choices.length) {
            event.preventDefault();setOpen(true);setIndex(previous => event.key === 'ArrowDown' ? (previous + 1) % choices.length : previous <= 0 ? choices.length - 1 : previous - 1)
          }
        }}/>
      <button type="submit" className="secondary-button">查找</button>
      <div id={listId} role="listbox" aria-label="搜索联想" hidden={!showChoices}>
        {choices.map((choice, position) => <div key={`${choice.kind}-${choice.id ?? choice.query}`} id={`${listId}-${position}`} role="option" aria-selected={position === index} onPointerDown={event => event.preventDefault()} onClick={() => commit(choice)}><span>{choice.label}</span><small>{choice.kind}</small></div>)}
      </div>
    </form>
    <label>日期<select value={value.date} onChange={event => onChange({ ...value, date: event.target.value as Period })}><option value="">全部日期</option><option value="today">今天</option><option value="week">近 7 天</option><option value="previous_week">上周</option><option value="month">本月</option><option value="custom">自选日期</option></select></label>
    <label>学科<select value={value.subject} onChange={event => onChange({ ...value, subject: event.target.value })}><option value="">全部学科</option>{subjects.map(subject => <option key={subject}>{subject}</option>)}</select></label>
    <label>错因<select value={value.cause} onChange={event => onChange({ ...value, cause: event.target.value })}><option value="">全部错因</option>{causes.map(cause => <option key={cause}>{cause}</option>)}</select></label>
    <button type="button" className="secondary-button" onClick={() => { setDraft('');close();onChange(emptyBookFilters) }}>清空</button>
    {value.date === 'custom' && <div className="cloud-book-date-range"><label>起始日期<input type="date" value={value.from} max={value.to || undefined} onChange={event => onChange({ ...value, from: event.target.value })}/></label><label>结束日期<input type="date" value={value.to} min={value.from || undefined} onChange={event => onChange({ ...value, to: event.target.value })}/></label></div>}
  </div>
}
