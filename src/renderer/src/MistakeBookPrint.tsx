import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Printer, RefreshCw } from 'lucide-react'
import { bookCauses, bookSubjects, emptyBookFilters, filteredMistakes, MistakeBookFilters, type BookFilters, type BookSearchExtras } from './MistakeBookBrowse'
import { loadMistakePrintCrop, loadMistakePrintSnapshot, type MistakePrintSnapshot, type PrintMastery } from './mistakeBookPrintApi'
import { buildMistakePrintDocument, defaultPrintOptions, printDate, type MistakePrintOptions } from './MistakePrintDocument'
import type { ChildProfile } from './types'
import './mistakeBookPrint.css'

const masteryLabels: Record<PrintMastery, string> = { unassessed: '尚未验证', not_mastered: '未掌握', retest_pending: '待复测', mastered: '已掌握' }
const periodLabels: Record<string, string> = { today: '今天', week: '近 7 天', previous_week: '上周', month: '本月' }
type Props = { child: ChildProfile; initialFilters?: BookFilters; backLabel?: string; onClose: () => void }

export function MistakeBookPrint({ child, initialFilters = emptyBookFilters, backLabel = '返回错题本', onClose }: Props): React.JSX.Element {
  const [snapshot, setSnapshot] = useState<MistakePrintSnapshot | null>(null)
  const [filters, setFilters] = useState<BookFilters>(initialFilters)
  const [mastery, setMastery] = useState<PrintMastery | ''>('')
  const [minReviews, setMinReviews] = useState('')
  const [maxReviews, setMaxReviews] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [options, setOptions] = useState<MistakePrintOptions>(defaultPrintOptions)
  const [preview, setPreview] = useState<string | null>(null)
  const [busy, setBusy] = useState<'loading' | 'preparing' | 'printing' | null>('loading')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const controller = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const selectionChild = useRef<string | null>(null)
  const all = useMemo(() => snapshot?.items.map(item => item.record) ?? [], [snapshot])
  const subjects = useMemo(() => bookSubjects(child.grade, all), [child.grade, all])
  const searchExtras = useMemo<BookSearchExtras>(() => {
    const diagnoses = new Map(snapshot?.items.map(item => [item.record.mistake_id, item.diagnosis]) ?? [])
    return record => { const diagnosis = diagnoses.get(record.mistake_id);return diagnosis ? { causes: [diagnosis.label], fields: [diagnosis.explanation] } : { causes: [], fields: [] } }
  }, [snapshot])
  const causes = useMemo(() => [...new Set(all.flatMap(item => [...bookCauses(item), ...searchExtras(item).causes]))], [all, searchExtras])
  const invalidDates = filters.date === 'custom' && !!filters.from && !!filters.to && filters.from > filters.to
  const invalidCount = [minReviews, maxReviews].some(value => value !== '' && (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)))) || minReviews !== '' && maxReviews !== '' && Number(minReviews) > Number(maxReviews)
  const matches = useMemo(() => {
    const ids = new Set(filteredMistakes(all, filters, subjects, searchExtras).map(item => item.mistake_id))
    return (snapshot?.items ?? []).filter(item => ids.has(item.record.mistake_id) && (!mastery || item.mastery.status === mastery) && (minReviews === '' || item.review_count >= Number(minReviews)) && (maxReviews === '' || item.review_count <= Number(maxReviews)))
      .sort((a, b) => (b.record.source.created_at ?? '').localeCompare(a.record.source.created_at ?? '') || a.record.mistake_id.localeCompare(b.record.mistake_id))
  }, [snapshot, all, filters, subjects, searchExtras, mastery, minReviews, maxReviews])
  const chosen = matches.filter(item => selected.has(item.record.mistake_id))
  const nativeAvailable = typeof window.desktop?.printMistakeBook === 'function'
  const filterSummary = [filters.subject || '全部学科', filters.date === 'custom' ? `${filters.from || '不限起始'} 至 ${filters.to || '不限结束'}` : periodLabels[filters.date] || '全部日期', filters.cause && `错因：${filters.cause}`, filters.query && `搜索：${filters.query}`, mastery && masteryLabels[mastery], (minReviews !== '' || maxReviews !== '') && `复习 ${minReviews || '0'}～${maxReviews || '不限'} 次`, `勾选 ${chosen.length} 题`].filter(Boolean).join(' · ')

  useEffect(() => {
    const task = new AbortController();controller.current = task
    const mine = ++generation.current
    setBusy('loading');setError(null);setPreview(null);setNotice('');setSnapshot(null)
    void loadMistakePrintSnapshot(child.child_id, task.signal).then(value => {
      if (task.signal.aborted || mine !== generation.current) return
      const ids = new Set(value.items.map(item => item.record.mistake_id))
      const keepSelection = selectionChild.current === child.child_id
      selectionChild.current = child.child_id
      setSnapshot(value);setSelected(previous => keepSelection ? new Set([...previous].filter(id => ids.has(id))) : ids)
    }).catch(() => {
      if (!task.signal.aborted && mine === generation.current) setError('暂时没有读到打印资料，已保存的错题不受影响。请重试读取。')
    }).finally(() => { if (!task.signal.aborted && mine === generation.current) setBusy(null) })
    return () => { task.abort();controller.current?.abort();generation.current++ }
  }, [child.child_id, reload])

  function invalidate(): void { setPreview(null);setError(null);setNotice('') }
  function select(id: string): void {
    invalidate();setSelected(previous => { const next = new Set(previous);if (next.has(id)) next.delete(id);else next.add(id);return next })
  }
  function selectMatches(include: boolean): void {
    invalidate();setSelected(previous => { const next = new Set(previous);for (const item of matches) { if (include) next.add(item.record.mistake_id);else next.delete(item.record.mistake_id) }return next })
  }
  function updateOption(key: keyof MistakePrintOptions, value: boolean): void { invalidate();setOptions(previous => ({ ...previous, [key]: value })) }
  async function prepare(): Promise<void> {
    if (busy || !snapshot || !chosen.length || invalidDates || invalidCount) return
    const mine = ++generation.current
    const task = new AbortController();controller.current = task
    setBusy('preparing');setPreview(null);setError(null);setNotice('正在整理已选题目…')
    try {
      const crops: Record<string, string> = {}
      const imageItems = options.images ? chosen.filter(item => item.record.source.has_question_crop) : []
      let cropBytes = 0
      for (let index = 0; index < imageItems.length; index++) {
        setNotice(`正在读取原题图片 ${index + 1}/${imageItems.length}…`)
        const item = imageItems[index]
        crops[item.record.mistake_id] = await loadMistakePrintCrop(item, task.signal)
        cropBytes += crops[item.record.mistake_id].length
        if (cropBytes > 28 * 1024 * 1024) throw Error('本次原图较多，请缩小日期范围或分批勾选打印。')
        if (task.signal.aborted || mine !== generation.current) return
      }
      const html = buildMistakePrintDocument({ child, items: chosen, generatedAt: snapshot.generated_at, filterSummary, options, crops })
      if (new TextEncoder().encode(html).length > 31 * 1024 * 1024) throw Error('本次内容较多，请分批勾选打印。')
      if (task.signal.aborted || mine !== generation.current) return
      setPreview(html);setNotice(`已整理 ${chosen.length} 道错题。请检查下方预览，再打印或保存 PDF。`)
    } catch (cause) {
      if (!task.signal.aborted && mine === generation.current) {
        setNotice('');setError(cause instanceof Error && /本次/.test(cause.message) ? cause.message : '有题目或原图未能完整读取，尚未生成打印稿。请重试；如题目已更新，请重新读取资料。')
      }
    } finally { if (!task.signal.aborted && mine === generation.current) setBusy(null) }
  }
  function cancelPrepare(): void { controller.current?.abort();generation.current++;setBusy(null);setNotice('已取消整理，可以继续选择题目。') }
  async function output(action: 'print' | 'pdf'): Promise<void> {
    if (!preview || busy || !nativeAvailable) return
    const mine = generation.current
    setBusy('printing');setError(null);setNotice(action === 'print' ? '正在发送到系统默认打印机…' : '请选择 PDF 保存位置…')
    try {
      const result = await window.desktop!.printMistakeBook!({ html: preview, title: `${child.nickname}的错题本`, action })
      if (mine !== generation.current) return
      if (result.status === 'failed') { setNotice('');setError(result.error || '打印未完成，请检查打印机或保存位置后重试。') }
      else if (result.status === 'unconfirmed') { setNotice('');setError('系统尚未确认收到打印任务，请先检查默认打印机的队列，再决定是否重试。') }
      else setNotice(result.status === 'saved' ? 'PDF 已保存。' : result.status === 'submitted' ? '已提交到默认打印机的队列，请查看出纸情况。' : '已取消，打印预览仍保留。')
    } catch { if (mine === generation.current) { setNotice('');setError('打印未完成，请检查打印机或保存位置后重试。') } }
    finally { if (mine === generation.current) setBusy(null) }
  }

  return <section className="mistake-print-shell" aria-label="错题本打印">
    <header className="mistake-print-header"><button type="button" className="secondary-button" disabled={busy === 'printing'} onClick={onClose}><ArrowLeft size={17}/>{backLabel}</button><div><h1>打印错题本</h1><p>{child.nickname} · {child.grade} · A4 纸张</p></div><button type="button" className="secondary-button" disabled={!!busy} onClick={() => setReload(value => value + 1)}><RefreshCw size={16}/>重新读取资料</button></header>
    <p className="mistake-print-intro">先筛选并勾选要复习的题目，再整理成纸质错题本。错因、掌握情况和复习次数会随题打印。</p>
    {busy === 'loading' && <p role="status">正在读取已保存的错题和复习记录…</p>}
    {error && <div role="alert" className="mistake-print-error">{error}{!snapshot && <button type="button" className="secondary-button" onClick={() => setReload(value => value + 1)}>重试读取</button>}</div>}
    {snapshot && <>
      <fieldset disabled={!!busy} className="mistake-print-controls"><legend>1. 筛选题目</legend>
        <MistakeBookFilters items={all} subjects={subjects} causes={causes} value={filters} onChange={value => { invalidate();setFilters(value) }} onOpen={() => {}} searchExtras={searchExtras}/>
        <p className="mistake-print-help">日期按错题入册时间筛选。</p>
        <div className="mistake-print-extra"><label>掌握情况<select value={mastery} onChange={event => { invalidate();setMastery(event.target.value as PrintMastery | '') }}><option value="">全部掌握情况</option>{Object.entries(masteryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>最少复习次数<input type="number" min="0" step="1" inputMode="numeric" value={minReviews} placeholder="不限" onChange={event => { invalidate();setMinReviews(event.target.value) }}/></label><label>最多复习次数<input type="number" min="0" step="1" inputMode="numeric" value={maxReviews} placeholder="不限" onChange={event => { invalidate();setMaxReviews(event.target.value) }}/></label><button type="button" className="secondary-button" onClick={() => { invalidate();setFilters(emptyBookFilters);setMastery('');setMinReviews('');setMaxReviews('') }}>重置全部筛选</button></div>
        {invalidDates && <p role="alert">起始日期不能晚于结束日期。</p>}{invalidCount && <p role="alert">复习次数请输入非负整数，最少次数不能大于最多次数。</p>}
      </fieldset>
      <fieldset disabled={!!busy} className="mistake-print-controls"><legend>2. 勾选打印范围</legend><div className="mistake-print-selection"><strong aria-live="polite">匹配 {matches.length} 题 · 已选 {chosen.length} 题</strong><button type="button" className="secondary-button" onClick={() => selectMatches(true)} disabled={!matches.length}>全选筛选结果</button><button type="button" className="secondary-button" onClick={() => selectMatches(false)} disabled={!chosen.length}>取消当前选择</button></div>
        <div className="mistake-print-questions">{matches.map(item => <label key={item.record.mistake_id} className="mistake-print-row"><input type="checkbox" aria-label={`选择：${item.record.source.title || item.record.source.question}`} checked={selected.has(item.record.mistake_id)} onChange={() => select(item.record.mistake_id)}/><span><strong>{item.record.source.title || item.record.source.question}</strong><span className="mistake-print-excerpt">{item.record.source.question}</span><small>{item.record.source.subject || '未记录学科'} · {printDate(item.record.source.created_at)} · {item.mastery.label} · 复习作答 {item.review_count} 次</small></span></label>)}
          {!matches.length && <p className="mistake-print-empty">{snapshot.total ? '没有符合条件的错题，请调整筛选条件。' : '还没有保存的错题，可以返回错题本录入。'}</p>}</div>
        <p className="mistake-print-help">只打印当前筛选范围内勾选的题目。复习次数统计已保存的作答，不包含只听讲解。</p>
      </fieldset>
      <fieldset disabled={!!busy} className="mistake-print-controls"><legend>3. 排版选项</legend><div className="mistake-print-options"><label><input type="checkbox" checked={options.images} onChange={event => updateOption('images', event.target.checked)}/>附上已保存的原题裁图</label><label><input type="checkbox" checked={options.originalAnswer} onChange={event => updateOption('originalAnswer', event.target.checked)}/>显示当时作答</label><label><input type="checkbox" checked={options.writingSpace} onChange={event => updateOption('writingSpace', event.target.checked)}/>预留手写订正区</label><label><input type="checkbox" checked={options.onePerPage} onChange={event => updateOption('onePerPage', event.target.checked)}/>每题另起一页</label></div><p className="mistake-print-help">原图可能包含旧作答或批改。默认连续排版；长题会自然续页。打印稿以读取资料时的记录为准。</p></fieldset>
      <div className="mistake-print-actions"><button type="button" className="primary-button" disabled={!!busy || !chosen.length || invalidDates || invalidCount} onClick={() => void prepare()}>{busy === 'preparing' ? '正在整理…' : '生成打印预览'}</button>{busy === 'preparing' && <button type="button" className="secondary-button" onClick={cancelPrepare}>取消整理</button>}<button type="button" className="secondary-button" disabled={!preview || !!busy || !nativeAvailable} onClick={() => void output('print')}><Printer size={17}/>发送到默认打印机</button><button type="button" className="secondary-button" disabled={!preview || !!busy || !nativeAvailable} onClick={() => void output('pdf')}>保存 PDF</button></div>
      {!nativeAvailable && <p role="status">请在桌面客户端打开此页面，以使用系统打印和保存 PDF。</p>}
    </>}
    {notice && <p role="status" className="mistake-print-notice">{notice}</p>}
    {preview && <section className="mistake-print-preview"><h2>打印预览</h2><p>打印时会按 A4 自动分页。需要逐页核对时可以先保存 PDF。</p><iframe title="错题本打印预览" sandbox="" srcDoc={preview}/></section>}
  </section>
}
