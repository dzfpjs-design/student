import { createServer } from 'node:net'

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

async function eventually(read, label, timeoutMs = 70_000) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    try { const value = await read(); if (value) return value } catch (error) { last = error }
    await sleep(150)
  }
  throw new Error(`${label} timed out${last ? `: ${last.message}` : ''}`)
}

export function createInstallationSmtpSink({ username, password }) {
  const messages = []; let authSuccesses = 0
  const server = createServer((socket) => {
    socket.setEncoding('utf8'); socket.write('220 hsc-loopback ESMTP\r\n')
    let buffer = '', data = false, body = '', authState = '', authUser = ''
    const acceptCredentials = (user, candidate) => {
      if (user === username && candidate === password) { authSuccesses += 1; socket.write('235 2.7.0 authenticated\r\n') }
      else socket.write('535 5.7.8 invalid credentials\r\n')
    }
    socket.on('data', (chunk) => {
      buffer += chunk
      while (buffer.includes('\n')) {
        const index = buffer.indexOf('\n'); const line = buffer.slice(0, index + 1).replace(/\r?\n$/, ''); buffer = buffer.slice(index + 1)
        if (data) {
          if (line === '.') { messages.push(body); body = ''; data = false; socket.write('250 2.0.0 accepted\r\n') }
          else body += `${line}\n`
          continue
        }
        if (authState === 'plain') { const values = Buffer.from(line, 'base64').toString().split('\0'); authState = ''; acceptCredentials(values.at(-2) ?? '', values.at(-1) ?? ''); continue }
        if (authState === 'login-user') { authUser = Buffer.from(line, 'base64').toString(); authState = 'login-pass'; socket.write('334 UGFzc3dvcmQ6\r\n'); continue }
        if (authState === 'login-pass') { authState = ''; acceptCredentials(authUser, Buffer.from(line, 'base64').toString()); continue }
        const [command, ...rest] = line.split(' '); const upper = command.toUpperCase()
        if (upper === 'EHLO' || upper === 'HELO') socket.write('250-hsc-loopback\r\n250 AUTH PLAIN LOGIN\r\n')
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN' && rest[1]) { const values = Buffer.from(rest[1], 'base64').toString().split('\0'); acceptCredentials(values.at(-2) ?? '', values.at(-1) ?? '') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN') { authState = 'plain'; socket.write('334 \r\n') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'LOGIN') { authState = 'login-user'; socket.write('334 VXNlcm5hbWU6\r\n') }
        else if (upper === 'MAIL' || upper === 'RCPT' || upper === 'RSET' || upper === 'NOOP') socket.write('250 2.0.0 ok\r\n')
        else if (upper === 'DATA') { data = true; socket.write('354 end with <CRLF>.<CRLF>\r\n') }
        else if (upper === 'QUIT') { socket.write('221 2.0.0 bye\r\n'); socket.end() }
        else socket.write('250 2.0.0 ok\r\n')
      }
    })
  })
  return { server, messages, authSuccesses: () => authSuccesses }
}

async function setField(cdp, label, value) {
  await cdp.evaluate(`(() => { const node=document.querySelector('[aria-label=${JSON.stringify(label)}]'); if (!node) throw new Error('missing field: ${label}'); const proto=node instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto,'value').set.call(node,${JSON.stringify(value)}); node.dispatchEvent(new Event('input',{bubbles:true})); node.dispatchEvent(new Event('change',{bubbles:true})); })()`)
}

async function click(cdp, text) {
  await eventually(() => cdp.evaluate(`Array.from(document.querySelectorAll('button')).some((item) => !item.disabled && item.textContent?.includes(${JSON.stringify(text)}))`), `clickable: ${text}`)
  await cdp.evaluate(`Array.from(document.querySelectorAll('button')).find((item) => !item.disabled && item.textContent?.includes(${JSON.stringify(text)}))?.click()`)
}

export async function startInstallationSmtpSink(sink) {
  await new Promise((resolve, reject) => sink.server.once('error', reject).listen(0, '127.0.0.1', resolve))
  return sink.server.address().port
}

export async function stopInstallationSmtpSink(sink) {
  await new Promise((resolve) => sink.server.close(() => resolve()))
}

export async function completeSyntheticInstallation({ cdp, sink, port, username, password, recipient, pin = '2468' }) {
  const text = await eventually(async () => {
    const current = await cdp.evaluate('document.body.innerText')
    const childSurface = await cdp.evaluate(`Boolean(document.querySelector('.child-home-surface'))`)
    return current.includes('先完成家长与日报配置') || childSurface ? current : null
  }, 'installation or child home surface')
  if (!text.includes('先完成家长与日报配置')) return false
  const beforeMessages = sink.messages.length, beforeAuth = sink.authSuccesses()
  await setField(cdp, '初始化家长PIN', pin); await setField(cdp, '再次输入家长PIN', pin); await click(cdp, '保存 PIN')
  await eventually(() => cdp.evaluate(`document.body.innerText.includes('配置日报 SMTP')`), 'SMTP setup')
  await setField(cdp, '初始化SMTP主机', '127.0.0.1'); await setField(cdp, '初始化SMTP加密', 'none'); await setField(cdp, '初始化SMTP端口', String(port))
  await setField(cdp, '初始化SMTP用户名', username); await setField(cdp, '初始化SMTP密码', password); await setField(cdp, '初始化SMTP发件地址', 'acceptance@local.test'); await click(cdp, '保存 SMTP')
  await eventually(() => cdp.evaluate(`document.body.innerText.includes('设置每日报告收件人')`), 'recipient setup')
  await setField(cdp, '初始化收件邮箱', recipient); await click(cdp, '保存收件人')
  await eventually(() => cdp.evaluate(`document.body.innerText.includes('发送固定测试邮件')`), 'delivery verification setup')
  await click(cdp, '发送并验证')
  await eventually(() => sink.messages.length === beforeMessages + 2 && sink.authSuccesses() === beforeAuth + 2, 'two-child installation SMTP verification', 30_000)
  await eventually(() => cdp.evaluate(`Boolean(document.querySelector('.child-home-surface'))`), 'child home after setup')
  return true
}
