import { lstat, mkdir, readFile, open, rename, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { dpapi, fail, ChatGPTError } from './common.mjs';

export class CredentialStore {
  constructor(directory, crypt = dpapi) { this.directory = directory; this.crypt = crypt; }
  async withLock(operation) {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    const dir = await lstat(this.directory);
    if (!dir.isDirectory() || dir.isSymbolicLink()) fail('CHATGPT_STORAGE_UNSAFE');
    const lock = join(this.directory, 'session.lock');
    let handle;
    for (let attempt = 0; attempt < 50; attempt++) {
      try { handle = await open(lock, 'wx', 0o600); await handle.writeFile(String(process.pid)); break; }
      catch (error) {
        if (error.code !== 'EEXIST') throw new ChatGPTError('CHATGPT_STORAGE_ERROR');
        // Do not unlink stale locks automatically (a replacement process could acquire
        // the lock between probe and unlink). Explicit recovery is documented.
        const info = await lstat(lock);
        if (info.isSymbolicLink()) fail('CHATGPT_STORAGE_UNSAFE');
        const pid = Number(await readFile(lock, 'utf8'));
        if (Number.isSafeInteger(pid) && pid > 0) {
          try { process.kill(pid, 0); }
          catch (probe) { if (probe.code === 'ESRCH') fail('CHATGPT_STORAGE_LOCK_STALE'); }
        }
        await delay(100);
      }
    }
    if (!handle) fail('CHATGPT_STORAGE_BUSY');
    try { return await operation(); }
    finally {
      // Never unlock from a signal handler while an atomic write/rotation is in flight.
      await handle.close(); await unlink(lock).catch(() => {});
    }
  }
  async load() {
    const path = join(this.directory, 'session.dpapi.json');
    try {
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink() || info.size > 2 * 1024 * 1024) fail('CHATGPT_STORAGE_UNSAFE');
      const envelope = JSON.parse(await readFile(path, 'utf8'));
      if (envelope.version !== 1 || envelope.protection !== 'windows-dpapi-current-user'
          || typeof envelope.ciphertext !== 'string') fail('CHATGPT_STORAGE_INVALID');
      const value = JSON.parse((await this.crypt('unprotect', Buffer.from(envelope.ciphertext, 'base64'))).toString('utf8'));
      if (value.version !== 1 || typeof value.host_id !== 'string' || !Array.isArray(value.profiles)) fail('CHATGPT_STORAGE_INVALID');
      return value;
    } catch (error) {
      if (error.code === 'ENOENT') return { version: 1, host_id: 'urn:uuid:' + randomUUID(), profiles: [], active: null };
      if (error instanceof ChatGPTError) throw error;
      fail('CHATGPT_STORAGE_INVALID'); // Preserve unreadable credentials; never silently reset.
    }
  }
  async save(value) {
    const ciphertext = (await this.crypt('protect', Buffer.from(JSON.stringify(value)))).toString('base64');
    const serialized = JSON.stringify({ version: 1, protection: 'windows-dpapi-current-user', ciphertext });
    if (Buffer.byteLength(serialized) > 2 * 1024 * 1024) fail('CHATGPT_STORAGE_LIMIT');
    const temp = join(this.directory, 'session.' + randomUUID() + '.tmp');
    try {
      const handle = await open(temp, 'wx', 0o600);
      try {
        await handle.writeFile(serialized);
        await handle.sync();
      } finally { await handle.close(); }
      await rename(temp, join(this.directory, 'session.dpapi.json'));
    } finally { await unlink(temp).catch(() => {}); }
  }
}
