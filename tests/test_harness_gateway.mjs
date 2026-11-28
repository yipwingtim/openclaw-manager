import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import http from 'node:http';
import { test } from 'node:test';

test('gateway checks owner and origin for HTTP and upgrades and hides credentials', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'harness-gateway-test-'));
  await writeFile(join(directory, 'backend.cjs'), `#!/usr/bin/env node
const http = require('node:http');
const server = http.createServer((req, res) => {
  if (req.url.includes('?token=')) {
    if (req.headers.host !== 'manager.test:30015') return res.writeHead(400).end();
    res.writeHead(303, {'set-cookie':'dsh-auth-test=signed; Path=/'}).end();
    return;
  }
  if (req.headers.cookie !== 'dsh-auth-test=signed') return res.writeHead(401).end();
  if (req.headers.authorization || req.headers['x-openclaw-internal-token'])
    return res.writeHead(400).end();
  res.writeHead(200).end(req.url);
});
server.on('upgrade', (req, socket) => {
  if (req.headers.cookie !== 'dsh-auth-test=signed') return socket.destroy();
  socket.end('HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\n\\r\\n');
});
server.listen(3080, '127.0.0.1', () => {

});
`, { mode: 0o700 });
  await writeFile(join(directory, 'auth.mjs'), `
globalThis.fetch = async (url, options) => {
  if (!url.startsWith('http://openclaw-instance-auth-proxy:8084/authorize/')) throw new Error(url);
  return { status: options.headers.cookie === 'openclaw_manager_session=owner' ? 204
    : options.headers.cookie ? 403 : 401 };
};
`);
  await writeFile(join(directory, 'token'), 'private-launch-token');
  const backend = spawn(process.execPath, [join(directory, 'backend.cjs')]);
  let output = '';
  const child = spawn(process.execPath, ['--import', join(directory, 'auth.mjs'),
    resolve('templates/deepseek-harness/gateway.mjs')], {
    env: { ...process.env, PATH: directory + ':' + process.env.PATH,
      HARNESS_AUTHORITY: 'manager.test:30015', HARNESS_MOUNT: '/alice/',
      HARNESS_BACKEND: 'http://127.0.0.1:3080', HARNESS_TOKEN_FILE: join(directory, 'token'),
      HARNESS_INSTANCE_ID: '11111111-1111-4111-8111-111111111111' },
  });
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { output += chunk; });
  function request({ cookie = '', origin, method = 'GET', upgrade = false, host = 'manager.test:30015' } = {}) {
    return new Promise((resolveResult, reject) => {
      const headers = { host, cookie, authorization: 'must-not-forward',
        'x-openclaw-internal-token': 'must-not-forward' };
      if (origin) headers.origin = origin;
      if (upgrade) { headers.upgrade = 'websocket'; headers.connection = 'Upgrade'; }
      const req = http.request({ hostname: '127.0.0.1', port: 3081, path: '/', method, headers }, res => {
        let body = '';
        res.on('data', chunk => { body += chunk; });
        res.on('end', () => resolveResult({ status: res.statusCode, headers: res.headers, body }));
      });
      req.on('upgrade', (res, socket) => { socket.destroy(); resolveResult({ status: res.statusCode }); });
      req.on('error', reject);
      req.setTimeout(3000, () => req.destroy(new Error('timeout')));
      req.end();
    });
  }
  try {
    for (let attempt = 0; attempt < 50; attempt++) {
      try { if ((await request({ cookie: 'openclaw_manager_session=owner' })).status === 200) break; } catch {}
      await new Promise(resolveWait => setTimeout(resolveWait, 50));
    }
    assert.equal((await request()).status, 302);
    assert.equal((await request({ cookie: 'openclaw_manager_session=stranger' })).status, 403);
    const owner = 'openclaw_manager_session=owner; dsh-auth-evil=bad';
    const response = await request({ cookie: owner });
    assert.equal(response.status, 200);
    assert.equal(response.headers['set-cookie'], undefined);
    assert.equal(response.body, '/');
    assert.equal((await request({ cookie: owner, host: 'evil.test' })).status, 403);
    assert.equal((await request({ cookie: owner, origin: 'https://evil.test' })).status, 403);
    assert.equal((await request({ cookie: owner, method: 'POST' })).status, 403);
    assert.equal((await request({ cookie: owner, method: 'POST', origin: 'https://manager.test:30015' })).status, 200);
    assert.equal((await request({ cookie: owner, upgrade: true })).status, 403);
    assert.equal((await request({ cookie: owner, upgrade: true, origin: 'https://manager.test:30015' })).status, 101);
    assert.equal((await request({ upgrade: true, origin: 'https://manager.test:30015' })).status, 401);
    assert.ok(!output.includes('private-launch-token'));
  } finally {
    const exited = new Promise(resolveExit => child.once('exit', resolveExit));
    child.kill('SIGTERM');
    backend.kill('SIGTERM');
    await exited;
    await rm(directory, { recursive: true, force: true });
  }
});
