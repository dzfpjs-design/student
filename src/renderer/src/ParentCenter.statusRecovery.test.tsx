import {act,cleanup,fireEvent,render,screen} from '@testing-library/react'
import {StrictMode} from 'react'
import {afterEach,describe,expect,it,vi} from 'vitest'
import {localApi} from './api'
import {ParentCenter} from './ParentCenter'
import {changeParentDraftAuthority} from './parentDraftAuthority'
import type {BootstrapState} from './types'
const model:BootstrapState['model']={provider:'deepseek',model:'public-model',voice_intent_model:'public-model',configured:false,fallback_provider:null,fallback_model:null,fallback_configured:false}
function pending<T>(){let resolve!:(value:T)=>void;let reject!:(e:Error)=>void;const promise=new Promise<T>((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
const component=()=> <ParentCenter children={[]} model={model} onClose={()=>undefined}/>
describe('ParentCenter authoritative status recovery',()=>{
 afterEach(()=>{cleanup();vi.restoreAllMocks();vi.useRealTimers()})
 it('shows truthful loading without suggesting first PIN setup before status arrives',()=>{
  vi.spyOn(localApi,'parentStatus').mockReturnValue(new Promise(()=>{}))
  const unlock=vi.spyOn(localApi,'parentUnlock')
  render(component())
  expect(screen.getByText('正在核对家长身份状态')).toBeInTheDocument()
  expect(screen.queryByText('首次设置家长 PIN')).toBeNull()
  expect(screen.queryByLabelText('家长 PIN')).toBeNull()
  expect(unlock).not.toHaveBeenCalled()
 })
 it('shows retry on status failure without disclosing exception text or presenting setup',async()=>{
  vi.spyOn(localApi,'parentStatus').mockRejectedValue(new Error('PRIVATE_SENTINEL'))
  render(component())
  expect(await screen.findByRole('button',{name:'重试读取家长状态'})).toBeEnabled()
  expect(screen.getByRole('alert')).toHaveTextContent('暂时无法核对家长身份状态')
  expect(screen.queryByText('首次设置家长 PIN')).toBeNull()
  expect(screen.queryByLabelText('家长 PIN')).toBeNull()
  expect(document.body).not.toHaveTextContent('PRIVATE_SENTINEL')
 })
 it('offers setup only after a successful authoritative unconfigured response',async()=>{
  const status=vi.spyOn(localApi,'parentStatus').mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({configured:false,locked_seconds:0})
  render(component());fireEvent.click(await screen.findByRole('button',{name:'重试读取家长状态'}))
  expect(await screen.findByText('首次设置家长 PIN')).toBeInTheDocument()
  expect(status).toHaveBeenCalledTimes(2)
  expect(screen.getByRole('button',{name:'设置并进入'})).toBeDisabled()
 })
 it('bounds an unresolved status read and ignores its late false result after retry',async()=>{
  vi.useFakeTimers();const old=pending<{configured:boolean;locked_seconds:number}>()
  const status=vi.spyOn(localApi,'parentStatus').mockReturnValueOnce(old.promise).mockResolvedValueOnce({configured:true,locked_seconds:0})
  render(component())
  await act(async()=>{await vi.advanceTimersByTimeAsync(10001)})
  fireEvent.click(screen.getByRole('button',{name:'重试读取家长状态'}))
  await act(async()=>{await Promise.resolve()})
  expect(screen.getByText('输入家长 PIN')).toBeInTheDocument()
  await act(async()=>{old.resolve({configured:false,locked_seconds:0})})
  expect(screen.getByText('输入家长 PIN')).toBeInTheDocument()
  expect(screen.queryByText('首次设置家长 PIN')).toBeNull();expect(status).toHaveBeenCalledTimes(2)
 })
 it('does not let a status response overwrite a newer parent authority lock',async()=>{
  const old=pending<{configured:boolean;locked_seconds:number}>();vi.spyOn(localApi,'parentStatus').mockReturnValue(old.promise)
  render(component());act(()=>changeParentDraftAuthority('locked'))
  await act(async()=>{old.resolve({configured:false,locked_seconds:0})})
  expect(screen.getByText('输入家长 PIN')).toBeInTheDocument()
  expect(screen.queryByText('首次设置家长 PIN')).toBeNull()
 })
 it('keeps the newest status after actual StrictMode effect replay',async()=>{
  const old=pending<{configured:boolean;locked_seconds:number}>()
  const status=vi.spyOn(localApi,'parentStatus').mockReturnValueOnce(old.promise).mockResolvedValueOnce({configured:true,locked_seconds:0})
  render(<StrictMode>{component()}</StrictMode>)
  expect(await screen.findByText('输入家长 PIN')).toBeInTheDocument()
  await act(async()=>{old.resolve({configured:false,locked_seconds:0})})
  expect(screen.getByText('输入家长 PIN')).toBeInTheDocument();expect(status).toHaveBeenCalledTimes(2)
 })
})
