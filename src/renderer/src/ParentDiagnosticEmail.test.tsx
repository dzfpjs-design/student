import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ParentDiagnosticEmail } from './ParentDiagnosticEmail'
import { localApi } from './api'

beforeEach(() => vi.restoreAllMocks())
afterEach(() => cleanup())
it('shows authorization and persisted sending state separately from learning reports', async () => {
  const load = vi.spyOn(localApi, 'pilotDiagnosticStatus')
    .mockResolvedValueOnce({recipient:'guardian@example.test',consent:'not_authorized',scheduler_enabled:true,blocked_children:[],latest:null})
    .mockResolvedValueOnce({recipient:'guardian@example.test',consent:'consented',scheduler_enabled:true,blocked_children:[],latest:{date:'2026-11-02',status:'sent'}})
  const save = vi.spyOn(localApi, 'setPilotDiagnosticConsent').mockResolvedValue({recipient:'guardian@example.test',status:'consented'})
  render(<ParentDiagnosticEmail />)
  fireEvent.click(await screen.findByRole('button',{name:'授权发送试用摘要和运行日志'}))
  await waitFor(() => expect(save).toHaveBeenCalledWith('guardian@example.test',true))
  expect(await screen.findByText(/邮件服务器已接收/)).toBeTruthy()
  expect(screen.getByRole('button',{name:'停止发送运行日志'})).toBeTruthy()
  expect(load).toHaveBeenCalledTimes(2)
})
it('explains a child-level revocation instead of offering a misleading enable action', async () => {
  vi.spyOn(localApi, 'pilotDiagnosticStatus').mockResolvedValue({recipient:'guardian@example.test',consent:'revoked',scheduler_enabled:true,blocked_children:['小明'],latest:null})
  render(<ParentDiagnosticEmail />)
  expect(await screen.findByRole('alert')).toHaveTextContent('小明')
  expect(screen.queryByRole('button',{name:'授权发送试用摘要和运行日志'})).toBeNull()
})
it('does not offer a false authorization action when no recipient is configured', async () => {
  vi.spyOn(localApi, 'pilotDiagnosticStatus').mockResolvedValue({recipient:'',consent:'not_authorized',scheduler_enabled:false,blocked_children:[],latest:null})
  render(<ParentDiagnosticEmail />)
  expect(await screen.findByRole('alert')).toHaveTextContent('运行日志邮件已暂停')
  expect(screen.queryByRole('button',{name:'授权发送试用摘要和运行日志'})).toBeNull()
})

it('requires matching typed addresses, retains them on failure, and keeps authorization separate', async () => {
  vi.spyOn(localApi, 'pilotDiagnosticStatus')
    .mockResolvedValueOnce({recipient:'',consent:'not_authorized',scheduler_enabled:false,blocked_children:[],latest:null,recipient_editable:true})
    .mockResolvedValueOnce({recipient:'guardian@example.test',consent:'revoked',scheduler_enabled:true,blocked_children:[],latest:null,recipient_editable:true})
  const save = vi.spyOn(localApi, 'setPilotDiagnosticRecipient')
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce({recipient:'guardian@example.test'})
  const authorize = vi.spyOn(localApi, 'setPilotDiagnosticConsent').mockResolvedValue({recipient:'guardian@example.test',status:'consented'})
  render(<ParentDiagnosticEmail />)
  fireEvent.click(await screen.findByRole('button', {name:'设置收件邮箱'}))
  fireEvent.change(screen.getByLabelText('收件邮箱'), {target:{value:'guardian@example.test'}})
  fireEvent.change(screen.getByLabelText('再次输入邮箱'), {target:{value:'wrong@example.test'}})
  fireEvent.click(screen.getByRole('button', {name:'确认保存邮箱'}))
  expect(await screen.findByText('两次输入的邮箱不一致，请检查。')).toBeTruthy()
  expect(save).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('再次输入邮箱'), {target:{value:'guardian@example.test'}})
  fireEvent.click(screen.getByRole('button', {name:'确认保存邮箱'}))
  expect(await screen.findByText('邮箱未能保存，请检查家长身份和本机服务后重试。')).toBeTruthy()
  expect(screen.getByLabelText('收件邮箱')).toHaveValue('guardian@example.test')
  fireEvent.click(screen.getByRole('button', {name:'确认保存邮箱'}))
  expect(await screen.findByRole('button',{name:'授权发送试用摘要和运行日志'})).toBeTruthy()
  expect(authorize).not.toHaveBeenCalled()
  expect(save).toHaveBeenCalledTimes(2)
})

it('explains a conflicting externally managed address and disables authorization', async () => {
  vi.spyOn(localApi, 'pilotDiagnosticStatus').mockResolvedValue({recipient:'external@example.test',consent:'not_authorized',scheduler_enabled:false,blocked_children:[],latest:null,recipient_editable:false,recipient_configuration_conflict:true})
  render(<ParentDiagnosticEmail />)
  expect(await screen.findByRole('alert')).toHaveTextContent('配置与保存的日志邮箱不一致')
  expect(screen.queryByRole('button',{name:'授权发送试用摘要和运行日志'})).toBeNull()
  expect(screen.queryByRole('button',{name:'更换收件邮箱'})).toBeNull()
})
