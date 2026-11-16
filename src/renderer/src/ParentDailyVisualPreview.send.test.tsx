import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from './api'
import { ParentDailyVisualPreview } from './ParentDailyVisualPreview'

beforeEach(() => localStorage.clear())
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })

it('shows a disabled send button until the combined report is ready and never sends', async () => {
  const expiry = new Date(Date.now()+60000).toISOString()
  const mock = vi.spyOn(api, 'requestParentLocal').mockImplementation(async path => {
    if (path.endsWith('/session')) return {expires_at: expiry} as never
    if (path.endsWith('/delivery-preparation')) return {child_id:'a',day:path.split('/').at(-2),delivery_enabled:false,recipients:[{recipient_id:'r',label:'家长',address:'parent@example.test',status:'prepared',source_id:'s'}]} as never
    return {child_id:'a',learning_date:path.split('/').pop(),local_only:true,is_formal_report:false,document_observation:{pages:[]},page_observation_time:{status:'no_segments',pages:[]}} as never
  })
  render(<ParentDailyVisualPreview childId="a" />)
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  fireEvent.click(await screen.findByRole('button',{name:'查看日报准备状态'}))
  const button = await screen.findByRole('button',{name:'手动发送（未开放）'}) as HTMLButtonElement
  expect(button.disabled).toBe(true)
  expect(screen.getByText(/自动发送进度以综合日报发送状态为准/)).toBeTruthy()
  fireEvent.click(button)
  expect(mock.mock.calls.filter(([path])=>path.endsWith('/send'))).toHaveLength(0)
  expect(Object.values(localStorage)).not.toContain('pending')
})
