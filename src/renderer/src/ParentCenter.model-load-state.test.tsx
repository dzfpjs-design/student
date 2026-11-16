import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ModelSettingsPanel } from './ParentCenter'
import type { BootstrapState } from './types'

const initial: BootstrapState['model'] = {
  provider: 'qwen', configured: false, model: 'public-test-model', voice_intent_model: 'public-intent-model',
  fallback_configured: false, fallback_provider: null, fallback_model: null,
}
type Diagnostic = NonNullable<DesktopModelSummary['loadDiagnostic']>
function summary(loadDiagnostic: Diagnostic): DesktopModelSummary {
  return { configured: loadDiagnostic === 'loaded', loadDiagnostic, provider: 'qwen', baseUrl: 'https://public.example/v1', model: 'public-test-model', voiceIntentModel: 'public-intent-model', fallbackConfigured: false }
}
function desktop(value: Partial<NonNullable<Window['desktop']>>) {
  window.desktop = value as NonNullable<Window['desktop']>
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
afterEach(() => { cleanup(); window.desktop = undefined; vi.restoreAllMocks() })

describe('ModelSettingsPanel desktop loading diagnostics', () => {
  const messages: Array<[Diagnostic, string]> = [
    ['loaded', '本机保存的模型配置可读取'],
    ['not_configured', '本机尚未保存模型配置'],
    ['read_failed', '不能确认是否已配置'],
    ['parse_failed', '本机已有模型配置，但本次未能读取'],
    ['secure_storage_unavailable', '系统安全存储暂不可用'],
    ['config_unreadable', '本机已有模型配置，但本次未能读取'],
  ]
  for (const [reason, message] of messages) it(`shows finite ${reason} status without making a credential change`, async () => {
    const getModelSettings = vi.fn().mockResolvedValue(summary(reason))
    const saveModelSettings = vi.fn(); const clearModelSettings = vi.fn(); const testModelSettings = vi.fn()
    desktop({ getModelSettings, saveModelSettings, clearModelSettings, testModelSettings })
    render(<ModelSettingsPanel initial={initial} />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(message))
    expect(getModelSettings).toHaveBeenCalledTimes(1)
    expect(saveModelSettings).not.toHaveBeenCalled(); expect(clearModelSettings).not.toHaveBeenCalled(); expect(testModelSettings).not.toHaveBeenCalled()
    expect(screen.getByLabelText('学习辅导模型')).toHaveValue(initial.model)
    expect(screen.getByLabelText('API Key')).toHaveValue('')
  })

  it('re-reads only after an explicit action and preserves edits while that read is pending', async () => {
    const pending = deferred<DesktopModelSummary>()
    const getModelSettings = vi.fn().mockResolvedValueOnce(summary('read_failed')).mockReturnValueOnce(pending.promise)
    const saveModelSettings = vi.fn(); const clearModelSettings = vi.fn(); const testModelSettings = vi.fn()
    desktop({ getModelSettings, saveModelSettings, clearModelSettings, testModelSettings })
    render(<ModelSettingsPanel initial={initial} />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('不能确认是否已配置'))
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'synthetic-parent-edit' } })
    fireEvent.click(screen.getByRole('button', { name: '重新读取状态' }))
    expect(getModelSettings).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('status')).toHaveTextContent('正在读取')
    expect(screen.getByRole('button', { name: '重新读取状态' })).toBeDisabled()
    await act(async () => { pending.resolve(summary('loaded')) })
    expect(screen.getByRole('status')).toHaveTextContent('本机保存的模型配置可读取')
    expect(screen.getByLabelText('API Key')).toHaveValue('synthetic-parent-edit')
    expect(saveModelSettings).not.toHaveBeenCalled(); expect(clearModelSettings).not.toHaveBeenCalled(); expect(testModelSettings).not.toHaveBeenCalled()
  })

  it('keeps the newer read when a previous StrictMode effect resolves late', async () => {
    const old = deferred<DesktopModelSummary>()
    const getModelSettings = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(summary('loaded'))
    desktop({ getModelSettings })
    render(<StrictMode><ModelSettingsPanel initial={initial} /></StrictMode>)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('本机保存的模型配置可读取'))
    expect(getModelSettings).toHaveBeenCalledTimes(2)
    await act(async () => { old.resolve(summary('config_unreadable')) })
    expect(screen.getByRole('status')).toHaveTextContent('本机保存的模型配置可读取')
    expect(screen.getByRole('status')).not.toHaveTextContent('本次未能读取')
  })

  for (const mode of ['unknown', 'missing', 'throw'] as const) it(`does not expose an ${mode} diagnostic or mistake it for an absent configuration`, async () => {
    const privateText = 'SYNTHETIC_PRIVATE_ERROR_CIPHER_PATH'
    const getModelSettings = mode === 'throw' ? vi.fn().mockRejectedValue(Error(privateText)) : vi.fn().mockResolvedValue({ ...summary('loaded'), loadDiagnostic: mode === 'unknown' ? privateText : undefined })
    desktop({ getModelSettings })
    render(<ModelSettingsPanel initial={initial} />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('暂时无法确认'))
    expect(screen.getByRole('status')).not.toHaveTextContent('尚未保存')
    expect(document.body.textContent).not.toContain(privateText)
  })

  it('keeps explicit full-credential saving available after an unreadable result without auto-saving', async () => {
    const saveModelSettings = vi.fn().mockResolvedValue(summary('loaded'))
    const restart = vi.fn()
    desktop({ getModelSettings: vi.fn().mockResolvedValue(summary('config_unreadable')), saveModelSettings })
    render(<ModelSettingsPanel initial={initial} onRuntimeRestart={restart} />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('重新保存会替换已有配置'))
    expect(saveModelSettings).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'synthetic-new-parent-key' } })
    expect(screen.getByRole('button', { name: '保存并启用' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '保存并启用' }))
    await waitFor(() => expect(saveModelSettings).toHaveBeenCalledTimes(1))
    expect(saveModelSettings).toHaveBeenCalledWith({ provider: 'qwen', apiKey: 'synthetic-new-parent-key', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: initial.model, voiceIntentModel: initial.voice_intent_model })
    expect(restart).toHaveBeenCalledTimes(1)
  })
})
