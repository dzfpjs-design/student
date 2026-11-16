import type { DiagnosticPromptResult } from './types'

export const DIAGNOSTIC_TASK_START_MESSAGE = '原题已保留，但原计划任务尚未确认启动。请先说“继续原任务”，确认启动后再作答；现在不播题、不评分，也不另出新题。'

/** Preserve the public recovery receipt through legacy item/attempt adapters. */
export class DiagnosticTaskStartRequiredError extends Error {
  constructor(readonly result: DiagnosticPromptResult) {
    super(DIAGNOSTIC_TASK_START_MESSAGE)
    this.name = 'DiagnosticTaskStartRequiredError'
  }
}
