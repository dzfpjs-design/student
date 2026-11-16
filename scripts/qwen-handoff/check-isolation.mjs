import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

// All probes use synthetic files. Never open a family database as a canary.
const root = realpathSync(mkdtempSync(join(tmpdir(), 'hsc-qwen-isolation-')));
const work = join(root, 'work');
mkdirSync(work);
mkdirSync(join(work, 'home'));
mkdirSync(join(work, 'tmp'));
const outside = join(root, 'outside.txt');
writeFileSync(outside, 'synthetic outside boundary');
const node = realpathSync(process.execPath);
const nodeRoot = resolve(dirname(node), '..');
const qwenRoot = realpathSync(join(homedir(), '.local/share/hsc-qwen-code'));
const profile = join(dirname(fileURLToPath(import.meta.url)), 'worker.sb');
const env = { HOME: join(work, 'home'), TMPDIR: join(work, 'tmp'),
  PATH: `${dirname(node)}:/usr/bin:/bin`, LANG: 'en_US.UTF-8' };
const prefix = ['-D', `NODE_ROOT=${nodeRoot}`, '-D', `QWEN_ROOT=${qwenRoot}`,
  '-D', `WORK_ROOT=${work}`, '-f', profile];
function run(args) {
  const r = spawnSync('/usr/bin/sandbox-exec', [...prefix, node, ...args],
    { cwd: work, env, encoding: 'utf8', timeout: 20000, maxBuffer: 256000 });
  assert.equal(r.error, undefined, r.error?.message);
  assert.equal(r.status, 0, `signal=${r.signal}; ${r.stderr.slice(-2000)}`);
  return r.stdout;
}
try {
  const script = `
    const fs = require('node:fs');
    const assert = require('node:assert/strict');
    const cp = require('node:child_process');
    const outside = ${JSON.stringify(outside)};
    fs.writeFileSync('inside.txt', 'candidate');
    assert.equal(fs.readFileSync('inside.txt', 'utf8'), 'candidate');
    assert.throws(() => fs.readFileSync(outside), {code:'EPERM'});
    assert.throws(() => fs.writeFileSync(outside, 'overwrite'), {code:'EPERM'});
    fs.symlinkSync(outside, 'escape');
    assert.throws(() => fs.readFileSync('escape'), {code:'EPERM'});
    const child = cp.spawnSync(process.execPath, ['-e',
      'require("node:fs").readFileSync(' + JSON.stringify(outside) + ')'],
      {encoding:'utf8'});
    assert.notEqual(child.status, 0);
    assert.match(child.stderr, /EPERM/);
    const net = require('node:net');
    const socket = net.connect({host:'127.0.0.1',port:9});
    socket.on('connect', () => { throw new Error('network isolation failed'); });
    socket.on('error', error => {
      assert.ok(['EPERM','EACCES'].includes(error.code), error.code);
      console.log('file, write, symlink, child inheritance, network: PASS');
    });
    setTimeout(() => process.exit(2), 2000).unref();
  `;
  console.log(run(['-e', script]).trim());
  const output = run([join(qwenRoot, 'node_modules/@qwen-code/qwen-code/cli.js'), '--help']);
  assert.match(output, /max-session-turns/);
  assert.match(output, /max-wall-time/);
  console.log('Qwen CLI startup inside closed sandbox: PASS (no model call)');
} finally {
  rmSync(root, { recursive: true, force: true });
}
