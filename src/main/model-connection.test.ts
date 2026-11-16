import { describe, expect, it } from 'vitest'
import { modelConnectionPayload } from './model-connection'

describe('modelConnectionPayload', () => {
  it('uses a short non-thinking probe for Qwen', () => {
    expect(modelConnectionPayload({ provider: 'qwen', model: 'qwen3.8-flash' })).toMatchObject({
      model: 'qwen3.8-flash',
      max_tokens: 16,
      enable_thinking: false,
      stream: false,
    })
  })

  it('does not send a Qwen-only option to DeepSeek', () => {
    expect(modelConnectionPayload({ provider: 'deepseek', model: 'deepseek-chat' })).not.toHaveProperty('enable_thinking')
  })
})
