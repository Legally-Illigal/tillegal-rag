"""Gemini Developer API for Judge. Optional SDK; no OAuth, retries or fallback."""
from copy import deepcopy
from dataclasses import dataclass, field
import math
import os
import re
from time import perf_counter

from ai_core.data_contracts import canonical_json
from ai_core.judge_contracts import OUTPUT_SCHEMA
from ai_core.rag_generation import GenerationError, GenerationText

BASE_URL = "https://generativelanguage.googleapis.com/"


def _error_diagnostics(exc):
    """Fixed labels only; never retain Google's free-form messages/details."""
    statuses = {"INVALID_ARGUMENT", "UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND",
                "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION", "INTERNAL", "UNAVAILABLE",
                "DEADLINE_EXCEEDED", "UNKNOWN"}
    status = getattr(exc, "status", None)
    message = getattr(exc, "message", None)
    text = message.casefold() if isinstance(message, str) else ""
    reason = None
    if "no longer available to new users" in text:
        reason = "MODEL_ACCESS_RESTRICTED"
    elif "overloaded" in text:
        reason = "MODEL_OVERLOADED"
    elif "high demand" in text or "high traffic" in text:
        reason = "MODEL_HIGH_DEMAND"
    elif "temporarily unavailable" in text:
        reason = "TEMPORARILY_UNAVAILABLE"
    return {"api_status": status if status in statuses else None, "error_reason": reason}


def gemini_output_schema():
    """Portable decoding subset. Full constraints remain enforced by JudgeAgent."""
    omit = {"minLength", "maxLength", "pattern", "uniqueItems"}

    def project(value):
        if isinstance(value, dict):
            return {k: project(v) for k, v in value.items() if k not in omit}
        if isinstance(value, list):
            return [project(v) for v in value]
        return value
    return project(deepcopy(OUTPUT_SCHEMA))


def _load_sdk():
    # Import only inside generate: mock/baseline and empty evidence need no SDK.
    from google import genai
    from google.genai import types, errors
    import httpx
    return genai, types, errors, httpx


