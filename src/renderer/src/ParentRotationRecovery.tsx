import { useEffect, useRef, useState } from 'react'
import { localApi } from './api'
import { parentDraftAuthorityEvent } from './parentDraftAuthority'

/** The limited backend checks the existing PIN; it has no learning or cloud routes. */
export function ParentRotationRecovery({ onContinued }: { onContinued: () => Promise<void> }): React.JSX.Element {
  const [phase, setPhase] = useState<'loading' | 'unlock' | 'review' | 'restart'>('loading')
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(true)
  const generation = useRef(0)
  const running = useRef(false)
  const unlockSource = useRef(Symbol('rotation-recovery-unlock'))
  useEffect(() => {
    const revoke = (event: Event): void => {
      const detail = (event as CustomEvent<{ reason?: string; source?: symbol }>).detail
      if ((detail?.reason === 'unlocked' && detail.source === unlockSource.current) || detail?.reason === 'rotation_complete') return
      generation.current++; running.current = false
      void window.desktop?.rotationRecoveryCancel?.().catch(() => {})
      setPin(''); setBusy(false); setPhase('loading'); setError('家长身份或本地服务已变化，请重新核对。')
    }
    window.addEventListener(parentDraftAuthorityEvent, revoke)
    return () => window.removeEventListener(parentDraftAuthorityEvent, revoke)
  }, [])
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false; generation.current++; running.current = false
      void window.desktop?.rotationRecoveryCancel?.().catch(() => {})
    }
  }, [])
  const current = (version: number): boolean => mounted.current && generation.current === version
  const start = async (): Promise<void> => {
    if (running.current) return
    running.current = true
    const version = ++generation.current
    setBusy(true); setError(null)
    try {
      const status = await localApi.parentStatus()
      if (!current(version)) return
      if (status.configured !== true) throw new Error('ORIGINAL_PIN_REQUIRED')
      setPhase('unlock')
    } catch {
      if (current(version)) setError('无法核对原家长 PIN。请联系维护者；原配置和待更新的密钥均已保留。')
    } finally { if (current(version)) { running.current = false; setBusy(false) } }
  }
  useEffect(() => { void start() }, [])
  const unlock = async (): Promise<void> => {
    if (running.current || !/^\d{4,8}$/.test(pin)) return
    running.current = true
    const version = ++generation.current, value = pin
    setPin(''); setBusy(true); setError(null)
    try {
      await localApi.parentUnlock(value, false, unlockSource.current)
      if (current(version)) setPhase('review')
    } catch {
      if (current(version)) setError('家长身份未确认，请核对 PIN 或稍后重试。')
    } finally { if (current(version)) { running.current = false; setBusy(false) } }
  }
  const proceed = async (): Promise<void> => {
    if (running.current || phase !== 'review') return
    running.current = true
    const version = ++generation.current
    setBusy(true); setError(null)
    try {
      await localApi.continueQwenRotationRecovery()
      if (current(version)) await onContinued()
    } catch (reason) {
      if (current(version)) {
        if (reason instanceof Error && reason.message === 'ROTATION_RECOVERY_RESTART_REQUIRED') {
          setPhase('restart'); setError('无法确认原服务退出，请关闭客户端后重新打开；如仍无法恢复，请联系维护者。')
        } else { setPhase('unlock'); setError('未能确认恢复结果，请重新解锁核对；原配置和待更新的密钥均已保留。') }
      }
    } finally { if (current(version)) { running.current = false; setBusy(false) } }
  }
  return <main className="app-shell parent-recovery" aria-label="家长核对密钥更新"><section className="panel">
    <h1>需要家长核对密钥更新</h1>
    <p>无法确认某个模型是否属于千问，更新已暂停，原资料和待更新的密钥均已保留。</p>
    {phase === 'loading' && <><p role="status">{busy ? '正在核对家长身份设置…' : '尚未核对家长身份设置。'}</p>{!busy && <button className="secondary-button" onClick={() => void start()}>重试核对</button>}</>}
    {phase === 'unlock' && <form onSubmit={event => { event.preventDefault(); void unlock() }}>
      <p>请使用原家长 PIN，不会创建新的家长身份。</p>
      <label>家长 PIN<input type="password" inputMode="numeric" autoComplete="off" maxLength={8} value={pin} onChange={event => setPin(event.target.value)} disabled={busy}/></label>
      <button type="submit" className="primary-button" disabled={busy || !/^\d{4,8}$/.test(pin)}>{busy ? '正在核对…' : '解锁并查看选项'}</button>
    </form>}
    {phase === 'review' && <>
      <h2>仅本次继续使用原配置</h2>
      <p>新密钥尚未更新。本次将保留原配置继续运行；重启应用后会再次尝试更新。请联系维护者核实模型归属。</p>
      <div className="parent-recovery-actions">
        <button className="primary-button" onClick={() => void proceed()} disabled={busy}>{busy ? '正在核对并启动…' : '仅本次使用原配置继续'}</button>
        <button className="secondary-button" onClick={() => { generation.current++; void window.desktop?.rotationRecoveryCancel?.().catch(() => {}); setPhase('unlock'); setError(null) }} disabled={busy}>返回解锁</button>
      </div>
    </>}
    {error && <p role="alert">{error}</p>}
  </section></main>
}
