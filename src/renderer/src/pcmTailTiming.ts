/** Numerical comparison for an original audio source's ended callback.
 * This does not establish delivery without that callback and its owner checks.
 */
export function hasPcmTailRendered(
  currentTime: number, scheduledEnd: number, scheduledBlocks: number, sampleRate: number,
): boolean {
  if (!Number.isFinite(currentTime) || currentTime < 0
    || !Number.isFinite(scheduledEnd) || scheduledEnd < 0
    || !Number.isSafeInteger(scheduledBlocks) || scheduledBlocks < 1
    || !Number.isFinite(sampleRate) || sampleRate <= 0) return false
  if (currentTime >= scheduledEnd) return true

  // Bound positive duration accumulation and the rounded clock/duration
  // quotients. EPSILON is twice the IEEE-754 unit roundoff, so q is
  // deliberately conservative. Retain a fixed ordinal for each source.
  const q = (2 * scheduledBlocks + 8) * Number.EPSILON
  if (!Number.isFinite(q) || q >= 1) return false
  const roundoff = q / (1 - q) * Math.max(1, currentTime, scheduledEnd)
  // This cap rejects an excessive error bound; it is never a time grace.
  return Number.isFinite(roundoff) && roundoff < 1 / (8 * sampleRate)
    && scheduledEnd - currentTime <= roundoff
}
