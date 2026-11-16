type Runtime = { status: string; localApiUrl: string; sessionToken: string; runtimeEpoch?: number }
type Authority = {
  senderId: number
  current: () => boolean
  runtime: () => Runtime
  externalBackend: boolean
  fetchImpl?: typeof fetch
}

/** Check the real backend parent token, local runtime epoch, and calling frame. */
export function createTutorExplanationAuthorizer(authority: Authority) {
  return async (senderId: number, parentToken: string): Promise<void> => {
    if (!authority.current() || authority.senderId !== senderId || typeof parentToken !== 'string' || !parentToken) throw Error('TUTOR_EXPLANATION_PARENT_REQUIRED')
    if (authority.externalBackend) throw Error('TUTOR_EXPLANATION_RUNTIME_UNAVAILABLE')
    const before = authority.runtime()
    let url: URL
    try { url = new URL(before.localApiUrl) } catch { throw Error('TUTOR_EXPLANATION_RUNTIME_UNAVAILABLE') }
    if (before.status !== 'ready' || !before.sessionToken || url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw Error('TUTOR_EXPLANATION_RUNTIME_UNAVAILABLE')
    let response: Response
    try {
      response = await (authority.fetchImpl ?? fetch)(`${before.localApiUrl.replace(/\/$/, '')}/v1/parent/tutor-explanation/authorization`, {
        headers: { 'X-HSC-Session': before.sessionToken, 'X-HSC-Parent': parentToken }, redirect: 'error', signal: AbortSignal.timeout(5000)
      })
    } catch { throw Error('TUTOR_EXPLANATION_PARENT_REQUIRED') }
    if (!response.ok) throw Error('TUTOR_EXPLANATION_PARENT_REQUIRED')
    let body: { authorized?: unknown }
    try { body = await response.json() as { authorized?: unknown } } catch { throw Error('TUTOR_EXPLANATION_PARENT_REQUIRED') }
    const after = authority.runtime()
    if (body.authorized !== true || !authority.current() || after.status !== 'ready' || after.localApiUrl !== before.localApiUrl
      || after.sessionToken !== before.sessionToken || after.runtimeEpoch !== before.runtimeEpoch) throw Error('TUTOR_EXPLANATION_PARENT_REQUIRED')
  }
}
