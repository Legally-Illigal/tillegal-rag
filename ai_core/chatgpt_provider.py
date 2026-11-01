"""ChatGPT plan provider via the official SIWC OAuth/Responses flow.

Node handles OAuth and Windows DPAPI. Python receives only safe state or completed
text, never access/refresh tokens. No browser sessions or Codex credentials are read.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from time import perf_counter

from ai_core.rag_generation import GenerationError, GenerationText, strict_json

BRIDGE = Path(__file__).resolve().parents[1] / "tools/chatgpt/bridge.mjs"


def chatgpt_command(action, *, timeout_seconds=60, **payload):
    node = shutil.which("node")
    if not node:
        raise GenerationError("CHATGPT_NODE_REQUIRED")
    if not BRIDGE.is_file():
        raise GenerationError("CHATGPT_BRIDGE_MISSING")
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600):
        raise GenerationError("JUDGE_CONFIGURATION_ERROR")
    try:
        process = subprocess.run(
            [node, str(BRIDGE)], input=json.dumps({"action": action, "timeout_seconds": timeout_seconds, **payload},
                                                ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=None if action == "login" else subprocess.DEVNULL,
            timeout=timeout_seconds + 20, check=False,
            creationflags=0 if action == "login" else getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if len(process.stdout) > 400000:
            raise GenerationError("CHATGPT_BRIDGE_OUTPUT_LIMIT")
        value = strict_json(process.stdout.decode("utf-8"))
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise ValueError()
        if not value["ok"]:
            code = value.get("error_code")
            exc = GenerationError(code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", code)
                                  else "CHATGPT_BRIDGE_ERROR")
            if isinstance(value.get("metadata"), dict):
                exc.metadata = {k: v for k, v in value["metadata"].items()
                                if k in ("requested_model", "returned_model", "http_status", "stop_reason", "stream_summary")}
            raise exc
        if process.returncode != 0:
            raise GenerationError("CHATGPT_BRIDGE_ERROR")
        return value
    except GenerationError:
        raise
    except subprocess.TimeoutExpired:
        raise GenerationError("PROVIDER_TIMEOUT") from None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise GenerationError("CHATGPT_BRIDGE_ERROR") from None


@dataclass(frozen=True)
class ChatGPTProvider:
    model: str = "chatgpt-account-default"
    allow_live: bool = False
    timeout_seconds: float = 60
    max_output_chars: int = 24000
    mode = "live"

    def __post_init__(self):
        if (self.allow_live is not True or not isinstance(self.model, str) or not self.model.strip()
                or len(self.model) > 200 or any(ord(c) < 32 for c in self.model)
                or type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 120
                or type(self.max_output_chars) is not int or not 128 <= self.max_output_chars <= 65536):
            raise GenerationError("CHATGPT_LIVE_CONFIGURATION_REQUIRED")

    def generate(self, messages):
        if (not isinstance(messages, list) or len(messages) != 2
                or any(not isinstance(m, dict) for m in messages)
                or messages[0].get("role") != "system" or messages[1].get("role") != "user"
                or any(not isinstance(m.get("content"), str) for m in messages)):
            raise GenerationError("CHATGPT_INVALID_INPUT")
        started = perf_counter()
        value = chatgpt_command("generate", timeout_seconds=self.timeout_seconds,
            allow_live=True, model=None if self.model == "chatgpt-account-default" else self.model,
            messages=messages, max_output_chars=self.max_output_chars)
        raw, metadata = value.get("text"), value.get("metadata")
        if not isinstance(raw, str) or not isinstance(metadata, dict):
            raise GenerationError("CHATGPT_BRIDGE_ERROR")
        if len(raw) > self.max_output_chars:
            raise GenerationError("JUDGE_OUTPUT_LIMIT")
        if metadata.get("stop_reason") != "response.completed":
            raise GenerationError("CHATGPT_INCOMPLETE_RESPONSE")
        safe = {k: metadata[k] for k in ("requested_model", "returned_model", "usage", "stop_reason", "http_status", "stream_summary") if k in metadata}
        safe["latency_ms"] = round((perf_counter() - started) * 1000, 3)
        return GenerationText(raw, safe)
