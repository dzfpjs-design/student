import { loadTutorExplanationConfig, saveTutorExplanationConfig, summarizeTutorExplanation, type TutorExplanationConfig, type TutorExplanationSummary } from './tutor-explanation-settings'

export type TutorExplanationCommand = { action: 'get' } | { action: 'save'; baseUrl: string; model: string; apiKey: string; confirmed: true }
export type TutorExplanationResponse = TutorExplanationSummary & { runtimeApplied?: boolean; appliedRuntimeEpoch?: number; previousRuntimeMayStillRun?: boolean }

type Dependencies = {
  userData: string
  authorize: (senderId: number, parentToken: string) => Promise<void>
  apply: (config: TutorExplanationConfig) => Promise<number | void>
}

/** Only a verified parent may inspect/change this durable outbound destination. */
export function createTutorExplanationHandler(deps: Dependencies) {
  return async (senderId: number, parentToken: string, command: TutorExplanationCommand): Promise<TutorExplanationResponse> => {
    if (!Number.isSafeInteger(senderId) || typeof parentToken !== 'string' || !parentToken || !command || typeof command !== 'object') throw Error('TUTOR_EXPLANATION_PARENT_REQUIRED')
    if (command.action !== 'get' && (command.action !== 'save' || command.confirmed !== true)) throw Error('TUTOR_EXPLANATION_CONFIRMATION_REQUIRED')
    const authorize = (): Promise<void> => deps.authorize(senderId, parentToken)
    await authorize()
    if (command.action === 'get') return summarizeTutorExplanation(await loadTutorExplanationConfig(deps.userData))
    const summary = await saveTutorExplanationConfig(deps.userData, command, authorize)
    const stored = await loadTutorExplanationConfig(deps.userData)
    if (!stored?.baseUrl || !stored.model) throw Error('TUTOR_EXPLANATION_CONFIG_UNCONFIRMED')
    try { const appliedRuntimeEpoch = await deps.apply(stored); return { ...summary, runtimeApplied: true, appliedRuntimeEpoch: appliedRuntimeEpoch ?? undefined } }
    catch { return { ...summary, runtimeApplied: false, previousRuntimeMayStillRun: true } }
  }
}
