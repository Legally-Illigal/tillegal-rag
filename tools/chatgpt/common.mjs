// Independent implementation against the published SIWC protocol; no DevKit code dependency.
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

export class ChatGPTError extends Error {
  constructor(code) { super(code); this.code = code; }
}
export function fail(code) { throw new ChatGPTError(code); }
export function safeError(error) {
  if (error instanceof ChatGPTError) return error.code;
  if (['TimeoutError', 'AbortError'].includes(error?.name)) return 'PROVIDER_TIMEOUT';
  return 'CHATGPT_INTERNAL_ERROR';
}
export function text(value, max = 10000) {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= max;
}
export async function readBounded(response, max = 1024 * 1024) {
  if (!response.body) fail('CHATGPT_INVALID_RESPONSE');
  let size = 0;
  const chunks = [];
  try {
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > max) fail('CHATGPT_RESPONSE_LIMIT');
      chunks.push(chunk);
    }
    return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
  } catch (error) {
    if (error instanceof ChatGPTError || ['AbortError', 'TimeoutError'].includes(error?.name)) throw error;
    fail('CHATGPT_INVALID_RESPONSE');
  }
}
export async function jsonResponse(response) {
  if (!response.ok) {
    // Never print an upstream error body, which could contain submitted evidence or tokens.
    await response.body?.cancel();
    fail(({ 400: 'CHATGPT_BAD_REQUEST', 401: 'CHATGPT_SIGN_IN_REQUIRED',
      403: 'CHATGPT_PERMISSION_DENIED', 404: 'CHATGPT_MODEL_OR_RESOURCE_NOT_FOUND',
      429: 'CHATGPT_USAGE_LIMIT', 504: 'PROVIDER_TIMEOUT' })[response.status] || 'CHATGPT_HTTP_ERROR');
  }
  try { return JSON.parse(await readBounded(response)); }
  catch (error) {
    if (error instanceof ChatGPTError || ['TimeoutError', 'AbortError'].includes(error?.name)) throw error;
    fail('CHATGPT_INVALID_RESPONSE');
  }
}
export async function requestJSON(url, options = {}, fetchImpl = fetch) {
  try {
    return await jsonResponse(await fetchImpl(url, { ...options, redirect: 'error',
      signal: options.signal ?? AbortSignal.timeout(30000) }));
  } catch (error) {
    if (error instanceof ChatGPTError || ['AbortError', 'TimeoutError'].includes(error?.name)) throw error;
    fail('CHATGPT_NETWORK_ERROR');
  }
}
export async function runChild(command, args, input, { timeout = 15000, max = 4 * 1024 * 1024 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '', size = 0, settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      error ? reject(error) : resolve(value);
    };
    const timer = setTimeout(() => { child.kill(); finish(new ChatGPTError('CHATGPT_HELPER_TIMEOUT')); }, timeout);
    child.stdout.on('data', data => {
      size += data.length;
      if (size > max) { child.kill(); finish(new ChatGPTError('CHATGPT_HELPER_LIMIT')); }
      else output += data.toString('utf8');
    });
    child.stderr.on('data', () => {}); // Sanitized error codes only; never echo child diagnostics.
    child.on('error', () => finish(new ChatGPTError('CHATGPT_HELPER_UNAVAILABLE')));
    child.on('close', code => finish(code === 0 ? null : new ChatGPTError('CHATGPT_SECURE_STORAGE_ERROR'), output));
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}
export async function dpapi(operation, bytes) {
  if (process.platform !== 'win32') fail('CHATGPT_WINDOWS_REQUIRED');
  const powershell = join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const output = await runChild(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', fileURLToPath(new URL('./dpapi.ps1', import.meta.url)), '-Operation', operation], Buffer.from(bytes).toString('base64'));
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(output.trim())) fail('CHATGPT_SECURE_STORAGE_ERROR');
  return Buffer.from(output.trim(), 'base64');
}
