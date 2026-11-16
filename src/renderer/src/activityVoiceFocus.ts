import { synchronizeVoiceActivityCapture, voiceActivityCaptureEpoch, type VoiceActivityReference } from './voiceCaptureContext'

export type ActivityVoiceFocus = {
  session_id: string
  child_id: string
  activity_focus: VoiceActivityReference | null
  local_only: true
  cloud_transmission_allowed: false
}

export function createActivityFocusReader() {
  let generation = 0
  let synchronized: { sessionId: string; childId: string; epoch: number } | null = null
  return {
    invalidate() { generation++; synchronized = null },
    async refresh(sessionId: string, childId: string, load: () => Promise<ActivityVoiceFocus>, isCurrent: () => boolean): Promise<boolean> {
      const reading = ++generation
      const epoch = voiceActivityCaptureEpoch(sessionId)
      try {
        const result = await load()
        if (reading !== generation || epoch !== voiceActivityCaptureEpoch(sessionId) || !isCurrent() || result.session_id !== sessionId || result.child_id !== childId
          || result.local_only !== true || result.cloud_transmission_allowed !== false
          || !Object.hasOwn(result, 'activity_focus')) return false
        const focus = result.activity_focus
        if (focus !== null && (!focus || Object.keys(focus).sort().join(',') !== 'activity_id,last_turn_id,version'
          || !Object.values(focus).every(value => typeof value === 'string' && value.length > 0))) return false
        const expectedEpoch = synchronized?.sessionId === sessionId && synchronized.childId === childId
          ? synchronized.epoch : null
        synchronizeVoiceActivityCapture(sessionId, childId, focus, expectedEpoch)
        synchronized = { sessionId, childId, epoch: voiceActivityCaptureEpoch(sessionId) }
        return true
      } catch {
        // A failed read grants no new focus. The backend rejects any stale
        // captured reference; never replace it with an inferred latest focus.
        return false
      }
    },
  }
}
