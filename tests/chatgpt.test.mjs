// All HTTP is faked except the loopback callback. No model inference or user account is accessed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign, randomUUID } from 'node:crypto';
import { mkdtemp, readFile, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { createAttempt, authorizeURL, callbackResult, validateIdToken, createAuth, ISSUER, SHARING_SCOPE } from '../tools/chatgpt/auth.mjs';
import { responsePayload, consumeSSE, generate, listModels } from '../tools/chatgpt/transport.mjs';
import { CredentialStore } from '../tools/chatgpt/store.mjs';
import { dpapi, requestJSON } from '../tools/chatgpt/common.mjs';

const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: 'jwk' }), alg: 'RS256', use: 'sig', kid: 'fixture-key' };
const jwks = { keys: [jwk] };
const now = Date.now();
function jwt(claims = {}, header = {}) {
  const first = Buffer.from(JSON.stringify({ alg: 'RS256', kid: 'fixture-key', ...header })).toString('base64url');
  const second = Buffer.from(JSON.stringify({ iss: ISSUER, aud: 'oaiapp_fixture', sub: 'fixture-subject',
    iat: Math.floor(now / 1000), exp: Math.floor(now / 1000) + 3600, nonce: 'fixture-nonce', email: 'fixture@example.invalid', ...claims })).toString('base64url');
  const unsigned = first + '.' + second;
  return unsigned + '.' + sign('sha256', Buffer.from(unsigned), privateKey).toString('base64url');
}
const tokenResponse = (idToken = jwt(), extras = {}) => ({ access_token: 'fixture-access-never-real',
  refresh_token: 'fixture-refresh-never-real', token_type: 'Bearer', expires_in: 3600,
  scope: 'openid email profile offline_access resource.invoke ' + SHARING_SCOPE, id_token: idToken, ...extras });
const discoveryJSON = { issuer: ISSUER, jwks_uri: ISSUER + '/jwks', revocation_endpoint: ISSUER + '/revoke' };
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
function memoryStore(initial = { version: 1, host_id: 'urn:uuid:' + randomUUID(), profiles: [], active: null }) {
  let state = structuredClone(initial);
  return { withLock: f => f(), load: async () => structuredClone(state), save: async v => { state = structuredClone(v); },
    snapshot: () => structuredClone(state) };
}
function profile(extras = {}) {
  return { id: 'fixture-profile', client_id: 'oaiapp_fixture', subject: 'fixture-subject',
    email: 'fixture@example.invalid', tokens: { ...tokenResponse(), scopes: [SHARING_SCOPE], expires_at: now + 3600000 }, ...extras };
}
function stateWith(p = profile()) { return { version: 1, host_id: 'urn:uuid:' + randomUUID(), profiles: [p], active: p.id }; }
function sse(events, split = false) {
  const data = Buffer.from(events.map(e => 'data: ' + JSON.stringify(e) + '\r\n\r\n').join(''));
  const body = new ReadableStream({ start(c) {
    if (split) for (const byte of data) c.enqueue(Uint8Array.of(byte)); else c.enqueue(data);
    c.close();
  } });
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
}
const complete = text => ({ type: 'response.completed', response: { status: 'completed', model: 'fixture-gpt',
  output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text }] }] } });

