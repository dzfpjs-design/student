import('../probe-qwen-realtime-electron.mjs').catch((error) => {
  console.error(JSON.stringify({ ok: false, stage: 'startup', error: error?.message || String(error) }))
  process.exitCode = 1
})
