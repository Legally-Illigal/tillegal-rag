"""Server-side OpenAI Responses API provider, Python stdlib only.

One HTTPS POST, no SDK/automatic retry/fallback, no personal ChatGPT session.
Structured decoding uses the Judge schema shape; JudgeAgent still validates the
full contract and cross-field/ID constraints. No raw inputs/responses in telemetry.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import math
import os
import re
from time import perf_counter
from urllib import error, request

from ai_core.data_contracts import canonical_json
from ai_core.judge_contracts import OUTPUT_SCHEMA
from ai_core.rag_generation import GenerationError, GenerationText, NoRedirect, strict_json

ENDPOINT = "https://api.openai.com/v1/responses"
MAX_RESPONSE_BYTES = 1048576


def openai_output_schema():
    """Portable strict-decoding subset, same fields/enums as the Judge contract.

    Length/regex/array-count/uniqueness constraints stay in the full local
    validator; omitting wire constraints is not permission to repair bad output.
    No model-name heuristics or fallback to unconstrained JSON mode.
    """
    omit = {"minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems"}

    def project(value):
        if isinstance(value, dict):
            return {key: project(item) for key, item in value.items() if key not in omit}
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    return project(deepcopy(OUTPUT_SCHEMA))


def _usage(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("usage shape")
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if key in value:
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError("usage count")
            result[key] = value[key]
    for key, nested in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        if value.get(key) is not None:
            if not isinstance(value[key], dict):
                raise ValueError("usage details")
            if nested in value[key]:
                count = value[key][nested]
                if type(count) is not int or count < 0:
                    raise ValueError("usage detail count")
                result[key] = {nested: count}
    return result


@dataclass(frozen=True)
class OpenAIProvider:
    model: str
    api_key: str = field(repr=False)
    allow_live: bool = False
    timeout_seconds: float = 30
    max_output_tokens: int = 2048
    max_input_bytes: int = 120000
    max_output_chars: int = 24000
    mode = "live"

    def __post_init__(self):
        if self.allow_live is not True:
            raise GenerationError("OPENAI_LIVE_OPT_IN_REQUIRED")
        if not isinstance(self.api_key, str) or not self.api_key:
            raise GenerationError("OPENAI_API_KEY_REQUIRED")
        if not isinstance(self.model, str) or not self.model:
            raise GenerationError("OPENAI_MODEL_REQUIRED")
        if (not re.fullmatch(r"[\x21-\x7e]{1,4096}", self.api_key)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", self.model)
                or self.api_key in self.model
                or type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 120
                or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 32768
                or type(self.max_input_bytes) is not int or not 1024 <= self.max_input_bytes <= 120000
                or type(self.max_output_chars) is not int or not 128 <= self.max_output_chars <= 65536):
            raise GenerationError("OPENAI_CONFIGURATION_ERROR")

    @classmethod
    def from_environment(cls, *, model=None, allow_live=False, timeout_seconds=30,
                         max_output_tokens=2048, max_input_bytes=120000, max_output_chars=24000):
        """Explicit environment only. Does not load .env, cookies or OAuth tokens."""
        return cls(model=os.environ.get("OPENAI_MODEL", "") if model is None else model,
                   api_key=os.environ.get("OPENAI_API_KEY", ""), allow_live=allow_live,
                   timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens,
                   max_input_bytes=max_input_bytes, max_output_chars=max_output_chars)

    def generate(self, messages):
        started = perf_counter()
        metadata = {"requested_model": self.model, "returned_model": None, "usage": None,
                    "stop_reason": None, "http_status": None, "latency_ms": None}

        def fail(code):
            metadata["latency_ms"] = round((perf_counter() - started) * 1000, 3)
            exc = GenerationError(code)
            exc.metadata = deepcopy(metadata)
            raise exc from None

        if (not isinstance(messages, list) or len(messages) != 2 or any(not isinstance(m, dict) for m in messages)
                or messages[0].get("role") != "system" or messages[1].get("role") != "user"
                or any(not isinstance(m.get("content"), str) or not m["content"].strip() for m in messages)):
            fail("OPENAI_INVALID_INPUT")
        payload = {"model": self.model, "instructions": messages[0]["content"],
                   "input": [{"role": "user", "content": messages[1]["content"]}],
                   "text": {"format": {"type": "json_schema", "name": "tillegal_judge_v1",
                                          "strict": True, "schema": openai_output_schema()}},
                   "max_output_tokens": self.max_output_tokens,
                   "store": False, "stream": False, "truncation": "disabled"}
        body = canonical_json(payload)
        if len(body) > self.max_input_bytes:
            fail("OPENAI_INPUT_LIMIT")
        if self.api_key in body.decode("utf-8"):
            fail("OPENAI_SECRET_IN_INPUT")
        req = request.Request(ENDPOINT, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": "Bearer " + self.api_key})
        try:
            # No SDK retry, redirect, proxy inheritance, alternative endpoint or provider.
            with request.build_opener(request.ProxyHandler({}), NoRedirect()).open(
                    req, timeout=self.timeout_seconds) as response:
                metadata["http_status"] = response.status
                if response.status != 200:
                    fail("OPENAI_HTTP_ERROR")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                fail("OPENAI_RESPONSE_LIMIT")
            decoded = raw.decode("utf-8")
            if self.api_key in decoded:
                fail("OPENAI_SECRET_IN_RESPONSE")
            value = strict_json(decoded)
            if not isinstance(value, dict) or value.get("object") != "response":
                fail("OPENAI_INVALID_RESPONSE")
            returned = value.get("model")
            if returned is not None and (not isinstance(returned, str) or not returned.strip() or len(returned) > 200):
                fail("OPENAI_INVALID_RESPONSE")
            metadata["returned_model"] = returned
            metadata["usage"] = _usage(value.get("usage"))
            status = value.get("status")
            metadata["stop_reason"] = status if status in ("completed", "incomplete", "failed", "cancelled", "queued", "in_progress") else None
            if status == "incomplete":
                fail("OPENAI_INCOMPLETE_RESPONSE")
            if status != "completed" or value.get("error") is not None:
                fail("OPENAI_RESPONSE_FAILED")
            output = value.get("output")
            if not isinstance(output, list):
                fail("OPENAI_INVALID_RESPONSE")
            texts = []
            for item in output:
                if not isinstance(item, dict):
                    fail("OPENAI_INVALID_RESPONSE")
                if item.get("type") == "reasoning":
                    continue
                if (item.get("type") != "message" or item.get("role") != "assistant"
                        or item.get("status") != "completed" or not isinstance(item.get("content"), list)):
                    fail("OPENAI_UNEXPECTED_OUTPUT")
                for part in item["content"]:
                    if not isinstance(part, dict):
                        fail("OPENAI_INVALID_RESPONSE")
                    if part.get("type") == "refusal":
                        fail("OPENAI_REFUSAL")
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        fail("OPENAI_UNEXPECTED_OUTPUT")
                    texts.append(part["text"])
            text = "".join(texts)
            if not text.strip():
                fail("OPENAI_EMPTY_RESPONSE")
            if len(text) > self.max_output_chars:
                fail("OPENAI_OUTPUT_LIMIT")
            metadata["latency_ms"] = round((perf_counter() - started) * 1000, 3)
            return GenerationText(text, metadata)
        except GenerationError:
            raise
        except error.HTTPError as exc:
            metadata["http_status"] = exc.code
            status = exc.code
            # Read only bounded error code/param to classify. Never return/log message or body.
            api_code, api_type, param = None, None, None
            try:
                error_body = exc.read(65537)
                if len(error_body) <= 65536:
                    detail = strict_json(error_body.decode("utf-8"))
                    detail = detail.get("error", {}) if isinstance(detail, dict) else {}
                    if isinstance(detail, dict):
                        api_code, api_type, param = detail.get("code"), detail.get("type"), detail.get("param")
            except (ValueError, TypeError, OSError, RecursionError):
                pass
            finally:
                exc.close()
            code = {401: "OPENAI_AUTHENTICATION_ERROR", 403: "OPENAI_PERMISSION_ERROR",
                    404: "OPENAI_MODEL_UNAVAILABLE", 408: "PROVIDER_TIMEOUT", 504: "PROVIDER_TIMEOUT",
                    429: "OPENAI_RATE_LIMIT"}.get(status, "OPENAI_HTTP_ERROR")
            if 300 <= status < 400:
                code = "OPENAI_REDIRECT_BLOCKED"
            elif status == 429 and (api_code in ("insufficient_quota", "credit_balance_exhausted")
                                    or api_type == "insufficient_quota"):
                code = "OPENAI_QUOTA_EXCEEDED"
            elif status == 400:
                code = "OPENAI_BAD_REQUEST"
                if api_code == "context_length_exceeded":
                    code = "OPENAI_CONTEXT_LIMIT"
                elif api_code == "model_not_found":
                    code = "OPENAI_MODEL_UNAVAILABLE"
                elif api_code == "unsupported_parameter":
                    code = "OPENAI_UNSUPPORTED_PARAMETER"
                elif api_code == "invalid_json_schema" or param in ("text.format", "text.format.schema", "response_format"):
                    code = "OPENAI_SCHEMA_OR_MODEL_UNSUPPORTED"
            fail(code)
        except TimeoutError:
            fail("PROVIDER_TIMEOUT")
        except error.URLError as exc:
            fail("PROVIDER_TIMEOUT" if isinstance(exc.reason, TimeoutError) else "OPENAI_NETWORK_ERROR")
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError, UnicodeError):
            fail("OPENAI_INVALID_RESPONSE")
        except OSError:
            fail("OPENAI_NETWORK_ERROR")
