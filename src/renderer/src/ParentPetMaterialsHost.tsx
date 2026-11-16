import { useEffect, useRef, useState } from 'react'
import { requestParentLocal, requestParentPetAudio, requestParentPetLocal } from './api'
import { ParentPetMaterialsPanel } from './ParentPetMaterialsPanel'
import { ParentPetAudioTeaching } from './ParentPetAudioTeaching'
import type { ParentPetPrompt } from './petMaterialSupplyTypes'

export function ParentPetMaterialsHost({ childId }: { childId: string }): React.JSX.Element {
  const [prompts, setPrompts] = useState<ParentPetPrompt[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const epoch = useRef(0)
  useEffect(() => {
    const run = ++epoch.current
    const abort = new AbortController()
    setLoading(true)
    setError(null)
    setPrompts([])
    requestParentLocal<{ child_id: string; prompts: ParentPetPrompt[]; local_only: boolean; cloud_transmission_allowed: boolean }>(
      `/v1/parent/children/${encodeURIComponent(childId)}/pet-prompts`, { signal: abort.signal })
      .then(result => {
        if (run !== epoch.current) return
        if (result.child_id !== childId || !result.local_only || result.cloud_transmission_allowed
          || result.prompts.some(prompt => prompt.child_id !== childId)) throw new Error('原题身份尚未核实。')
        setPrompts(result.prompts)
      })
      .catch(cause => { if (run === epoch.current) setError(cause instanceof Error ? cause.message : '原题暂不可用。') })
      .finally(() => { if (run === epoch.current) setLoading(false) })
    return () => { ++epoch.current; abort.abort() }
  }, [childId, revision])
  return <><ParentPetAudioTeaching key={`audio-purpose-${childId}`} childId={childId} /><ParentPetMaterialsPanel childId={childId} prompts={prompts} loading={loading} error={error}
    request={requestParentPetLocal} requestPreviewAudio={requestParentPetAudio}
    onRefreshPrompts={() => setRevision(value => value + 1)} /></>
}
