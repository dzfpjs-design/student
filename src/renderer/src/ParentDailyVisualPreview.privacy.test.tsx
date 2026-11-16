import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ParentDailyVisualPreview } from './ParentDailyVisualPreview'
import { PrivacyOperations } from './ParentCenter'
import { changeParentDraftAuthority } from './parentDraftAuthority'
import * as api from './api'
import type { PrivacyPreferences, SystemHealth } from './types'

const preferences = { cloud_model_text_allowed:true, cloud_audio_allowed:false, raw_media_storage_allowed:false, model_improvement_allowed:false, model_call_retention_days:90, tutor_turn_retention_days:180, report_retention_days:730, updated_at:'2026-01-01T00:00:00Z' } as PrivacyPreferences
const health = { database_integrity:'ok', database_size_bytes:0, disk_free_bytes:100, pending_report_jobs:0, pending_agent_turns:0, model_failures_last_7_days:0, active_sensor_leases:0, last_backup:null, maintenance:{daily_backup:null,retention_cleanup:null}, pending_deletion_jobs:0, recent_maintenance_failures:[], raw_media_files_stored:0, devices:[], device_incidents:[], recent_model_failures:[], recent_report_failures:[], recent_agent_failures:[], recent_deletion_failures:[] } as SystemHealth
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.localStorage.clear() })
function preview(day: string) { return { child_id:'a',learning_date:day,local_only:true,is_formal_report:false,document_observation:{pages:[{text:'孩子页面原文',observations:[{revision_id:'r',confidence:.9,review_required:false}]}]},page_observation_time:{status:'no_segments',pages:[]} } }
function setup() {
  vi.spyOn(api.localApi,'listBackups').mockResolvedValue([])
  vi.spyOn(api.localApi,'captureArtifacts').mockResolvedValue([{artifact_id:'art',artifact_type:'document_ocr',created_at:'2026-11-04T00:00:00Z',extracted_text_chars:5,raw_file_stored:false}] as never)
  vi.spyOn(api.localApi,'systemHealth').mockResolvedValue(health)
  return render(<><ParentDailyVisualPreview childId="a" /><PrivacyOperations childId="a" initial={preferences} health={health} onHealthChange={() => undefined} /></>)
}

it.each(['shown','pending'])('real delete action clears %s data and refuses its late response', async mode => {
  let finishRead!: (value: unknown) => void
  const request=vi.spyOn(api,'requestParentLocal').mockImplementation(path => path.includes('/deletion-receipt/') ? Promise.resolve({verified:true,child_id:'a',artifact_id:'art',action_id:path.split('/').pop()}) as never : path.endsWith('/session') ? Promise.resolve({expires_at:new Date(Date.now()+60000).toISOString()}) as never : mode==='pending' ? new Promise(done => { finishRead=done }) as never : Promise.resolve(preview(path.split('/').pop()!)) as never)
  let finishDelete!: (value: unknown) => void
  const deleted=vi.spyOn(api.localApi,'deleteCaptureArtifact').mockImplementation(() => new Promise(done => { finishDelete=done }) as never)
  setup()
  await screen.findByRole('button',{name:'删除此条'})
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  const day=(screen.getByLabelText('学习记录日期') as HTMLInputElement).value
  if(mode==='shown') expect(await screen.findByText('孩子页面原文')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('删除确认'),{target:{value:'确认删除'}})
  fireEvent.click(screen.getByRole('button',{name:'删除此条'}))
  await waitFor(()=>expect(deleted).toHaveBeenCalled())
  expect(screen.queryByText('孩子页面原文')).toBeNull()
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  if(mode==='pending') await act(async()=>{finishRead(preview(day))})
  expect(screen.queryByText('孩子页面原文')).toBeNull()
  await act(async()=>{finishDelete({status:'completed',database_reference_deleted:true,raw_file_deleted:false,recovery_rotation:{status:'completed',invalidated_backup_count:0,removed_backup_bundle_files:0,removed_upgrade_snapshot_files:0,replacement_backup_id:'x'}})})
  await waitFor(()=>expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(false))
  expect(screen.queryByText('孩子页面原文')).toBeNull()
  expect(request).toHaveBeenCalledTimes(mode==='shown' ? 4 : 3)
})