test('PKCE, host identity, scope, callback and real app name are explicit', () => {
  const a = createAttempt(), b = createAttempt();
  assert.notEqual(a.state, b.state); assert.notEqual(a.nonce, b.nonce); assert.equal(a.challenge.length, 43);
  const url = new URL(authorizeURL(a, 'http://127.0.0.1:54321/auth/callback', 'urn:uuid:fixture'));
  assert.equal(url.origin, ISSUER); assert.equal(url.searchParams.get('client_id'), 'dynamic_agent_client');
  assert.equal(url.searchParams.get('agent_name_hint'), 'Tillegal Judge');
  assert.match(url.searchParams.get('scope'), /chatgpt.tokens.use.direct/);
  const returning = new URL(authorizeURL(a, 'http://127.0.0.1:54322/auth/callback', 'urn:uuid:fixture', profile()));
  assert.equal(returning.searchParams.get('client_id'), 'oaiapp_fixture');
  assert.equal(returning.searchParams.has('agent_name_hint'), false);
});
test('OAuth rejects wrong state, denied grant, missing/changed client ID and duplicate code', () => {
  const a = createAttempt();
  const url = new URL('http://127.0.0.1/auth/callback?state=' + a.state + '&code=fixture&client_id=oaiapp_fixture');
  assert.deepEqual(callbackResult(url, a), { code: 'fixture', clientId: 'oaiapp_fixture' });
  assert.throws(() => callbackResult(url, { state: 'wrong' }), /STATE_MISMATCH/);
  assert.throws(() => callbackResult(url, a, 'oaiapp_other'), /CLIENT_MISMATCH/);
  const missing = new URL(url); missing.searchParams.delete('client_id');
  assert.throws(() => callbackResult(missing, a), /REGISTRATION_INCOMPLETE/);
  const denied = new URL(url); denied.searchParams.set('error', 'access_denied');
  assert.throws(() => callbackResult(denied, a), /ACCESS_DENIED/);
  const duplicate = new URL(url); duplicate.searchParams.append('code', 'second');
  assert.throws(() => callbackResult(duplicate, a), /INVALID_CALLBACK/);
});
test('ID token verifies real RSA signature, issuer, audience, expiry, nonce and subject', () => {
  const settings = { clientId: 'oaiapp_fixture', nonce: 'fixture-nonce', now };
  assert.equal(validateIdToken(jwt(), jwks, settings).subject, 'fixture-subject');
  for (const claims of [{ iss: 'https://attacker.invalid' }, { aud: 'other-client' }, { exp: 1 }, { nonce: 'other' },
      { sub: '' }, { aud: ['oaiapp_fixture', 'other'], azp: 'other' }]) {
    assert.throws(() => validateIdToken(jwt(claims), jwks, settings), /INVALID_ID_TOKEN/);
  }
  assert.throws(() => validateIdToken(jwt(), { keys: [] }, settings), /INVALID_ID_TOKEN/);
  assert.throws(() => validateIdToken(jwt({}, { alg: 'HS256' }), jwks, settings), /INVALID_ID_TOKEN/);
  const tampered = jwt().split('.'); tampered[1] = Buffer.from('{}').toString('base64url');
  assert.throws(() => validateIdToken(tampered.join('.'), jwks, settings), /INVALID_ID_TOKEN/);
  assert.throws(() => validateIdToken(jwt(), jwks, { ...settings, subject: 'other-account' }), /ACCOUNT_MISMATCH/);
});
test('Full loopback login persists issued registration and verified identity; credentials never in status', async () => {
  const store = memoryStore(), calls = [];
  let nonce, verifier;
  const auth = createAuth(store, { clock: () => now,
    openBrowser: async raw => {
      const url = new URL(raw); nonce = url.searchParams.get('nonce');
      const redirect = new URL(url.searchParams.get('redirect_uri'));
      assert.equal(redirect.hostname, '127.0.0.1'); assert.equal(redirect.pathname, '/auth/callback');
      redirect.searchParams.set('state', url.searchParams.get('state'));
      redirect.searchParams.set('code', 'fixture-code'); redirect.searchParams.set('client_id', 'oaiapp_fixture');
      assert.equal((await fetch(redirect)).status, 200);
    }, fetchImpl: async (url, options) => {
      calls.push(String(url));
      if (String(url).endsWith('/oauth/token')) {
        assert.equal(store.snapshot().profiles[0].client_id, 'oaiapp_fixture');
        assert.equal(store.snapshot().active, null);
        assert.equal(options.body.get('client_id'), 'oaiapp_fixture');
        verifier = options.body.get('code_verifier'); assert.ok(verifier.length > 40);
        return json(tokenResponse(jwt({ nonce })));
      }
      if (String(url).endsWith('openid-configuration')) return json(discoveryJSON);
      if (String(url).endsWith('/jwks')) return json(jwks);
      throw new Error('Unexpected HTTP');
    } });
  const session = await auth.login({ signal: AbortSignal.timeout(5000) });
  assert.equal(session.sharing, true); assert.equal(session.email, 'fixture@example.invalid');
  assert.ok(store.snapshot().active); assert.equal(calls.length, 3);
  assert.doesNotMatch(JSON.stringify(await auth.status()), /fixture-access|fixture-refresh|id_token/);
});
test('Failed account verification preserves previous active account and pending registration', async () => {
  const store = memoryStore(stateWith());
  let nonce;
  const auth = createAuth(store, { openBrowser: async raw => {
    const url = new URL(raw); nonce = url.searchParams.get('nonce');
    const cb = new URL(url.searchParams.get('redirect_uri'));
    for (const [k, v] of Object.entries({ code: 'fixture', state: url.searchParams.get('state'), client_id: 'oaiapp_new' })) cb.searchParams.set(k, v);
    await fetch(cb);
  }, fetchImpl: async url => String(url).endsWith('/oauth/token') ? json(tokenResponse(jwt({ nonce, aud: 'wrong' })))
    : String(url).endsWith('/jwks') ? json(jwks) : json(discoveryJSON) });
  await assert.rejects(auth.login({ newAccount: true, signal: AbortSignal.timeout(5000) }), /INVALID_ID_TOKEN/);
  assert.equal(store.snapshot().active, 'fixture-profile');
  assert.equal(store.snapshot().profiles.length, 2);
  assert.equal(store.snapshot().profiles[1].client_id, 'oaiapp_new');
  assert.equal(store.snapshot().profiles[1].tokens, undefined);
});
test('Cancelled login never opens browser or sends an OAuth request', async () => {
  const auth = createAuth(memoryStore(), {
    openBrowser: () => assert.fail('must not open browser'), fetchImpl: () => assert.fail('must not call OAuth') });
  await assert.rejects(auth.login({ signal: AbortSignal.abort() }), error => error.name === 'AbortError');
});
test('Refresh uses issued client ID, serializes replacement and recovers a JWKS outage', async () => {
  const p = profile(); p.tokens.expires_at = now - 1;
  const store = memoryStore(stateWith(p)); let outage = true, refreshes = 0;
  const auth = createAuth(store, { clock: () => now, fetchImpl: async (url, options) => {
    if (String(url).endsWith('/oauth/token')) {
      refreshes++; assert.equal(options.body.get('client_id'), 'oaiapp_fixture');
      assert.equal(options.body.has('scope'), false);
      return json(tokenResponse(jwt(), { refresh_token: 'fixture-rotated', access_token: 'fixture-new-access' }));
    }
    if (String(url).endsWith('/jwks')) { if (outage) throw new Error('offline'); return json(jwks); }
    return json(discoveryJSON);
  } });
  await assert.rejects(auth.accessToken(AbortSignal.timeout(5000)), /NETWORK_ERROR/);
  assert.equal(store.snapshot().profiles[0].pending.tokens.refresh_token, 'fixture-rotated');
  outage = false;
  assert.equal(await auth.accessToken(AbortSignal.timeout(5000)), 'fixture-new-access');
  assert.equal(refreshes, 1); assert.equal(store.snapshot().profiles[0].pending, undefined);
});
test('Logout clears local tokens but preserves registration; reports failed revocation', async () => {
  const store = memoryStore(stateWith());
  const auth = createAuth(store, { fetchImpl: async () => { throw new Error('offline'); } });
  const result = await auth.logout(AbortSignal.timeout(5000));
  assert.equal(result.remote_revocation_confirmed, false);
  assert.equal(store.snapshot().profiles[0].client_id, 'oaiapp_fixture');
  assert.equal(store.snapshot().profiles[0].tokens, undefined);
});
test('Profile selection never combines registrations and token sets', async () => {
  const state = stateWith(); state.profiles.push(profile({ id: 'second', client_id: 'oaiapp_second' }));
  const store = memoryStore(state), auth = createAuth(store);
  assert.equal((await auth.select('second')).profile_id, 'second');
  assert.equal(store.snapshot().active, 'second');
  await assert.rejects(auth.select('missing'), /PROFILE_NOT_FOUND/);
});
test('Responses payload uses instructions, store=false and stream=true without unsupported fields', () => {
  const payload = responsePayload([{ role: 'system', content: 'Judge policy' }, { role: 'user', content: 'Evidence' }], 'fixture-gpt');
  assert.deepEqual(Object.keys(payload).sort(), ['input', 'instructions', 'model', 'store', 'stream']);
  assert.equal(payload.store, false); assert.equal(payload.stream, true);
  assert.equal(payload.input[0].role, 'user');
});
test('SSE reassembles UTF-8 and CRLF across byte boundaries and requires completed', async () => {
  const text = '{"explanation":"Điều kiện"}';
  const result = await consumeSSE(sse([{ type: 'response.output_text.delta', delta: text }, complete(text)], true));
  assert.equal(result.text, text); assert.equal(result.metadata.stop_reason, 'response.completed');
  await assert.rejects(consumeSSE(sse([{ type: 'response.output_text.delta', delta: text }])), /STREAM_INTERRUPTED/);
});
test('Regression: preserve delta text when completed output is empty or absent', async () => {
  const value = '{"status":"INSUFFICIENT","explanation":"Nội dung thực được stream"}';
  for (const output of [[], null, undefined]) {
    const terminal = { type: 'response.completed', response: { status: 'completed', model: 'fixture-gpt', output } };
    const result = await consumeSSE(sse([{ type: 'response.output_text.delta', delta: value }, terminal], true));
    assert.equal(result.text, value);
    assert.equal(result.metadata.stream_summary.text_source, 'stream_deltas');
    assert.equal(result.metadata.stream_summary.completed_chars, 0);
  }
});
test('Reconstruct done events in content order without duplicating repeated text', async () => {
  const terminal = { type: 'response.completed', response: { status: 'completed', model: 'fixture-gpt', output: [] } };
  const value = '{"fixture":"done event"}';
  const result = await consumeSSE(sse([
    { type: 'response.output_text.done', output_index: 0, content_index: 0, text: value },
    { type: 'response.content_part.done', output_index: 0, content_index: 0, part: { type: 'output_text', text: value } },
    { type: 'response.output_item.done', output_index: 0, item: { type: 'message', role: 'assistant', status: 'completed',
      content: [{ type: 'output_text', text: value }] } }, terminal]));
  assert.equal(result.text, value); assert.equal(result.metadata.stream_summary.done_events, 3);
  assert.equal(result.metadata.stream_summary.text_source, 'done_events');
});
test('Empty genuine completion remains an error with safe diagnostics', async () => {
  const terminal = { type: 'response.completed', response: { status: 'completed', model: 'fixture-gpt', output: [] } };
  await assert.rejects(consumeSSE(sse([terminal])), error => {
    assert.equal(error.code, 'CHATGPT_EMPTY_RESPONSE');
    assert.equal(error.metadata.stop_reason, 'response.completed');
    assert.equal(error.metadata.stream_summary.delta_chars, 0);
    assert.equal(error.metadata.stream_summary.completed_chars, 0);
    return true;
  });
});
test('Collected text is rejected without completed, on failed/incomplete or inconsistent done text', async () => {
  const delta = { type: 'response.output_text.delta', delta: 'fixture-private-text' };
  for (const tail of [[], [{ type: 'response.failed' }], [{ type: 'response.incomplete' }]]) {
    await assert.rejects(consumeSSE(sse([delta, ...tail])), error => {
      assert.match(error.code, /STREAM_INTERRUPTED|RESPONSE_FAILED|INCOMPLETE_RESPONSE/);
      assert.doesNotMatch(JSON.stringify(error.metadata), /fixture-private-text/);
      return true;
    });
  }
  await assert.rejects(consumeSSE(sse([delta,
    { type: 'response.output_text.done', output_index: 0, content_index: 0, text: 'different' },
    { type: 'response.completed', response: { status: 'completed', model: 'fixture-gpt', output: [] } }])), /INVALID_COMPLETION/);
});
test('SSE rejects malformed, incomplete, refusal, tool calls and inconsistent completion', async () => {
  for (const [events, code] of [
    [[{ type: 'response.incomplete' }], /INCOMPLETE/], [[{ type: 'response.failed' }], /RESPONSE_FAILED/],
    [[{ type: 'response.refusal.delta', delta: 'no' }], /REFUSED/],
    [[{ type: 'response.output_item.added', item: { type: 'function_call' } }], /UNEXPECTED_OUTPUT/],
    [[{ type: 'response.output_text.delta', delta: 'x' }, complete('y')], /INVALID_COMPLETION/],
  ]) await assert.rejects(consumeSSE(sse(events)), code);
  await assert.rejects(consumeSSE(new Response('data: nope\n\n', { headers: { 'Content-Type': 'text/event-stream' } })), /INVALID_STREAM/);
});
test('SSE output limit and HTTP errors cannot produce successful partial output', async () => {
  await assert.rejects(consumeSSE(sse([{ type: 'response.output_text.delta', delta: 'x'.repeat(200) }]), { maxOutputChars: 128 }), /OUTPUT_LIMIT/);
  for (const [status, code] of [[401, /SIGN_IN_REQUIRED/], [403, /PERMISSION_DENIED/], [429, /USAGE_LIMIT/]]) {
    await assert.rejects(consumeSSE(new Response('secret upstream body', { status })), code);
  }
});
test('JSON transport preserves full-body timeout classification', async () => {
  const abort = new DOMException('fixture only', 'TimeoutError');
  const response = new Response(new ReadableStream({ start(controller) { controller.error(abort); } }));
  await assert.rejects(requestJSON('https://auth.openai.com/fixture', {}, async () => response), error => error.name === 'TimeoutError');
});
test('Model catalog preserves account ordering; unavailable model makes no inference call', async () => {
  const auth = { accessToken: async () => 'fixture-token' }; let calls = 0;
  const http = async (url, options) => { calls++; assert.equal(options.redirect, 'error'); return json({ models: [
    { slug: 'hidden', display_name: 'Hidden', visibility: 'hidden' }, { slug: 'fixture-gpt', display_name: 'Fixture', visibility: 'list' }] }); };
  assert.equal((await listModels(auth, AbortSignal.timeout(5000), http))[0].slug, 'fixture-gpt');
  await assert.rejects(generate(auth, { model: 'missing', signal: AbortSignal.timeout(5000) }, http), /MODEL_UNAVAILABLE/);
  assert.equal(calls, 2);
});
test('Generate waits for real completion and sends no mock-response payload', async () => {
  const auth = { accessToken: async () => 'fixture-token' }, messages = [{ role: 'system', content: 'Judge' }, { role: 'user', content: 'Evidence' }];
  let inferenceCalls = 0;
  const result = await generate(auth, { messages, signal: AbortSignal.timeout(5000) }, async (url, options) => {
    if (String(url).endsWith('/models')) return json({ models: [{ slug: 'fixture-gpt', display_name: 'Fixture', visibility: 'list' }] });
    inferenceCalls++; assert.equal(url, 'https://api.openai.com/v1/responses');
    assert.equal(options.headers.Authorization, 'Bearer fixture-token');
    assert.deepEqual(JSON.parse(options.body), responsePayload(messages, 'fixture-gpt'));
    return sse([complete('{"fixture":"only tests"}')]);
  });
  assert.equal(inferenceCalls, 1); assert.equal(result.metadata.returned_model, 'fixture-gpt');
});
test('Windows DPAPI roundtrip and encrypted credential persistence with synthetic tokens', { skip: process.platform !== 'win32' }, async () => {
  const plain = Buffer.from('fixture-secret-not-a-real-token');
  const cipher = await dpapi('protect', plain);
  assert.notDeepEqual(cipher, plain); assert.deepEqual(await dpapi('unprotect', cipher), plain);
  const directory = await mkdtemp(join(tmpdir(), 'tillegal-chatgpt-test-'));
  try {
    const store = new CredentialStore(directory);
    await store.withLock(async () => { await store.save(stateWith()); assert.equal((await store.load()).active, 'fixture-profile'); });
    const files = await readdir(directory);
    assert.deepEqual(files, ['session.dpapi.json']);
    const persisted = await readFile(join(directory, files[0]), 'utf8');
    assert.doesNotMatch(persisted, /fixture-access|fixture-refresh|fixture@example/);
  } finally {
    assert.equal(dirname(resolve(directory)), resolve(tmpdir()));
    assert.match(directory, /tillegal-chatgpt-test-/);
    await rm(directory, { recursive: true, force: true });
  }
});
