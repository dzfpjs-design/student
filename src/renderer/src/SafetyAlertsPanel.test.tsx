import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SafetyAlertsPanel } from './ParentCenter'
import { localApi } from './api'
import type { ChildSafetyEvent } from './types'

const event = (id: string, child = 'child-a'): ChildSafetyEvent => ({
  safety_event_id: id, child_id: child, child_nickname: '测试同学', risk_level: 'R2',
  category: 'fear_or_bullying', status: 'pending', created_at: '2026-09-07T00:00:00Z', acknowledged_at: null,
  guidance: { summary: id, script: ['我先听你说'], action: { what: '线下确认', duration_minutes: 5, steps: ['确认安全'], stop_condition: '孩子拒绝不追问' }, avoid: [] },
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('opens the exact safety event missing from the dashboard without acknowledging it', async () => {
  vi.spyOn(localApi, 'safetyEvent').mockResolvedValue(event('old-event'))
  const ack = vi.spyOn(localApi, 'acknowledgeSafetyEvent')
  const resolved = vi.fn()
  render(<SafetyAlertsPanel childId="child-a" events={[event('new-event')]} onEventsChange={vi.fn()}
    focusRequest={{ objectId: 'old-event', nonce: 'focus-old' }} onFocusResolved={resolved} />)
  await waitFor(() => expect(resolved).toHaveBeenCalledWith('focus-old'))
  expect(localApi.safetyEvent).toHaveBeenCalledExactlyOnceWith('child-a', 'old-event')
  expect(document.activeElement?.getAttribute('data-work-object')).toBe('old-event')
  expect(ack).not.toHaveBeenCalled()
})

it('rejects a cross-child exact response and never focuses another event', async () => {
  vi.spyOn(localApi, 'safetyEvent').mockResolvedValue(event('old-event', 'child-b'))
  const resolved = vi.fn()
  render(<SafetyAlertsPanel childId="child-a" events={[event('new-event')]} onEventsChange={vi.fn()}
    focusRequest={{ objectId: 'old-event', nonce: 'wrong-owner' }} onFocusResolved={resolved} />)
  await waitFor(() => expect(resolved).toHaveBeenCalledWith('wrong-owner', expect.any(String)))
  expect(resolved).not.toHaveBeenCalledWith('wrong-owner')
  expect(screen.queryByText('old-event')).toBeNull()
})

it('discards late exact reads after switching children', async () => {
  let finish!: (value: ChildSafetyEvent) => void
  vi.spyOn(localApi, 'safetyEvent').mockReturnValue(new Promise(resolve => { finish = resolve }))
  const resolved = vi.fn()
  const props = { events: [event('b-event', 'child-b')], onEventsChange: vi.fn(), onFocusResolved: resolved }
  const view = render(<SafetyAlertsPanel {...props} childId="child-a" focusRequest={{ objectId: 'old-event', nonce: 'late' }} />)
  view.rerender(<SafetyAlertsPanel {...props} childId="child-b" />)
  await act(async () => { finish(event('old-event')) })
  expect(screen.queryByText('old-event')).toBeNull()
  expect(resolved).not.toHaveBeenCalled()
})
