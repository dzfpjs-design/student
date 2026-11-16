import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ParentRotationRecovery } from './ParentRotationRecovery'
import { StrictMode } from 'react'
import { changeParentDraftAuthority } from './parentDraftAuthority'
import { act } from '@testing-library/react'

afterEach(() => { cleanup(); delete window.desktop; vi.restoreAllMocks() })

it('cancels pending recovery when the page is left, even if PIN verification returns later', async () => {
  let resolveUnlock!: () => void
  const unlock = new Promise<void>(resolve => { resolveUnlock = resolve })
  const cancel = vi.fn().mockResolvedValue(undefined)
  window.desktop = { rotationRecoveryCancel: cancel } as unknown as NonNullable<Window['desktop']>
  vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  vi.spyOn(localApi, 'parentUnlock').mockReturnValue(unlock)
  const proceed = vi.spyOn(localApi, 'continueQwenRotationRecovery')
  const view = render(<ParentRotationRecovery onContinued={vi.fn()} />)
  fireEvent.change(await screen.findByLabelText('家长 PIN'), { target: { value: '1234' } })
  fireEvent.click(screen.getByRole('button', { name: '解锁并查看选项' }))
  view.unmount(); resolveUnlock()
  await waitFor(() => expect(cancel).toHaveBeenCalledOnce())
  expect(proceed).not.toHaveBeenCalled()
})

it.each(['locked', 'unlocked'] as const)('cancels an in-flight continuation when parent authority changes to %s while mounted', async reason => {
  let finish!: () => void
  const pending = new Promise<void>(resolve => { finish = resolve })
  const cancel = vi.fn().mockResolvedValue(undefined), onContinued = vi.fn()
  window.desktop = { rotationRecoveryCancel: cancel } as unknown as NonNullable<Window['desktop']>
  vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
  vi.spyOn(localApi, 'continueQwenRotationRecovery').mockReturnValue(pending)
  render(<ParentRotationRecovery onContinued={onContinued} />)
  fireEvent.change(await screen.findByLabelText('家长 PIN'), { target: { value: '1234' } })
  fireEvent.click(screen.getByRole('button', { name: '解锁并查看选项' }))
  fireEvent.click(await screen.findByRole('button', { name: '仅本次使用原配置继续' }))
  act(() => changeParentDraftAuthority(reason))
  expect(cancel).toHaveBeenCalledOnce()
  await act(async () => { finish(); await pending })
  expect(onContinued).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('家长身份或本地服务已变化')
})

it('retries the initial status check after StrictMode effect replay and ignores the stale reply', async () => {
  let resolveFirst!: (value: Awaited<ReturnType<typeof localApi.parentStatus>>) => void
  const first = new Promise<Awaited<ReturnType<typeof localApi.parentStatus>>>(resolve => { resolveFirst = resolve })
  vi.spyOn(localApi, 'parentStatus').mockReturnValueOnce(first).mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  render(<StrictMode><ParentRotationRecovery onContinued={vi.fn()} /></StrictMode>)
  expect(await screen.findByLabelText('家长 PIN')).toBeInTheDocument()
  resolveFirst({ configured: false } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  await waitFor(() => expect(screen.getByLabelText('家长 PIN')).toBeInTheDocument())
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('does not offer a bypass when no original parent PIN exists', async () => {
  vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: false } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  const proceed = vi.spyOn(localApi, 'continueQwenRotationRecovery')
  render(<ParentRotationRecovery onContinued={vi.fn()} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('无法核对原家长 PIN')
  expect(screen.queryByRole('button', { name: '仅本次使用原配置继续' })).not.toBeInTheDocument()
  expect(proceed).not.toHaveBeenCalled()
})

it('requires the original PIN and explicit acknowledgement before continuing old configuration', async () => {
  vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  const unlock = vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
  const proceed = vi.spyOn(localApi, 'continueQwenRotationRecovery').mockResolvedValue()
  const onContinued = vi.fn().mockResolvedValue(undefined)
  render(<ParentRotationRecovery onContinued={onContinued} />)
  const input = await screen.findByLabelText('家长 PIN')
  expect(screen.queryByRole('button', { name: '仅本次使用原配置继续' })).not.toBeInTheDocument()
  fireEvent.change(input, { target: { value: '1234' } })
  fireEvent.click(screen.getByRole('button', { name: '解锁并查看选项' }))
  expect(await screen.findByText(/新密钥尚未更新/)).toBeInTheDocument()
  expect(unlock).toHaveBeenCalledExactlyOnceWith('1234', false, expect.any(Symbol))
  expect(proceed).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '仅本次使用原配置继续' }))
  await waitFor(() => expect(onContinued).toHaveBeenCalledOnce())
  expect(proceed).toHaveBeenCalledOnce()
})

it('never continues when PIN verification fails and does not display raw errors', async () => {
  vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  vi.spyOn(localApi, 'parentUnlock').mockRejectedValue(new Error('PRIVATE_PROVIDER_TEXT'))
  const proceed = vi.spyOn(localApi, 'continueQwenRotationRecovery')
  render(<ParentRotationRecovery onContinued={vi.fn()} />)
  fireEvent.change(await screen.findByLabelText('家长 PIN'), { target: { value: '1234' } })
  fireEvent.click(screen.getByRole('button', { name: '解锁并查看选项' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('家长身份未确认')
  expect(screen.queryByText(/PRIVATE_PROVIDER_TEXT/)).not.toBeInTheDocument()
  expect(proceed).not.toHaveBeenCalled()
})

it('requires fresh parent identity after an uncertain continuation', async () => {
  vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
  vi.spyOn(localApi, 'continueQwenRotationRecovery').mockRejectedValue(new Error('UNKNOWN'))
  const onContinued = vi.fn()
  render(<ParentRotationRecovery onContinued={onContinued} />)
  fireEvent.change(await screen.findByLabelText('家长 PIN'), { target: { value: '1234' } })
  fireEvent.click(screen.getByRole('button', { name: '解锁并查看选项' }))
  fireEvent.click(await screen.findByRole('button', { name: '仅本次使用原配置继续' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('未能确认恢复结果')
  expect(screen.queryByRole('button', { name: '仅本次使用原配置继续' })).not.toBeInTheDocument()
  expect(onContinued).not.toHaveBeenCalled()
})

it('requires a client restart when stopping the old process is unconfirmed', async () => {
  vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof localApi.parentStatus>>)
  vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
  vi.spyOn(localApi, 'continueQwenRotationRecovery').mockRejectedValue(new Error('ROTATION_RECOVERY_RESTART_REQUIRED'))
  render(<ParentRotationRecovery onContinued={vi.fn()} />)
  fireEvent.change(await screen.findByLabelText('家长 PIN'), { target: { value: '1234' } })
  fireEvent.click(screen.getByRole('button', { name: '解锁并查看选项' }))
  fireEvent.click(await screen.findByRole('button', { name: '仅本次使用原配置继续' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('关闭客户端后重新打开')
  expect(screen.queryByLabelText('家长 PIN')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '重试核对' })).not.toBeInTheDocument()
})
