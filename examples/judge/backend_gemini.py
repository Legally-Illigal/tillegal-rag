"""Server integration; importing this file never calls Gemini or imports its SDK."""
from ai_core.gemini_provider import GeminiProvider
from ai_core.judge_agent import JudgeAgent
from ai_core.judge_adapter import adapt_evidence_bundle
from ai_core.judge_contracts import JudgeError, error_result


def create_server_judge(*, allow_live=False, timeout_seconds=30, max_output_tokens=4096):
    return JudgeAgent(GeminiProvider.from_environment(allow_live=allow_live,
        timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens))


def judge_input(agent, value):
    """Backend supplies a standard Judge input, returns (JSON dict, private telemetry)."""
    telemetry = {}
    return agent.evaluate(value, telemetry=telemetry), telemetry


def judge_bundle(agent, bundle, *, question, query_plan, as_of_date,
                 search_rounds_completed, query_history, query_text_history=None):
    """Caller supplies Query context; adapter failures remain technical errors."""
    try:
        value = adapt_evidence_bundle(bundle, question=question, query_plan=query_plan,
            as_of_date=as_of_date, search_rounds_completed=search_rounds_completed,
            query_history=query_history, query_text_history=query_text_history)
    except JudgeError as exc:
        return error_result(exc.code), {"stage": "adapter", "error_code": exc.code,
                                       "provider_calls": 0, "provider_name": "gemini"}
    return judge_input(agent, value)
