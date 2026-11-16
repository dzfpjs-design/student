import type {QwenNativeRuntimeBridge} from './qwenNativeProtocol'

/** Keep one transport adapter while selecting the latest UI bridge
 * for each new operation. An in-flight operation retains its selected bridge
 * and original arguments; it is never retried or rebound after a render.
 */
export function currentNativeRuntime(
  latest:()=>QwenNativeRuntimeBridge|undefined,
  current:()=>boolean,
):QwenNativeRuntimeBridge {
  const invoke=async<T>(operation:(bridge:QwenNativeRuntimeBridge)=>Promise<T>):Promise<T>=>{
    if(!current())throw Error('QWEN_NATIVE_CONTEXT_OWNER_CHANGED')
    const bridge=latest()
    if(!bridge)throw Error('QWEN_NATIVE_RUNTIME_UNAVAILABLE')
    const result=await operation(bridge)
    if(!current())throw Error('QWEN_NATIVE_CONTEXT_OWNER_CHANGED')
    return result
  }
  const household:NonNullable<QwenNativeRuntimeBridge['admitHousehold']>=(entry,host)=>invoke(bridge=>{
    if(!bridge.admitHousehold)throw Error('QWEN_NATIVE_RUNTIME_UNAVAILABLE')
    return bridge.admitHousehold(entry,host)
  })
  return {
    loadCatalog:host=>invoke(bridge=>bridge.loadCatalog(host)),
    loadContext:host=>invoke(bridge=>bridge.loadContext(host)),
    admit:(utterance,capture,host)=>invoke(bridge=>bridge.admit(utterance,capture,host)),
    propose:proposal=>invoke(bridge=>bridge.propose(proposal)),
    recordDialogue:dialogue=>invoke(bridge=>bridge.recordDialogue(dialogue)),
    readReceipt:identity=>invoke(bridge=>bridge.readReceipt(identity)),
    get admitHousehold(){return current()&&latest()?.admitHousehold?household:undefined},
  }
}