it.each(['lock','child'])('direct deletion %s change blocks old successful response', async mode => {
  let finishDelete!: (value: unknown)=>void
  const deletion=vi.spyOn(api.localApi,'deleteCaptureArtifact').mockImplementation(()=>new Promise(done=>{finishDelete=done}) as never)
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path=>path.includes('/deletion-receipt/') ? {verified:true,child_id:'a',artifact_id:'art',action_id:path.split('/').pop()} as never : path.endsWith('/session') ? {expires_at:new Date(Date.now()+60000).toISOString()} as never : preview(path.split('/').pop()!) as never)
  const view=setup()
  await screen.findByRole('button',{name:'删除此条'})
  fireEvent.change(screen.getByLabelText('删除确认'),{target:{value:'确认删除'}})
  fireEvent.click(screen.getByRole('button',{name:'删除此条'}))
  await waitFor(()=>expect(deletion).toHaveBeenCalledTimes(1))
  if(mode==='lock') act(()=>changeParentDraftAuthority('locked'))
  else view.rerender(<><ParentDailyVisualPreview childId="b" /><PrivacyOperations childId="b" initial={preferences} health={health} onHealthChange={()=>undefined}/></>)
  await act(async()=>finishDelete({status:'completed',database_reference_deleted:true,raw_file_deleted:false,recovery_rotation:{status:'completed'}}))
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  if(mode==='child') view.rerender(<><ParentDailyVisualPreview childId="a" /><PrivacyOperations childId="a" initial={preferences} health={health} onHealthChange={()=>undefined}/></>)
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  expect(await screen.findByText(/原删除已确认完成/)).toBeTruthy()
  expect(deletion).toHaveBeenCalledTimes(1)
})

it('direct deletion receipt cannot unlock after parent authority changes', async () => {
  vi.spyOn(api.localApi,'deleteCaptureArtifact').mockResolvedValue({status:'completed',database_reference_deleted:true,raw_file_deleted:false,recovery_rotation:{status:'completed',invalidated_backup_count:0,removed_backup_bundle_files:0,removed_upgrade_snapshot_files:0,replacement_backup_id:'x'}})
  let resolveReceipt!: (value: unknown)=>void
  let delayed = true
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path=>path.includes('/deletion-receipt/') ? delayed ? await new Promise<unknown>(done=>{resolveReceipt=done}) as never : {verified:true,child_id:'a',artifact_id:'art',action_id:path.split('/').pop()} as never : path.endsWith('/session') ? {expires_at:new Date(Date.now()+60000).toISOString()} as never : preview(path.split('/').pop()!) as never)
  setup()
  await screen.findByRole('button',{name:'删除此条'})
  fireEvent.change(screen.getByLabelText('删除确认'),{target:{value:'确认删除'}})
  fireEvent.click(screen.getByRole('button',{name:'删除此条'}))
  await waitFor(()=>expect(resolveReceipt).toBeTypeOf('function'))
  const action = vi.mocked(api.localApi.deleteCaptureArtifact).mock.calls[0][2]
  act(()=>changeParentDraftAuthority('locked'))
  await act(async()=>resolveReceipt({verified:true,child_id:'a',artifact_id:'art',action_id:action}))
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  delayed = false
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  expect(await screen.findByText(/原删除已确认完成/)).toBeTruthy()
})

