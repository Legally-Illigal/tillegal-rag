"""Summary JSON CLI and one-pass BM25 -> Builder -> Judge -> Summary demo."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import unicodedata

from ai_core.data_contracts import ContractError
from ai_core.rag_generation import GenerationError, strict_json
from ai_core.summary_adapter import make_summary_input
from ai_core.summary_agent import SummaryAgent, FixtureSummaryProvider
from ai_core.summary_contracts import (SummaryError, SummaryLimits, LLM_OUTPUT_SCHEMA,
                                       empty_result, json_schemas, validate_input)


def _save_cli_error(args, result):
    """Persist pre-agent failures when a fresh standalone output was requested."""
    output = getattr(args, "output", None)
    if output is not None and not output.exists() and output.parent.is_dir():
        try:
            write_json(output, result)
        except OSError:
            pass  # Error still reaches UTF-8 stdout; never overwrite an existing file.


def read_json(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(2000001)
        if len(raw) > 2000000:
            raise SummaryError("SUMMARY_FILE_LIMIT")
        return strict_json(raw.decode("utf-8-sig"))
    except SummaryError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise SummaryError("SUMMARY_INVALID_INPUT_JSON") from None
    except OSError:
        raise SummaryError("SUMMARY_INPUT_READ_ERROR") from None


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def configured_provider(args, *, judge=False):
    from ai_core.judge_agent import FixtureJudgeProvider
    fixture = args.judge_mock_response if judge else args.mock_response
    if args.mode == "mock":
        if args.allow_live or fixture is None:
            raise SummaryError("SUMMARY_EXPLICIT_FIXTURE_REQUIRED")
        return (FixtureJudgeProvider if judge else FixtureSummaryProvider)(read_json(fixture))
    if fixture is not None or not args.allow_live:
        raise SummaryError("SUMMARY_LIVE_OPT_IN_REQUIRED")
    if args.provider == "chatgpt":
        if args.max_output_tokens is not None:
            raise SummaryError("CHATGPT_TOKEN_LIMIT_UNSUPPORTED")
        from ai_core.chatgpt_provider import ChatGPTProvider
        return ChatGPTProvider(model=args.model or "chatgpt-account-default", allow_live=True,
            timeout_seconds=args.timeout, max_output_chars=args.max_output_chars)
    from ai_core.gemini_provider import GeminiProvider
    return GeminiProvider.from_environment(model=args.model, allow_live=True,
        timeout_seconds=args.timeout, max_output_tokens=args.max_output_tokens if args.max_output_tokens is not None else 4096,
        max_input_bytes=args.max_input_bytes, max_output_chars=args.max_output_chars,
        output_schema=None if judge else LLM_OUTPUT_SCHEMA)


def run_single_pass(index, context, judge_agent, summary_agent, *, top_k=3,
                    evidence_budget=None, retrieval_query=None, on_artifact=None, on_progress=None):
    """Exactly one retrieval and one Judge evaluation. No loop or BasicRAG hooks."""
    from ai_core.evidence_builder import EvidenceBuilder
    from ai_core.judge_adapter import adapt_evidence_bundle
    from ai_core.retrieval_runtime import make_retriever
    if (not isinstance(context, dict) or set(context) != {"request_id", "question", "query_plan", "as_of_date"}
            or type(top_k) is not int or not 1 <= top_k <= 100):
        raise SummaryError("SUMMARY_INVALID_DEMO_CONTEXT")
    judge_telemetry, summary_telemetry = {}, {}
    artifacts, stage = {}, "retrieval"
    telemetry = {"retrieval_calls": 0, "judge_evaluations": 0, "status": "RUNNING",
                 "stage": stage, "judge": judge_telemetry, "summary": summary_telemetry}
    value = {**context, "draft_version": 1, "stop_reason": "single_pass_completed"}

    def emit(name, data):
        artifacts[name] = deepcopy(data)
        if on_artifact is not None:
            on_artifact(name, deepcopy(data))

    def progress(name):
        nonlocal stage
        stage = name
        telemetry["stage"] = name
        emit("telemetry", telemetry)
        if on_progress is not None:
            on_progress(name)

    try:
        query = context["question"] if retrieval_query is None else retrieval_query
        progress("retrieval")
        telemetry["retrieval_calls"] = 1
        retrieval = make_retriever(index, mode="bm25").search(query, top_k)
        emit("retrieval", retrieval)
        progress("evidence_builder")
        bundle = EvidenceBuilder(index, evidence_budget).build(retrieval, request_id=context["request_id"])
        emit("bundle", bundle)
        judge_input = adapt_evidence_bundle(bundle, question=context["question"], query_plan=context["query_plan"],
            as_of_date=context["as_of_date"], search_rounds_completed=1,
            query_history=["Q1"], query_text_history=[query])
        emit("judge_input", judge_input)
        telemetry["judge_evaluations"] = 1
        progress("judge")
        judge_result = judge_agent.evaluate(judge_input, telemetry=judge_telemetry)
        emit("judge_result", judge_result)
        value = make_summary_input(bundle, judge_result, question=context["question"],
            query_plan=context["query_plan"], as_of_date=context["as_of_date"])
        emit("input", value)
        progress("summary")
        result = summary_agent.summarize(value, telemetry=summary_telemetry)
        telemetry.update(status="ERROR" if result["status"] == "ERROR" else "COMPLETED", stage="finished")
    except KeyboardInterrupt:
        result = empty_result(value, code="SUMMARY_CANCELLED", stage=stage,
                              limitations=["RUN_CANCELLED_BEFORE_COMPLETION", "DRAFT_NOT_VERIFIED"])
        telemetry.update(status="CANCELLED", stage=stage, error_code="SUMMARY_CANCELLED")
    except Exception:
        # Preserve completed checkpoints and telemetry even on assembly/callback failure.
        telemetry.update(status="ERROR", stage=stage)
        emit("telemetry", telemetry)
        raise
    emit("result", result)
    emit("telemetry", telemetry)
    return artifacts


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    summarize = commands.add_parser("summarize", help="Saved Summary input -> unverified draft")
    summarize.add_argument("--input", type=Path, required=True)
    adapt = commands.add_parser("adapt", help="Combine matching saved bundle + Judge output + caller context")
    adapt.add_argument("--bundle", type=Path, required=True)
    adapt.add_argument("--judge-result", type=Path, required=True)
    adapt.add_argument("--context", type=Path, required=True)
    demo = commands.add_parser("demo", help="One pass; mock is explicit fixture replay, live runs both agents")
    demo.add_argument("--context", type=Path, required=True)
    demo.add_argument("--index", type=Path, required=True)
    demo.add_argument("--artifacts-dir", type=Path, required=True)
    demo.add_argument("--retrieval-query")
    demo.add_argument("--top-k", type=int, default=3)
    demo.add_argument("--max-evidence-chunks", type=int, default=32)
    demo.add_argument("--max-quote-chars", type=int, default=24000)
    demo.add_argument("--judge-mock-response", type=Path)
    schemas = commands.add_parser("schemas")
    schemas.add_argument("--output-dir", type=Path, required=True)
    for p in (summarize, demo):
        p.add_argument("--mode", choices=["mock", "live"], default="mock")
        p.add_argument("--provider", choices=["gemini", "chatgpt"], default="gemini")
        p.add_argument("--allow-live", action="store_true")
        p.add_argument("--mock-response", type=Path, help="Required in mock mode; no semantic evaluation")
        p.add_argument("--model", help="Gemini ID or ChatGPT catalog slug; defaults to env/account catalog")
        p.add_argument("--timeout", type=float, default=30)
        p.add_argument("--max-output-tokens", type=int, help="Gemini default 4096; unsupported by ChatGPT provider")
        p.add_argument("--max-input-bytes", type=int, default=100000)
        p.add_argument("--max-output-chars", type=int, default=24000)
        p.add_argument("--telemetry", type=Path)
    for p in (summarize, adapt):
        p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    value, telemetry, stage = {}, {}, "cli"
    try:
        # Refuse overwrites and colliding input/output paths before provider I/O.
        outputs = [p for p in (getattr(args, "output", None), getattr(args, "telemetry", None)) if p]
        if len({p.resolve() for p in outputs}) != len(outputs) or any(p.exists() for p in outputs):
            raise SummaryError("SUMMARY_OUTPUT_ALREADY_EXISTS")
        if any(not p.parent.is_dir() for p in outputs):
            raise SummaryError("SUMMARY_OUTPUT_PARENT_MISSING")
        for field in ("artifacts_dir", "output_dir"):
            p = getattr(args, field, None)
            if p is not None and p.exists():
                raise SummaryError("SUMMARY_OUTPUT_ALREADY_EXISTS")
        if args.command == "schemas":
            args.output_dir.mkdir(parents=True, exist_ok=False)
            for name, schema in json_schemas().items():
                write_json(args.output_dir / (name + ".schema.json"), schema)
            print(json.dumps({"schemas_written": 4}))
            return 0
        if args.command == "adapt":
            context, bundle = read_json(args.context), read_json(args.bundle)
            if (not isinstance(context, dict) or not isinstance(bundle, dict)
                    or context.get("request_id") != bundle.get("request_id")
                    or set(context) != {"request_id", "question", "query_plan", "as_of_date"}):
                raise SummaryError("SUMMARY_REQUEST_ID_OR_CONTEXT_MISMATCH")
            value = make_summary_input(bundle, read_json(args.judge_result), question=context["question"],
                query_plan=context["query_plan"], as_of_date=context["as_of_date"])
            validate_input(value)
            write_json(args.output, value)
            print(json.dumps(value, ensure_ascii=False))
            return 0
        limits = SummaryLimits(max_input_bytes=args.max_input_bytes, max_output_chars=args.max_output_chars)
        if args.command == "summarize":
            value = read_json(args.input)
            agent = SummaryAgent(configured_provider(args), limits)
            stage = "summary"
            result = agent.summarize(value, telemetry=telemetry)
            write_json(args.output, result)
        else:
            from ai_core.bm25_retrieval import load_index
            from ai_core.evidence_builder import EvidenceBudget
            from ai_core.judge_agent import JudgeAgent, JudgeLimits
            context = read_json(args.context)
            stage = "index"
            try:
                index = load_index(args.index)
            except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
                # Preserve useful integrity/runtime diagnostic, without changing artifacts.
                print(f"Index load failed: {exc}. Python={sys.version.split()[0]}, Unicode={unicodedata.unidata_version}. "
                      "Use the pinned runtime or rebuild from the original processed manifest; do not edit hashes/tokenizer.", file=sys.stderr)
                raise SummaryError("SUMMARY_INDEX_LOAD_ERROR") from None
            stage = "demo"
            judge = JudgeAgent(configured_provider(args, judge=True), JudgeLimits(args.max_input_bytes, args.max_output_chars))
            summary = SummaryAgent(configured_provider(args), limits)
            # Own a fresh run directory before any inference; persist each completed step.
            args.artifacts_dir.mkdir(parents=True, exist_ok=False)
            print(f"Artifacts: {args.artifacts_dir.resolve()}", file=sys.stderr, flush=True)

            def checkpoint(name, data):
                path = args.artifacts_dir / (name + ".json")
                if name == "telemetry" and path.exists():
                    # This file belongs to the fresh directory created by this invocation.
                    with path.open("w", encoding="utf-8", newline="\n") as stream:
                        stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                else:
                    write_json(path, data)

            def progress(name):
                labels = {"retrieval": "Retrieval BM25", "evidence_builder": "EvidenceBuilder",
                          "judge": "Judge: đang chờ provider", "summary": "Summary: đang chờ provider"}
                print(f"[{name}] {labels[name]}", file=sys.stderr, flush=True)

            artifacts = run_single_pass(index, context, judge, summary, top_k=args.top_k,
                evidence_budget=EvidenceBudget(args.max_evidence_chunks, args.max_quote_chars),
                retrieval_query=args.retrieval_query, on_artifact=checkpoint, on_progress=progress)
            result, telemetry = artifacts["result"], artifacts["telemetry"]
        if args.telemetry:
            write_json(args.telemetry, telemetry)
    except KeyboardInterrupt:
        result = empty_result(value, code="SUMMARY_CANCELLED", stage=stage)
        _save_cli_error(args, result)
    except (SummaryError, GenerationError) as exc:
        result = empty_result(value, code=exc.code, stage=stage)
        _save_cli_error(args, result)
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        result = empty_result(value, code="SUMMARY_CLI_ERROR", stage=stage)
        _save_cli_error(args, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 130 if (result.get("error") or {}).get("code") == "SUMMARY_CANCELLED" else (1 if result["status"] == "ERROR" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
