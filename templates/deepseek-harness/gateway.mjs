import http from 'node:http';
import { readFile } from 'node:fs/promises';

const authority = process.env.HARNESS_AUTHORITY;
const publicId = process.env.HARNESS_INSTANCE_ID;
const mount = process.env.HARNESS_MOUNT;
if (!authority || !/^[a-zA-Z0-9.:[\]-]+$/.test(authority)
    || !/^[a-f0-9-]{36}$/.test(publicId || '')
    || !/^\/[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\/$/.test(mount || '')) {
  throw new Error('Invalid Harness gateway configuration');
}
const backend = new URL(process.env.HARNESS_BACKEND);
if (backend.protocol !== 'http:' || backend.port !== '3080'
    || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(backend.hostname)) {
  throw new Error('Invalid Harness backend');
}
const origin = 'https://' + authority;
let browserCookie = '';
let cookiePromise;

async function authorized(req) {
  if (req.headers.host !== authority) return 403;
  if (req.headers.origin && req.headers.origin !== origin) return 403;
  if ((req.method !== 'GET' && req.method !== 'HEAD' || req.headers.upgrade)
      && req.headers.origin !== origin) return 403;
  try {
    const response = await fetch(
      'http://openclaw-instance-auth-proxy:8084/authorize/' + publicId,
      { headers: { cookie: (req.headers.cookie || '').split(';').map(value => value.trim())
        .filter(value => value.startsWith('openclaw_manager_session=')).join(';') }, redirect: 'manual',
        signal: AbortSignal.timeout(5000) });
    return response.status === 204 ? 204
      : [401, 403].includes(response.status) ? response.status : 503;
  } catch {
    return 503;
  }
}

async function sessionCookie() {
  if (browserCookie) return browserCookie;
  const launchToken = (await readFile(process.env.HARNESS_TOKEN_FILE || '/run/harness-auth/token', 'utf8')).trim();
  if (!/^[A-Za-z0-9_-]+$/.test(launchToken)) throw new Error('Harness is starting');
  if (!cookiePromise) {
    cookiePromise = new Promise((resolve, reject) => {
      const exchange = http.get({
        hostname: backend.hostname, port: 3080, path: '/?token=' + launchToken,
        headers: { host: authority }, timeout: 5000,
      }, response => {
        const cookie = response.headers['set-cookie']?.find(value => value.startsWith('dsh-auth-'));
        response.resume();
        if (response.statusCode !== 303 || !cookie) {
          reject(new Error('Harness authentication failed'));
          return;
        }
        browserCookie = cookie.split(';')[0];
        resolve(browserCookie);
      });
      exchange.on('timeout', () => exchange.destroy(new Error('Harness authentication timeout')));
      exchange.on('error', reject);
    }).finally(() => { cookiePromise = undefined; });
  }
  return cookiePromise;
}

function headersFor(req, cookie) {
  const headers = { ...req.headers, host: authority, cookie };
  delete headers.authorization;
  for (const name of Object.keys(headers)) {
    if (name.startsWith('x-openclaw-') || name.startsWith('x-uis-')
        || name.startsWith('x-forwarded-') || name === 'x-remote-user') delete headers[name];
  }
  return headers;
}

const server = http.createServer(async (req, res) => {
  if (req.url === '/_gateway_health' && req.socket.remoteAddress === '127.0.0.1') {
    try {
      await sessionCookie();
      res.writeHead(200).end('ready');
    } catch {
      res.writeHead(503).end('starting');
    }
    return;
  }
  const status = await authorized(req);
  if (status !== 204) {
    if (status === 401 && req.method === 'GET') {
      res.writeHead(302, { location: '/login?instance=' + publicId, 'cache-control': 'no-store' }).end();
    } else {
      res.writeHead(status, { 'cache-control': 'no-store' }).end();
    }
    return;
  }
  try {
    const cookie = await sessionCookie();
    const upstream = http.request({ hostname: backend.hostname, port: 3080,
      path: req.url, method: req.method, headers: headersFor(req, cookie) }, response => {
      if (response.statusCode === 401) browserCookie = '';
      const headers = { ...response.headers, 'cache-control': 'no-store' };
      delete headers['set-cookie'];
      res.writeHead(response.statusCode, headers);
      response.pipe(res);
    });
    upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
    req.on('aborted', () => upstream.destroy());
    req.pipe(upstream);
  } catch {
    res.writeHead(503).end();
  }
});
server.on('upgrade', async (req, socket, head) => {
  const status = await authorized(req);
  if (status !== 204) {
    socket.end('HTTP/1.1 ' + status + ' Forbidden\r\nConnection: close\r\n\r\n');
    return;
  }
  try {
    const cookie = await sessionCookie();
    const upstream = http.request({ hostname: backend.hostname, port: 3080,
      path: req.url, headers: headersFor(req, cookie) });
    upstream.on('upgrade', (response, peer, upstreamHead) => {
      const headers = Object.entries(response.headers)
        .filter(([name]) => name !== 'set-cookie')
        .map(([name, value]) => name + ': ' + value).join('\r\n');
      socket.write('HTTP/1.1 101 Switching Protocols\r\n' + headers + '\r\n\r\n');
      if (upstreamHead.length) socket.write(upstreamHead);
      if (head.length) peer.write(head);
      peer.on('error', () => socket.destroy());
      socket.on('error', () => peer.destroy());
      peer.on('close', () => socket.destroy());
      socket.on('close', () => peer.destroy());
      peer.pipe(socket).pipe(peer);
    });
    upstream.on('response', response => {
      socket.end('HTTP/1.1 ' + response.statusCode + ' Rejected\r\nConnection: close\r\n\r\n');
      response.resume();
    });
    upstream.on('error', () => socket.destroy());
    upstream.end();
  } catch {
    socket.destroy();
  }
});
server.listen(3081, '0.0.0.0');
