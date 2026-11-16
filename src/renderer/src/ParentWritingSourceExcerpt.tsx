import { CurriculumOriginal } from './OralTaskPreview'

export function ParentWritingSourceExcerpt({text}:{text:string}):React.JSX.Element {
  let value:unknown
  try { value=JSON.parse(text) } catch { return <CurriculumOriginal text={text}/> }
  const source=value&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null
  if(!source||source.schema!=='pet_writing_source_v1'||typeof source.prompt!=='string'||!source.prompt.trim()
    ||!Array.isArray(source.task_points)||!source.task_points.length
    ||!source.task_points.every(p=>typeof p==='string'&&p.trim()))return <CurriculumOriginal text={text}/>
  const range=typeof source.min_words==='number'&&Number.isInteger(source.min_words)&&source.min_words>=0
    &&typeof source.max_words==='number'&&Number.isInteger(source.max_words)&&source.max_words>=source.min_words
    ?`${source.min_words}–${source.max_words}`:null
  return <div className="parent-writing-source-excerpt">
    <blockquote className="csr-evidence">{source.prompt}</blockquote>
    <h4>原题任务点</h4><ol>{source.task_points.map((point,index)=><li key={index}>{point}</li>)}</ol>
    {range&&<p>建议字数：{range}</p>}
    <details><summary>查看原始资料记录</summary><blockquote className="csr-evidence">{text}</blockquote></details>
  </div>
}
