export type WritingConfirmation = {
  confirmation_id: string; binding_version: string; child_id: string; session_id: string;
  prompt_id: string; item_version: string; artifact_id: string; revision_id: string;
  response_text: string; readback_text: string; readback_sha256: string; expires_at: string;
  local_only: true; cloud_transmission_allowed: false;
}

export type WritingReadbackRequest = {
  confirmation_id: string; binding_version: string; playback_id: string;
  event: 'started' | 'ended'; readback_sha256: string; client_action_id: string;
}

export async function playWritingReadback(input: {
  childId: string; sessionId: string; confirmationId: string; bindingVersion: string;
  isCurrent: () => boolean;
  read: () => Promise<WritingConfirmation>;
  record: (event: WritingReadbackRequest) => Promise<unknown>;
  play: (text: string, callbacks: { started: () => void; ended: () => void; unavailable: () => void }) => void;
}): Promise<{ status: 'readback_completed'; confirmation_id: string; binding_version: string; playback_id: string }> {
  const packet = await input.read()
  if (!input.isCurrent() || packet.child_id !== input.childId || packet.session_id !== input.sessionId
    || packet.confirmation_id !== input.confirmationId || packet.binding_version !== input.bindingVersion
    || packet.local_only !== true || packet.cloud_transmission_allowed !== false
    || !packet.readback_text?.trim() || !/^[a-f0-9]{64}$/.test(packet.readback_sha256)
    || !Number.isFinite(Date.parse(packet.expires_at)) || Date.parse(packet.expires_at) <= Date.now()) {
    throw new Error('写作原稿或读回范围已经变化，请重新核对。')
  }
  const playbackId = crypto.randomUUID()
  await new Promise<void>((resolve, reject) => {
    let settled = false
    let started: Promise<unknown> | null = null
    let ending = false
    const finish = (error?: Error): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (error) reject(error)
      else resolve()
    }
    const unavailable = (): void => finish(new Error('原稿朗读没有完整确认，尚未提交作答。'))
    const timer = setTimeout(unavailable, 180_000)
    const event = (kind: 'started' | 'ended'): WritingReadbackRequest => ({
      confirmation_id: packet.confirmation_id, binding_version: packet.binding_version,
      playback_id: playbackId, event: kind, readback_sha256: packet.readback_sha256,
      client_action_id: `writing-readback:${playbackId}:${kind}`,
    })
    try {
      input.play(packet.readback_text, {
        started: () => {
          if (settled || started) return
          if (!input.isCurrent()) { unavailable(); return }
          started = Promise.resolve().then(() => {
            if (!input.isCurrent()) throw new Error('写作读回已经取消。')
            return input.record(event('started'))
          })
          void started.catch(unavailable)
        },
        ended: () => {
          if (settled || ending) return
          if (!started || !input.isCurrent()) { unavailable(); return }
          ending = true
          void started.then(async () => {
            if (settled || !input.isCurrent()) { unavailable(); return }
            await input.record(event('ended'))
            if (!input.isCurrent()) { unavailable(); return }
            finish()
          }).catch(unavailable)
        },
        unavailable,
      })
    } catch { unavailable() }
  })
  return { status: 'readback_completed', confirmation_id: packet.confirmation_id,
    binding_version: packet.binding_version, playback_id: playbackId }
}
