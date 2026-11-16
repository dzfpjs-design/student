import {NATIVE_COMPLETION_READY} from './nativeCompletionRecovery'
import { localApi } from './api'
import type { AgentTurn } from './types'

export type CaptureReceipt = {
  sessionId: string
  commandId: string
  status: 'accepted' | 'captured' | 'recognized' | 'failed' | 'cancelled'
  artifactId?: string
  errorCode?: string
  ocrStatus?: 'completed' | 'failed'
}
export type CaptureRequest = {
  sessionId: string
  commandId: string
  forMistake: boolean
  observationOnly?: boolean
  accept: () => void
}
type PendingCapture = CaptureReceipt & { capability: string; actionType?: string }
const PREFIX = 'hsc-device-capture-v1:'
export const CAPTURE_RECEIPT_EVENT = 'hsc-document-capture-receipt'
export const CAPTURE_SAVED_EVENT = 'hsc-document-capture-saved'
export const privacyStopKey = (sessionId: string): string => `hsc-privacy-stop-v1:${sessionId}`
export function privacyStopPending(sessionId: string): boolean {
  try { return localStorage.getItem(privacyStopKey(sessionId)) !== null } catch { return true }
}
export function isImmediatePrivacyStop(text: string): boolean {
  return /^(?:请)?(?:停止全部监听|关闭摄像头和麦克风|关闭麦克风和摄像头|别听了|不要再听了|停止所有监听)[吧了]?$/u.test(text.replace(/[\s，。！？,.!?]/g, ''))
}
export function emitCaptureReceipt(receipt: CaptureReceipt): void {
  window.dispatchEvent(new CustomEvent(CAPTURE_RECEIPT_EVENT, { detail: receipt }))
}

