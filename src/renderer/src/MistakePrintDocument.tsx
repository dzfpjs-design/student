import { renderToStaticMarkup } from 'react-dom/server'
import type { MistakePrintItem } from './mistakeBookPrintApi'
import type { ChildProfile } from './types'

export type MistakePrintOptions = { images: boolean; originalAnswer: boolean; writingSpace: boolean; onePerPage: boolean }
export const defaultPrintOptions: MistakePrintOptions = { images: true, originalAnswer: false, writingSpace: true, onePerPage: false }
export const printDate = (value?: string | null): string => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleDateString('zh-CN') : '未记录'

const documentCss = `
@page{size:A4 portrait;margin:14mm 15mm}
*{box-sizing:border-box}html{color-scheme:light;background:#fff}body{margin:0;color:#20252d;font:11pt/1.65 "Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif;overflow-wrap:anywhere}
h1{margin:0;font-size:23pt;letter-spacing:1px}h2{font-size:13pt;margin:0 0 2mm}h3{font-size:10pt;margin:0 0 1mm;color:#424a54}p{margin:1mm 0;white-space:pre-wrap}header{border-bottom:2px solid #283f49;padding-bottom:4mm;margin-bottom:6mm}header p{font-size:9pt;color:#505a62}.count{float:right;font-size:11pt;margin-top:4mm}.question{padding:0 0 5mm;margin-bottom:5mm;border-bottom:1px solid #bac2c8}.question-title{break-after:avoid}.question-body{margin:3mm 0;font-size:12pt}.question img{display:block;max-width:100%;width:auto;height:auto;max-height:160mm;object-fit:contain;margin:3mm 0;break-inside:avoid}.meta{display:flex;flex-wrap:wrap;gap:1mm 6mm;color:#505a62;font-size:9pt}.section{margin-top:3mm}.section h3{break-after:avoid}.progress{border-left:3px solid #899da5;padding:2mm 3mm;margin-top:3mm;break-inside:avoid}.progress p{font-size:9pt;color:#505a62}.writing{height:26mm;margin-top:2mm;background:repeating-linear-gradient(#fff 0,#fff 8mm,#d9dee2 8mm,#d9dee2 8.2mm);border-bottom:1px solid #d9dee2}.writing-section{break-inside:avoid}.footnote{font-size:8pt;color:#626b73}.single-page .question+.question{break-before:page}.single-page .question{border-bottom:0}
@media screen{body{max-width:210mm;margin:auto;padding:14mm 15mm;background:white}html{background:#e8ecef}.question{background:#fff}}
@media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
`

export function buildMistakePrintDocument(input: { child: ChildProfile; items: MistakePrintItem[]; generatedAt: string; filterSummary: string; options: MistakePrintOptions; crops: Record<string, string> }): string {
  const { child, items, generatedAt, filterSummary, options, crops } = input
  if (!items.length || items.some(item => item.record.child_id !== child.child_id)) throw Error('没有可打印的当前孩子错题。')
  if (options.images && items.some(item => item.record.source.has_question_crop && !crops[item.record.mistake_id])) throw Error('还有原题图片没有读取完成。')
  return '<!doctype html>' + renderToStaticMarkup(<html lang="zh-CN"><head><meta charSet="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>{`${child.nickname}的错题本`}</title><style>{documentCss}</style></head>
    <body className={options.onePerPage ? 'single-page' : undefined}><header><span className="count">共 {items.length} 题</span><h1>错题复习本</h1><p>{child.nickname} · {child.grade}　整理日期：{printDate(generatedAt)}</p><p>筛选：{filterSummary}</p><p>统计截至 {new Date(generatedAt).toLocaleString('zh-CN')}，复习次数含原题复做、变式作答和强化复测。</p></header>
      <main>{items.map((item, index) => <article className="question" key={item.record.mistake_id}>
        <div className="question-title"><h2>{index + 1}. {item.record.source.title || '错题'}</h2><div className="meta"><span>{item.record.source.subject || '学科未记录'}</span><span>入册：{printDate(item.record.source.created_at)}</span><span>复习作答 {item.review_count} 次</span><span>最近复习：{printDate(item.last_review_at)}</span></div></div>
        <p className="question-body">{item.record.source.question || '请查看原题图片。'}</p>
        {options.images && crops[item.record.mistake_id] && <img src={crops[item.record.mistake_id]} alt={`第 ${index + 1} 题原题裁图`}/>}
        {options.originalAnswer && <section className="section"><h3>当时作答</h3><p>{item.record.source.original_answer || '未记录'}</p></section>}
        <section className="section"><h3>错因与复习重点</h3>{item.record.analysis?.wrong_causes.length ? item.record.analysis.wrong_causes.map((cause, i) => <p key={i}><strong>{cause.label}</strong>{cause.explanation ? `：${cause.explanation}` : ''}</p>) : item.diagnosis ? <p><strong>{item.diagnosis.label}{item.diagnosis.status === 'candidate' ? '（待验证）' : ''}</strong>：{item.diagnosis.explanation}</p> : <p>尚未记录错因，待核对。</p>}
          {(item.record.analysis?.wrong_causes.length || item.diagnosis) ? <p className="footnote">错因是已保存的分析记录，需结合实际作答核对。</p> : null}
          {!!item.record.analysis?.knowledge_points.length && <p>知识点：{item.record.analysis.knowledge_points.map(point => point.label).join('、')}</p>}
        </section>
        <section className="progress"><strong>掌握情况：{item.mastery.label}</strong><p>{item.mastery.detail}</p><p>原题复做 {item.review_counts.original} 次 · 变式作答 {item.review_counts.practice} 次 · 强化与复测 {item.review_counts.formal} 次</p>{item.record.next_review_at && <p>下次复习：{printDate(item.record.next_review_at)}</p>}</section>
        {options.writingSpace && <section className="section writing-section"><h3>我的订正与复习记录</h3><div className="writing"/><p className="footnote">复习日期：____________　这次能独立完成：□ 是　□ 还需帮助</p></section>}
      </article>)}</main>
    </body></html>)
}
