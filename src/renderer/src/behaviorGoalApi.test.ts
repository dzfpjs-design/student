import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearBehaviorGoalLocalState, createBehaviorGoalApi, behaviorGoalLocalKey, parseBehaviorGoalJournal } from './behaviorGoalApi'
import { goalView, command } from './BehaviorGoalReviewPanel.fixtures'

afterEach(() => localStorage.clear())
describe('local behavior confirmation contract', () => {
  it('binds scope, keeps nullable criteria, and never sends actor provenance', async () => {
    const request = vi.fn().mockResolvedValueOnce(goalView()).mockResolvedValueOnce({status:'recorded', event_id:'event', replayed:false, historical_receipt:false, local_only:true, cloud_transmission_allowed:false})
    const api = createBehaviorGoalApi(request)
    await api.read('a', 'goal')
    await api.confirm('a', 'goal', command())
    expect(request.mock.calls[0]).toEqual(['/v1/parent/children/a/behavior/habit-experiments/goal/opportunities', {cache:'no-store'}])
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual(command())
    await expect(api.confirm('a','goal',{...command(), actor_id:'forged'} as never)).rejects.toThrow()
    expect(request).toHaveBeenCalledTimes(2)
  })
  it.each(['child', 'experiment', 'boolean', 'local'])('rejects invalid %s read instead of showing prior evidence', async kind => {
    const view = goalView()
    if (kind==='child') view.child_id='b'
    if (kind==='experiment') view.experiment_id='other'
    if (kind==='boolean') view.opportunities[0].automatic.criteria.marked_uncertain_step = 1 as never
    if (kind==='local') view.cloud_transmission_allowed=true as never
    await expect(createBehaviorGoalApi(vi.fn().mockResolvedValue(view)).read('a','goal')).rejects.toThrow()
  })
  it('clears only this child or the entire own namespace, not other pending commands', () => {
    const a=behaviorGoalLocalKey('a','goal'), b=behaviorGoalLocalKey('ab','goal')
    localStorage.setItem(a,'{}'); localStorage.setItem(b,'{}'); localStorage.setItem('hsc:other','keep')
    clearBehaviorGoalLocalState('a')
    expect(localStorage.getItem(a)).toBeNull(); expect(localStorage.getItem(b)).toBe('{}')
    clearBehaviorGoalLocalState()
    expect(localStorage.getItem(b)).toBeNull(); expect(localStorage.getItem('hsc:other')).toBe('keep')
  })
  it('rejects malformed, cross-child and old-schema recovery rather than silently issuing a new command', () => {
    expect(() => parseBehaviorGoalJournal('{','a','goal')).toThrow()
    expect(() => parseBehaviorGoalJournal(JSON.stringify({schema:0}),'a','goal')).toThrow()
  })
})