@dataclass(frozen=True)
class GeminiProvider:
    model: str
    api_key: str = field(repr=False)
    allow_live: bool = False
    timeout_seconds: float = 30
    max_output_tokens: int = 4096
    max_input_bytes: int = 120000
    max_output_chars: int = 24000
    mode = "live"
    provider_name = "gemini"

    def __post_init__(self):
        if self.allow_live is not True:
            raise GenerationError("GEMINI_LIVE_OPT_IN_REQUIRED")
        if not isinstance(self.api_key, str) or not self.api_key:
            raise GenerationError("GEMINI_API_KEY_REQUIRED")
        if not isinstance(self.model, str) or not self.model:
            raise GenerationError("GEMINI_MODEL_REQUIRED")
        if (not re.fullmatch(r"[\x21-\x7e]{1,4096}", self.api_key)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", self.model)
                or self.api_key in self.model
                or type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120
                or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 32768
                or type(self.max_input_bytes) is not int or not 1024 <= self.max_input_bytes <= 120000
                or type(self.max_output_chars) is not int or not 128 <= self.max_output_chars <= 65536):
            raise GenerationError("GEMINI_CONFIGURATION_ERROR")

    @classmethod
    def from_environment(cls, *, model=None, allow_live=False, timeout_seconds=30,
                         max_output_tokens=4096, max_input_bytes=120000, max_output_chars=24000):
        """Only the explicit server env; no .env discovery, ADC, cookie or OAuth."""
        return cls(model=os.environ.get("GEMINI_MODEL", "") if model is None else model,
                   api_key=os.environ.get("GEMINI_API_KEY", ""), allow_live=allow_live,
                   timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens,
                   max_input_bytes=max_input_bytes, max_output_chars=max_output_chars)

    def generate(self, messages):
        started = perf_counter()
        metadata = {"requested_model": self.model, "returned_model": None, "usage": None,
                    "stop_reason": None, "http_status": None, "latency_ms": None,
                    "api_status": None, "error_reason": None, "timeout_kind": None}

        def fail(code):
            metadata["latency_ms"] = round((perf_counter() - started) * 1000, 3)
            exc = GenerationError(code)
            exc.metadata = deepcopy(metadata)
            raise exc from None

        if (not isinstance(messages, list) or len(messages) != 2
                or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                       or not isinstance(m["content"], str) for m in messages)
                or [m["role"] for m in messages] != ["system", "user"]):
            fail("GEMINI_INVALID_MESSAGES")
        if len(canonical_json(messages)) > self.max_input_bytes:
            fail("GEMINI_INPUT_LIMIT")
        try:
            genai, types, errors, httpx = _load_sdk()
        except ImportError:
            fail("GEMINI_DEPENDENCY_MISSING")

        try:
            # Fixed Developer API host; do not inherit enterprise/Vertex/ADC env.
            options = types.HttpOptions(base_url=BASE_URL, api_version="v1beta",
                timeout=max(1, math.ceil(self.timeout_seconds * 1000)),
                retry_options=types.HttpRetryOptions(attempts=1),
                client_args={"trust_env": False, "follow_redirects": False})
            config = types.GenerateContentConfig(system_instruction=messages[0]["content"],
                response_mime_type="application/json", response_json_schema=gemini_output_schema(),
                # candidate_count is unsupported on Gemini 3/4. Omit universally;
                # require exactly one returned candidate below instead.
                max_output_tokens=self.max_output_tokens,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                automatic_continuation=False)
            with genai.Client(api_key=self.api_key, enterprise=False, vertexai=False,
                              http_options=options) as client:
                response = client.models.generate_content(model=self.model,
                    contents=[types.Content(role="user", parts=[types.Part(text=messages[1]["content"])])],
                    config=config)
        except errors.APIError as exc:
            status = getattr(exc, "code", None)
            metadata["http_status"] = status if type(status) is int else None
            metadata.update(_error_diagnostics(exc))
            if status in (408, 504):
                metadata["timeout_kind"] = "upstream_http"
            details = getattr(exc, "details", None)
            nested = details.get("error", {}) if isinstance(details, dict) else {}
            reasons = nested.get("details", []) if isinstance(nested, dict) else []
            if isinstance(reasons, list) and any(isinstance(d, dict) and d.get("reason") in
                    {"API_KEY_INVALID", "API_KEY_EXPIRED"} for d in reasons):
                fail("GEMINI_AUTHENTICATION_ERROR")
            codes = {400: "GEMINI_BAD_REQUEST", 401: "GEMINI_AUTHENTICATION_ERROR",
                     403: "GEMINI_PERMISSION_DENIED", 404: "GEMINI_MODEL_UNAVAILABLE",
                     408: "GEMINI_TIMEOUT", 429: "GEMINI_QUOTA_OR_RATE_LIMIT",
                     503: "GEMINI_SERVICE_UNAVAILABLE", 504: "GEMINI_TIMEOUT"}
            fail(codes.get(status, "GEMINI_SERVER_ERROR" if isinstance(status, int) and status >= 500
                           else "GEMINI_API_ERROR"))
        except (httpx.TimeoutException, TimeoutError) as exc:
            metadata["timeout_kind"] = ("connect" if isinstance(exc, httpx.ConnectTimeout) else
                "read" if isinstance(exc, httpx.ReadTimeout) else
                "write" if isinstance(exc, httpx.WriteTimeout) else
                "pool" if isinstance(exc, httpx.PoolTimeout) else "transport")
            fail("GEMINI_TIMEOUT")
        except (httpx.RequestError, ConnectionError, OSError):
            fail("GEMINI_NETWORK_ERROR")
        except Exception:
            # Never retain exception messages: they can contain input or credentials.
            fail("GEMINI_SDK_ERROR")

        # Only explicitly safe metadata. Never use response.text (may log warnings/parts).
        returned = getattr(response, "model_version", None)
        if isinstance(returned, str) and re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", returned) and self.api_key not in returned:
            metadata["returned_model"] = returned
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            metadata["usage"] = {k: v for k in ("prompt_token_count", "candidates_token_count",
                "thoughts_token_count", "total_token_count", "cached_content_token_count")
                if type(v := getattr(usage, k, None)) is int and v >= 0}
        feedback = getattr(response, "prompt_feedback", None)
        block = getattr(getattr(feedback, "block_reason", None), "value", getattr(feedback, "block_reason", None))
        if block and block != "BLOCK_REASON_UNSPECIFIED":
            fail("GEMINI_SAFETY_BLOCK")
        candidates = getattr(response, "candidates", None)
        if not candidates:
            fail("GEMINI_EMPTY_RESPONSE")
        if len(candidates) != 1:
            fail("GEMINI_INVALID_RESPONSE")
        candidate = candidates[0]
        finish = getattr(candidate.finish_reason, "value", candidate.finish_reason)
        known = {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT",
                 "SPII", "OTHER", "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "FINISH_REASON_UNSPECIFIED"}
        metadata["stop_reason"] = finish if finish in known else None
        if finish == "MAX_TOKENS":
            fail("GEMINI_INCOMPLETE_RESPONSE")
        if finish in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
            fail("GEMINI_SAFETY_BLOCK")
        if finish != "STOP":
            fail("GEMINI_INCOMPLETE_RESPONSE")
        if any(getattr(r, "blocked", False) for r in (candidate.safety_ratings or [])):
            fail("GEMINI_SAFETY_BLOCK")
        parts = getattr(candidate.content, "parts", None) or []
        texts = []
        for part in parts:
            if part.thought:
                continue
            # No tools, code, audio, or non-text content accepted as Judge JSON.
            if part.text is None:
                fail("GEMINI_INVALID_RESPONSE")
            texts.append(part.text)
        raw = "".join(texts)
        if not raw.strip():
            fail("GEMINI_EMPTY_RESPONSE")
        if len(raw) > self.max_output_chars:
            fail("GEMINI_OUTPUT_LIMIT")
        metadata["latency_ms"] = round((perf_counter() - started) * 1000, 3)
        return GenerationText(raw, metadata=metadata)
