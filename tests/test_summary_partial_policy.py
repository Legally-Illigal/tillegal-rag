"""Policy/schema/fixture tests only. They do not establish semantic faithfulness."""
from copy import deepcopy
import json
import unittest

from ai_core.summary_adapter import select_context
from ai_core.summary_agent import SummaryAgent
from ai_core.summary_contracts import validate_input, validate_public_output, SummaryError
from examples.summary.partial_review import partial_case
from tests.test_summary_agent import RecordingProvider, rehash


class PartialPolicyTests(unittest.TestCase):
    def setUp(self):
        self.value, self.model = partial_case()

    def run_agent(self):
        provider = RecordingProvider(self.model)
        result = SummaryAgent(provider).summarize(self.value)
        validate_public_output(result)
        return result, provider

    def test_gap_in_one_group_does_not_block_independent_full_issue(self):
        self.model["units"] = self.model["units"][1:]
        self.model["units"][0]["statement_type"] = "application"
        before = deepcopy(self.value)
        result, provider = self.run_agent()
        self.assertEqual(result["status"], "DRAFT_UNVERIFIED")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["claims"][0]["issue_ids"], ["I2"])
        r1, r2 = result["issue_reports"]
        self.assertEqual(r1["summary_status"], "NO_DRAFT_CLAIMS")
        self.assertIn("không có nghĩa là không có quy định", result["display_response"])
        self.assertTrue(r1["context_gap_ids"])
        self.assertEqual(r2["context_gap_ids"], [])
        self.assertEqual(r2["judge_coverage"], "full")
        self.assertEqual(self.value, before)

    def test_partial_issue_has_grounded_units_and_visible_gaps(self):
        result, _ = self.run_agent()
        self.assertEqual(result["status"], "DRAFT_UNVERIFIED")
        self.assertEqual(len(result["claims"]), 2)
        report = result["issue_reports"][0]
        self.assertEqual(report["judge_coverage"], "partial")
        self.assertEqual(report["summary_status"], "HAS_DRAFT_CLAIMS")
        gap = self.value["judge_result"]["missing_items"][0]
        self.assertIn(gap, report["limitations"])
        self.assertIn(gap.split(":", 1)[1].strip(), result["display_response"])
        self.assertNotIn("Giới hạn", result["answer_draft"])  # Verify handoff stays claim-only.

    def test_mapping_uses_group_members_and_checks_not_rank(self):
        _, projected = validate_input(self.value)
        selected, context, gaps = select_context(self.value, projected)
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["kind"], "NON_USABLE_MEMBERS")
        self.assertEqual(gap["issue_ids"], ["I1"])
        self.assertEqual(gap["evidence_ids"], self.model["units"][0]["evidence_ids"])
        self.assertEqual(gap["scope_status"], "MAPPED")
        changed = deepcopy(self.value)
        changed["evidence_bundle"]["groups"][0]["bm25_score"] = 999999
        rehash(changed)
        _, other = validate_input(changed)
        self.assertEqual(select_context(changed, other)[2], gaps)
        self.assertEqual({e["evidence_id"] for e in selected}, set(self.value["judge_result"]["usable_evidence_ids"]))

    def test_three_structural_gap_kinds_stay_separate(self):
        g = self.value["evidence_bundle"]["groups"][0]
        g.update(retrieved_chunk_id="seed-not-in-bundle", seed_included=False, omitted_chunk_ids=["seed-not-in-bundle"])
        self.value["evidence_bundle"]["status"] = "PARTIAL"
        rehash(self.value)
        result, _ = self.run_agent()
        gaps = result["context_gaps"]
        self.assertEqual({g["kind"] for g in gaps}, {"OMITTED_CHUNKS", "NON_USABLE_MEMBERS", "SEED_NOT_INCLUDED"})
        for gap in gaps:
            self.assertEqual(gap["issue_ids"], ["I1"])
            self.assertEqual(bool(gap["omitted_chunk_ids"]), gap["kind"] == "OMITTED_CHUNKS")
            self.assertEqual(bool(gap["non_usable_evidence_ids"]), gap["kind"] == "NON_USABLE_MEMBERS")

    def test_unknown_scope_stays_in_group_not_assigned_to_other_issues(self):
        g = deepcopy(self.value["evidence_bundle"]["groups"][1])
        g.update(retrieved_chunk_id="unknown-seed", members=[], seed_included=False, omitted_chunk_ids=["unknown-seed"])
        self.value["evidence_bundle"]["groups"].append(g)
        self.value["evidence_bundle"]["status"] = "PARTIAL"
        rehash(self.value)
        result, _ = self.run_agent()
        unknown = [g for g in result["context_gaps"] if g["scope_status"] == "GROUP_ONLY_UNKNOWN_ISSUES"]
        self.assertEqual(len(unknown), 2)
        self.assertTrue(all(not g["issue_ids"] and not g["evidence_ids"] for g in unknown))
        self.assertEqual(result["status"], "DRAFT_UNVERIFIED")
        self.assertIn("chưa xác định vấn đề liên quan", result["display_response"])
        self.assertFalse(set(g["gap_id"] for g in unknown).intersection(result["issue_reports"][1]["context_gap_ids"]))

    def test_application_on_affected_or_partial_source_is_rejected(self):
        for statement in (None, "application"):
            model = deepcopy(self.model)
            if statement is None:
                del model["units"][0]["statement_type"]
            else:
                model["units"][0]["statement_type"] = statement
            result = SummaryAgent(RecordingProvider(model)).summarize(self.value)
            self.assertEqual(result["error"]["code"], "SUMMARY_CONTEXT_SCOPE_REQUIRED")

    def test_partial_scope_required_even_without_structural_group_gap(self):
        self.value["judge_result"]["usable_evidence_ids"] = [e["evidence_id"] for e in self.value["evidence_bundle"]["evidence"]]
        self.model["units"][0]["statement_type"] = "application"
        result, _ = self.run_agent()
        self.assertEqual(result["error"]["code"], "SUMMARY_CONTEXT_SCOPE_REQUIRED")

    def test_group_gap_still_restricts_application_when_judge_coverage_full(self):
        j = self.value["judge_result"]
        j.update(status="SUFFICIENT", missing_items=[])
        j["issue_checks"][0].update(coverage="full", evidence_ids=self.model["units"][0]["evidence_ids"][:])
        self.model["units"][0]["statement_type"] = "application"
        self.assertEqual(self.run_agent()[0]["error"]["code"], "SUMMARY_CONTEXT_SCOPE_REQUIRED")
        self.model["units"] = self.model["units"][1:]
        self.model["units"][0]["statement_type"] = "application"
        self.assertEqual(self.run_agent()[0]["status"], "DRAFT_UNVERIFIED")

    def test_prompt_states_whole_claim_support_and_no_negative_inference(self):
        _, provider = self.run_agent()
        system = provider.messages[0]["content"]
        for phrase in ("toàn bộ nội dung mỗi unit", "Không thêm câu 'chưa đầy đủ'", "Không nói 'không có quy định'",
                       "statement_type='source_statement'", "không từ chối mọi issue"):
            self.assertIn(phrase, system)

    def test_excluded_evidence_not_in_prompt_or_citations(self):
        result, provider = self.run_agent()
        prompt = json.loads(provider.messages[1]["content"])
        allowed = set(self.value["judge_result"]["usable_evidence_ids"])
        self.assertEqual({e["evidence_id"] for e in prompt["evidence"]}, allowed)
        self.assertTrue(all(c["evidence_id"] in allowed for c in result["citations"]))
        bad = result["context_gaps"][0]["non_usable_evidence_ids"][0]
        self.model["units"][0]["evidence_ids"] = [bad]
        self.assertEqual(self.run_agent()[0]["error"]["code"], "SUMMARY_INVALID_OUTPUT_REFERENCE")

    def test_unscoped_judge_gap_is_not_semantically_assigned(self):
        self.value["judge_result"]["missing_items"] = ["Thiếu dữ kiện chưa xác định thuộc issue nào."]
        result, _ = self.run_agent()
        self.assertIn(self.value["judge_result"]["missing_items"][0], result["unscoped_limitations"])
        self.assertNotIn(self.value["judge_result"]["missing_items"][0], result["issue_reports"][1]["limitations"])
        self.assertIn("chưa gắn issue cụ thể", result["display_response"])

    def test_model_can_abstain_when_no_independent_unit_is_possible(self):
        self.model.update(units=[], abstain=True, limitations=["I1: Không thể giữ đúng nghĩa nếu thiếu điều kiện."])
        result, provider = self.run_agent()
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["status"], "ABSTAINED_UNVERIFIED")
        self.assertEqual(result["claims"], [])
        self.assertIn("Không thể giữ đúng nghĩa", result["display_response"])

    def test_new_output_report_and_display_tampering_rejected(self):
        result, _ = self.run_agent()
        for mutation in (lambda r: r["issue_reports"][0].update(claim_ids=["C999"]),
                         lambda r: r.update(display_response="Đã trả lời đầy đủ"),
                         lambda r: r["claims"][0].update(statement_type="application"),
                         lambda r: r["context_gaps"][0].update(issue_ids=["I999"])):
            changed = deepcopy(result)
            mutation(changed)
            with self.assertRaises(SummaryError):
                validate_public_output(changed)

    def test_issue_prefix_variants_map_known_id_without_losing_raw_details(self):
        from ai_core.summary_display import parse_issue_limitation
        for prefix in ("I1:", "I1：", "I1 —", "issue_id: I1 —", " ISSUE_ID = I1 :", "- issue_id: I1 –"):
            with self.subTest(prefix=prefix):
                message = prefix + " Thiếu ngoại lệ C."
                self.assertEqual(parse_issue_limitation(message, ["I1", "I2"]), ("I1", "Thiếu ngoại lệ C."))
                self.model["limitations"] = [message]
                result, _ = self.run_agent()
                self.assertIn(message, result["issue_reports"][0]["limitations"])
                self.assertNotIn(message, result["unscoped_limitations"])
                self.assertIn("Thiếu ngoại lệ C.", result["display_response"])

    def test_unknown_ambiguous_or_mid_sentence_issue_is_not_guessed(self):
        from ai_core.summary_display import parse_issue_limitation
        for message in ("issue_id: I10 — Thiếu nguồn.", "I1, I2: Thiếu nguồn.",
                        "I1 - I2: Thiếu nguồn.", "Thông tin về I1: chưa rõ."):
            self.assertIsNone(parse_issue_limitation(message, ["I1", "I2"])[0])
        self.model["limitations"] = ["issue_id: I10 — Thiếu nguồn."]
        result, _ = self.run_agent()
        self.assertEqual(result["unscoped_limitations"], self.model["limitations"])

    def test_duplicate_judge_model_message_shown_once_but_raw_retained(self):
        self.value["judge_result"]["missing_items"] = ["I1: Thiếu ngoại lệ C."]
        self.model["limitations"] = ["issue_id: I1 — Thiếu ngoại lệ C", "I1:   Thiếu ngoại lệ C."]
        result, _ = self.run_agent()
        self.assertEqual(result["display_response"].count("Thiếu ngoại lệ C"), 1)
        self.assertEqual(result["unscoped_limitations"], [])
        for message in self.value["judge_result"]["missing_items"] + self.model["limitations"]:
            self.assertIn(message, result["issue_reports"][0]["limitations"])

    def test_readable_display_hides_codes_and_repeated_group_notices(self):
        group = deepcopy(self.value["evidence_bundle"]["groups"][0])
        group.update(retrieved_chunk_id="missing-seed", seed_included=False, omitted_chunk_ids=["missing-seed"])
        self.value["evidence_bundle"]["groups"].append(group)
        self.value["evidence_bundle"]["status"] = "PARTIAL"
        rehash(self.value)
        result, _ = self.run_agent()
        display = result["display_response"]
        for code in ("NON_USABLE_MEMBERS", "OMITTED_CHUNKS", "SEED_NOT_INCLUDED", "HAS_DRAFT_CLAIMS", "Judge coverage=", "G1:"):
            self.assertNotIn(code, display)
        self.assertEqual(display.count("Một số đoạn cùng nhóm chưa được chọn làm căn cứ"), 1)
        self.assertIn("NON_USABLE_MEMBERS", json.dumps(result["context_gaps"]))
        self.assertIn("Mới trả lời được một phần", display)

    def test_dedup_preserves_different_numbers_negation_and_paraphrases(self):
        messages = ["I1: Cần ít nhất 70% tham gia.", "issue_id: I1 — Cần ít nhất 80% tham gia.",
                    "I1: Không áp dụng khi có C.", "I1: Áp dụng khi có C.",
                    "I1: Thiếu thông tin ngoại lệ.", "I1: Chưa rõ điều kiện của ngoại lệ."]
        self.model["limitations"] = messages
        result, _ = self.run_agent()
        for message in messages:
            self.assertIn(message.split("—", 1)[-1].split("I1:", 1)[-1].strip(), result["display_response"])

    def test_legacy_display_remains_valid_revision_input(self):
        from ai_core.summary_contracts import render_display_response
        result, _ = self.run_agent()
        self.assertEqual(result["display_version"], "2")
        old = deepcopy(result)
        del old["display_version"]
        old["display_response"] = render_display_response(old)
        self.assertIn("Judge coverage=", old["display_response"])
        validate_public_output(old)
        self.value.update(draft_version=2, previous_draft=old, verification_report={"schema_version": "1",
            "request_id": old["request_id"], "draft_version": old["draft_version"], "claim_reports": []})
        revised, _ = self.run_agent()
        self.assertEqual(revised["status"], "DRAFT_UNVERIFIED")
        self.assertEqual(revised["draft_version"], 2)
        self.assertEqual(revised["display_version"], "2")

    def test_metadata_terms_translated_only_in_display(self):
        self.model["limitations"] = ["issue_id: I1 — Nguồn QUARANTINE, xuất xứ UNVERIFIED và temporal_status UNKNOWN."]
        result, _ = self.run_agent()
        self.assertIn("chưa được phê duyệt làm nguồn pháp lý", result["display_response"])
        self.assertNotIn("QUARANTINE", result["display_response"])
        self.assertIn(self.model["limitations"][0], result["issue_reports"][0]["limitations"])
        self.assertEqual(result["citations"][0]["source_metadata"]["legal_approval"], "QUARANTINE")


if __name__ == "__main__":
    unittest.main()
