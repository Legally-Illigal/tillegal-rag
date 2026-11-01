"""Synthetic sources for tests ONLY. Rules here are invented and are not law."""
from ai_core.bm25_retrieval import (BM25Index, INDEX_SCHEMA, ALGORITHM_VERSION,
                                    TOKENIZER, DEFAULT_CONFIG, term_statistics)
from ai_core.data_contracts import build_document, NULLABLE_FIELDS, file_hash
from ai_core.legal_parser import parse_document


def synthetic_index(source=None):
    document = build_document(
        **{k: None for k in NULLABLE_FIELDS}, source_id="synthetic", source_record_id="judge-fixture",
        source_snapshot_id="a" * 64, content_format="text",
        content="VÍ DỤ GIẢ ĐỊNH, KHÔNG PHẢI PHÁP LUẬT\nĐiều 1. Đối thoại giả định\n"
                "1. Đơn vị mẫu tổ chức đối thoại với nhóm A khi có điều kiện B.\n"
                "2. Ngoại lệ giả định C cần căn cứ riêng.\n",
        raw_ref={"uri": "fixture:///judge.txt", "file_sha256": "b" * 64,
                 "record_key": "id", "record_value": "judge-fixture"},
        processing_steps=["synthetic_test_fixture"], issues=["SYNTHETIC_NOT_LAW"])
    parsed = parse_document(document)
    chunks = sorted(parsed["chunks"], key=lambda c: c["chunk_id"])
    payload = {"schema_version": INDEX_SCHEMA, "algorithm_version": ALGORITHM_VERSION,
               "tokenizer": TOKENIZER, "config": DEFAULT_CONFIG,
               "usage_policy": "EXPERIMENTAL_ONLY", "approved_for_legal_verification": False,
               "corpus_snapshot_id": None, "source": source or {
                   "fixture": "SYNTHETIC_NOT_LAW", "processed_snapshot_id": "c" * 64},
               "documents": [{k: v for k, v in document.to_dict().items() if k != "content"}],
               "nodes": {document.document_version_id: parsed["nodes"]},
               "chunks": chunks, "statistics": term_statistics(chunks)}
    return BM25Index(payload)
