type TimedPlanItem = {
  status: string
  active_seconds: number
  active_segment_started_at: string | null
}

export function effectiveActiveMilliseconds(item: TimedPlanItem, nowMs = Date.now()): number {
  const saved = Math.max(0, item.active_seconds) * 1000
  if (item.status !== 'active' || !item.active_segment_started_at) return saved
  const segmentStartedAt = new Date(item.active_segment_started_at).getTime()
  if (!Number.isFinite(segmentStartedAt)) return saved
  return saved + Math.max(0, nowMs - segmentStartedAt)
}

export function effectiveActiveMinutes(item: TimedPlanItem, nowMs = Date.now()): number {
  return Math.max(1, Math.ceil(effectiveActiveMilliseconds(item, nowMs) / 60_000))
}
