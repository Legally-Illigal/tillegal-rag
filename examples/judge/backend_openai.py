"""Backend integration example. No HTTP server and no implicit live invocation.

Import these functions from an application service. Supply issues and date from
your Query/caller, keep server credentials out of request bodies and frontend.
"""
from time import perf_counter

from ai_core.judge_adapter import adapt_evidence_bundle
from ai_core.judge_agent import JudgeAgent, JudgeLimits, PROMPT_VERSION
from ai_core.judge_contracts import JudgeError, error_result
from ai_core.openai_provider import OpenAIProvider


def create_server_judge(*, allow_live=False, timeout_seconds=30, max_output_tokens=2048):
    """Call once at server startup with explicit live opt-in and server environment."""
    provider = OpenAIProvider.from_environment(allow_live=allow_live, timeout_seconds=timeout_seconds,
                                               max_output_tokens=max_output_tokens)
    return JudgeAgent(provider, JudgeLimits())


def judge_bundle(agent, bundle, *, question, query_plan, as_of_date, search_rounds_completed,
                 query_history, query_text_history=None):
    """Per request: returns (Judge JSON dict, separate private telemetry dict)."""
    telemetry = {}
    started = perf_counter()
    try:
        value = adapt_evidence_bundle(bundle, question=question, query_plan=query_plan,
            as_of_date=as_of_date, search_rounds_completed=search_rounds_completed,
            query_history=query_history, query_text_history=query_text_history)
    except JudgeError as exc:
        telemetry.update(stage="adapter", error_code=exc.code, provider_calls=0,
                         prompt_version=PROMPT_VERSION, latency_ms=round((perf_counter() - started) * 1000, 3))
        return error_result(exc.code), telemetry
    return agent.evaluate(value, telemetry=telemetry), telemetry
