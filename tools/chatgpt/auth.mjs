import { createServer } from 'node:http';
import { randomBytes, randomUUID, createHash, createPublicKey, verify, timingSafeEqual } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fail, text, requestJSON, ChatGPTError } from './common.mjs';

export const ISSUER = 'https://auth.openai.com';
export const RESOURCE = 'https://api.openai.com/v1';
export const TOKEN_URL = ISSUER + '/api/accounts/oauth/token';
export const SHARING_SCOPE = 'chatgpt.tokens.use.direct';
const SCOPES = 'openid profile email offline_access resource.invoke ' + SHARING_SCOPE;
const equal = (a, b) => typeof a === 'string' && typeof b === 'string'
  && Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));

export function createAttempt() {
  const verifier = randomBytes(48).toString('base64url');
  return { state: randomBytes(32).toString('base64url'), nonce: randomBytes(32).toString('base64url'),
    verifier, challenge: createHash('sha256').update(verifier).digest('base64url') };
}
export function authorizeURL(attempt, redirect, hostId, profile) {
  const url = new URL(ISSUER + '/api/accounts/authorize');
  const params = { client_id: profile?.client_id || 'dynamic_agent_client', response_type: 'code',
    redirect_uri: redirect, scope: SCOPES, resource: RESOURCE, state: attempt.state, nonce: attempt.nonce,
    code_challenge_method: 'S256', code_challenge: attempt.challenge, ext_agent_host_id: hostId };
  if (!profile?.client_id) params.agent_name_hint = 'Tillegal Judge';
  // Never print this URL: a returning login may contain the permitted ID-token hint.
  if (profile?.tokens?.id_token) params.id_token_hint = profile.tokens.id_token;
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  return url.href;
}
export function callbackResult(url, attempt, oldClientId) {
  if (!equal(url.searchParams.get('state'), attempt.state)) fail('CHATGPT_OAUTH_STATE_MISMATCH');
  for (const field of ['state', 'code', 'client_id', 'error']) {
    if (url.searchParams.getAll(field).length > 1) fail('CHATGPT_OAUTH_INVALID_CALLBACK');
  }
  if (url.searchParams.has('error')) fail('CHATGPT_OAUTH_ACCESS_DENIED');
  const issued = url.searchParams.get('client_id');
  if (oldClientId && issued && issued !== oldClientId) fail('CHATGPT_OAUTH_CLIENT_MISMATCH');
  const clientId = oldClientId || issued;
  if (!text(clientId, 200) || clientId === 'dynamic_agent_client' || !/^[A-Za-z0-9_-]+$/.test(clientId)) {
    fail('CHATGPT_OAUTH_REGISTRATION_INCOMPLETE');
  }
  const code = url.searchParams.get('code');
  if (!text(code)) fail('CHATGPT_OAUTH_INVALID_CALLBACK');
  return { clientId, code };
}
export function validateIdToken(token, jwks, { clientId, nonce, subject, now = Date.now() }) {
  try {
    if (!text(token, 20000)) throw new Error();
    const parts = token.split('.');
    if (parts.length !== 3 || parts.some(p => !/^[A-Za-z0-9_-]+$/.test(p))) throw new Error();
    const header = JSON.parse(Buffer.from(parts[0], 'base64url'));
    const claims = JSON.parse(Buffer.from(parts[1], 'base64url'));
    // Explicit algorithm allowlist; no none/HMAC/remote key URLs from untrusted JWT headers.
    if (!['RS256', 'ES256'].includes(header.alg) || !text(header.kid) || header.crit) throw new Error();
    const keys = jwks?.keys?.filter(k => k.kid === header.kid && (!k.use || k.use === 'sig')
      && (!k.alg || k.alg === header.alg) && k.kty === (header.alg === 'RS256' ? 'RSA' : 'EC'));
    if (!keys || keys.length !== 1) throw new Error();
    const key = createPublicKey({ key: keys[0], format: 'jwk' });
    const options = header.alg === 'ES256' ? { key, dsaEncoding: 'ieee-p1363' } : key;
    if (!verify('sha256', Buffer.from(parts[0] + '.' + parts[1]), options, Buffer.from(parts[2], 'base64url'))) throw new Error();
    const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (claims.iss !== ISSUER || !audiences.includes(clientId) || (audiences.length > 1 && claims.azp !== clientId)
      || !Number.isFinite(claims.exp) || claims.exp <= now / 1000
      || !Number.isFinite(claims.iat) || claims.iat > now / 1000 + 60 || !text(claims.sub)
      || (claims.nbf !== undefined && (!Number.isFinite(claims.nbf) || claims.nbf > now / 1000 + 60))) throw new Error();
    if (nonce !== undefined && !equal(claims.nonce, nonce)) throw new Error();
    if (subject && claims.sub !== subject) fail('CHATGPT_ACCOUNT_MISMATCH');
    return { subject: claims.sub, email: typeof claims.email === 'string' ? claims.email : null };
  } catch (error) {
    if (error instanceof ChatGPTError) throw error;
    fail('CHATGPT_INVALID_ID_TOKEN');
  }
}
export async function discovery(fetchImpl = fetch, signal) {
  const value = await requestJSON(ISSUER + '/.well-known/openid-configuration', { signal }, fetchImpl);
  if (value.issuer !== ISSUER) fail('CHATGPT_DISCOVERY_INVALID');
  for (const field of ['jwks_uri', 'revocation_endpoint']) {
    if (!text(value[field])) fail('CHATGPT_DISCOVERY_INVALID');
    const url = new URL(value[field]);
    if (url.origin !== ISSUER || url.username || url.password || url.hash) fail('CHATGPT_DISCOVERY_INVALID');
  }
  return value;
}
export async function verifyIdentity(idToken, settings, fetchImpl = fetch, signal) {
  const config = await discovery(fetchImpl, signal);
  const jwks = await requestJSON(config.jwks_uri, { signal }, fetchImpl);
  return validateIdToken(idToken, jwks, settings);
}
function tokenRecord(tokens, previous, now) {
  if (!text(tokens?.access_token, 30000) || !text(tokens?.id_token, 20000)
    || tokens.token_type?.toLowerCase() !== 'bearer' || !Number.isFinite(tokens.expires_in)
    || tokens.expires_in <= 0 || tokens.expires_in > 31536000) fail('CHATGPT_INVALID_TOKEN_RESPONSE');
  const scopes = typeof tokens.scope === 'string' ? tokens.scope.split(/\s+/).filter(Boolean) : previous?.scopes;
  if (!Array.isArray(scopes) || !scopes.includes(SHARING_SCOPE)) fail('CHATGPT_SHARING_NOT_ENABLED');
  if (tokens.refresh_token !== undefined && !text(tokens.refresh_token, 30000)) fail('CHATGPT_INVALID_TOKEN_RESPONSE');
  return { access_token: tokens.access_token, refresh_token: tokens.refresh_token || previous?.refresh_token || null,
    id_token: tokens.id_token, expires_at: now + tokens.expires_in * 1000, scopes };
}
export function safeProfile(profile) {
  return { profile_id: profile.id, email: profile.email || null, status: profile.tokens ? 'connected' : 'disconnected',
    sharing: Boolean(profile.tokens?.scopes?.includes(SHARING_SCOPE)), active: false };
}
export function openSystemBrowser(url) {
  if (process.platform !== 'win32') fail('CHATGPT_WINDOWS_REQUIRED');
  return new Promise((resolve, reject) => {
    const child = spawn('rundll32.exe', ['url.dll,FileProtocolHandler', url], { windowsHide: true, stdio: 'ignore' });
    child.on('error', () => reject(new ChatGPTError('CHATGPT_BROWSER_OPEN_ERROR')));
    child.on('spawn', () => { child.unref(); resolve(); });
  });
}
export async function callbackListener(attempt, oldClientId, signal) {
  signal.throwIfAborted();
  let resolve, reject;
  const result = new Promise((yes, no) => { resolve = yes; reject = no; });
  // The caller may still be opening the browser when the deadline fires.
  result.catch(() => {});
  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (req.method !== 'GET' || url.pathname !== '/auth/callback') { res.writeHead(404); res.end(); return; }
    try {
      const value = callbackResult(url, attempt, oldClientId);
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'none'" });
      res.end('Tillegal: da nhan callback. Quay lai PowerShell de xem ket qua dang nhap.');
      resolve(value);
    } catch (error) {
      res.writeHead(400, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end('Tillegal: callback rejected.');
      // Unrelated requests with wrong state cannot terminate a valid login attempt.
      if (error.code !== 'CHATGPT_OAUTH_STATE_MISMATCH') reject(error);
    }
  });
  await new Promise((yes, no) => { server.once('error', no); server.listen(0, '127.0.0.1', yes); });
  const abort = () => { reject(new ChatGPTError('CHATGPT_LOGIN_TIMEOUT')); server.closeAllConnections(); server.close(); };
  if (signal.aborted) abort(); else signal.addEventListener('abort', abort, { once: true });
  return { redirect: `http://127.0.0.1:${server.address().port}/auth/callback`, result,
    close: () => { signal.removeEventListener('abort', abort); server.closeAllConnections(); server.close(); } };
}

