"""Direct Anthropic Messages API, one request, stdlib, no credential discovery."""
from dataclasses import dataclass, field
from copy import deepcopy
import math
import os
import re
from time import perf_counter
from urllib import error, request

from ai_core.data_contracts import canonical_json
from ai_core.rag_generation import GenerationError, GenerationText, NoRedirect, strict_json, MAX_OUTPUT_CHARS, DRAFT_OUTPUT_SCHEMA

ENDPOINT = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2026-10-21'
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


@dataclass(frozen=True)
class AnthropicProvider:
    model: str
    api_key: str = field(repr=False)
    allow_live: bool = False
    timeout_seconds: float = 30
    max_tokens: int = 1024
    max_input_bytes: int = 121000
    thinking_type: str | None = None
    structured_output: bool = False
    input_token_cap: int | None = None
    output_schema: dict | None = field(default=None, repr=False)
    mode = 'live'

    def __post_init__(self):
        if (not self.allow_live or not isinstance(self.api_key, str) or not self.api_key.strip()
                or any(c.isspace() for c in self.api_key)
                or not isinstance(self.model, str) or not re.fullmatch(r'claude-[a-z0-9.-]+', self.model)
                or self.api_key in self.model
                or type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120
                or type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 2048
                or type(self.max_input_bytes) is not int or not 1 <= self.max_input_bytes <= 121000
                or self.thinking_type not in (None, 'disabled', 'between_tools')
                or type(self.structured_output) is not bool
                or (self.output_schema is not None and (not isinstance(self.output_schema, dict) or not self.output_schema))
                or (self.input_token_cap is not None and (type(self.input_token_cap) is not int or not 1 <= self.input_token_cap <= 12000))):
            raise GenerationError('ANTHROPIC_CONFIGURATION_REQUIRED')

    @classmethod
    def from_environment(cls, *, model=None, allow_live=False, timeout_seconds=30, max_tokens=1024,
                         max_input_bytes=121000, thinking_type=None, structured_output=False, input_token_cap=None, output_schema=None):
        # No generic key fallback, .env loading, OS credential store, or proxy URL.
        return cls(model=model if model is not None else os.environ.get('ANTHROPIC_MODEL', ''),
                   api_key=os.environ.get('ANTHROPIC_API_KEY', ''), allow_live=allow_live,
                   timeout_seconds=timeout_seconds, max_tokens=max_tokens,
                   max_input_bytes=max_input_bytes, thinking_type=thinking_type,
                   structured_output=structured_output, input_token_cap=input_token_cap,
                   output_schema=deepcopy(output_schema))

    def generate(self, messages):
        started = perf_counter()
        response_text = None
        metadata = {'provider': 'anthropic', 'endpoint': ENDPOINT, 'api_version': API_VERSION,
                    'requested_model': self.model, 'returned_model': None, 'usage': None,
                    'stop_reason': None, 'http_status': None, 'max_output_tokens': self.max_tokens,
                    'thinking_type': self.thinking_type, 'max_input_bytes': self.max_input_bytes,
                    'structured_output': self.structured_output, 'input_token_cap': self.input_token_cap,
                    'token_count_http_attempts': 0, 'generation_http_attempts': 0}

        def fail(code):
            metadata['latency_ms'] = (perf_counter()-started)*1000
            exc = GenerationError(code)
            exc.metadata = dict(metadata)
            if response_text is not None:
                exc.raw_text = response_text[:MAX_OUTPUT_CHARS]
                exc.raw_text_truncated = len(response_text) > MAX_OUTPUT_CHARS
            raise exc from None

        if (not isinstance(messages, list) or len(messages) != 2
                or messages[0].get('role') != 'system' or messages[1].get('role') != 'user'
                or any(not isinstance(m.get('content'), str) for m in messages)):
            fail('ANTHROPIC_INVALID_INPUT')
        payload = {'model': self.model, 'max_tokens': self.max_tokens, 'stream': False,
                   'system': messages[0]['content'],
                   'messages': [{'role': 'user', 'content': messages[1]['content']}]}
        if self.thinking_type is not None:
            payload['thinking'] = {'type': self.thinking_type}
        if self.structured_output:
            payload['output_config'] = {'format': {'type': 'json_schema', 'schema': deepcopy(self.output_schema if self.output_schema is not None else DRAFT_OUTPUT_SCHEMA)}}
            metadata['output_config'] = deepcopy(payload['output_config'])
        body = canonical_json(payload)
        metadata['request_body_bytes'] = len(body)
        if len(body) > self.max_input_bytes:
            fail('GENERATION_INPUT_LIMIT')
        if self.api_key in body.decode('utf-8'):
            fail('SECRET_IN_PROVIDER_INPUT')
        req = request.Request(ENDPOINT, data=body, method='POST', headers={
            'Content-Type': 'application/json', 'x-api-key': self.api_key,
            'anthropic-version': API_VERSION})
        try:
            if self.input_token_cap is not None:
                count_payload = {k: v for k, v in payload.items() if k not in ('max_tokens', 'stream')}
                count_req = request.Request(ENDPOINT + '/count_tokens', data=canonical_json(count_payload),
                                            method='POST', headers=dict(req.header_items()))
                metadata['token_count_http_attempts'] = 1
                with request.build_opener(request.ProxyHandler({}), NoRedirect()).open(
                        count_req, timeout=self.timeout_seconds) as response:
                    if response.status != 200:
                        fail('ANTHROPIC_TOKEN_COUNT_ERROR')
                    count_raw = response.read(1048577)
                if len(count_raw) > 1048576:
                    fail('ANTHROPIC_TOKEN_COUNT_ERROR')
                counted = strict_json(count_raw.decode('utf-8')).get('input_tokens')
                if type(counted) is not int or counted < 0:
                    fail('ANTHROPIC_TOKEN_COUNT_ERROR')
                metadata['counted_input_tokens'] = counted
                if counted > self.input_token_cap:
                    fail('INPUT_TOKEN_BUDGET_EXCEEDED')
            # Ignore environment/OS proxies; never forward the key on redirects.
            metadata['generation_http_attempts'] = 1
            with request.build_opener(request.ProxyHandler({}), NoRedirect()).open(
                    req, timeout=self.timeout_seconds) as response:
                metadata['http_status'] = response.status
                if response.status != 200:
                    fail('ANTHROPIC_HTTP_ERROR')
                raw = response.read(1048577)
            if len(raw) > 1048576:
                fail('PROVIDER_RESPONSE_LIMIT')
            decoded = raw.decode('utf-8')
            if self.api_key in decoded:
                fail('SECRET_IN_PROVIDER_RESPONSE')
            obj = strict_json(decoded)
            if not isinstance(obj, dict) or obj.get('type') != 'message' or obj.get('role') != 'assistant':
                fail('ANTHROPIC_INVALID_RESPONSE')
            model = obj.get('model')
            if not isinstance(model, str) or not re.fullmatch(r'claude-[a-z0-9.-]+', model):
                fail('ANTHROPIC_INVALID_RESPONSE')
            metadata['returned_model'] = model
            usage = obj.get('usage')
            if usage is not None:
                if (not isinstance(usage, dict) or any(k not in usage for k in USAGE_KEYS[:2])
                        or any(type(usage[k]) is not int or usage[k] < 0 for k in USAGE_KEYS if k in usage)):
                    fail('ANTHROPIC_INVALID_USAGE')
                metadata['usage'] = {k: usage[k] for k in USAGE_KEYS if k in usage}
            reason = obj.get('stop_reason')
            allowed = ('end_turn', 'max_tokens', 'stop_sequence', 'tool_use', 'pause_turn', 'refusal', 'model_context_window_exceeded')
            metadata['stop_reason'] = reason if reason in allowed else 'UNKNOWN'
            blocks = obj.get('content')
            # Preserve only text (never thinking/signatures), including incomplete drafts.
            if isinstance(blocks, list) and all(isinstance(b, dict) and b.get('type') == 'text'
                                                and isinstance(b.get('text'), str) for b in blocks):
                response_text = ''.join(b['text'] for b in blocks)
            if reason != 'end_turn' or (obj.get('stop_details') or {}).get('type') == 'refusal':
                fail('ANTHROPIC_INCOMPLETE_OR_REFUSED')
            if (not isinstance(blocks, list) or not blocks
                    or any(not isinstance(b, dict) or b.get('type') != 'text'
                           or not isinstance(b.get('text'), str) for b in blocks)):
                fail('ANTHROPIC_UNEXPECTED_CONTENT')
            text = ''.join(b['text'] for b in blocks)
            if not text.strip():
                fail('ANTHROPIC_EMPTY_CONTENT')
            metadata['latency_ms'] = (perf_counter()-started)*1000
            return GenerationText(text, metadata)
        except GenerationError:
            raise
        except error.HTTPError as exc:
            status = exc.code
            metadata['http_status'] = status
            exc.close()  # never read/log the error body or request headers
            fail({401: 'ANTHROPIC_AUTHENTICATION_ERROR', 402: 'ANTHROPIC_BILLING_ERROR',
                  403: 'ANTHROPIC_PERMISSION_ERROR', 504: 'PROVIDER_TIMEOUT',
                  429: 'ANTHROPIC_RATE_LIMIT', 404: 'ANTHROPIC_MODEL_OR_RESOURCE_NOT_FOUND',
                  400: 'ANTHROPIC_BAD_REQUEST'}.get(status, 'ANTHROPIC_PROVIDER_ERROR'))
        except TimeoutError:
            fail('PROVIDER_TIMEOUT')
        except error.URLError as exc:
            fail('PROVIDER_TIMEOUT' if isinstance(exc.reason, TimeoutError) else 'ANTHROPIC_NETWORK_ERROR')
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            fail('ANTHROPIC_INVALID_RESPONSE')
        except OSError:
            fail('ANTHROPIC_NETWORK_ERROR')
