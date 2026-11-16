// Keep acceptance launchers from inheriting provider credentials from a
// developer or CI shell. Each harness restores only the explicitly supplied
// local runtime paths after this boundary.
const INHERITED_RUNTIME_PATHS = new Set(['HSC_LOCAL_AGENT_PYTHON', 'HSC_LOCAL_MODEL_DIR'])
const SECRET_NAMES = new Set([
  'DASHSCOPE_API_KEY', 'QWEN_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY',
  'ANTHROPIC_API_KEY', 'AZURE_OPENAI_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY',
  'MISTRAL_API_KEY', 'COHERE_API_KEY', 'GROQ_API_KEY', 'TOGETHER_API_KEY',
  'SILICONFLOW_API_KEY', 'MOONSHOT_API_KEY', 'ZHIPUAI_API_KEY', 'BAIDU_API_KEY',
  'VOLCENGINE_API_KEY', 'ARK_API_KEY', 'MINIMAX_API_KEY', 'XAI_API_KEY',
  'PERPLEXITY_API_KEY', 'OPENROUTER_API_KEY', 'REPLICATE_API_TOKEN',
  'FIREWORKS_API_KEY', 'NVIDIA_API_KEY', 'HF_TOKEN', 'HUGGINGFACEHUB_API_TOKEN',
  'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
  'AZURE_CLIENT_SECRET', 'GOOGLE_APPLICATION_CREDENTIALS',
  'TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY',
  'ALIYUN_ACCESS_KEY_ID', 'ALIYUN_ACCESS_KEY_SECRET', 'COSYVOICE_API_KEY'
])

export function sanitizedHarnessEnvironment(environment) {
  const sanitized = { ...environment }
  for (const key of Object.keys(sanitized)) {
    if ((key.startsWith('HSC_') && !INHERITED_RUNTIME_PATHS.has(key)) || SECRET_NAMES.has(key)) delete sanitized[key]
  }
  // Each launcher adds its own profile/database and any explicitly authorized
  // provider configuration after this boundary, never from an inherited shell.
  return sanitized
}