it('direct deletion receipt after server session expiry keeps preview blocked', async () => {
  vi.spyOn(api.localApi,'deleteCaptureArtifact').mockResolvedValue({status:'completed',database_reference_deleted:true,raw_file_deleted:false,recovery_rotation:{status:'completed',invalidated_backup_count:0,removed_backup_bundle_files:0,removed_upgrade_snapshot_files:0,replacement_backup_id:'x'}})
  let resolveReceipt!: (value: unknown)=>void
  let expires = new Date(Date.now()+60000).toISOString()
  let delayed = true
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path=>path.includes('/deletion-receipt/') ? delayed ? await new Promise<unknown>(done=>{resolveReceipt=done}) as never : {verified:true,child_id:'a',artifact_id:'art',action_id:path.split('/').pop()} as never : path.endsWith('/session') ? {expires_at:expires} as never : preview(path.split('/').pop()!) as never)
  setup()
  await screen.findByRole('button',{name:'删除此条'})
  fireEvent.change(screen.getByLabelText('删除确认'),{target:{value:'确认删除'}})
  fireEvent.click(screen.getByRole('button',{name:'删除此条'}))
  await waitFor(()=>expect(resolveReceipt).toBeTypeOf('function'))
  const action = vi.mocked(api.localApi.deleteCaptureArtifact).mock.calls[0][2]
  vi.useFakeTimers(); vi.setSystemTime(new Date(Date.parse(expires)+1))
  await act(async()=>resolveReceipt({verified:true,child_id:'a',artifact_id:'art',action_id:action}))
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  vi.useRealTimers(); delayed = false; expires = new Date(Date.now()+60000).toISOString()
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  expect(await screen.findByText(/原删除已确认完成/)).toBeTruthy()
})

it('parent lock revokes displayed and pending preview data', async () => {
  let finish!: (value: unknown)=>void
  vi.spyOn(api,'requestParentLocal').mockImplementation(()=>new Promise(done=>{finish=done}) as never)
  render(<ParentDailyVisualPreview childId="a" />)
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  const day=(screen.getByLabelText('学习记录日期') as HTMLInputElement).value
  act(()=>changeParentDraftAuthority('locked'))
  await act(async()=>{finish(preview(day))})
  expect(screen.queryByText('孩子页面原文')).toBeNull()
})

it('parent authority expiry removes already rendered OCR', async () => {
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path=>path.endsWith('/session') ? {expires_at:new Date(Date.now()+60000).toISOString()} as never : preview(path.split('/').pop()!) as never)
  render(<ParentDailyVisualPreview childId="a" />)
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  expect(await screen.findByText('孩子页面原文')).toBeTruthy()
  act(()=>changeParentDraftAuthority('expired'))
  expect(screen.queryByText('孩子页面原文')).toBeNull()
})

it.each(['delete','restore'])('real %s entry clears preview before preparation and never restores it', async mode => {
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path=>path.endsWith('/session') ? {expires_at:new Date(Date.now()+60000).toISOString()} as never : preview(path.split('/').pop()!) as never)
  vi.spyOn(api.localApi,'captureArtifacts').mockResolvedValue([])
  vi.spyOn(api.localApi,'deletionPreview').mockResolvedValue({learning:1} as never)
  vi.spyOn(api.localApi,'listBackups').mockResolvedValue([{backup_id:'backup',path:'synthetic',database_version:93,size_bytes:1,sha256:'a'.repeat(64),integrity_status:'ok',reason:'manual',created_at:'2026-01-01T00:00:00Z'}] as never)
  let rejectGeneration!: (reason: Error)=>void
  vi.spyOn(api.localApi,'privacyOperationGeneration').mockImplementation(()=>new Promise((_ok,reject)=>{rejectGeneration=reject}) as never)
  render(<><ParentDailyVisualPreview childId="a" /><PrivacyOperations childId="a" initial={preferences} health={health} onHealthChange={() => undefined}/></>)
  await screen.findByRole('option',{name:/家长手动/})
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  expect(await screen.findByText('孩子页面原文')).toBeTruthy()
  if(mode==='delete') {
    fireEvent.change(screen.getByLabelText('删除确认'),{target:{value:'确认删除'}})
    fireEvent.click(screen.getByRole('button',{name:'删除'}))
  } else {
    fireEvent.change(screen.getByLabelText('恢复确认'),{target:{value:'确认恢复'}})
    fireEvent.click(screen.getByRole('button',{name:'恢复'}))
  }
  await waitFor(()=>expect(rejectGeneration).toBeTypeOf('function'))
  expect(screen.queryByText('孩子页面原文')).toBeNull()
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  await act(async()=>rejectGeneration(new Error('synthetic failure')))
  await waitFor(()=>expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(false))
  expect(screen.queryByText('孩子页面原文')).toBeNull()
})

