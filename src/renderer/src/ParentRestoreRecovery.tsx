import {useEffect,useRef,useState} from 'react'
import {ApiError,localApi} from './api'
import type {PrivacyOperationReceipt} from './privacyOperationRecovery'

export type ParentRestoreStatus={schema:1;state:'UNAPPLIED'|'ABORTED_NOT_APPLIED'|'DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS'|'COMPLETE'|'RESOLVED_COMMITTED'|'RESOLVED_NOT_APPLIED'|'UNKNOWN';request_id:string;kind:'restore'|'portable_restore';normal_business_ready:false;restart_required:boolean}
const states=new Set(['UNAPPLIED','ABORTED_NOT_APPLIED','DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS','COMPLETE','RESOLVED_COMMITTED','RESOLVED_NOT_APPLIED'])
function statusValid(value:unknown):value is ParentRestoreStatus{
  if(!value||typeof value!=='object'||Array.isArray(value))return false
  const v=value as ParentRestoreStatus
  return v.schema===1&&states.has(v.state)&&(v.kind==='restore'||v.kind==='portable_restore')&&v.normal_business_ready===false
    &&typeof v.restart_required==='boolean'&&typeof v.request_id==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(v.request_id)
}
function verifiedReceipt(value:unknown,requestId:string,kind:ParentRestoreStatus['kind']):value is PrivacyOperationReceipt{
  if(!value||typeof value!=='object'||Array.isArray(value))return false
  const v=value as PrivacyOperationReceipt
  if(v.request_id!==requestId||v.kind!==kind||v.child_id!==null||v.scope!==null||v.local_only!==true||v.cloud_transmission_allowed!==false
    ||typeof v.origin_generation!=='string'||!v.origin_generation||typeof v.current_generation!=='string'||!v.current_generation
    ||typeof v.cleanup_manifest_sha256!=='string'||!/^[a-f0-9]{64}$/.test(v.cleanup_manifest_sha256))return false
  return v.status==='committed'&&v.cleanup_allowed===true&&v.result_generation===v.current_generation&&v.result_generation!==v.origin_generation
    &&v.reference?.type==='audit_event'&&typeof v.reference.id==='string'&&!!v.reference.id
    ||v.status==='failed'&&v.cleanup_allowed===false&&v.result_generation===null&&v.current_generation===v.origin_generation&&v.reference===null
}
const message=(error:unknown,fallback:string):string=>error instanceof ApiError?error.message:fallback

