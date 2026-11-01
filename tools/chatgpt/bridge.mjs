// One JSON stdin request / one JSON stdout response; no tokens leave this process.
import { join } from 'node:path';
import { createAuth } from './auth.mjs';
import { CredentialStore } from './store.mjs';
import { listModels, generate } from './transport.mjs';
import { fail, safeError } from './common.mjs';

const cancelled = new AbortController();
const interrupt = () => cancelled.abort();
process.once('SIGINT', interrupt);
process.once('SIGTERM', interrupt);

async function main() {
  if (Number(process.versions.node.split('.')[0]) < 22) fail('CHATGPT_NODE_22_REQUIRED');
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 200000) fail('JUDGE_INPUT_LIMIT');
    chunks.push(chunk);
  }
  const request = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
  if (process.platform !== 'win32') fail('CHATGPT_WINDOWS_REQUIRED');
  const timeout = request.timeout_seconds ?? 60;
  if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout <= 0 || timeout > 600) fail('JUDGE_CONFIGURATION_ERROR');
  const directory = join(process.env.LOCALAPPDATA || join(process.env.USERPROFILE, 'AppData', 'Local'), 'Tillegal', 'Judge', 'ChatGPT');
  const store = new CredentialStore(directory), auth = createAuth(store);
  const signal = AbortSignal.any([cancelled.signal, AbortSignal.timeout(timeout * 1000)]);
  let result;
  if (request.action === 'login') {
    process.stderr.write('Continue with ChatGPT: trinh duyet se mo. Dang nhap va cho phep Tillegal Judge dung goi ChatGPT. Ctrl+C de huy.\n');
    result = await auth.login({ newAccount: request.new_account === true, signal });
  } else if (request.action === 'status') result = await auth.status();
  else if (request.action === 'select') result = await auth.select(request.profile_id);
  else if (request.action === 'models') result = { models: await listModels(auth, signal) };
  else if (request.action === 'logout') result = await auth.logout(signal);
  else if (request.action === 'generate') {
    if (request.allow_live !== true) fail('CHATGPT_LIVE_OPT_IN_REQUIRED');
    result = await generate(auth, { model: request.model, messages: request.messages, signal,
      maxOutputChars: request.max_output_chars ?? 24000 });
  } else fail('CHATGPT_INVALID_ACTION');
  process.stdout.write(JSON.stringify({ ok: true, ...result }) + '\n');
}
main().catch(error => {
  // Only metadata authored by our transport; never raw text, tokens or upstream error bodies.
  process.stdout.write(JSON.stringify({ ok: false, error_code: cancelled.signal.aborted ? 'CHATGPT_CANCELLED' : safeError(error),
    ...(error.metadata ? { metadata: error.metadata } : {}) }) + '\n');
  process.exitCode = 1;
}).finally(() => {
  process.removeListener('SIGINT', interrupt);
  process.removeListener('SIGTERM', interrupt);
});