it('lost capture deletion acknowledgement keeps preview blocked across remount and prevents re-deletion', async () => {
  let receipt: unknown = {verified:false}
  let delayed = false
  let resolveReceipt!: (value: unknown)=>void
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path=>path.includes('/deletion-receipt/') ? delayed ? await new Promise<unknown>(done=>{resolveReceipt=done}) as never : receipt as never : path.endsWith('/session') ? {expires_at:new Date(Date.now()+60000).toISOString()} as never : preview(path.split('/').pop()!) as never)
  const deletion = vi.spyOn(api.localApi,'deleteCaptureArtifact').mockRejectedValue(new Error('reply lost while server may still commit'))
  setup()
  await screen.findByRole('button',{name:'删除此条'})
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  expect(await screen.findByText('孩子页面原文')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('删除确认'),{target:{value:'确认删除'}})
  fireEvent.click(screen.getByRole('button',{name:'删除此条'}))
  expect(await screen.findByText(/删除结果尚未确认，暂时无法查看/)).toBeTruthy()
  expect(screen.queryByText('孩子页面原文')).toBeNull()
  expect((screen.getByRole('button',{name:'删除此条'}) as HTMLButtonElement).disabled).toBe(true)
  cleanup()
  const view = render(<ParentDailyVisualPreview childId="a" />)
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  expect(await screen.findByText(/原操作尚未完成核验/)).toBeTruthy()
  await waitFor(()=>expect((screen.getByRole('button',{name:'核对原删除'}) as HTMLButtonElement).disabled).toBe(false))
  const action = deletion.mock.calls[0][2]
  expect(action).toBeTruthy()
  receipt={verified:true,child_id:'b',artifact_id:'art',action_id:action}
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  expect(await screen.findByText(/原操作尚未完成核验/)).toBeTruthy()
  await waitFor(()=>expect((screen.getByRole('button',{name:'核对原删除'}) as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  delayed = true
  receipt={verified:true,child_id:'a',artifact_id:'art',action_id:action}
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  await waitFor(()=>expect(resolveReceipt).toBeTypeOf('function'))
  view.rerender(<ParentDailyVisualPreview childId="b" />)
  await act(async()=>resolveReceipt(receipt))
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  view.rerender(<ParentDailyVisualPreview childId="a" />)
  resolveReceipt = undefined as never
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  await waitFor(()=>expect(resolveReceipt).toBeTypeOf('function'))
  act(()=>changeParentDraftAuthority('locked'))
  await act(async()=>resolveReceipt(receipt))
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  resolveReceipt = undefined as never
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  await waitFor(()=>expect(resolveReceipt).toBeTypeOf('function'))
  view.unmount()
  await act(async()=>resolveReceipt(receipt))
  render(<ParentDailyVisualPreview childId="a" />)
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(true)
  delayed = false
  fireEvent.click(screen.getByRole('button',{name:'核对原删除'}))
  expect(await screen.findByText(/原删除已确认完成/)).toBeTruthy()
  expect((screen.getByRole('button',{name:'查看记录'}) as HTMLButtonElement).disabled).toBe(false)
  expect(screen.queryByText('孩子页面原文')).toBeNull()
  expect(deletion).toHaveBeenCalledTimes(1)
})
