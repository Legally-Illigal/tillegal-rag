import io
import json
import subprocess
import shutil
from unittest.mock import patch
import unittest

from ai_core.chatgpt_provider import ChatGPTProvider, chatgpt_command
from ai_core.judge_agent import JudgeAgent
from ai_core.judge_cli import main
from ai_core.rag_generation import GenerationError
from tests.test_judge import input_fixture, sufficient


class ChatGPTTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js 22+ is required for real local bridge test")
    def test_real_local_bridge_rejects_inference_without_opt_in(self):
        # Real Python -> Node process, but rejected before storage/network/account access.
        with self.assertRaises(GenerationError) as caught:
            chatgpt_command("generate", allow_live=False, timeout_seconds=5)
        self.assertEqual(caught.exception.code, "CHATGPT_LIVE_OPT_IN_REQUIRED")

    def test_no_live_without_opt_in(self):
        for kwargs in ({}, {"allow_live": True, "timeout_seconds": 0}, {"allow_live": True, "max_output_chars": 1}):
            with self.assertRaises(GenerationError): ChatGPTProvider(**kwargs)

    def test_completed_text_goes_through_full_judge_validation(self):
        completed = {"ok": True, "text": json.dumps(sufficient()),
                     "metadata": {"returned_model": "fixture-gpt", "stop_reason": "response.completed", "http_status": 200}}
        telemetry = {}
        with patch("ai_core.chatgpt_provider.chatgpt_command", return_value=completed) as bridge:
            result = JudgeAgent(ChatGPTProvider(allow_live=True)).evaluate(input_fixture(), telemetry=telemetry)
        self.assertEqual(result, sufficient())
        self.assertEqual(bridge.call_args.kwargs["messages"][0]["role"], "system")
        self.assertTrue(bridge.call_args.kwargs["allow_live"])
        self.assertEqual(telemetry["provider"]["returned_model"], "fixture-gpt")

    def test_invalid_live_output_is_never_repaired_to_sufficient(self):
        for raw in ('not-json', json.dumps({**sufficient(), "usable_evidence_ids": ["missing"]})):
            with patch("ai_core.chatgpt_provider.chatgpt_command", return_value={"ok": True, "text": raw,
                       "metadata": {"stop_reason": "response.completed"}}):
                result = JudgeAgent(ChatGPTProvider(allow_live=True)).evaluate(input_fixture())
                self.assertEqual(result["status"], "ERROR")
                self.assertTrue(result["error_code"].startswith("INVALID_OUTPUT"))

    def test_missing_login_and_usage_limit_remain_technical_errors(self):
        for code in ("CHATGPT_SIGN_IN_REQUIRED", "CHATGPT_USAGE_LIMIT", "PROVIDER_TIMEOUT"):
            with patch("ai_core.chatgpt_provider.chatgpt_command", side_effect=GenerationError(code)):
                result = JudgeAgent(ChatGPTProvider(allow_live=True)).evaluate(input_fixture())
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["issue_checks"], [])

    def test_safe_stream_diagnostics_survive_provider_error(self):
        error = GenerationError("CHATGPT_EMPTY_RESPONSE")
        error.metadata = {"requested_model": "fixture-gpt", "returned_model": "fixture-gpt",
                          "stop_reason": "response.completed", "http_status": 200,
                          "stream_summary": {"delta_chars": 0, "completed_chars": 0},
                          "raw_text": "do-not-log", "access_token": "do-not-log"}
        telemetry = {}
        with patch("ai_core.chatgpt_provider.chatgpt_command", side_effect=error):
            result = JudgeAgent(ChatGPTProvider(allow_live=True)).evaluate(input_fixture(), telemetry=telemetry)
        self.assertEqual(result["error_code"], "CHATGPT_EMPTY_RESPONSE")
        self.assertEqual(telemetry["provider"]["returned_model"], "fixture-gpt")
        self.assertEqual(telemetry["provider"]["stream_summary"]["delta_chars"], 0)
        self.assertNotIn("do-not-log", json.dumps(telemetry))

    def test_bridge_error_metadata_is_allowlisted(self):
        response = {"ok": False, "error_code": "CHATGPT_EMPTY_RESPONSE", "metadata": {
            "requested_model": "fixture-gpt", "stream_summary": {"delta_chars": 1}, "access_token": "do-not-log"}}
        with patch("ai_core.chatgpt_provider.shutil.which", return_value="node"), patch(
                "ai_core.chatgpt_provider.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout=json.dumps(response).encode())):
            with self.assertRaises(GenerationError) as caught: chatgpt_command("generate")
            self.assertEqual(caught.exception.metadata["requested_model"], "fixture-gpt")
            self.assertNotIn("access_token", caught.exception.metadata)

    def test_no_partial_completion_accepted(self):
        with patch("ai_core.chatgpt_provider.chatgpt_command", return_value={"ok": True, "text": json.dumps(sufficient()),
                   "metadata": {"stop_reason": "response.incomplete"}}):
            self.assertEqual(JudgeAgent(ChatGPTProvider(allow_live=True)).evaluate(input_fixture())["error_code"],
                             "CHATGPT_INCOMPLETE_RESPONSE")

    def test_bridge_uses_json_stdin_without_token_args_and_has_process_timeout(self):
        process = subprocess.CompletedProcess([], 0, stdout=b'{"ok":true,"profiles":[]}')
        with patch("ai_core.chatgpt_provider.shutil.which", return_value="node"), \
             patch("ai_core.chatgpt_provider.subprocess.run", return_value=process) as run:
            self.assertEqual(chatgpt_command("status")["profiles"], [])
            self.assertEqual(len(run.call_args.args[0]), 2)
            self.assertEqual(json.loads(run.call_args.kwargs["input"])["action"], "status")
            self.assertEqual(run.call_args.kwargs["timeout"], 80)

    def test_bridge_timeout_and_bad_response_are_classified(self):
        with patch("ai_core.chatgpt_provider.shutil.which", return_value="node"):
            with patch("ai_core.chatgpt_provider.subprocess.run", side_effect=subprocess.TimeoutExpired("node", 1)):
                with self.assertRaises(GenerationError) as caught: chatgpt_command("models")
                self.assertEqual(caught.exception.code, "PROVIDER_TIMEOUT")
            with patch("ai_core.chatgpt_provider.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=b'broken')):
                with self.assertRaises(GenerationError) as caught: chatgpt_command("status")
                self.assertEqual(caught.exception.code, "CHATGPT_BRIDGE_ERROR")

    def test_cli_chatgpt_status_has_no_inference(self):
        stream = io.StringIO()
        with patch("sys.stdout", stream), patch("ai_core.chatgpt_provider.chatgpt_command", return_value={"ok": True, "profiles": []}) as command:
            self.assertEqual(main(["chatgpt-status"]), 0)
            self.assertEqual(command.call_args.args, ("status",))
        self.assertEqual(json.loads(stream.getvalue())["profiles"], [])

    def test_cli_live_opt_in_and_unsupported_token_limit(self):
        for extra, expected in (([], "CHATGPT_LIVE_CONFIGURATION_REQUIRED"),
                                (["--allow-live", "--max-output-tokens", "2048"], "CHATGPT_TOKEN_LIMIT_UNSUPPORTED")):
            stream = io.StringIO()
            with patch("sys.stdout", stream), patch("ai_core.chatgpt_provider.chatgpt_command") as command:
                code = main(["evaluate", "--input", "examples/judge/input.json", "--mode", "live", "--provider", "chatgpt", *extra])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(stream.getvalue())["error_code"], expected)
                command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
