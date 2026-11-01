"""Mock HTTP tests only. No OpenAI generation, account access, or paid requests."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib import error

from ai_core.judge_agent import JudgeAgent, judge_messages
from ai_core.judge_cli import main
from ai_core.judge_contracts import OUTPUT_SCHEMA
from ai_core.openai_provider import OpenAIProvider, ENDPOINT, openai_output_schema
from ai_core.rag_generation import GenerationError
from examples.judge.backend_openai import create_server_judge, judge_bundle
from tests.judge_support import synthetic_index
from tests.test_judge import input_fixture, sufficient, insufficient
from ai_core.evidence_builder import EvidenceBuilder

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "fixture-openai-key-never-real"
FAKE_MODEL = "fixture-api-model-not-a-real-model"


def provider(**kwargs):
    return OpenAIProvider(model=FAKE_MODEL, api_key=FAKE_KEY, allow_live=True, **kwargs)


def response(raw=None, **overrides):
    return {"object": "response", "status": "completed", "model": "fixture-returned-model",
            "usage": {"input_tokens": 120, "output_tokens": 80, "total_tokens": 200,
                      "input_tokens_details": {"cached_tokens": 10}, "output_tokens_details": {"reasoning_tokens": 20}},
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(sufficient()) if raw is None else raw}]}],
            **overrides}


class OpenAIProviderTests(unittest.TestCase):
    def call(self, value=None, *, raw=None, exc=None, configured=None, judge_input=None):
        telemetry = {}
        with patch("ai_core.openai_provider.request.build_opener") as opener:
            if exc is not None:
                opener.return_value.open.side_effect = exc
            else:
                upstream = opener.return_value.open.return_value.__enter__.return_value
                upstream.status = 200
                upstream.read.return_value = raw if raw is not None else json.dumps(value or response()).encode("utf-8")
            result = JudgeAgent(configured or provider()).evaluate(
                input_fixture() if judge_input is None else judge_input, telemetry=telemetry)
            args = opener.return_value.open.call_args
            count = opener.return_value.open.call_count
        return result, telemetry, args, count

    def test_environment_config_and_live_gate(self):
        cases = [({}, False, "OPENAI_LIVE_OPT_IN_REQUIRED"), ({}, True, "OPENAI_API_KEY_REQUIRED"),
                 ({"OPENAI_API_KEY": FAKE_KEY}, True, "OPENAI_MODEL_REQUIRED")]
        for env, live, code in cases:
            with self.subTest(code=code), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(GenerationError) as caught:
                    OpenAIProvider.from_environment(allow_live=live)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(FAKE_KEY, str(caught.exception))
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY, "OPENAI_MODEL": FAKE_MODEL}, clear=True):
            configured = OpenAIProvider.from_environment(allow_live=True)
            self.assertEqual(configured.model, FAKE_MODEL)
            self.assertEqual(configured.timeout_seconds, 30)
            self.assertNotIn(FAKE_KEY, repr(configured))

    def test_invalid_configuration_is_not_silently_coerced(self):
        for kwargs in ({"timeout_seconds": 0}, {"timeout_seconds": float("nan")}, {"max_output_tokens": True},
                       {"max_output_tokens": 0}, {"max_input_bytes": 1}, {"max_output_chars": 0}):
            with self.assertRaises(GenerationError): provider(**kwargs)

    def test_one_official_request_strict_judge_schema_no_model_specific_knobs(self):
        result, telemetry, args, count = self.call()
        self.assertEqual(result, sufficient())
        self.assertEqual(count, 1)
        req = args.args[0]
        self.assertEqual(req.full_url, ENDPOINT)
        self.assertNotIn(FAKE_KEY, req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + FAKE_KEY)
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], FAKE_MODEL)
        self.assertEqual(payload["text"]["format"]["schema"], openai_output_schema())
        self.assertEqual(set(payload["text"]["format"]["schema"]["properties"]), set(OUTPUT_SCHEMA["properties"]))
        self.assertNotIn("draft_answer", json.dumps(payload["text"]["format"]))
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["store"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["truncation"], "disabled")
        self.assertEqual(payload["max_output_tokens"], 2048)
        for field in ("temperature", "reasoning", "top_p", "tools", "max_tokens"):
            self.assertNotIn(field, payload)
        self.assertEqual(args.kwargs["timeout"], 30)
        self.assertEqual(telemetry["provider"]["requested_model"], FAKE_MODEL)
        self.assertEqual(telemetry["provider"]["returned_model"], "fixture-returned-model")
        self.assertEqual(telemetry["provider"]["usage"]["output_tokens_details"]["reasoning_tokens"], 20)
        self.assertNotIn(FAKE_KEY, json.dumps(telemetry))

    def test_wire_schema_is_projection_not_mutation(self):
        before = deepcopy(OUTPUT_SCHEMA)
        wire = openai_output_schema()
        self.assertEqual(wire["required"], OUTPUT_SCHEMA["required"])
        self.assertEqual(wire["properties"]["status"], OUTPUT_SCHEMA["properties"]["status"])
        self.assertEqual(OUTPUT_SCHEMA, before)
        for key in ("uniqueItems", "minLength", "pattern", "maxItems"):
            self.assertNotIn(key, json.dumps(wire))

    def test_sufficient_and_insufficient_validate_locally(self):
        for expected in (sufficient(), insufficient()):
            result, _, _, count = self.call(response(raw=json.dumps(expected)))
            self.assertEqual(result, expected)
            self.assertEqual(count, 1)

    def test_invalid_ids_and_status_coverage_are_not_repaired(self):
        bad_id = sufficient(); bad_id["usable_evidence_ids"] = ["E999"]
        bad_status = sufficient(); bad_status["issue_checks"][0]["coverage"] = "partial"
        for output, expected in ((bad_id, "INVALID_OUTPUT_REFERENCE"), (bad_status, "INVALID_OUTPUT_CONSISTENCY")):
            result, _, _, count = self.call(response(raw=json.dumps(output)))
            self.assertEqual(result["error_code"], expected)
            self.assertEqual(count, 1)

    def test_schema_wrong_or_invalid_json_is_error_after_completed_generation(self):
        for output, code in (("not json", "INVALID_OUTPUT_JSON"), ('{"status":"SUFFICIENT"}', "INVALID_OUTPUT_SCHEMA"),
                             ('{"x":1,"x":2}', "INVALID_OUTPUT_JSON")):
            result, telemetry, _, _ = self.call(response(raw=output))
            self.assertEqual(result["error_code"], code)
            self.assertEqual(telemetry["provider"]["stop_reason"], "completed")

    def test_empty_evidence_never_calls_http(self):
        value = input_fixture(); value["evidence"] = []
        result, _, _, count = self.call(judge_input=value)
        self.assertEqual(result["status"], "INSUFFICIENT")
        self.assertEqual(count, 0)

    def test_network_and_timeout_are_technical_errors_one_attempt(self):
        for exc, code in ((TimeoutError(FAKE_KEY), "PROVIDER_TIMEOUT"),
                          (error.URLError(TimeoutError()), "PROVIDER_TIMEOUT"),
                          (error.URLError("fixture offline"), "OPENAI_NETWORK_ERROR")):
            result, telemetry, _, count = self.call(exc=exc)
            self.assertEqual(result["error_code"], code)
            self.assertEqual(result["issue_checks"], [])
            self.assertEqual(count, 1)
            self.assertNotIn(FAKE_KEY, json.dumps(telemetry))

    def test_http_statuses_and_quota_classification_without_retry(self):
        cases = [(401, None, None, "OPENAI_AUTHENTICATION_ERROR"), (403, None, None, "OPENAI_PERMISSION_ERROR"),
                 (404, None, None, "OPENAI_MODEL_UNAVAILABLE"), (429, None, None, "OPENAI_RATE_LIMIT"),
                 (429, "insufficient_quota", None, "OPENAI_QUOTA_EXCEEDED"),
                 (429, "credit_balance_exhausted", None, "OPENAI_QUOTA_EXCEEDED"),
                 (400, "context_length_exceeded", None, "OPENAI_CONTEXT_LIMIT"),
                 (400, "unsupported_parameter", "temperature", "OPENAI_UNSUPPORTED_PARAMETER"),
                 (400, "invalid_json_schema", "text.format.schema", "OPENAI_SCHEMA_OR_MODEL_UNSUPPORTED"),
                 (500, None, None, "OPENAI_HTTP_ERROR"), (302, None, None, "OPENAI_REDIRECT_BLOCKED")]
        for status, code, param, expected in cases:
            with self.subTest(status=status, code=code):
                body = io.BytesIO(json.dumps({"error": {"code": code, "param": param, "type": "insufficient_quota" if code == "credit_balance_exhausted" else None, "message": FAKE_KEY}}).encode())
                exc = error.HTTPError(ENDPOINT, status, "upstream private message", {}, body)
                result, telemetry, _, count = self.call(exc=exc)
                self.assertEqual(result["error_code"], expected)
                self.assertEqual(count, 1)
                self.assertEqual(telemetry["provider"]["http_status"], status)
                self.assertNotIn(FAKE_KEY, json.dumps(telemetry))
                self.assertNotIn(FAKE_KEY, json.dumps(result))

    def test_refusal_incomplete_empty_and_unexpected_tool_output(self):
        refusal = response(); refusal["output"][0]["content"] = [{"type": "refusal", "refusal": "fixture refusal"}]
        for value, code in ((refusal, "OPENAI_REFUSAL"),
                            (response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), "OPENAI_INCOMPLETE_RESPONSE"),
                            (response(output=[]), "OPENAI_EMPTY_RESPONSE"), (response(raw="  "), "OPENAI_EMPTY_RESPONSE"),
                            (response(status="failed"), "OPENAI_RESPONSE_FAILED"),
                            (response(output=[{"type": "function_call"}]), "OPENAI_UNEXPECTED_OUTPUT")):
            result, _, _, count = self.call(value)
            self.assertEqual(result["error_code"], code)
            self.assertEqual(count, 1)

    def test_missing_provider_metadata_remains_none_not_inferred(self):
        value = response(); value.pop("model"); value.pop("usage")
        result, telemetry, _, _ = self.call(value)
        self.assertEqual(result["status"], "SUFFICIENT")
        self.assertIsNone(telemetry["provider"]["returned_model"])
        self.assertIsNone(telemetry["provider"]["usage"])

    def test_response_json_and_bounds_are_checked(self):
        cases = [(b"not API JSON", "OPENAI_INVALID_RESPONSE"),
                 (b'{"object":"response","object":"response"}', "OPENAI_INVALID_RESPONSE"),
                 (b"x" * 1048577, "OPENAI_RESPONSE_LIMIT")]
        for raw, code in cases:
            self.assertEqual(self.call(raw=raw)[0]["error_code"], code)
        result, _, _, count = self.call(configured=provider(max_input_bytes=1024))
        self.assertEqual(result["error_code"], "OPENAI_INPUT_LIMIT")
        self.assertEqual(count, 0)
        result = self.call(response(raw="x" * 1000), configured=provider(max_output_chars=128))[0]
        self.assertEqual(result["error_code"], "OPENAI_OUTPUT_LIMIT")

    def test_key_cannot_leak_via_body_response_or_model(self):
        value = input_fixture(); value["question"] += FAKE_KEY
        result, telemetry, _, count = self.call(judge_input=value)
        self.assertEqual(result["error_code"], "OPENAI_SECRET_IN_INPUT")
        self.assertEqual(count, 0)
        self.assertNotIn(FAKE_KEY, json.dumps(telemetry))
        self.assertEqual(self.call(response(raw=FAKE_KEY))[0]["error_code"], "OPENAI_SECRET_IN_RESPONSE")
        with self.assertRaises(GenerationError): OpenAIProvider(model=FAKE_KEY, api_key=FAKE_KEY, allow_live=True)

    def test_cli_defaults_to_mock_without_config_or_api(self):
        with patch.dict(os.environ, {}, clear=True), patch("ai_core.openai_provider.request.build_opener") as opener:
            stream = io.StringIO()
            with patch("sys.stdout", stream):
                code = main(["evaluate", "--input", "examples/judge/input.json", "--provider", "openai",
                             "--mock-response", "examples/judge/sufficient.json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stream.getvalue())["status"], "SUFFICIENT")
            opener.assert_not_called()

    def test_cli_live_and_allow_live_both_required(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY, "OPENAI_MODEL": FAKE_MODEL}, clear=True), \
             patch("ai_core.openai_provider.request.build_opener") as opener:
            for flags, expected in (([], "MOCK_NOT_EVALUATED"), (["--mode", "live"], "OPENAI_LIVE_OPT_IN_REQUIRED"),
                                    (["--allow-live"], "JUDGE_CONFIGURATION_ERROR")):
                stream = io.StringIO()
                with patch("sys.stdout", stream):
                    main(["evaluate", "--input", "examples/judge/input.json", "--provider", "openai", *flags])
                self.assertEqual(json.loads(stream.getvalue())["error_code"], expected)
            opener.assert_not_called()

    def test_cli_openai_live_uses_python_only_and_server_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY, "OPENAI_MODEL": FAKE_MODEL}, clear=True), \
             patch("ai_core.openai_provider.request.build_opener") as opener, \
             patch("subprocess.run", side_effect=AssertionError("Node or browser must not be used")):
            upstream = opener.return_value.open.return_value.__enter__.return_value
            upstream.status = 200; upstream.read.return_value = json.dumps(response()).encode()
            stream = io.StringIO()
            with patch("sys.stdout", stream):
                code = main(["evaluate", "--input", "examples/judge/input.json", "--provider", "openai", "--mode", "live", "--allow-live"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stream.getvalue()), sufficient())
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_backend_example_uses_adapter_then_judge_with_separate_telemetry(self):
        index = synthetic_index(); bundle = EvidenceBuilder(index).build(index.search("đối thoại", 2), request_id="backend-fixture")
        original = deepcopy(bundle); expected = sufficient()
        eid = bundle["evidence"][0]["evidence_id"]
        expected["issue_checks"][0]["evidence_ids"] = [eid]; expected["usable_evidence_ids"] = [eid]
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY, "OPENAI_MODEL": FAKE_MODEL}, clear=True), \
             patch("ai_core.openai_provider.request.build_opener") as opener:
            upstream = opener.return_value.open.return_value.__enter__.return_value
            upstream.status = 200; upstream.read.return_value = json.dumps(response(raw=json.dumps(expected))).encode()
            agent = create_server_judge(allow_live=True)
            result, telemetry = judge_bundle(agent, bundle, question=input_fixture()["question"], query_plan=input_fixture()["query_plan"],
                as_of_date=None, search_rounds_completed=1, query_history=["Q1"], query_text_history=["đối thoại"])
            self.assertEqual(result, expected)
            self.assertEqual(bundle, original)
            self.assertEqual(telemetry["provider_calls"], 1)
            self.assertNotIn("telemetry", result)

    def test_api_import_and_mock_http_work_without_node_or_chatgpt_modules(self):
        # Fresh interpreter, no PATH, no OAuth/DPAPI imports. Actual HTTP is still mocked.
        script = '''
import json,sys
from unittest.mock import patch
from ai_core.openai_provider import OpenAIProvider
from ai_core.judge_agent import JudgeAgent
value=json.loads(sys.stdin.readline())
reply=json.loads(sys.stdin.readline())
with patch('ai_core.openai_provider.request.build_opener') as opener:
 r=opener.return_value.open.return_value.__enter__.return_value
 r.status=200;r.read.return_value=json.dumps(reply).encode()
 result=JudgeAgent(OpenAIProvider(model='fixture-model',api_key='fixture-key',allow_live=True)).evaluate(value)
 assert result['status']=='SUFFICIENT',result
 assert 'ai_core.chatgpt_provider' not in sys.modules
 print('PYTHON_ONLY_OK')
'''
        env = dict(os.environ); env["PATH"] = ""
        executed = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, env=env,
            input=json.dumps(input_fixture()) + "\n" + json.dumps(response()) + "\n",
            text=True, capture_output=True, timeout=15)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertIn("PYTHON_ONLY_OK", executed.stdout)


if __name__ == "__main__":
    unittest.main()
