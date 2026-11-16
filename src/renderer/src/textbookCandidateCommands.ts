export type CandidateCommand = {
  candidateRef: string
  action: 'save' | 'confirm' | 'withdraw'
  payloadHash: string
  clientActionId: string
}

const PREFIX = 'hsc:textbook-language-candidate-command:'

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value as Record<string, unknown>).sort().map((key) => [key, canonicalize((value as Record<string, unknown>)[key])]))
  }
  return value
}

export function canonicalCandidatePayload(payload: Record<string, unknown>): string {
  return JSON.stringify(canonicalize(payload))
}

// This is a stable local fingerprint for retry identity, not a security hash.
export function candidatePayloadHash(payload: Record<string, unknown>): string {
  let hash = 2166136261
  for (const char of canonicalCandidatePayload(payload)) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619)
  return (hash >>> 0).toString(16)
}

function key(candidateRef: string, action: CandidateCommand['action']): string { return `${PREFIX}${candidateRef}:${action}` }

export function commandForCandidate(candidateRef: string, action: CandidateCommand['action'], payload: Record<string, unknown>): CandidateCommand {
  const payloadHash = candidatePayloadHash(payload)
  const stored = localStorage.getItem(key(candidateRef, action))
  if (stored) {
    try {
      const command = JSON.parse(stored) as CandidateCommand
      if (command.candidateRef === candidateRef && command.action === action && command.payloadHash === payloadHash) return command
    } catch { /* replace malformed local state */ }
  }
  const command = { candidateRef, action, payloadHash, clientActionId: `textbook-candidate-${crypto.randomUUID()}` }
  localStorage.setItem(key(candidateRef, action), JSON.stringify(command))
  return command
}

export function clearCandidateCommand(candidateRef: string, action: CandidateCommand['action']): void {
  localStorage.removeItem(key(candidateRef, action))
}

export function pendingCandidateCommand(candidateRef: string, action: CandidateCommand['action']): CandidateCommand | null {
  try {
    const stored = localStorage.getItem(key(candidateRef, action))
    return stored ? JSON.parse(stored) as CandidateCommand : null
  } catch { return null }
}

export function reconcileCandidateCommand(candidate: { candidate_ref: string, status: string }): void {
  if (candidate.status === 'confirmed') clearCandidateCommand(candidate.candidate_ref, 'confirm')
  if (candidate.status === 'withdrawn') clearCandidateCommand(candidate.candidate_ref, 'withdraw')
}
