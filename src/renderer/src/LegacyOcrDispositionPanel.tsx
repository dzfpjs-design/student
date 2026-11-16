import { useState } from 'react'
import { RefreshCw, Search, Trash2 } from 'lucide-react'
import { localApi } from './api'
import type { LegacyOcrCommand, LegacyOcrPreview, LegacyOcrReceipt } from './legacyOcrDispositionTypes'

const storageKey = 'hsc-legacy-ocr-disposition-v1'
type Journal = { command: LegacyOcrCommand; preview: LegacyOcrPreview; receipt?: LegacyOcrReceipt }
function loadJournal(): { journal: Journal | null; error: string | null } {
  try {
    const raw = localStorage.getItem(storageKey)
    if (!raw) return { journal: null, error: null }
    const value = JSON.parse(raw) as Journal
    if (!value.command?.client_action_id || !value.preview?.counts ||
        value.command.preview_id !== value.preview.preview_id ||
        value.command.expected_fingerprint !== value.preview.snapshot_fingerprint) throw new Error('invalid journal')
    return { journal: value, error: null }
  } catch { return { journal: null, error: '无法读取上次清理操作的恢复记录，未发送清理请求。' } }
}
function saveJournal(value: Journal): void {
  const encoded = JSON.stringify(value)
  localStorage.setItem(storageKey, encoded)
  if (localStorage.getItem(storageKey) !== encoded) throw new Error('操作恢复记录未保存，本次没有开始新的清理。')
}

export function LegacyOcrDispositionPanel(): React.JSX.Element {
  const [saved] = useState(loadJournal)
  const [journal, setJournal] = useState(saved.journal)
  const [preview, setPreview] = useState<LegacyOcrPreview | null>(saved.journal?.preview ?? null)
  const [receipt, setReceipt] = useState<LegacyOcrReceipt | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [message, setMessage] = useState<string | null>(saved.error)
  const [storageError, setStorageError] = useState(saved.error)
  const [busy, setBusy] = useState(false)
  const pending = Boolean(journal && !journal.receipt)
  const failure = (cause: unknown) => setMessage(cause instanceof Error ? cause.message : '请求没有完成，原操作记录已保留。')
  const inspect = async () => {
    setBusy(true); setMessage(null)
    try { setPreview(await localApi.legacyOcrPreview()); setConfirmation(''); setReceipt(null) }
    catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  const submit = async () => {
    if (!preview) return
    setBusy(true); setMessage(null)
    try {
      const next = pending && journal ? journal : { preview, command: {
        preview_id: preview.preview_id, expected_fingerprint: preview.snapshot_fingerprint,
        client_action_id: crypto.randomUUID(), confirmation,
      } }
      saveJournal(next); setJournal(next)
      const result = await localApi.legacyOcrDispose(next.command)
      const completed = { ...next, receipt: result.status === 'completed' ? result : undefined }
      if (loadJournal().journal?.command.client_action_id !== next.command.client_action_id) return
      saveJournal(completed); setJournal(completed); setReceipt(result); setConfirmation('')
    } catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  const readReceipt = async () => {
    if (!journal?.receipt) return
    setBusy(true); setMessage(null)
    try { setReceipt(await localApi.legacyOcrReceipt(journal.receipt.receipt_id)); setPreview(journal.preview) }
    catch (cause) { failure(cause) } finally { setBusy(false) }
  }
  const abandon = () => {
    try {
      localStorage.removeItem(storageKey)
      if (localStorage.getItem(storageKey) !== null) throw new Error('恢复记录未清除，原请求不会重发。')
      setJournal(null); setPreview(null); setReceipt(null); setConfirmation(''); setStorageError(null)
      setMessage('已停止重试原操作；此前清理不会撤销。重新预览后须重新确认剩余范围。')
    } catch (cause) { failure(cause) }
  }
  const completed = receipt?.status === 'completed' && !receipt.restored_invalidated
  const historicalPreview = Boolean(journal?.receipt && preview?.preview_id === journal.preview.preview_id)
  return <div className="legacy-ocr-disposition">
    <h3>无法归属的旧 OCR 缓存</h3>
    <p>范围是全家遗留的未知缓存，不是当前选中的孩子。当前学习库、已归属的旧记录和原始教材不在范围内。</p>
    <div className="data-actions">
      <button className="secondary-button" disabled={busy || pending || Boolean(storageError)} onClick={() => void inspect()}><Search size={16} />核对旧缓存</button>
      {journal?.receipt && <button className="secondary-button" disabled={busy} onClick={() => void readReceipt()}><RefreshCw size={16} />核对上次清理回执</button>}
    </div>
    {preview && <>
      <p>{historicalPreview ? '本次确认范围（历史）' : pending ? '原操作确认范围' : '待清理'}：{preview.counts.unattributed_jobs} 条旧识别记录、{preview.counts.unattributed_cache} 条旧缓存。保留：{preview.counts.protected_jobs} 条已归属记录、{preview.counts.protected_cache} 条已归属缓存。</p>
      {!pending && !receipt && (preview.counts.unattributed_jobs + preview.counts.unattributed_cache > 0) && <div className="delete-controls">
        <label>旧缓存清理确认<input aria-label="旧缓存清理确认" value={confirmation} onChange={event => setConfirmation(event.target.value)} placeholder={preview.confirmation_text} /></label>
        <button className="danger-button" disabled={busy || confirmation !== preview.confirmation_text || Boolean(storageError)} onClick={() => void submit()}><Trash2 size={16} />清理这批未知缓存</button>
      </div>}
    </>}
    {pending && <><p>上次操作结果尚未核对。重试只使用原确认范围，不会清理新增内容。</p>
      <button className="secondary-button" disabled={busy} onClick={() => void submit()}><RefreshCw size={16} />重试原清理操作</button>
      <button className="secondary-button" disabled={busy} onClick={abandon}>停止重试，重新核对范围</button></>}
    {completed && <p role="status">本次确认的未知缓存已清理。仍保留 {receipt.remaining_counts?.protected_jobs ?? 0} 条已归属旧记录。此前失败的孩子数据删除没有自动重试，请在运行状态中核对并重试原删除任务。</p>}
    {receipt && !completed && <p role="status">原回执尚不能证明当前清理完成，可能发生过数据库恢复；请核对当前范围。</p>}
    {busy && <p role="status">正在核对或处理，请稍候。</p>}
    {storageError && <button className="secondary-button" disabled={busy} onClick={abandon}>丢弃损坏的恢复记录，重新核对</button>}
    {message && <p role="alert">{message}</p>}
  </div>
}