export function ParentRestoreRecovery({onRestarted}:{onRestarted:()=>Promise<void>}):React.JSX.Element{
  const [phase,setPhase]=useState<'loading'|'unlock'|'review'|'verified'|'restarting'>('loading')
  const [restartingToReview,setRestartingToReview]=useState(false)
  const [pin,setPin]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState<string|null>(null)
  const [status,setStatus]=useState<ParentRestoreStatus|null>(null),[receipt,setReceipt]=useState<PrivacyOperationReceipt|null>(null)
  const generation=useRef(0),mounted=useRef(true)
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;generation.current++}},[])
  const current=(value:number):boolean=>mounted.current&&value===generation.current
  const initial=async():Promise<void>=>{
    const token=++generation.current;setBusy(true);setError(null)
    try{const value=await localApi.parentStatus();if(!current(token))return
      if(!value.configured)throw Error('ORIGINAL_PARENT_PIN_REQUIRED')
      setPhase('unlock')
    }catch(cause){if(current(token))setError(message(cause,'暂时无法读取原家长解锁状态。请重试读取，原恢复不会重新执行。'))}
    finally{if(current(token))setBusy(false)}
  }
  useEffect(()=>{void initial()},[])
  const unlock=async():Promise<void>=>{
    if(busy||!/^\d{4,8}$/.test(pin))return
    const token=++generation.current,value=pin;setPin('');setBusy(true);setError(null)
    try{await localApi.parentUnlock(value,false);const original=await localApi.parentRestoreRecoveryStatus();if(!current(token))return
      if(!statusValid(original))throw Error('ORIGINAL_RECOVERY_NOT_VERIFIED')
      setStatus(original);setPhase('review')
    }catch(cause){if(current(token))setError(message(cause,'原恢复状态尚未核对清楚。请重新解锁核对；不会重新恢复备份。'))}
    finally{if(current(token))setBusy(false)}
  }
  const resume=async():Promise<void>=>{
    if(busy||!status)return
    const token=++generation.current;setBusy(true);setError(null)
    try{
      if(!window.desktop?.recoverNativePrivacy||!window.desktop?.restartLocalService)throw Error('ORIGINAL_DESKTOP_REQUIRED')
      const fresh=await localApi.parentRestoreRecoveryStatus()
      if(!current(token))return
      if(!statusValid(fresh)||fresh.request_id!==status.request_id||fresh.kind!==status.kind)throw Error('ORIGINAL_RECOVERY_CHANGED')
      // Main reads its original durable activePrivacy and uses the same nonce.
      // No restore, new operation, new permit or renderer draft erase is issued.
      const result=await localApi.recoverNativePrivacyOperation(status.request_id)
      if(!current(token))return
      if(!verifiedReceipt(result,status.request_id,status.kind))throw Error('ORIGINAL_RECEIPT_NOT_VERIFIED')
      setReceipt(result);setPhase('verified')
    }catch(cause){if(current(token))setError(message(cause,'原操作或本机清理尚未核对完成。进度已保留，请重试核对原操作。'))}
    finally{if(current(token))setBusy(false)}
  }
  const restart=async():Promise<void>=>{
    if(busy||!receipt)return
    const token=++generation.current;setBusy(true);setPhase('restarting');setError(null)
    try{await localApi.restartRuntime();if(current(token))await onRestarted()}
    catch{if(current(token)){setPhase('verified');setError('原操作核对已经完成，但本地服务尚未重启。请重试重启，不要重新恢复备份。')}}
    finally{if(current(token))setBusy(false)}
  }
  const restartToReview=async():Promise<void>=>{
    if(busy||!error||phase!=='review')return
    const token=++generation.current;setBusy(true);setRestartingToReview(true);setError(null)
    try{
      if(!window.desktop?.restartLocalService)throw Error('ORIGINAL_DESKTOP_REQUIRED')
      // No receipt/erase/finish is inferred. Main retains its original durable
      // activePrivacy and draft barrier while the old backend really exits.
      await localApi.restartRuntime();if(current(token))await onRestarted()
    }catch{if(current(token))setError('本地服务尚未重启，原恢复仍待核对。请重试重启服务，再核对原操作；不会重新恢复备份。')}
    finally{if(current(token)){setBusy(false);setRestartingToReview(false)}}
  }
  const written=status&&['DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS','COMPLETE','RESOLVED_COMMITTED'].includes(status.state)
  return <main className="app-shell parent-recovery" aria-label="家长恢复核对"><section className="panel">
    <h1>核对上次备份恢复</h1>
    <p>学习功能保持暂停。这里只核对原操作，不会再次恢复备份。</p>
    {phase==='loading'&&<><p role="status">{busy?'正在读取家长解锁状态…':'需要先读取原家长解锁状态。'}</p>{!busy&&<button className="secondary-button" onClick={()=>void initial()}>重试读取解锁状态</button>}</>}
    {phase==='unlock'&&<form onSubmit={event=>{event.preventDefault();void unlock()}}>
      <p>请使用原家长 PIN 解锁；不会创建新的家长身份。</p>
      <label>家长 PIN<input type="password" inputMode="numeric" autoComplete="off" maxLength={8} value={pin} onChange={event=>setPin(event.target.value)} disabled={busy}/></label>
      <button type="submit" className="primary-button" disabled={busy||!/^\d{4,8}$/.test(pin)}>{busy?'正在解锁核对…':'解锁并读取原恢复'}</button>
    </form>}
    {phase==='review'&&status&&<>
      <h2>{written?'备份已写入，等待完成收尾':'原恢复尚未应用'}</h2>
      <p>{written?'将核对原恢复记录，并完成原操作的本机清理。':'核对后将结束这次未应用的请求，原数据不会被替换。以后如需恢复，请另行发起新的恢复操作。'}</p>
      <div className="parent-recovery-actions">
        <button className="primary-button" onClick={()=>void resume()} disabled={busy}>{restartingToReview?'正在重启服务…':busy?'正在核对原操作…':'核对原恢复并继续'}</button>
        <button className="secondary-button" onClick={()=>{++generation.current;setPhase('unlock');setError(null)}} disabled={busy}>重新解锁核对</button>
      </div>
      {error&&<><p>若旧服务无法继续核对，可先重启服务。原操作仍会保留，重启后需要再次解锁核对。</p><button className="secondary-button" onClick={()=>void restartToReview()} disabled={busy}>重启服务后核对原恢复</button></>}
    </>}
    {(phase==='verified'||phase==='restarting')&&receipt&&<>
      <h2>{receipt.status==='committed'?'已恢复，原操作核对完成':'原恢复未应用，原数据未替换'}</h2>
      <p>原操作的本机核对已完成。重启本地学习服务后再继续使用。</p>
      <button className="primary-button" onClick={()=>void restart()} disabled={busy}>{phase==='restarting'?'正在重启本地服务…':error?'重试重启本地服务':'重启本地服务并继续'}</button>
    </>}
    {error&&<p role="alert">{error}</p>}
  </section></main>
}
