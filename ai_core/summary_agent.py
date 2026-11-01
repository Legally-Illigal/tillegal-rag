"""Standalone Summary. One provider call maximum; telemetry is request-local."""
from copy import deepcopy
import json
import re
from time import perf_counter

from ai_core.data_contracts import canonical_json
from ai_core.draft_renderer import render_draft
from ai_core.rag_generation import GenerationError, GenerationText, strict_json
from ai_core.summary_adapter import select_context, collect_limitations
from ai_core.summary_contracts import (SummaryError, SummaryLimits, LLM_OUTPUT_SCHEMA,
    validate_input, validate_llm_output, empty_result)
from ai_core.summary_prompt import summary_messages, PROMPT_VERSION


class FixtureSummaryProvider:
    """Explicit response replay only. Does not assess semantics or synthesize law."""
    mode = "mock"
    model = "summary-fixture-no-semantic-evaluation"

    def __init__(self, response):
        self.response = deepcopy(response)

    def generate(self, messages):
        return self.response if isinstance(self.response, str) else json.dumps(self.response, ensure_ascii=False)


def check_provider_schema(provider):
    # Existing providers can hard-code decoding contracts; reject mismatch before I/O.
    from ai_core.gemini_provider import GeminiProvider, gemini_output_schema
    if isinstance(provider, GeminiProvider):
        if gemini_output_schema(provider.output_schema) != gemini_output_schema(LLM_OUTPUT_SCHEMA):
            raise SummaryError("SUMMARY_PROVIDER_SCHEMA_MISMATCH")
        return
    module = type(provider).__module__
    if module in ("ai_core.openai_provider", "ai_core.anthropic_provider"):
        raise SummaryError("SUMMARY_PROVIDER_NOT_SUPPORTED")
    # Custom generate(messages) providers are caller-owned; local output validation always runs.


class SummaryAgent:
    def __init__(self, provider, limits=None):
        self.provider = provider
        self.limits = limits or SummaryLimits()

    def summarize(self, value, *, telemetry=None):
        started, stage, limitations = perf_counter(), "input", ["DRAFT_NOT_VERIFIED"]
        info = {"prompt_version": PROMPT_VERSION, "model": getattr(self.provider, "model", None),
            "provider_mode": getattr(self.provider, "mode", None), "provider_calls": 0,
            "error_code": None}
        try:
            value, projected = validate_input(value, self.limits)
            selected, context, context_gaps = select_context(value, projected)
            limitations = collect_limitations(value, selected)
            if value["judge_result"]["status"] == "ERROR":
                raise SummaryError("SUMMARY_JUDGE_ERROR")
            if not selected:
                return render_draft(value, {"units": [], "limitations": ["NO_USABLE_EVIDENCE"], "abstain": True},
                                    limitations, context_gaps)
            stage = "prompt"
            messages = summary_messages(value, selected, context, self.limits)
            info["input_bytes"] = len(canonical_json(messages))
            stage = "provider"
            check_provider_schema(self.provider)
            info["provider_calls"] = 1
            raw = self.provider.generate(deepcopy(messages))
            if isinstance(raw, GenerationText):
                info["provider"] = self._metadata(raw.metadata)
            stage = "output"
            if not isinstance(raw, str):
                raise SummaryError("SUMMARY_INVALID_OUTPUT_SCHEMA")
            if len(raw) > self.limits.max_output_chars:
                raise SummaryError("SUMMARY_OUTPUT_LIMIT")
            try:
                parsed = strict_json(raw)
            except (ValueError, RecursionError):
                raise SummaryError("SUMMARY_INVALID_OUTPUT_JSON") from None
            model_output = validate_llm_output(parsed, value, [e["evidence_id"] for e in selected], context_gaps)
            # Deterministic removal guard only; paraphrase faithfulness needs future Verify.
            if value["verification_report"] is not None:
                old = {c["claim_id"]: c["text"] for c in value["previous_draft"]["claims"]}
                removed = {old[r["claim_id"]] for r in value["verification_report"]["claim_reports"] if r["action"] == "remove"}
                if any(u["text"] in removed for u in model_output["units"]):
                    raise SummaryError("SUMMARY_REMOVED_CLAIM_REPEATED")
            stage = "render"
            return render_draft(value, model_output, limitations, context_gaps)
        except (SummaryError, GenerationError) as exc:
            code = exc.code if isinstance(exc.code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", exc.code) else "SUMMARY_PROVIDER_ERROR"
            info["error_code"] = code
            if isinstance(getattr(exc, "metadata", None), dict):
                info["provider"] = self._metadata(exc.metadata)
            return empty_result(value, code=code, stage=stage, limitations=limitations)
        except TimeoutError:
            code = "SUMMARY_PROVIDER_TIMEOUT" if stage == "provider" else "SUMMARY_INTERNAL_ERROR"
            info["error_code"] = code
            return empty_result(value, code=code, stage=stage, limitations=limitations)
        except Exception:
            code = "SUMMARY_PROVIDER_ERROR" if stage == "provider" else "SUMMARY_INTERNAL_ERROR"
            info["error_code"] = code
            return empty_result(value, code=code, stage=stage, limitations=limitations)
        finally:
            info.update(stage=stage, latency_ms=round((perf_counter() - started) * 1000, 3))
            if telemetry is not None:
                telemetry.update(deepcopy(info))

    @staticmethod
    def _metadata(metadata):
        return {k: deepcopy(v) for k, v in metadata.items() if k in
            ("requested_model", "returned_model", "usage", "stop_reason", "http_status", "latency_ms",
             "api_status", "error_reason", "timeout_kind")}
