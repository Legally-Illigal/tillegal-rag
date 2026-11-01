"""Judge CLI; JSON stdout is always the selected contract, telemetry is separate."""
import argparse
import json
from pathlib import Path
import sys

from ai_core.judge_adapter import adapt_evidence_bundle
from ai_core.judge_agent import FixtureJudgeProvider, JudgeAgent, JudgeLimits
from ai_core.judge_contracts import (JudgeError, error_result, input_json_schema,
                                      output_json_schema, anthropic_output_schema)
from ai_core.rag_generation import GenerationError, strict_json

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 2000000


def read_json(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise JudgeError("INPUT_FILE_LIMIT")
        return strict_json(data.decode("utf-8-sig"))
    except JudgeError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise JudgeError("INVALID_INPUT_JSON") from None
    except OSError:
        raise JudgeError("INPUT_READ_ERROR") from None


def write_json(path, value):
    """Exclusive creation: never overwrite an input or an earlier run."""
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def configured_provider(args):
    if args.mode == "mock":
        if args.allow_live:
            raise JudgeError("JUDGE_CONFIGURATION_ERROR")
        # Honest default: no model assessed semantics, so no fabricated coverage.
        fixture = read_json(args.mock_response) if args.mock_response else error_result("MOCK_NOT_EVALUATED")
        if not args.mock_response:
            fixture["explanation"] = "MOCK: chưa đánh giá ngữ nghĩa; chỉ kiểm tra luồng kỹ thuật."
        return FixtureJudgeProvider(fixture)
    if args.mock_response:
        raise JudgeError("JUDGE_CONFIGURATION_ERROR")
    if args.provider == "gemini":
        from ai_core.gemini_provider import GeminiProvider
        return GeminiProvider.from_environment(model=args.model, allow_live=args.allow_live,
            timeout_seconds=args.timeout,
            max_output_tokens=args.max_output_tokens if args.max_output_tokens is not None else 4096,
            max_output_chars=args.max_output_chars)
    if args.provider == "openai":
        from ai_core.openai_provider import OpenAIProvider
        return OpenAIProvider.from_environment(model=args.model, allow_live=args.allow_live,
            timeout_seconds=args.timeout,
            max_output_tokens=args.max_output_tokens if args.max_output_tokens is not None else 2048,
            max_output_chars=args.max_output_chars)
    if args.provider == "chatgpt":
        if args.max_output_tokens is not None:
            raise JudgeError("CHATGPT_TOKEN_LIMIT_UNSUPPORTED")
        from ai_core.chatgpt_provider import ChatGPTProvider
        return ChatGPTProvider(model=args.model or "chatgpt-account-default", allow_live=args.allow_live,
            timeout_seconds=args.timeout, max_output_chars=args.max_output_chars)
    from ai_core.anthropic_provider import AnthropicProvider
    return AnthropicProvider.from_environment(
        model=args.model, allow_live=args.allow_live, timeout_seconds=args.timeout,
        max_tokens=args.max_output_tokens if args.max_output_tokens is not None else 2048,
        max_input_bytes=121000, structured_output=True,
        output_schema=anthropic_output_schema())


def _context_payload(args):
    context = read_json(args.context)
    required = {"question", "query_plan", "as_of_date", "search_rounds_completed", "query_history"}
    allowed = required | {"query_text_history", "request_id"}
    if not isinstance(context, dict) or not required <= set(context) or set(context) - allowed:
        raise JudgeError("INVALID_CONTEXT_SCHEMA")
    return context


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    evaluate = commands.add_parser("evaluate", help="Judge input JSON -> Judge output JSON")
    evaluate.add_argument("--input", type=Path, required=True)
    adapt = commands.add_parser("adapt", help="EvidenceBuilder bundle + caller context -> Judge input")
    adapt.add_argument("--bundle", type=Path, required=True)
    adapt.add_argument("--context", type=Path, required=True)
    demo = commands.add_parser("demo", help="BM25 -> EvidenceBuilder -> adapter -> Judge; no generation")
    demo.add_argument("--context", type=Path, required=True)
    demo.add_argument("--index", type=Path, default=ROOT / "data/indexes/labor-pilot-3-t4-bm25/manifest.json")
    demo.add_argument("--retrieval-query", help="Refined search text; original question remains unchanged")
    demo.add_argument("--top-k", type=int, default=3)
    demo.add_argument("--max-evidence-chunks", type=int, default=24)
    demo.add_argument("--max-quote-chars", type=int, default=8000)
    demo.add_argument("--artifacts-dir", type=Path, required=True, help="New directory for inspectable run files")
    schemas = commands.add_parser("schemas")
    schemas.add_argument("--output-dir", type=Path, required=True)
    login = commands.add_parser("chatgpt-login", help="Continue with ChatGPT in the system browser; no API key")
    login.add_argument("--new-account", action="store_true")
    login.add_argument("--timeout", type=float, default=300)
    commands.add_parser("chatgpt-status", help="Safe saved account status; no tokens")
    commands.add_parser("chatgpt-models", help="Models available to the selected ChatGPT account")
    select = commands.add_parser("chatgpt-select", help="Select a saved ChatGPT account")
    select.add_argument("--profile", required=True)
    commands.add_parser("chatgpt-logout", help="Revoke/clear the selected Tillegal ChatGPT session")
    for command in (evaluate, demo):
        command.add_argument("--mode", choices=["mock", "live"], default="mock")
        command.add_argument("--mock-response", type=Path, help="Explicit response fixture; no semantic evaluation")
        command.add_argument("--allow-live", action="store_true")
        command.add_argument("--provider", choices=["anthropic", "chatgpt", "openai", "gemini"], default="anthropic")
        command.add_argument("--model", help="API model ID (or GEMINI_MODEL/OPENAI_MODEL/ANTHROPIC_MODEL); ChatGPT uses account catalog slug")
        command.add_argument("--timeout", type=float, default=30, help="Provider socket timeout, seconds (0,120]")
        command.add_argument("--max-output-tokens", type=int, help="API output limit: Gemini default 4096, OpenAI/Anthropic 2048. ChatGPT plan uses char/deadline limits")
        command.add_argument("--max-input-bytes", type=int, default=100000)
        command.add_argument("--max-output-chars", type=int, default=24000)
        command.add_argument("--telemetry", type=Path)
    for command in (evaluate, adapt):
        command.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    telemetry, result = {}, None
    try:
        if args.command.startswith("chatgpt-"):
            from ai_core.chatgpt_provider import chatgpt_command
            action = args.command.removeprefix("chatgpt-")
            result = chatgpt_command(action, timeout_seconds=getattr(args, "timeout", 60),
                **({"new_account": args.new_account} if action == "login" else {}),
                **({"profile_id": args.profile} if action == "select" else {}))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result.get("error_code") else 1
        # Fail before any possible paid call if output targets already exist.
        targets = [getattr(args, k, None) for k in ("output", "telemetry", "artifacts_dir")]
        paths = [p.resolve() for p in targets if p is not None]
        if len(paths) != len(set(paths)) or any(p.exists() for p in paths):
            raise JudgeError("OUTPUT_ALREADY_EXISTS")
        if args.command == "schemas":
            args.output_dir.mkdir(parents=True, exist_ok=False)
            write_json(args.output_dir / "input.schema.json", input_json_schema())
            write_json(args.output_dir / "output.schema.json", output_json_schema())
            print(json.dumps({"schemas_written": 2}))
            return 0
        if args.command == "evaluate":
            value = read_json(args.input)
        else:
            context = _context_payload(args)
            if args.command == "adapt":
                bundle = read_json(args.bundle)
                if "request_id" in context and context["request_id"] != bundle.get("request_id"):
                    raise JudgeError("BUNDLE_REQUEST_ID_MISMATCH")
            else:
                from ai_core.bm25_retrieval import load_index
                from ai_core.evidence_builder import EvidenceBudget, EvidenceBuilder
                from ai_core.retrieval_runtime import make_retriever
                try:
                    index = load_index(args.index)
                    retrieval = make_retriever(index, mode="bm25").search(
                        args.retrieval_query if args.retrieval_query is not None else context["question"], args.top_k)
                    bundle = EvidenceBuilder(index, EvidenceBudget(
                        args.max_evidence_chunks, args.max_quote_chars)).build(
                            retrieval, request_id=context.get("request_id", "judge-demo"))
                except (ValueError, OSError, KeyError, TypeError):
                    raise JudgeError("RETRIEVAL_OR_INDEX_ERROR") from None
                args.artifacts_dir.mkdir(parents=True, exist_ok=False)
                write_json(args.artifacts_dir / "bundle.json", bundle)
            value = adapt_evidence_bundle(bundle, **{k: v for k, v in context.items() if k != "request_id"})
            if args.command == "demo":
                write_json(args.artifacts_dir / "input.json", value)
        if args.command == "adapt":
            result = value
        else:
            agent = JudgeAgent(configured_provider(args), JudgeLimits(args.max_input_bytes, args.max_output_chars))
            result = agent.evaluate(value, telemetry=telemetry)
            if args.command == "demo":
                write_json(args.artifacts_dir / "result.json", result)
                write_json(args.artifacts_dir / "telemetry.json", telemetry)
        if getattr(args, "output", None):
            write_json(args.output, result)
        if getattr(args, "telemetry", None):
            write_json(args.telemetry, telemetry)
    except (JudgeError, GenerationError) as exc:
        result = error_result(exc.code)
    except (OSError, ValueError, TypeError, KeyError):
        result = error_result("CLI_IO_OR_CONFIGURATION_ERROR")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "ERROR" else 0


if __name__ == "__main__":
    raise SystemExit(main())
