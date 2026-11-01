"""Real BM25/EvidenceBuilder technical smoke; fixture provider is NOT an LLM."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unicodedata
import unittest

from ai_core.basic_rag import BasicRAG
from ai_core.bm25_retrieval import load_index, export_index
from ai_core.data_contracts import ContractError, file_hash
from ai_core.evidence_builder import EvidenceBuilder
from ai_core.judge_adapter import adapt_evidence_bundle
from ai_core.judge_agent import FixtureJudgeProvider, JudgeAgent
from ai_core.judge_cli import main
from ai_core.rag_generation import MockProvider
from ai_core.retrieval_runtime import make_retriever
from tests.judge_support import synthetic_index
from tests.test_judge import input_fixture, sufficient
from tests import test_judge as fixtures

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_bm25_builder_adapter_judge_without_generation(self):
        index = synthetic_index()
        retrieval = make_retriever(index, mode="bm25").search("đối thoại", 2)
        bundle = EvidenceBuilder(index).build(retrieval, request_id="integration")
        original = deepcopy(bundle)
        value = adapt_evidence_bundle(bundle, question="Câu hỏi gốc giả định", query_plan=input_fixture()["query_plan"],
            as_of_date=None, search_rounds_completed=1, query_history=["Q1"], query_text_history=["đối thoại"])
        fixture = sufficient()
        fixture["issue_checks"][0]["evidence_ids"] = [value["evidence"][0]["evidence_id"]]
        fixture["usable_evidence_ids"] = fixture["issue_checks"][0]["evidence_ids"][:]
        result = JudgeAgent(FixtureJudgeProvider(fixture)).evaluate(value)
        self.assertEqual(result, fixture)
        self.assertEqual(bundle, original)
        self.assertEqual(value["evidence"][0]["temporal_status"], "UNKNOWN")

    def test_basic_rag_baseline_still_runs(self):
        result = BasicRAG(synthetic_index(), MockProvider()).answer("đối thoại", request_id="baseline", top_k=2)
        self.assertEqual(result["status"], "DRAFT_UNVERIFIED")
        self.assertEqual(result["verification_status"], "NOT_PERFORMED")

    def test_demo_cli_loads_verified_synthetic_index(self):
        # Test export/load integrity with runtime-specific tokenizer, never alter pilot hashes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            source = root / "source/fixture-source.json"
            source.write_text('{"kind":"SYNTHETIC_NOT_LAW"}', encoding="utf-8")
            index = synthetic_index({"processed_snapshot_id": "c" * 64,
                                     "processed_manifest_sha256": file_hash(source)})
            export_index(index, root / "index", source)
            code, result = fixtures.CLITests().call(["demo", "--context", str(ROOT / "examples/judge/context.json"),
                "--index", str(root / "index/manifest.json"), "--retrieval-query", "đối thoại",
                "--artifacts-dir", str(root / "run")])
            self.assertEqual(code, 1)  # default mock honestly reports not evaluated
            self.assertEqual(result["error_code"], "MOCK_NOT_EVALUATED")
            value = json.loads((root / "run/input.json").read_text(encoding="utf-8"))
            self.assertTrue(value["evidence"])
            self.assertNotEqual(value["question"], "đối thoại")
            code, adapted = fixtures.CLITests().call(["adapt", "--bundle", str(root / "run/bundle.json"),
                                              "--context", str(ROOT / "examples/judge/context.json")])
            self.assertEqual(code, 0)
            self.assertEqual(adapted, value)

    def test_pilot_bm25_if_runtime_compatible(self):
        path = ROOT / "data/indexes/labor-pilot-3-t4-bm25/manifest.json"
        if not path.exists():
            self.skipTest("Pilot index not installed")
        try:
            index = load_index(path)
        except ContractError as exc:
            if str(exc) in ("incompatible or promoted index", "index artifact hash/bytes mismatch"):
                self.skipTest(f"Pilot integrity/runtime mismatch ({exc}); Unicode runtime={unicodedata.unidata_version}; see docs")
            raise
        bundle = EvidenceBuilder(index).build(index.search("đối thoại tại nơi làm việc", 3), request_id="pilot-smoke")
        self.assertTrue(bundle["evidence"])
        value = adapt_evidence_bundle(bundle, question="Điều kiện đối thoại tại nơi làm việc?",
            query_plan=input_fixture()["query_plan"], as_of_date="2026-10-09",
            search_rounds_completed=1, query_history=["Q1"], query_text_history=["đối thoại tại nơi làm việc"])
        self.assertTrue(value["evidence"])


if __name__ == "__main__":
    unittest.main()
