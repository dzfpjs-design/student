export function modelConnectionPayload(config: { provider: 'qwen' | 'deepseek'; model: string }): Record<string, unknown> {
  return {
    model: config.model,
    messages: [{ role: 'user', content: '只回复OK' }],
    temperature: 0,
    max_tokens: 16,
    stream: false,
    ...(config.provider === 'qwen' ? { enable_thinking: false } : {}),
  }
}
