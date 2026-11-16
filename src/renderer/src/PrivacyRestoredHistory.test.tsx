import {cleanup, render, screen} from '@testing-library/react'
import {afterEach, expect, it, vi} from 'vitest'
import {OperationsHealthPanel} from './ParentCenter'
import {localApi} from './api'
import type {SystemHealth} from './types'

afterEach(()=>{cleanup();vi.restoreAllMocks()})
it('keeps restored deletion history visible without offering execution',()=>{
  const retry=vi.spyOn(localApi,'retryDeletion')
  const initial={database_integrity:'ok',database_size_bytes:0,disk_free_bytes:100000,
    pending_report_jobs:0,pending_agent_turns:0,pending_deletion_jobs:0,model_failures_last_7_days:0,
    active_sensor_leases:0,last_backup:null,maintenance:{daily_backup:null,retention_cleanup:null},
    recent_maintenance_failures:[],devices:[],device_incidents:[],recent_model_failures:[],
    recent_report_failures:[],recent_agent_failures:[],recent_deletion_failures:[{
      deletion_id:'original-job',scope:'all_history',target_type:'child_scope',error_code:'APP_RESTART',
      error_message:'恢复前的历史删除记录，不适用于当前数据，不会重复执行。',remaining_file_count:0,
      updated_at:'2026-09-10T00:00:00Z',child_nickname:'同学',historical:true,retry_allowed:false,
    }]} as unknown as SystemHealth
  const first=render(<OperationsHealthPanel initial={initial} onHealthChange={vi.fn()} onReturnToChild={vi.fn()}/>)
  expect(screen.getByText(/恢复前的历史删除记录/)).toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'重试并验证删除'})).not.toBeInTheDocument()
  first.unmount()
  render(<OperationsHealthPanel initial={initial} onHealthChange={vi.fn()} onReturnToChild={vi.fn()}/>)
  expect(screen.queryByRole('button',{name:'重试并验证删除'})).not.toBeInTheDocument()
  expect(retry).not.toHaveBeenCalled()
})
