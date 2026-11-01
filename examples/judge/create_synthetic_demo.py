"""Create an isolated synthetic BM25 index and explicit mock response fixture.

Run from repo root: python -B -m examples.judge.create_synthetic_demo --output-dir outputs/judge-fixture
No real law, model download, generation call or paid API. Uses test fixture support.
"""
import argparse
from pathlib import Path

from ai_core.bm25_retrieval import export_index
from ai_core.data_contracts import file_hash
from ai_core.evidence_builder import EvidenceBuilder, EvidenceBudget
from ai_core.judge_cli import read_json, write_json
from tests.judge_support import synthetic_index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=False)
    (root / "source").mkdir()
    source = root / "source/fixture-source.json"
    write_json(source, {"kind": "SYNTHETIC_NOT_LAW"})
    index = synthetic_index({"fixture": "SYNTHETIC_NOT_LAW", "processed_snapshot_id": "c" * 64,
                             "processed_manifest_sha256": file_hash(source)})
    export_index(index, root / "index", source)
    context = read_json(Path(__file__).with_name("synthetic_context.json"))
    # Reproduce the documented demo settings to prepare fixed canonical IDs.
    bundle = EvidenceBuilder(index, EvidenceBudget(24, 8000)).build(
        index.search(context["question"], 3), request_id=context["request_id"])
    ids = [e["evidence_id"] for e in bundle["evidence"]]
    write_json(root / "mock-response.json", {
        "status": "SUFFICIENT", "issue_checks": [{"issue_id": "I1", "evidence_ids": ids, "coverage": "full"}],
        "missing_items": [], "next_queries": [], "usable_evidence_ids": ids,
        "explanation": "MOCK fixture dựng sẵn cho ví dụ giả định: căn cứ điều kiện B. Không phải đánh giá LLM.",
        "error_code": None})
    print("Created SYNTHETIC_NOT_LAW index and explicit MOCK fixture:", root)


if __name__ == "__main__":
    main()
