"""Reproducible invented source + real parser/BM25/Builder; fixture LLM responses."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from ai_core.bm25_retrieval import BM25Index, INDEX_SCHEMA, ALGORITHM_VERSION, TOKENIZER, DEFAULT_CONFIG, term_statistics, export_index
from ai_core.data_contracts import build_document, NULLABLE_FIELDS, file_hash
from ai_core.evidence_builder import EvidenceBuilder
from ai_core.legal_parser import parse_document
from ai_core.summary_adapter import make_summary_input
from ai_core.summary_agent import FixtureSummaryProvider, SummaryAgent


def fixture_index(source=None):
    values = {k: None for k in NULLABLE_FIELDS}
    values.update(source_url="https://example.invalid/summary-fixture", title="Nguồn giả định, không phải pháp luật")
    document = build_document(**values, source_id="synthetic", source_record_id="summary-fixture",
        source_snapshot_id="a" * 64, content_format="text",
        content="VÍ DỤ GIẢ ĐỊNH, KHÔNG PHẢI PHÁP LUẬT\nĐiều 1. Quy tắc mẫu\n"
                "1. Đơn vị mẫu tổ chức đối thoại với nhóm A khi có điều kiện B.\n"
                "2. Ngoại lệ giả định: không áp dụng quy tắc mẫu khi có điều kiện C.\n",
        raw_ref={"uri": "fixture:///summary.txt", "file_sha256": "b" * 64,
                 "record_key": "id", "record_value": "summary-fixture"},
        processing_steps=["synthetic_summary_fixture"], issues=["SYNTHETIC_NOT_LAW"])
    parsed = parse_document(document)
    chunks = sorted(parsed["chunks"], key=lambda c: c["chunk_id"])
    return BM25Index({"schema_version": INDEX_SCHEMA, "algorithm_version": ALGORITHM_VERSION,
        "tokenizer": TOKENIZER, "config": DEFAULT_CONFIG, "usage_policy": "EXPERIMENTAL_ONLY",
        "approved_for_legal_verification": False, "corpus_snapshot_id": None,
        "source": source or {"fixture": "SYNTHETIC_NOT_LAW", "processed_snapshot_id": "c" * 64},
        "documents": [{k: v for k, v in document.to_dict().items() if k != "content"}],
        "nodes": {document.document_version_id: parsed["nodes"]}, "chunks": chunks,
        "statistics": term_statistics(chunks)})


def fixture_input(index=None):
    index = index or fixture_index()
    context = {"request_id": "summary-fixture", "question": "Theo nguồn giả định, quy tắc đối thoại và ngoại lệ là gì?",
        "query_plan": {"issues": [{"issue_id": "I1", "text": "Quy tắc và ngoại lệ giả định"}]},
        "as_of_date": "2026-11-01"}
    bundle = EvidenceBuilder(index).build(index.search(context["question"], 3), request_id=context["request_id"])
    ids = [e["evidence_id"] for e in bundle["evidence"]]
    judge = {"status": "SUFFICIENT", "issue_checks": [{"issue_id": "I1", "coverage": "full", "evidence_ids": ids}],
        "usable_evidence_ids": ids[:], "missing_items": [], "next_queries": [],
        "explanation": "FIXTURE: nhãn có chủ đích để test plumbing; không phải đánh giá của LLM.", "error_code": None}
    clause = next(e["evidence_id"] for e in bundle["evidence"] if "Đơn vị mẫu" in e["quote"])
    model = {"units": [{"text": "Theo ví dụ giả định, đơn vị mẫu tổ chức đối thoại với nhóm A khi có điều kiện B.",
                        "issue_ids": ["I1"], "evidence_ids": [clause], "statement_type": "source_statement"}],
             "limitations": ["FIXTURE: chỉ kiểm tra plumbing; chưa đánh giá chất lượng nội dung."], "abstain": False}
    return make_summary_input(bundle, judge, question=context["question"], query_plan=context["query_plan"],
                              as_of_date=context["as_of_date"]), model, context


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=False)
    (root / "source").mkdir()
    source = root / "source/fixture-manifest.json"
    source.write_text('{"kind":"SYNTHETIC_NOT_LAW"}\n', encoding="utf-8")
    index = fixture_index({"processed_snapshot_id": "c" * 64, "processed_manifest_sha256": file_hash(source)})
    export_index(index, root / "index", source)
    value, model, context = fixture_input(index)
    result = SummaryAgent(FixtureSummaryProvider(model)).summarize(value)
    insufficient = deepcopy(value)
    insufficient["judge_result"].update(status="INSUFFICIENT", missing_items=["Chưa có căn cứ hiệu lực tại thời điểm yêu cầu."])
    insufficient["judge_result"]["issue_checks"][0]["coverage"] = "partial"
    report = {"schema_version": "1", "request_id": value["request_id"], "draft_version": 1,
        "claim_reports": [{"claim_id": "C1", "citation_ids": ["R1"], "action": "needs_evidence",
                           "reason": "REPORT GIẢ ĐỊNH: cần nêu rõ ngoại lệ; chưa có Verify implementation."}]}
    revision = deepcopy(value)
    revision.update(draft_version=2, previous_draft=result, verification_report=report)
    files = {"input": value, "bundle": value["evidence_bundle"], "judge_result": value["judge_result"],
        "context": context, "model_output": model, "output": result, "insufficient_input": insufficient,
        "revision_input": revision, "revision_report": report}
    for name, data in files.items():
        with (root / (name + ".json")).open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "SYNTHETIC_FIXTURES_CREATED", "summary_status": result["status"]}))
    return 0 if result["status"] == "DRAFT_UNVERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
