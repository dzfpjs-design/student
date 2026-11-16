import type { ParentDraftRecoveryCheckpoint, ParentDraftRecoveryPrepared, ParentDraftRecoveryRecovered, ParentDraftRecoveryScope, ParentDraftRecoverySnapshot } from '../../shared/parentDraftRecovery'

/** Tokenless scope port; only api.ts may supply the current parent token. */
export type ScopedParentDraftRecoveryPort = {
  prepare(): Promise<ParentDraftRecoveryPrepared>
  read(expectedLocalRevision: number): Promise<ParentDraftRecoveryRecovered>
  checkpoint(leaseId: string, sequence: number, snapshot: ParentDraftRecoverySnapshot): ParentDraftRecoveryCheckpoint
  revoke(): void
  discard(expectedLocalRevision: number, explicit: true): Promise<ParentDraftRecoveryPrepared>
}
export type ParentDraftRecoveryConfiguration = ScopedParentDraftRecoveryPort | 'memory-only' | undefined
export type ParentDraftRecoveryFactory = (childId: string, scope: ParentDraftRecoveryScope) => ParentDraftRecoveryConfiguration
/** Explicit fixture configuration. Product App/ParentCenter inject the native factory. */
export const memoryOnlyParentDraftRecovery: ParentDraftRecoveryFactory = () => 'memory-only'
