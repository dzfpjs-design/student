// Geometry regression against the shipped CSS in Chromium, without family data.
const { app, BrowserWindow } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')

app.whenReady().then(async () => {
  const css = fs.readFileSync(path.join(__dirname, '../src/renderer/src/styles.css'), 'utf8')
  const source = fs.readFileSync(path.join(__dirname, '../src/renderer/src/SessionWorkspace.tsx'), 'utf8')
  assert.match(source, /<div className="agent-learning-content" hidden=\{!learningSurfaceVisible\}>/)
  const win = new BrowserWindow({ show: false, width: 1280, height: 900, webPreferences: { backgroundThrottling: false } })
  try {
    await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html><style>${css}</style>
      <div class="child-voice-only"><aside class="agent-command-panel">
      <div class="companion-avatar is-compact">知</div><div class="agent-dialogue"><strong>学习伙伴</strong><span>我们一起学习。</span></div>
      <div class="agent-learning-content"><div class="agent-objective"><strong>学习目标</strong><span>请复习初三地理第一课并检查今天需要掌握的知识点。</span><small>目标标准：能够理解和解释</small></div></div>
      </aside></div>`))
    for (const width of [1280, 600]) {
      win.setContentSize(width, 900)
      const result = await win.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => {
        const card=document.querySelector('.agent-command-panel').getBoundingClientRect();
        const goal=document.querySelector('.agent-objective').getBoundingClientRect();
        const summary=document.querySelector('.agent-objective span').getBoundingClientRect();
        resolve({card:card.width, goal:goal.width, summary:summary.width, right:goal.right, cardRight:card.right});
      }))`)
      assert.ok(result.goal > result.card * 0.6, `${width}: objective too narrow: ${JSON.stringify(result)}`)
      assert.ok(result.summary > 100 && result.right <= result.cardRight, `${width}: unreadable/overflow: ${JSON.stringify(result)}`)
    }
    const hidden = await win.webContents.executeJavaScript(`(() => {const x=document.querySelector('.agent-learning-content'); x.hidden=true; return getComputedStyle(x).display})()`)
    assert.equal(hidden, 'none', 'hidden learning content must remain hidden')
    console.log('PASS: objective readable at desktop and narrow widths; hidden state preserved')
    app.exit(0)
  } catch (error) {
    console.error(error.message)
    app.exit(1)
  } finally { win.destroy() }
}).catch(error => { console.error(error.message); app.exit(1) })
