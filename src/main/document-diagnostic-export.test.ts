import { execFileSync } from 'node:child_process'
import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, it } from 'vitest'
import { DiagnosticRecorder } from './diagnostics'

it('keeps only closed page-state and pending-OCR metadata in the daily log ZIP', async () => {
  const root = await mkdtemp(join(await realpath(tmpdir()), 'hsc-document-export-'))
  const states = ['no_page', 'stabilizing', 'writing', 'occluded', 'page_changed', 'stable']
  try {
    const recorder = new DiagnosticRecorder(join(root, 'diagnostics'))
    for (const frame_state of states) await recorder.record({ source: 'renderer', event: 'document.capture.frame_state',
      details: { frame_state, frame_motion_band: '2_to_4_5', frame_skin_band: 'lt_025',
        frame_content_motion: 'changed', frame_paper_band: 'ge_10', frame_light_band: 'usable',
        pixels: 'PRIVATE_PIXELS', page: 'PRIVATE_PAGE' } })
    await recorder.record({ source: 'renderer', event: 'document.capture.frame_state', details: {
      frame_state: 'PRIVATE_STATE', frame_motion_band: 'PRIVATE_MOTION', frame_skin_band: 'PRIVATE_SKIN',
      frame_content_motion: 'PRIVATE_CONTENT', frame_paper_band: 'PRIVATE_PAPER', frame_light_band: 'PRIVATE_LIGHT',
      contentFingerprint: 'PRIVATE_FINGERPRINT', raw_frame: 'PRIVATE_FRAME',
    } })
    await recorder.record({ source: 'renderer', event: 'document.capture.ocr_pending', details: { latency_ms: 90000, image: 'PRIVATE_IMAGE' } })
    expect(await readFile(recorder.logPath, 'utf8')).not.toContain('PRIVATE_')
    const script = ['from datetime import datetime,timedelta,timezone', 'from io import BytesIO', 'import sys,zipfile',
      'from home_study_agent.daily_diagnostic_bundle import build_daily_diagnostic_bundle', 'now=datetime.now(timezone.utc)',
      'b=build_daily_diagnostic_bundle(sys.argv[1],start_utc=now-timedelta(hours=1),end_utc=now+timedelta(hours=1))',
      'print(zipfile.ZipFile(BytesIO(b.content)).read("events.jsonl").decode())'].join('\n')
    const exported = execFileSync(resolve('.venv/bin/python'), ['-c', script, root], {
      encoding: 'utf8', env: { ...process.env, PYTHONPATH: resolve('services/local_agent/src') }, timeout: 15000,
    })
    expect(exported).not.toContain('PRIVATE_')
    const rows = exported.trim().split('\n').map(line => JSON.parse(line))
    expect(rows.slice(0, 6).map(row => row.details.frame_state)).toEqual(states)
    expect(rows[0].details).toEqual({ frame_state: 'no_page', frame_motion_band: '2_to_4_5', frame_skin_band: 'lt_025',
      frame_content_motion: 'changed', frame_paper_band: 'ge_10', frame_light_band: 'usable' })
    expect(rows[6].details).toEqual({})
    expect(rows[7].details).toEqual({ latency_ms: 90000 })
  } finally { await rm(root, { recursive: true, force: true }) }
})
