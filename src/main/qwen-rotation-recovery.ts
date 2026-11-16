type Runtime = { status: string; rotationAuthOnly?: boolean; runtimeEpoch?: number; localApiUrl: string; sessionToken: string }
type Dependencies = {
  runtime: () => Runtime
  binding: () => Promise<string>
  authorize: (token: string, runtime: Runtime) => Promise<boolean>
  stopAuth: () => Promise<void>
  startOld: (current: () => boolean) => Promise<void>
  restoreAuth: () => Promise<void>
  cancelStarting: () => Promise<void>
}

/** One parent-authorized decision bound to the pending installer and source slots. */
export class QwenRotationRecovery {
  private pending: string | null = null
  private running = false
  private revision = 0
  private oldStarting = false
  constructor(private readonly dep: Dependencies) {}
  async arm(): Promise<void> { this.pending = await this.dep.binding() }
  async cancel(): Promise<void> {
    if (!this.running) return
    this.revision += 1
    if (this.oldStarting) await this.dep.cancelStarting()
  }
  async continueWithOldKey(token: string, ownerCurrent: () => boolean): Promise<void> {
    if (this.running || !this.pending || typeof token !== 'string' || !token || !ownerCurrent()) throw new Error('ROTATION_RECOVERY_UNAVAILABLE')
    this.running = true
    let authStopAttempted = false
    try {
      const binding = this.pending, before = { ...this.dep.runtime() }, revision = this.revision
      const current = (): boolean => ownerCurrent() && this.revision === revision
      const check = (): void => {
        const now = this.dep.runtime()
        if (!current() || this.pending !== binding || before.status !== 'ready' || !before.rotationAuthOnly
          || now.status !== 'ready' || !now.rotationAuthOnly || now.runtimeEpoch !== before.runtimeEpoch
          || now.localApiUrl !== before.localApiUrl || now.sessionToken !== before.sessionToken) throw new Error('ROTATION_RECOVERY_CHANGED')
      }
      check()
      if (await this.dep.binding() !== binding) throw new Error('ROTATION_RECOVERY_CHANGED')
      check()
      if (!await this.dep.authorize(token, before)) throw new Error('ROTATION_RECOVERY_AUTH_REQUIRED')
      check()
      if (await this.dep.binding() !== binding) throw new Error('ROTATION_RECOVERY_CHANGED')
      check()
      authStopAttempted = true
      await this.dep.stopAuth()
      if (!current() || this.pending !== binding || await this.dep.binding() !== binding || !current() || this.pending !== binding) throw new Error('ROTATION_RECOVERY_CHANGED')
      this.pending = null
      this.oldStarting = true
      await this.dep.startOld(current)
      if (!current()) throw new Error('ROTATION_RECOVERY_CHANGED')
    } catch (error) {
      if (authStopAttempted) {
        this.pending = null
        try { await this.dep.restoreAuth(); await this.arm() }
        catch { throw new Error('ROTATION_RECOVERY_RESTART_REQUIRED') }
      }
      throw error
    } finally { this.oldStarting = false; this.running = false }
  }
}
