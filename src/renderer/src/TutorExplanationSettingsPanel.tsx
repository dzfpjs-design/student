import { useEffect, useState } from 'react'
import { requestTutorExplanationSettings } from './api'
import type { TutorExplanationSummary } from '../../shared/tutorExplanation'

/** Rendered only inside parent center; the native host independently verifies parent authority. */
export function TutorExplanationSettingsPanel(): React.JSX.Element {
  const [summary,setSummary]=useState<TutorExplanationSummary|null>(null)
  const [baseUrl,setBaseUrl]=useState(''),[model,setModel]=useState('deepseek-v4.1-flash'),[apiKey,setApiKey]=useState('')
  const [confirmed,setConfirmed]=useState(false),[busy,setBusy]=useState(true),[message,setMessage]=useState('')
  useEffect(()=>{ let active=true
    void requestTutorExplanationSettings({action:'get'}).then(value=>{if(active){setSummary(value);setBaseUrl(value.baseUrl);setModel(value.model)}})
      .catch(error=>{if(active)setMessage(error instanceof Error && error.message.includes('TUTOR_EXPLANATION_RECOVERY_MARKER_BLOCKED') ? '讲题配置恢复正在占用或上次恢复未完成。请关闭所有学习伙伴和相关后台进程，在本机数据目录中仅删除 tutor-explanation-settings.lock.recovery 空目录，再重启；请保留其他文件，尤其是 .pending 文件。' : '未能读取讲题服务配置，请重新解锁家长中心后重试。')}).finally(()=>{if(active)setBusy(false)})
    return()=>{active=false}
  },[])
  const save=async():Promise<void>=>{
    if (!confirmed || !baseUrl.trim() || !model.trim()) { setMessage('请填写服务地址和模型名，并确认题目会发送到该服务。'); return }
    setBusy(true);setMessage('')
    try {
      const result=await requestTutorExplanationSettings({action:'save',baseUrl:baseUrl.trim(),model:model.trim(),apiKey,confirmed:true})
      setApiKey('');setConfirmed(false);setSummary(result)
      setMessage(result.runtimeApplied ? '讲题服务配置已保存。' : '配置已保存，但旧服务未确认停止，已有请求可能仍在进行；请恢复并核对本地服务后再继续。')
    } catch (error) { setMessage(error instanceof Error && error.message.includes('TUTOR_EXPLANATION_RECOVERY_MARKER_BLOCKED')
      ? '讲题配置恢复正在占用或上次恢复未完成。请关闭所有学习伙伴和相关后台进程，在本机数据目录中仅删除 tutor-explanation-settings.lock.recovery 空目录，再重启；请保留其他文件，尤其是 .pending 文件。'
      : '配置未能确认。若服务刚刚重启，请重新解锁家长中心并查看已保存配置，再决定是否重试。') }
    finally { setBusy(false) }
  }
  return <section className="tutor-explanation-settings" aria-labelledby="tutor-explanation-heading">
    <h3 id="tutor-explanation-heading">讲题与错题分析模型</h3>
    <p>题目和必要的学习上下文会发送到你确认的服务地址。</p>
    {summary?.requiresEndpoint && <p role="status">已保留原来的密钥，请补充服务地址后启用。</p>}
    {summary && !summary.keyConfigured && <p>尚未保存讲题模型密钥。</p>}
    <label>服务地址<input aria-label="讲题服务地址" type="url" autoComplete="off" value={baseUrl} onChange={event=>{setBaseUrl(event.target.value);setConfirmed(false)}} disabled={busy} placeholder="https://…/v1" /></label>
    <label>模型名称<input aria-label="讲题模型名称" value={model} onChange={event=>{setModel(event.target.value);setConfirmed(false)}} disabled={busy} /></label>
    <label>模型密钥<input aria-label="讲题模型密钥" type="password" autoComplete="new-password" value={apiKey} onChange={event=>setApiKey(event.target.value)} disabled={busy} placeholder={summary?.keyConfigured?'已保存；留空保留':'输入模型密钥'} /></label>
    <label className="tutor-explanation-confirm"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)} disabled={busy} />我确认使用上述服务处理题目和必要的学习上下文。</label>
    <button type="button" className="primary-button" disabled={busy || !confirmed} onClick={()=>void save()}>保存讲题服务</button>
    {message && <p role="status">{message}</p>}
  </section>
}
