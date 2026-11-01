import { RESOURCE } from './auth.mjs';
import { ChatGPTError, fail, requestJSON, text } from './common.mjs';

export async function listModels(auth, signal, fetchImpl = fetch) {
  const token = await auth.accessToken(signal);
  const result = await requestJSON(RESOURCE + '/models', {
    headers: { Authorization: 'Bearer ' + token }, signal }, fetchImpl);
  if (!Array.isArray(result.models)) fail('CHATGPT_INVALID_MODEL_CATALOG');
  const models = result.models.filter(m => m.visibility === 'list');
  if (models.some(m => !text(m.slug, 200) || !text(m.display_name, 300))) fail('CHATGPT_INVALID_MODEL_CATALOG');
  return models.map(m => ({ slug: m.slug, display_name: m.display_name }));
}

export function responsePayload(messages, model) {
  if (!Array.isArray(messages) || messages.length !== 2 || messages[0]?.role !== 'system'
      || messages[1]?.role !== 'user' || messages.some(m => !text(m.content, 120000)) || !text(model, 200)) {
    fail('CHATGPT_INVALID_INPUT');
  }
  // SIWC preview: system policy goes into instructions, no max_output_tokens or API-key route.
  return { model, instructions: messages[0].content, input: [{ role: 'user', content: messages[1].content }],
    store: false, stream: true };
}
function completedText(response) {
  if (response?.status !== 'completed') fail('CHATGPT_INVALID_COMPLETION');
  // Some streaming responses deliver output through events, without repeating it at completion.
  if (response.output === null || response.output === undefined) return '';
  if (!Array.isArray(response.output)) fail('CHATGPT_INVALID_COMPLETION');
  let result = '';
  for (const item of response.output) {
    if (item.type === 'reasoning') continue; // Never expose or log reasoning blocks.
    if (item.type !== 'message' || item.role !== 'assistant' || !Array.isArray(item.content)) fail('CHATGPT_UNEXPECTED_OUTPUT');
    for (const part of item.content) {
      if (part.type === 'refusal') fail('CHATGPT_REFUSED');
      if (part.type !== 'output_text' || typeof part.text !== 'string') fail('CHATGPT_UNEXPECTED_OUTPUT');
      result += part.text;
    }
  }
  return result;
}
export async function consumeSSE(response, { signal, maxOutputChars = 24000 } = {}) {
  if (!Number.isInteger(maxOutputChars) || maxOutputChars < 128 || maxOutputChars > 65536) fail('JUDGE_CONFIGURATION_ERROR');
  if (!response.ok) {
    await response.body?.cancel();
    fail(({ 401: 'CHATGPT_SIGN_IN_REQUIRED', 403: 'CHATGPT_PERMISSION_DENIED', 429: 'CHATGPT_USAGE_LIMIT',
      400: 'CHATGPT_BAD_REQUEST', 404: 'CHATGPT_MODEL_OR_RESOURCE_NOT_FOUND', 504: 'PROVIDER_TIMEOUT' })[response.status] || 'CHATGPT_HTTP_ERROR');
  }
  const contentType = response.headers.get('content-type')?.split(';')[0].trim().toLowerCase();
  if (!response.body || (contentType && contentType !== 'text/event-stream')) fail('CHATGPT_INVALID_STREAM');
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '', bytes = 0, textSoFar = '', completed;
  const doneParts = new Map();
  const summary = { event_count: 0, delta_events: 0, done_events: 0,
    delta_chars: 0, done_chars: 0, completed_chars: 0, text_source: null };
  const metadata = { http_status: response.status, stop_reason: null, stream_summary: summary };
  const charCount = value => [...value].length;
  const doneText = () => [...doneParts].sort((a, b) => {
    const x = a[0].split(':').map(Number), y = b[0].split(':').map(Number);
    return x[0] - y[0] || x[1] - y[1];
  }).map(([, value]) => value).join('');
  function rememberPart(outputIndex, contentIndex, value) {
    if (!Number.isSafeInteger(outputIndex) || outputIndex < 0 || !Number.isSafeInteger(contentIndex)
        || contentIndex < 0 || typeof value !== 'string') fail('CHATGPT_INVALID_STREAM');
    const key = `${outputIndex}:${contentIndex}`;
    if (doneParts.has(key) && doneParts.get(key) !== value) fail('CHATGPT_INVALID_COMPLETION');
    doneParts.set(key, value);
    summary.done_chars = charCount(doneText());
    if (summary.done_chars > maxOutputChars) fail('JUDGE_OUTPUT_LIMIT');
  }
  function frame(raw) {
    const data = raw.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
    if (!data || data === '[DONE]') return;
    let event;
    try { event = JSON.parse(data); } catch { fail('CHATGPT_INVALID_STREAM'); }
    summary.event_count++;
    if (event.type === 'response.output_text.delta') {
      if (typeof event.delta !== 'string') fail('CHATGPT_INVALID_STREAM');
      textSoFar += event.delta;
      summary.delta_events++; summary.delta_chars = charCount(textSoFar);
      if (summary.delta_chars > maxOutputChars) fail('JUDGE_OUTPUT_LIMIT');
    } else if (event.type === 'response.output_text.done') {
      summary.done_events++;
      rememberPart(event.output_index, event.content_index, event.text);
    } else if (event.type === 'response.content_part.done') {
      if (event.part?.type === 'refusal') fail('CHATGPT_REFUSED');
      if (event.part?.type === 'output_text') {
        summary.done_events++;
        rememberPart(event.output_index, event.content_index, event.part.text);
      }
    } else if (event.type === 'response.output_item.done') {
      const item = event.item;
      if (item?.type === 'reasoning') return;
      if (item?.type !== 'message' || item.role !== 'assistant' || !Array.isArray(item.content)
          || item.status !== 'completed') fail('CHATGPT_UNEXPECTED_OUTPUT');
      summary.done_events++;
      item.content.forEach((part, index) => {
        if (part.type === 'refusal') fail('CHATGPT_REFUSED');
        if (part.type !== 'output_text') fail('CHATGPT_UNEXPECTED_OUTPUT');
        rememberPart(event.output_index, index, part.text);
      });
    } else if (['response.incomplete', 'response.failed', 'error'].includes(event.type)) {
      fail(event.type === 'response.incomplete' ? 'CHATGPT_INCOMPLETE_RESPONSE' : 'CHATGPT_RESPONSE_FAILED');
    } else if (event.type === 'response.refusal.delta') {
      fail('CHATGPT_REFUSED');
    } else if (event.type === 'response.output_item.added' && event.item?.type
        && !['message', 'reasoning'].includes(event.item.type)) {
      fail('CHATGPT_UNEXPECTED_OUTPUT');
    } else if (event.type === 'response.completed') {
      metadata.stop_reason = 'response.completed';
      if (text(event.response?.model, 200)) metadata.returned_model = event.response.model;
      const terminal = completedText(event.response), done = doneText();
      summary.completed_chars = charCount(terminal);
      // Completion is mandatory. Recover actual streamed text, never fabricate a Judge result.
      const finalText = terminal || done || textSoFar;
      if (!finalText.trim()) fail('CHATGPT_EMPTY_RESPONSE');
      if ([...finalText].length > maxOutputChars) fail('JUDGE_OUTPUT_LIMIT');
      if (textSoFar && textSoFar !== finalText) fail('CHATGPT_INVALID_COMPLETION');
      if (done && done !== finalText) fail('CHATGPT_INVALID_COMPLETION');
      if (!text(event.response.model, 200)) fail('CHATGPT_INVALID_COMPLETION');
      summary.text_source = terminal ? 'completed_output' : done ? 'done_events' : 'stream_deltas';
      completed = { text: finalText, metadata };
      const usage = event.response.usage;
      if (usage && Number.isSafeInteger(usage.input_tokens) && usage.input_tokens >= 0
          && Number.isSafeInteger(usage.output_tokens) && usage.output_tokens >= 0) {
        completed.metadata.usage = { input_tokens: usage.input_tokens, output_tokens: usage.output_tokens };
      }
    }
  }
  try {
    for await (const chunk of response.body) {
      signal?.throwIfAborted();
      bytes += chunk.length;
      if (bytes > 1024 * 1024) fail('CHATGPT_RESPONSE_LIMIT');
      pending += decoder.decode(chunk, { stream: true }).replace(/\r\n/g, '\n');
      // CR/LF can straddle chunks; normalize the accumulated buffer too.
      pending = pending.replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = pending.indexOf('\n\n')) !== -1) {
        frame(pending.slice(0, boundary)); pending = pending.slice(boundary + 2);
        if (completed) return completed;
      }
    }
    pending += decoder.decode();
    if (pending.trim()) frame(pending);
    signal?.throwIfAborted();
    if (!completed) fail('CHATGPT_STREAM_INTERRUPTED');
    return completed;
  } catch (error) {
    const typed = error instanceof ChatGPTError || ['TimeoutError', 'AbortError'].includes(error?.name)
      ? error : new ChatGPTError('CHATGPT_INVALID_STREAM');
    typed.metadata = structuredClone(metadata);
    throw typed;
  }
}
export async function generate(auth, options, fetchImpl = fetch) {
  const { messages, signal, maxOutputChars = 24000 } = options;
  const models = await listModels(auth, signal, fetchImpl);
  const model = options.model || models[0]?.slug;
  if (!model || !models.some(m => m.slug === model)) fail('CHATGPT_MODEL_UNAVAILABLE');
  const token = await auth.accessToken(signal);
  const payload = responsePayload(messages, model);
  if (Buffer.byteLength(JSON.stringify(payload)) > 125000) fail('JUDGE_INPUT_LIMIT');
  let response;
  try {
    response = await fetchImpl(RESOURCE + '/responses', { method: 'POST', redirect: 'error', signal,
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(payload) });
  } catch (error) {
    if (['TimeoutError', 'AbortError'].includes(error?.name)) throw error;
    fail('CHATGPT_NETWORK_ERROR');
  }
  try {
    const result = await consumeSSE(response, { signal, maxOutputChars });
    result.metadata.returned_model ??= model;
    result.metadata.requested_model = model;
    return result;
  } catch (error) {
    error.metadata = { ...error.metadata, requested_model: model };
    throw error;
  }
}
