#!/usr/bin/env node
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'
import { startElectron, stopOwned, unlockParent, visible, screenshot, root, outputDir, pythonBin,
  owned, diagnosticSecrets, run, captureOwnedFailure, redactHarnessDiagnostic } from './electron-parent-report-delivery-e2e.mjs'

const options = new Map(process.argv.slice(2).reduce((items, value, index, values) => index % 2 ? items : [...items, [value, values[index+1]]], []))
const reused = options.get('--fixture-dir')
if (reused && !basename(reused).startsWith('hsc-report-source-native-')) throw new Error('Only a retained synthetic source fixture may be reused')
const temporaryRoot = reused ?? await mkdtemp(join(tmpdir(), 'hsc-report-source-native-'))
const dataDir = join(temporaryRoot, 'data'), profile = join(temporaryRoot, 'profile'), databasePath = join(dataDir, 'home-study.db')
await mkdir(dataDir, {recursive:true}); await mkdir(profile, {recursive:true}); await mkdir(outputDir, { recursive: true })
const checks = [], screenshots = []
try {
  if (!reused) {
    const seed = await run(pythonBin, [join(root, 'scripts/seed-report-source-recovery.py'), '--database-path', databasePath,
    '--receipt-path', join(outputDir, 'seed.json')], { cwd: root,
    env: parentReportDeliveryEnvironment({ root, pythonBin, dataDir, databasePath }) })
    await writeFile(join(outputDir, 'seed.log'), redactHarnessDiagnostic(seed.stdout + seed.stderr, [...diagnosticSecrets]))
  }
  for (const round of reused ? [2] : [1, 2]) {
    const app = await startElectron(profile, dataDir, databasePath)
    await unlockParent(app.cdp, false)
    await app.cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="选择孩子"] button')).find(b => b.textContent.includes('初三'))?.click()`)
    await visible(app.cdp, '历史报告需要核对')
    await visible(app.cdp, '缺少原始反思记录')
    for (const width of [1100, 390]) {
      await app.cdp.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false })
      await app.cdp.evaluate(`document.querySelector('[aria-label="历史报告来源问题"]')?.scrollIntoView({block:'center'})`)
      await new Promise(resolve => setTimeout(resolve, 200))
      const bounds = await app.cdp.evaluate(`(() => { const e=document.querySelector('[aria-label="历史报告来源问题"]'); const r=e.getBoundingClientRect(); return {left:r.left,right:r.right,viewport:innerWidth,scroll:e.scrollWidth,width:e.clientWidth} })()`)
      if (bounds.left < -1 || bounds.right > width + 1 || bounds.scroll > bounds.width + 1) throw new Error(`Issue overflow: ${JSON.stringify(bounds)}`)
      const name = `${round === 1 ? 'startup' : 'restart'}-${width}.png`
      await screenshot(app.cdp, join(outputDir, name)); screenshots.push(name)
    }
    checks.push(`native_startup_${round}_survives_invalid_source_and_reads_persisted_issue`)
    const periods = await app.cdp.evaluate(`Array.from(document.querySelectorAll('.period-list article')).map(e=>e.innerText)`)
    if (periods.length < 2 || periods.some(text => !text.includes('1次会话') || text.includes('2次会话'))) throw new Error('Unavailable daily report still contributes to native period coverage')
    checks.push(`period_coverage_${round}_excludes_missing_reflection`)
    const issueSource = await app.cdp.evaluate(`document.querySelector('[aria-label="历史报告来源问题"] [data-work-object]')?.getAttribute('data-work-object')`)
    await app.cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="历史报告来源问题"] button')).find(b=>b.textContent.includes('查看保留的原报告'))?.click()`)
    await new Promise(resolve => setTimeout(resolve, 350))
    const retained = await app.cdp.evaluate(`(() => {const e=document.querySelector('.report-delivery-preview'); return {exists:!!e, revoked:e?.innerText.includes('旧报告已停止使用'), id:e?.getAttribute('data-work-object')} })()`)
    if (!retained.exists || !retained.revoked || !retained.id) throw new Error('Retained source report was not readable as revoked')
    const original = await run(pythonBin, ['-c', 'import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps(c.execute("SELECT report_id,status FROM parent_report_snapshot WHERE snapshot_id=?",(sys.argv[2],)).fetchone()))', databasePath, retained.id])
    if (JSON.parse(original.stdout)[0] !== issueSource || JSON.parse(original.stdout)[1] !== 'revoked') throw new Error('The displayed report did not match the exact unavailable source')
    checks.push(`retained_report_${round}_is_readable_without_reapproval`)
    await app.cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="选择孩子"] button')).find(b => b.textContent.includes('四年级'))?.click()`)
    await new Promise(resolve => setTimeout(resolve, 1000))
    const other = await app.cdp.evaluate(`({ selected:Array.from(document.querySelectorAll('[aria-label="选择孩子"] button.is-active')).map(b=>b.textContent),issues:!!document.querySelector('[aria-label="历史报告来源问题"]') })`)
    if (other.issues || !other.selected.some(text => text.includes('四年级'))) throw new Error(`Child scope check failed: ${JSON.stringify(other)}`)
    checks.push(`other_child_${round}_does_not_inherit_issue`)
    await writeFile(join(outputDir, `native-${round}.log`), redactHarnessDiagnostic(app.logs(), [...diagnosticSecrets]))
    await stopOwned(app)
  }
  const facts = await run(pythonBin, ['-c', `import sqlite3,json,sys
c=sqlite3.connect(sys.argv[1])
print(json.dumps({"reports":c.execute("SELECT COUNT(*) FROM daily_report").fetchone()[0],"issues":c.execute("SELECT COUNT(*) FROM audit_event WHERE action='parent_report_source_issue'").fetchone()[0],"completed_learning":c.execute("SELECT COUNT(*) FROM plan_item WHERE status='completed'").fetchone()[0],"deliveries":c.execute("SELECT COUNT(*) FROM report_delivery_outbox").fetchone()[0]}))`, databasePath])
  const state = JSON.parse(facts.stdout)
  if (state.reports !== 3 || state.completed_learning || state.deliveries) throw new Error(`Unexpected facts: ${JSON.stringify(state)}`)
  checks.push('original_reports_preserved_no_learning_or_send_fabricated')
  await writeFile(join(outputDir, 'result.json'), JSON.stringify({ status:'passed', checks, state, screenshots,
    synthetic_only:true, external_delivery:false, cloud:false, windows_tested:false, retained_fixture:temporaryRoot }, null, 2))
  console.log(JSON.stringify({status:'passed', checks:checks.length, outputDir}))
} catch (error) {
  await captureOwnedFailure({ outputDir, temporaryRoot, apps:[...owned.values()], error, secrets:[...diagnosticSecrets], smtpMessages:0 })
  const latest = [...owned.values()].at(-1)
  if (latest?.cdp) await screenshot(latest.cdp, join(outputDir, 'failure.png')).catch(() => undefined)
  throw error
} finally {
  for (const app of [...owned.values()]) await stopOwned(app)
}
