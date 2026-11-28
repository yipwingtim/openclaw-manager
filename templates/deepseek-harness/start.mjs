import { spawn } from 'node:child_process';
import { networkInterfaces } from 'node:os';
import { writeFileSync, renameSync, unlinkSync } from 'node:fs';

const authority = process.env.HARNESS_AUTHORITY;
const mount = process.env.HARNESS_MOUNT;
if (!/^[A-Za-z0-9.:-]+$/.test(authority || '')
    || !/^\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\/$/.test(mount || '')) {
  throw new Error('Invalid Harness public URL');
}
const address = Object.values(networkInterfaces()).flat()
  .find(entry => entry.family === 'IPv4' && !entry.internal)?.address;
if (!address) throw new Error('Harness tenant network is missing');
try { unlinkSync('/run/harness-auth/token'); } catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
const child = spawn('dsh', ['web', '--no-open', '--host', address,
  '--public-url', 'https://' + authority + mount, '--trusted-host', authority],
{ stdio: ['ignore', 'pipe', 'pipe'] });
let pending = '';
function log(chunk) {
  pending += chunk.toString();
  let at;
  while ((at = pending.indexOf('\n')) >= 0) {
    const line = pending.slice(0, at);
    pending = pending.slice(at + 1);
    const match = line.match(/[?&]token=([A-Za-z0-9_-]+)/);
    if (match) {
      writeFileSync('/run/harness-auth/token.tmp', match[1], { mode: 0o644 });
      renameSync('/run/harness-auth/token.tmp', '/run/harness-auth/token');
    }
    process.stdout.write(line.replace(/([?&]token=)[^\s&#]+/g, '$1[redacted]') + '\n');
  }
  if (pending.length > 65536) pending = '';
}
child.stdout.on('data', log);
child.stderr.on('data', log);
child.on('error', () => process.exit(1));
child.on('exit', code => process.exit(code || 1));
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