export function createAuth(store, { fetchImpl = fetch, openBrowser = openSystemBrowser, clock = Date.now } = {}) {
  const active = state => {
    const profile = state.profiles.find(p => p.id === state.active);
    if (!profile) fail('CHATGPT_SIGN_IN_REQUIRED');
    return profile;
  };
  async function finishPending(state, profile, signal) {
    const pending = profile.pending;
    const identity = await verifyIdentity(pending.tokens.id_token,
      { clientId: profile.client_id, subject: profile.subject, now: pending.received_at }, fetchImpl, signal);
    profile.subject = identity.subject; profile.email = identity.email;
    profile.tokens = pending.tokens; delete profile.pending;
    await store.save(state);
  }
  return {
    async status() {
      return store.withLock(async () => {
        const state = await store.load();
        return { profiles: state.profiles.map(p => ({ ...safeProfile(p), active: p.id === state.active })) };
      });
    },
    async select(profileId) {
      return store.withLock(async () => {
        const state = await store.load();
        if (!state.profiles.some(p => p.id === profileId)) fail('CHATGPT_PROFILE_NOT_FOUND');
        state.active = profileId; await store.save(state);
        return { ...safeProfile(active(state)), active: true };
      });
    },
    async login({ newAccount = false, signal = AbortSignal.timeout(600000) } = {}) {
      return store.withLock(async () => {
        const state = await store.load();
        let profile = !newAccount ? state.profiles.find(p => p.id === (state.pending_profile || state.active)) : null;
        if (!profile) { profile = { id: randomUUID(), client_id: null }; state.profiles.push(profile); }
        state.pending_profile = profile.id;
        // Host and pending identity are durable even if browser login is cancelled.
        await store.save(state);
        const attempt = createAttempt();
        const listener = await callbackListener(attempt, profile.client_id, signal);
        try {
          signal.throwIfAborted();
          await openBrowser(authorizeURL(attempt, listener.redirect, state.host_id, profile));
          const result = await listener.result;
          profile.client_id = result.clientId;
          await store.save(state); // retain the issued ID if exchange later fails
          const tokens = await requestJSON(TOKEN_URL, { method: 'POST', signal,
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({ grant_type: 'authorization_code', client_id: result.clientId,
              code: result.code, code_verifier: attempt.verifier, redirect_uri: listener.redirect, resource: RESOURCE }) }, fetchImpl);
          const receivedAt = clock();
          const record = tokenRecord(tokens, null, receivedAt);
          const identity = await verifyIdentity(record.id_token,
            { clientId: result.clientId, nonce: attempt.nonce, subject: profile.subject, now: receivedAt }, fetchImpl, signal);
          profile.tokens = record; profile.subject = identity.subject; profile.email = identity.email;
          delete profile.pending; delete state.pending_profile;
          state.active = profile.id; await store.save(state);
          return { ...safeProfile(profile), active: true };
        } finally { listener.close(); }
      });
    },
    async accessToken(signal) {
      return store.withLock(async () => {
        const state = await store.load(), profile = active(state);
        if (profile.pending) await finishPending(state, profile, signal);
        if (!profile.tokens) fail('CHATGPT_SIGN_IN_REQUIRED');
        if (profile.tokens.expires_at <= clock() + 60000) {
          if (!profile.tokens.refresh_token) fail('CHATGPT_SIGN_IN_REQUIRED');
          const data = await requestJSON(TOKEN_URL, { method: 'POST', signal,
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({ grant_type: 'refresh_token', client_id: profile.client_id,
              refresh_token: profile.tokens.refresh_token, resource: RESOURCE }) }, fetchImpl);
          const receivedAt = clock();
          // Persist the replacement before JWKS fetch/verification, so an outage cannot lose rotation.
          profile.pending = { tokens: tokenRecord(data, profile.tokens, receivedAt), received_at: receivedAt };
          await store.save(state);
          await finishPending(state, profile, signal);
        }
        if (!profile.tokens.scopes.includes(SHARING_SCOPE)) fail('CHATGPT_SHARING_NOT_ENABLED');
        return profile.tokens.access_token; // Trusted local transport only; never part of bridge JSON.
      });
    },
    async logout(signal) {
      return store.withLock(async () => {
        const state = await store.load(), profile = active(state);
        let confirmed = !profile.tokens?.refresh_token;
        try {
          if (!confirmed) {
            const config = await discovery(fetchImpl, signal);
            const response = await fetchImpl(config.revocation_endpoint, { method: 'POST', redirect: 'error', signal,
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({
                client_id: profile.client_id, token: profile.pending?.tokens?.refresh_token || profile.tokens.refresh_token,
                token_type_hint: 'refresh_token' }) });
            confirmed = response.status === 200; await response.body?.cancel();
          }
        } catch { confirmed = false; }
        delete profile.tokens; delete profile.id_token; delete profile.pending;
        await store.save(state);
        return { signed_out: true, remote_revocation_confirmed: confirmed,
          ...(confirmed ? {} : { error_code: 'CHATGPT_REVOCATION_NOT_CONFIRMED' }) };
      });
    },
  };
}
