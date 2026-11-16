import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ModelOperationsPanel } from './ParentCenter'
import type { BootstrapState, ModelPricing } from './types'

const initial: BootstrapState['model'] = {
  provider: 'qwen', model: 'qwen-plus', voice_intent_model: 'qwen-plus', configured: true,
  fallback_provider: null, fallback_model: null, fallback_configured: false,
}
const price: ModelPricing = {
  provider: 'qwen', model: 'qwen-plus', input_price_cny_per_million: 3,
  output_price_cny_per_million: 9, currency: 'CNY', source: 'parent_configured',
  updated_at: '2026-09-09T00:00:00Z',
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function edit(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

beforeEach(() => {
  vi.spyOn(localApi, 'modelCalls').mockResolvedValue([])
  vi.spyOn(localApi, 'modelRuntimePolicy').mockRejectedValue(new Error('unavailable'))
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('parent pricing drafts', () => {
  it('keeps an edited value when initial pricing arrives and fills only untouched values', async () => {
    const pending = deferred<ModelPricing[]>()
    vi.spyOn(localApi, 'modelPricing').mockReturnValue(pending.promise)
    const save = vi.spyOn(localApi, 'saveModelPricing').mockResolvedValue({ ...price, input_price_cny_per_million: 4 })
    render(<ModelOperationsPanel childId="primary" initialModel={initial} />)
    edit('输入单价', '4')
    await act(async () => pending.resolve([price]))
    expect(screen.getByLabelText('输入单价')).toHaveValue(4)
    expect(screen.getByLabelText('输出单价')).toHaveValue(9)
    fireEvent.click(screen.getByRole('button', { name: '保存单价依据' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ provider: 'qwen', model: 'qwen-plus',
      input_price_cny_per_million: 4, output_price_cny_per_million: 9 }))
  })

  it('refreshes records without discarding unsaved or deliberately empty input', async () => {
    vi.spyOn(localApi, 'modelPricing').mockImplementation(async () => [{ ...price }])
    render(<ModelOperationsPanel childId="primary" initialModel={initial} />)
    await waitFor(() => expect(screen.getByLabelText('输入单价')).toHaveValue(3))
    edit('输入单价', '')
    edit('输出单价', '10')
    fireEvent.click(screen.getByTitle('刷新模型记录'))
    await waitFor(() => expect(screen.getByTitle('刷新模型记录')).toBeEnabled())
    expect(screen.getByLabelText('输入单价')).toHaveValue(null)
    expect(screen.getByLabelText('输出单价')).toHaveValue(10)
    expect(screen.getByRole('button', { name: '保存单价依据' })).toBeDisabled()
  })

  it('does not overwrite newer edits with a delayed save receipt', async () => {
    vi.spyOn(localApi, 'modelPricing').mockResolvedValue([price])
    const pending = deferred<ModelPricing>()
    vi.spyOn(localApi, 'saveModelPricing').mockReturnValue(pending.promise)
    render(<ModelOperationsPanel childId="primary" initialModel={initial} />)
    await waitFor(() => expect(screen.getByLabelText('输入单价')).toHaveValue(3))
    edit('输入单价', '4')
    fireEvent.click(screen.getByRole('button', { name: '保存单价依据' }))
    edit('输入单价', '5')
    await act(async () => pending.resolve({ ...price, input_price_cny_per_million: 4 }))
    expect(screen.getByLabelText('输入单价')).toHaveValue(5)
    expect(screen.getByText('上一版单价依据已保存；当前修改尚未保存。')).toBeInTheDocument()
  })

  it('does not replace a saved receipt with a read that started before saving', async () => {
    const pending = deferred<ModelPricing[]>()
    vi.spyOn(localApi, 'modelPricing').mockReturnValue(pending.promise)
    vi.spyOn(localApi, 'saveModelPricing').mockResolvedValue({ ...price, input_price_cny_per_million: 4,
      output_price_cny_per_million: 10 })
    render(<ModelOperationsPanel childId="primary" initialModel={initial} />)
    edit('输入单价', '4'); edit('输出单价', '10')
    fireEvent.click(screen.getByRole('button', { name: '保存单价依据' }))
    await screen.findByText('单价依据已保存。它只用于估算之后的调用，不会改写历史费用。')
    await act(async () => pending.resolve([price]))
    expect(screen.getByLabelText('输入单价')).toHaveValue(4)
    expect(screen.getByLabelText('输出单价')).toHaveValue(10)
  })

  it('keeps the failed draft while a retry is available and does not carry it to another model', async () => {
    vi.spyOn(localApi, 'modelPricing').mockResolvedValue([price])
    vi.spyOn(localApi, 'saveModelPricing').mockRejectedValue(new Error('offline'))
    render(<ModelOperationsPanel childId="primary" initialModel={initial} />)
    await waitFor(() => expect(screen.getByLabelText('输入单价')).toHaveValue(3))
    edit('输入单价', '4')
    fireEvent.click(screen.getByRole('button', { name: '保存单价依据' }))
    await screen.findByText('单价依据没有保存。')
    expect(screen.getByLabelText('输入单价')).toHaveValue(4)
    edit('计费模型', 'another-model')
    expect(screen.getByLabelText('输入单价')).toHaveValue(null)
    expect(screen.getByLabelText('输出单价')).toHaveValue(null)
  })

  it('matches the saved receipt when the entered model has surrounding whitespace', async () => {
    vi.spyOn(localApi, 'modelPricing').mockResolvedValue([price])
    const save = vi.spyOn(localApi, 'saveModelPricing').mockResolvedValue({ ...price, input_price_cny_per_million: 4 })
    render(<ModelOperationsPanel childId="primary" initialModel={initial} />)
    await waitFor(() => expect(screen.getByLabelText('输入单价')).toHaveValue(3))
    edit('计费模型', ' qwen-plus ')
    edit('输入单价', '4'); edit('输出单价', '9')
    fireEvent.click(screen.getByRole('button', { name: '保存单价依据' }))
    await screen.findByText('单价依据已保存。它只用于估算之后的调用，不会改写历史费用。')
    expect(save).toHaveBeenCalledWith({ provider: 'qwen', model: 'qwen-plus', input_price_cny_per_million: 4,
      output_price_cny_per_million: 9 })
    expect(screen.getByLabelText('输入单价')).toHaveValue(4)
    expect(screen.getByLabelText('输出单价')).toHaveValue(9)
  })
})
