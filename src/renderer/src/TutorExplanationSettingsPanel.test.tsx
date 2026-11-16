import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TutorExplanationSettingsPanel } from './TutorExplanationSettingsPanel'
const port=vi.hoisted(()=>vi.fn())
vi.mock('./api',()=>({requestTutorExplanationSettings:port}))
const legacy={configured:false,keyConfigured:true,requiresEndpoint:true,baseUrl:'',model:'deepseek-v4.1-flash'}
beforeEach(()=>{port.mockReset();port.mockResolvedValueOnce(legacy)})
afterEach(()=>cleanup())
describe('parent tutor endpoint settings',()=>{
 it('preserves the existing key and requires explicit disclosure confirmation',async()=>{
  render(<TutorExplanationSettingsPanel />)
  expect(await screen.findByText('已保留原来的密钥，请补充服务地址后启用。')).toBeInTheDocument()
  const save=screen.getByRole('button',{name:'保存讲题服务'});expect(save).toBeDisabled()
  fireEvent.change(screen.getByLabelText('讲题服务地址'),{target:{value:'https://tutor.example.test/v1'}})
  fireEvent.click(screen.getByRole('checkbox'));expect(save).toBeEnabled()
  port.mockResolvedValueOnce({...legacy,configured:true,requiresEndpoint:false,baseUrl:'https://tutor.example.test/v1',runtimeApplied:true})
  fireEvent.click(save)
  await waitFor(()=>expect(port).toHaveBeenCalledWith({action:'save',baseUrl:'https://tutor.example.test/v1',model:'deepseek-v4.1-flash',apiKey:'',confirmed:true}))
  expect(await screen.findByText('讲题服务配置已保存。')).toBeInTheDocument()
 })
 it('clears typed credentials after save and makes restart uncertainty visible',async()=>{
  render(<TutorExplanationSettingsPanel />);await screen.findByText('已保留原来的密钥，请补充服务地址后启用。')
  fireEvent.change(screen.getByLabelText('讲题服务地址'),{target:{value:'https://tutor.example.test/v1'}})
  fireEvent.change(screen.getByLabelText('讲题模型密钥'),{target:{value:'synthetic-replacement-key'}})
  fireEvent.click(screen.getByRole('checkbox'))
  port.mockResolvedValueOnce({...legacy,configured:true,baseUrl:'https://tutor.example.test/v1',runtimeApplied:false})
  fireEvent.click(screen.getByRole('button',{name:'保存讲题服务'}))
  expect(await screen.findByText(/旧服务未确认停止，已有请求可能仍在进行/)).toBeInTheDocument()
  expect(screen.getByLabelText('讲题模型密钥')).toHaveValue('')
 })
 it('does not silently retry after an uncertain result',async()=>{
  render(<TutorExplanationSettingsPanel />);await screen.findByText('已保留原来的密钥，请补充服务地址后启用。')
  fireEvent.change(screen.getByLabelText('讲题服务地址'),{target:{value:'https://tutor.example.test/v1'}});fireEvent.click(screen.getByRole('checkbox'))
  port.mockRejectedValueOnce(Error('unknown'));fireEvent.click(screen.getByRole('button',{name:'保存讲题服务'}))
  expect(await screen.findByText(/配置未能确认/)).toBeInTheDocument();expect(port).toHaveBeenCalledTimes(2)
 })
})

it('explains safe manual recovery when a stale recovery marker blocks settings',async()=>{
 port.mockReset();port.mockRejectedValueOnce(Error('TUTOR_EXPLANATION_RECOVERY_MARKER_BLOCKED'))
 render(<TutorExplanationSettingsPanel />)
 expect(await screen.findByText(/仅删除 tutor-explanation-settings.lock.recovery 空目录/)).toBeInTheDocument()
 expect(screen.getByText(/请保留其他文件/)).toBeInTheDocument()
})