// Only IDs and device outcomes are journaled, never images or OCR text. The
// server remains the system of record; terminal receipts retry after reload.
export function createCaptureReceiptBridge(sessionId: string, onError: (message: string) => void) {
  const pending = new Map<string, PendingCapture>()
  const saving = new Set<string>()
  const timers = new Set<number>()
  const key = (id: string): string => `${PREFIX}${sessionId}:${id}`
  const save = (item: PendingCapture): void => { localStorage.setItem(key(item.commandId), JSON.stringify(item)) }
  const flush = async (item: PendingCapture): Promise<void> => {
    if (saving.has(item.commandId)) return
    saving.add(item.commandId)
    const success = item.status === 'recognized'
    const message = success ? '拍摄与文字识别已完成，题干仍需核对。'
      : item.status === 'cancelled' ? '拍题已取消，没有确认完成。' : '拍题没有完成，请检查设备或重新拍摄。'
    const receipt = {
      capability: item.capability, execution_location: 'client_device_bridge',
      action_type: item.actionType ?? 'request_capture', status: success ? 'completed' : 'failed',
      completed_at: new Date().toISOString(),
      output: {
        command_id: item.commandId, device_status: item.status,
        capture_artifact_id: item.artifactId ?? null, artifact_id: item.artifactId ?? null,
        ocr: {
          status: item.ocrStatus ?? (success ? 'completed' : 'not_completed'),
          artifact_id: item.artifactId ?? null,
          error_code: item.ocrStatus === 'failed' ? item.errorCode ?? 'OCR_FAILED' : null,
        },
      },
    }
    try {
      await localApi.completeAgentTurn(item.commandId, message, success ? 'completed' : 'failed',
        success ? undefined : item.errorCode ?? 'DEVICE_CAPTURE_CANCELLED',
        { execution_receipt: receipt, client_bridge_receipt: receipt })
      localStorage.removeItem(key(item.commandId))
      pending.delete(item.commandId)
      window.dispatchEvent(new CustomEvent(CAPTURE_SAVED_EVENT, { detail: item }))
    } catch {
      onError('设备结果尚未同步保存，已保留回执；重开会话会重试同步，不会自动重拍。')
    } finally { saving.delete(item.commandId) }
  }
  const receive = (receipt: CaptureReceipt): void => {
    const item = pending.get(receipt.commandId)
    if (!item || receipt.sessionId !== sessionId || !['accepted', 'captured'].includes(item.status)) return
    if (receipt.status === 'accepted' && item.status === 'captured') return
    const next = { ...item, ...receipt }
    pending.set(next.commandId, next)
    try { save(next) } catch { onError('设备回执未能在本机保存，请保持当前页面并请家长检查存储。') }
    if (next.status === 'failed') onError(next.errorCode === 'DEVICE_CAPTURE_NO_LISTENER'
      ? '拍题请求没有设备接收，未完成拍摄。请重试或请家长检查高拍仪。'
      : '拍摄或文字识别失败，没有确认完成。请检查高拍仪后重新拍题。')
    if (!['accepted', 'captured'].includes(next.status)) void flush(next)
  }
  const listener = (event: Event): void => {
    const detail = (event as CustomEvent<CaptureReceipt>).detail
    if (detail && ['accepted', 'captured', 'recognized', 'failed', 'cancelled'].includes(detail.status)) receive(detail)
  }
  const completionReady=(event:Event):void=>{if((event as CustomEvent<{sessionId:string}>).detail?.sessionId===sessionId)for(const item of pending.values())if(!['accepted','captured'].includes(item.status))void flush(item)}
  return {
    start(): () => void {
      window.addEventListener(CAPTURE_RECEIPT_EVENT, listener)
      window.addEventListener(NATIVE_COMPLETION_READY,completionReady)
      try {
        for (const storageKey of Object.keys(localStorage)) {
          if (!storageKey.startsWith(`${PREFIX}${sessionId}:`)) continue
          const item = JSON.parse(localStorage.getItem(storageKey)!) as PendingCapture
          if (item.sessionId !== sessionId || typeof item.commandId !== 'string') continue
          if (['accepted', 'captured'].includes(item.status)) {
            item.status = 'cancelled'
            item.errorCode = 'DEVICE_CAPTURE_INTERRUPTED'
            save(item)
          }
          pending.set(item.commandId, item)
          void flush(item)
        }
      } catch { onError('上次设备回执无法恢复，请家长检查本机存储；不会自动重拍。') }
      return () => {
        window.removeEventListener(CAPTURE_RECEIPT_EVENT, listener)
        window.removeEventListener(NATIVE_COMPLETION_READY,completionReady)
        timers.forEach(window.clearTimeout)
      }
    },
    request(turn: AgentTurn, forMistake: boolean, dispatch: (request: CaptureRequest) => void): void {
      const item: PendingCapture = {
        sessionId, commandId: turn.agent_turn_id, status: 'accepted',
        actionType: turn.action.type,
        capability: turn.runtime_capability ?? turn.action.capability ?? 'learning.capture_question',
      }
      // Refuse to start a physical action without a recoverable association.
      save(item)
      pending.set(item.commandId, item)
      let accepted = false
      dispatch({ sessionId, commandId: item.commandId, forMistake, ...(turn.action.observation_only ? { observationOnly: true } : {}), accept: () => { accepted = true } })
      const timer = window.setTimeout(() => {
        timers.delete(timer)
        if (!accepted) receive({ ...item, status: 'failed', errorCode: 'DEVICE_CAPTURE_NO_LISTENER' })
      }, 250)
      timers.add(timer)
    },
    cancel(): void {
      for (const item of pending.values()) receive({ ...item, status: 'cancelled', errorCode: 'DEVICE_CAPTURE_CANCELLED' })
    },
  }
}

export class SupersededDeviceCommand extends Error {}
export function guardCommandApi<T extends object>(api: T, isCurrent: () => boolean): T {
  return new Proxy(api, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver)
      if (typeof value !== 'function') return value
      return async (...args: unknown[]) => {
        if (!isCurrent()) throw new SupersededDeviceCommand()
        const result = await value.apply(target, args)
        if (!isCurrent()) throw new SupersededDeviceCommand()
        return result
      }
    },
  })
}
