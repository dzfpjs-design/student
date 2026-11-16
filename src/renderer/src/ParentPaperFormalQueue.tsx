import {useCallback,useLayoutEffect,useRef,useState} from 'react'
import {ApiError} from './api'
import {CurriculumSupplyReviewPanel,type CurriculumSupplyReviewPanelProps} from './CurriculumSupplyReviewPanel'
import {parentDraftAuthorityEvent,parentDraftAuthorityVersion} from './parentDraftAuthority'

type Props=Omit<CurriculumSupplyReviewPanelProps,'isCurrentAuthority'>
const waitMs=10_000
type WaitOwner={generation:number;valid:boolean}
/** Only this embedded review journey gets bounded transport. The original panel
 * owns its durable command/action/version and explicit retry state unchanged. */
export function ParentPaperFormalQueue(props:Props):React.JSX.Element{
  return <ScopedQueue key={props.child.child_id} {...props}/>
}
function ScopedQueue({request:baseRequest,...props}:Props):React.JSX.Element{
  const source=useRef(baseRequest);source.current=baseRequest
  const mounted=useRef(false),revoked=useRef(false),authority=useRef(parentDraftAuthorityVersion()),generation=useRef(0)
  const requests=useRef(new Set<AbortController>())
  const [owner,setOwner]=useState<WaitOwner|null>(null)
  const [count,setCount]=useState(0),[notice,setNotice]=useState(''),[closed,setClosed]=useState(false)
  // Each setup owns a predicate that can only become false. StrictMode replay
  // creates a different keyed Panel; it never reauthorizes an old continuation.
  const current=useCallback(()=>owner!==null&&owner.valid&&owner.generation===generation.current&&mounted.current&&!revoked.current&&parentDraftAuthorityVersion()===authority.current,[owner])
  useLayoutEffect(()=>{
    const lifetime:WaitOwner={generation:++generation.current,valid:true}
    mounted.current=true;setOwner(lifetime)
    const invalidate=():void=>{
      lifetime.valid=false;revoked.current=true
      for(const controller of [...requests.current])controller.abort()
      setClosed(true)
    }
    window.addEventListener(parentDraftAuthorityEvent,invalidate)
    return()=>{
      lifetime.valid=false;generation.current++;mounted.current=false
      window.removeEventListener(parentDraftAuthorityEvent,invalidate)
      for(const controller of [...requests.current])controller.abort()
    }
  },[])
  const request=useCallback((path:string,init?:RequestInit):Promise<unknown>=>{
    if(!current())return Promise.reject(new ApiError('家长权限已变化，原审核操作仍保留，请重新解锁核对。',401,'PARENT_SESSION_REQUIRED'))
    const controller=new AbortController(),callerSignal=init?.signal
    requests.current.add(controller);setCount(requests.current.size);setNotice('')
    return new Promise((resolve,reject)=>{
      let settled=false
      const callerAbort=():void=>controller.abort()
      const finish=(success:boolean,value:unknown):void=>{
        if(settled)return
        settled=true;clearTimeout(timer);controller.signal.removeEventListener('abort',cancelled)
        callerSignal?.removeEventListener('abort',callerAbort);requests.current.delete(controller)
        if(mounted.current)setCount(requests.current.size)
        if(success&&current()&&!controller.signal.aborted)resolve(value)
        else reject(success?new ApiError('家长权限已变化，原审核结果仍待核对。',401,'PARENT_SESSION_REQUIRED'):value)
      }
      const cancelled=():void=>finish(false,new ApiError('已停止等待，审核结果仍待核对。原操作保留，请刷新记录或重试原请求。',undefined,'PAPER_REVIEW_WAIT_UNKNOWN'))
      const timer=setTimeout(()=>{
        if(current())setNotice('等待审核回执已超时，结果仍未确认。原操作保留；请核对已保存记录或重试原请求，不会另建发布。')
        finish(false,new ApiError('等待审核回执超时，原操作结果仍需核对。',undefined,'PAPER_REVIEW_WAIT_UNKNOWN'))
        controller.abort()
      },waitMs)
      controller.signal.addEventListener('abort',cancelled,{once:true})
      callerSignal?.addEventListener('abort',callerAbort,{once:true})
      if(callerSignal?.aborted){controller.abort();return}
      try{Promise.resolve(source.current(path,{...init,signal:controller.signal})).then(value=>finish(true,value),error=>finish(false,error))}
      catch(error){finish(false,error)}
    })
  },[current])
  function stop():void{
    for(const controller of [...requests.current])controller.abort()
    setNotice('已停止等待本面板的审核回执。原操作没有删除，结果仍未确认；请核对记录或重试原请求。')
  }
  if(closed)return <section role="status">家长权限已变化。本机待核对审核操作保留，请重新解锁后明确恢复。</section>
  if(!owner)return <section role="status">正在准备正式审核的本机等待状态…</section>
  return <section aria-label="试卷正式审核等待状态">
    {count>0&&<div role="status">正在核对正式审核的本机请求…<button className="secondary-button" type="button" onClick={stop}>停止等待审核，保留原操作</button></div>}
    {notice&&<p role="status" aria-live="polite">{notice}</p>}
    <CurriculumSupplyReviewPanel key={owner.generation} {...props} request={request} isCurrentAuthority={current}/>
  </section>
}
