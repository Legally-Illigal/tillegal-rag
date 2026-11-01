"""Real local retrieval/Builder, fixture models, CLI, and mocked Gemini wire tests."""
from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_core.basic_rag import BasicRAG
from ai_core.rag_generation import MockProvider, DRAFT_OUTPUT_SCHEMA
from ai_core.gemini_provider import GeminiProvider, gemini_output_schema
from ai_core.judge_agent import JudgeAgent, FixtureJudgeProvider
from ai_core.judge_contracts import OUTPUT_SCHEMA as JUDGE_SCHEMA
from ai_core.summary_agent import SummaryAgent, FixtureSummaryProvider
from ai_core.summary_cli import main, run_single_pass
from ai_core.summary_contracts import LLM_OUTPUT_SCHEMA, json_schemas
from examples.summary.build_fixture import fixture_input, fixture_index, main as build_fixture

try:
    from google import genai
    import httpx
except ImportError:
    genai = None


class SummaryIntegrationTests(unittest.TestCase):
    def test_one_pass_and_baseline_unchanged(self):
        index = fixture_index()
        value, model, context = fixture_input(index)
        judge = JudgeAgent(FixtureJudgeProvider(value["judge_result"]))
        with patch.object(judge, "evaluate", wraps=judge.evaluate) as call:
            artifacts = run_single_pass(index, context, judge, SummaryAgent(FixtureSummaryProvider(model)))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(artifacts["result"]["status"], "DRAFT_UNVERIFIED")
        self.assertEqual(artifacts["input"]["stop_reason"], "single_pass_completed")
        self.assertEqual(artifacts["telemetry"]["retrieval_calls"], 1)
        baseline = BasicRAG(index, MockProvider()).answer(context["question"], request_id="baseline")
        self.assertEqual(baseline["status"], "DRAFT_UNVERIFIED")
        self.assertEqual(set(baseline["trace"]["generation_draft"]), {"draft_answer", "candidate_evidence_ids", "abstain"})
        self.assertNotEqual(LLM_OUTPUT_SCHEMA, DRAFT_OUTPUT_SCHEMA)
        self.assertNotEqual(LLM_OUTPUT_SCHEMA, JUDGE_SCHEMA)

    def cli(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(args)
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_cli_saved_adapt_demo_revision_and_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture"
            self.assertEqual(build_fixture(["--output-dir", str(fixture)]), 0)
            code, result, _ = self.cli(["summarize", "--input", str(fixture / "input.json"),
                "--mock-response", str(fixture / "model_output.json"), "--output", str(root / "draft.json"),
                "--telemetry", str(root / "telemetry.json")])
            self.assertEqual(code, 0)
            self.assertEqual(result, json.loads((root / "draft.json").read_text(encoding="utf-8")))
            self.assertIn("đối thoại", (root / "draft.json").read_text(encoding="utf-8"))
            code, value, _ = self.cli(["adapt", "--bundle", str(fixture / "bundle.json"),
                "--judge-result", str(fixture / "judge_result.json"), "--context", str(fixture / "context.json"),
                "--output", str(root / "adapted.json")])
            self.assertEqual(code, 0)
            self.assertEqual(value, json.loads((fixture / "input.json").read_text(encoding="utf-8")))
            code, result, _ = self.cli(["demo", "--context", str(fixture / "context.json"),
                "--index", str(fixture / "index/manifest.json"), "--artifacts-dir", str(root / "demo"),
                "--mock-response", str(fixture / "model_output.json"),
                "--judge-mock-response", str(fixture / "judge_result.json")])
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "DRAFT_UNVERIFIED")
            self.assertEqual(len(list((root / "demo").glob("*.json"))), 7)
            code, result, _ = self.cli(["summarize", "--input", str(fixture / "revision_input.json"),
                "--mock-response", str(fixture / "model_output.json"), "--output", str(root / "revision.json")])
            self.assertEqual(code, 0)
            self.assertEqual(result["draft_version"], 2)

    def test_cli_optin_no_overwrite_and_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value, model, _ = fixture_input()
            source = root / "input.json"; source.write_text(json.dumps(value), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("ai_core.gemini_provider._load_sdk") as load:
                code, result, _ = self.cli(["summarize", "--input", str(source), "--mode", "live",
                    "--output", str(root / "out.json")])
                self.assertEqual(result["error"]["code"], "SUMMARY_LIVE_OPT_IN_REQUIRED")
                load.assert_not_called()
            before = source.read_bytes()
            code, result, _ = self.cli(["summarize", "--input", str(source), "--output", str(source)])
            self.assertEqual(result["error"]["code"], "SUMMARY_OUTPUT_ALREADY_EXISTS")
            self.assertEqual(source.read_bytes(), before)
            bad = root / "bad.json"; bad.write_text("not json", encoding="utf-8")
            code, result, _ = self.cli(["summarize", "--input", str(bad), "--output", str(root / "bad-output.json")])
            self.assertEqual(result["error"]["code"], "SUMMARY_INVALID_INPUT_JSON")
            self.assertEqual(result, json.loads((root / "bad-output.json").read_text(encoding="utf-8")))

    def test_index_error_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / "context.json"; context.write_text("{}", encoding="utf-8")
            _, result, err = self.cli(["demo", "--context", str(context), "--index", str(root / "missing.json"),
                                      "--artifacts-dir", str(root / "new-run")])
            self.assertEqual(result["error"]["code"], "SUMMARY_INDEX_LOAD_ERROR")
            self.assertIn("Unicode=", err)
            self.assertIn("rebuild", err)
            self.assertFalse((root / "new-run").exists())

    def test_schema_export_and_schema_mismatch_before_sdk(self):
        value, _, _ = fixture_input()
        bad = GeminiProvider("fixture-model", "fixture-secret", True)
        with patch("ai_core.gemini_provider._load_sdk") as load:
            result = SummaryAgent(bad).summarize(value)
            self.assertEqual(result["error"]["code"], "SUMMARY_PROVIDER_SCHEMA_MISMATCH")
            load.assert_not_called()
        for name, schema in json_schemas().items():
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(gemini_output_schema(), gemini_output_schema(JUDGE_SCHEMA))

    def test_chatgpt_summary_live_cli_with_mock_bridge(self):
        value, model, _ = fixture_input()
        completed = {"ok": True, "text": json.dumps(model), "metadata": {
            "returned_model": "fixture-gpt", "stop_reason": "response.completed", "http_status": 200}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            with patch("ai_core.chatgpt_provider.chatgpt_command", return_value=completed) as bridge:
                code, result, _ = self.cli(["summarize", "--input", str(source), "--provider", "chatgpt",
                    "--mode", "live", "--allow-live", "--timeout", "120", "--output", str(root / "draft.json"),
                    "--telemetry", str(root / "telemetry.json")])
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "DRAFT_UNVERIFIED")
            self.assertEqual(bridge.call_count, 1)
            self.assertIn("tillegal-summary-v1", bridge.call_args.kwargs["messages"][0]["content"])
            self.assertNotIn("max_output_tokens", bridge.call_args.kwargs)
            self.assertEqual(json.loads((root / "telemetry.json").read_text(encoding="utf-8"))["provider_mode"], "live")

    def test_chatgpt_optin_token_limit_and_invalid_completion(self):
        from ai_core.chatgpt_provider import ChatGPTProvider
        value, model, _ = fixture_input()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"; source.write_text(json.dumps(value), encoding="utf-8")
            for flags, expected in (([], "SUMMARY_LIVE_OPT_IN_REQUIRED"),
                (["--allow-live", "--max-output-tokens", "1000"], "CHATGPT_TOKEN_LIMIT_UNSUPPORTED")):
                with patch("ai_core.chatgpt_provider.chatgpt_command") as bridge:
                    _, result, _ = self.cli(["summarize", "--input", str(source), "--provider", "chatgpt",
                        "--mode", "live", "--output", str(root / (expected + ".json")), *flags])
                self.assertEqual(result["error"]["code"], expected)
                bridge.assert_not_called()
        for text, stop, code in (("not json", "response.completed", "SUMMARY_INVALID_OUTPUT_JSON"),
            (json.dumps(model), "response.incomplete", "CHATGPT_INCOMPLETE_RESPONSE")):
            with patch("ai_core.chatgpt_provider.chatgpt_command", return_value={"ok": True, "text": text,
                    "metadata": {"stop_reason": stop}}):
                result = SummaryAgent(ChatGPTProvider(allow_live=True)).summarize(value)
            self.assertEqual(result["error"]["code"], code)

    def test_chatgpt_single_pass_calls_judge_then_summary(self):
        from ai_core.chatgpt_provider import ChatGPTProvider
        index = fixture_index()
        value, model, context = fixture_input(index)
        responses = [{"ok": True, "text": json.dumps(data), "metadata": {
            "returned_model": "fixture-gpt", "stop_reason": "response.completed"}}
            for data in (value["judge_result"], model)]
        with patch("ai_core.chatgpt_provider.chatgpt_command", side_effect=responses) as bridge:
            artifacts = run_single_pass(index, context, JudgeAgent(ChatGPTProvider(allow_live=True)),
                SummaryAgent(ChatGPTProvider(allow_live=True)))
        self.assertEqual(bridge.call_count, 2)
        self.assertEqual(artifacts["result"]["status"], "DRAFT_UNVERIFIED")

    def test_checkpoints_exist_before_judge_and_interruption_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture"
            with contextlib.redirect_stdout(io.StringIO()):
                build_fixture(["--output-dir", str(fixture)])
            run = root / "run"

            def interrupted(*args, **kwargs):
                self.assertTrue((run / "retrieval.json").exists())
                self.assertTrue((run / "bundle.json").exists())
                self.assertTrue((run / "judge_input.json").exists())
                state = json.loads((run / "telemetry.json").read_text(encoding="utf-8"))
                self.assertEqual(state["stage"], "judge")
                self.assertEqual(state["status"], "RUNNING")
                raise KeyboardInterrupt

            with patch("ai_core.chatgpt_provider.chatgpt_command", side_effect=interrupted):
                code, result, err = self.cli(["demo", "--context", str(fixture / "context.json"),
                    "--index", str(fixture / "index/manifest.json"), "--artifacts-dir", str(run),
                    "--mode", "live", "--provider", "chatgpt", "--allow-live"])
            self.assertEqual(code, 130)
            self.assertEqual(result["error"], {"code": "SUMMARY_CANCELLED", "stage": "judge"})
            self.assertEqual(json.loads((run / "result.json").read_text(encoding="utf-8")), result)
            telemetry = json.loads((run / "telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(telemetry["status"], "CANCELLED")
            self.assertFalse((run / "judge_result.json").exists())
            self.assertIn("[judge]", err)

    def test_summary_interruption_keeps_completed_judge_result(self):
        index = fixture_index()
        value, model, context = fixture_input(index)
        judge = JudgeAgent(FixtureJudgeProvider(value["judge_result"]))
        summary = SummaryAgent(FixtureSummaryProvider(model))
        saved = {}
        with patch.object(summary.provider, "generate", side_effect=KeyboardInterrupt):
            artifacts = run_single_pass(index, context, judge, summary,
                                        on_artifact=lambda k, v: saved.update({k: v}))
        self.assertEqual(artifacts["result"]["error"]["stage"], "summary")
        self.assertEqual(saved["judge_result"], value["judge_result"])
        self.assertEqual(saved["input"]["draft_version"], 1)
        self.assertEqual(saved["telemetry"]["status"], "CANCELLED")

    def test_timeout_persists_judge_error_and_summary_error(self):
        from ai_core.chatgpt_provider import ChatGPTProvider
        from ai_core.rag_generation import GenerationError
        index = fixture_index()
        _, model, context = fixture_input(index)
        summary = SummaryAgent(FixtureSummaryProvider(model))
        with patch("ai_core.chatgpt_provider.chatgpt_command", side_effect=GenerationError("PROVIDER_TIMEOUT")):
            artifacts = run_single_pass(index, context, JudgeAgent(ChatGPTProvider(allow_live=True)), summary)
        self.assertEqual(artifacts["judge_result"]["error_code"], "PROVIDER_TIMEOUT")
        self.assertEqual(artifacts["result"]["error"]["code"], "SUMMARY_JUDGE_ERROR")
        self.assertEqual(artifacts["telemetry"]["summary"]["provider_calls"], 0)
        self.assertEqual(artifacts["telemetry"]["status"], "ERROR")


@unittest.skipIf(genai is None, "Optional requirements-gemini.txt not installed")
class SummaryGeminiWireTests(unittest.TestCase):
    def test_live_cli_demo_runs_both_actual_providers_on_mock_http(self):
        requests = []
        real_client = genai.Client
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture"
            with contextlib.redirect_stdout(io.StringIO()):
                build_fixture(["--output-dir", str(fixture)])
            judge_result = json.loads((fixture / "judge_result.json").read_text(encoding="utf-8"))
            model = json.loads((fixture / "model_output.json").read_text(encoding="utf-8"))

            def handle(req):
                body = json.loads(req.content)
                props = set(body["generationConfig"]["responseJsonSchema"]["properties"])
                requests.append(props)
                answer = model if "units" in props else judge_result
                return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {
                    "role": "model", "parts": [{"text": json.dumps(answer)}]}}]})

            def client(**kwargs):
                kwargs["http_options"].httpx_client = httpx.Client(transport=httpx.MockTransport(handle))
                return real_client(**kwargs)

            with patch("google.genai.Client", side_effect=client), patch.dict(os.environ,
                    {"GEMINI_MODEL": "fixture-model", "GEMINI_API_KEY": "fixture-secret"}, clear=True):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = main(["demo", "--context", str(fixture / "context.json"),
                        "--index", str(fixture / "index/manifest.json"), "--artifacts-dir", str(root / "run"),
                        "--mode", "live", "--allow-live", "--provider", "gemini"])
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(len(requests), 2)
            self.assertIn("issue_checks", requests[0])
            self.assertEqual(requests[1], {"units", "limitations", "abstain"})
            telemetry = json.loads((root / "run/telemetry.json").read_text(encoding="utf-8"))
            self.assertEqual(telemetry["judge"]["provider_mode"], "live")
            self.assertEqual(telemetry["summary"]["provider_calls"], 1)

    def test_actual_sdk_serializes_summary_schema_not_judge_or_baseline(self):
        value, model, _ = fixture_input()
        requests = []
        real_client = genai.Client

        def handle(req):
            requests.append(req)
            return httpx.Response(200, json={"modelVersion": "fixture-model", "candidates": [
                {"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": json.dumps(model)}]}}],
                "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 20, "totalTokenCount": 50}})

        def client(**kwargs):
            kwargs["http_options"].httpx_client = httpx.Client(transport=httpx.MockTransport(handle))
            return real_client(**kwargs)

        schema = deepcopy(LLM_OUTPUT_SCHEMA)
        provider = GeminiProvider("fixture-model", "fixture-secret", True, output_schema=schema)
        schema.clear()  # Constructor owns its copy.
        telemetry = {}
        with patch("google.genai.Client", side_effect=client):
            result = SummaryAgent(provider).summarize(value, telemetry=telemetry)
        self.assertEqual(result["status"], "DRAFT_UNVERIFIED")
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0].content)
        wire = body["generationConfig"]["responseJsonSchema"]
        self.assertEqual(wire, gemini_output_schema(LLM_OUTPUT_SCHEMA))
        self.assertEqual(set(wire["properties"]), {"units", "limitations", "abstain"})
        self.assertEqual(telemetry["provider"]["usage"]["total_token_count"], 50)
        self.assertNotIn("fixture-secret", json.dumps([result, telemetry]))


if __name__ == "__main__":
    unittest.main()
