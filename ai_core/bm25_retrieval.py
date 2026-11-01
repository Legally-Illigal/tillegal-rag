"""T4 experimental BM25 retrieval. No legal approval or citation verification."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from time import perf_counter
import unicodedata

from .data_contracts import ContractError, canonical_json, file_hash, load_documents
from . import legal_parser

INDEX_SCHEMA = "1"
ALGORITHM_VERSION = "bm25-okapi-v1"
TOKENIZER = {"version": "vi-syllable-nfc-v1", "unicode_version": unicodedata.unidata_version,
             "case": "casefold", "accents": "preserve", "stopwords": False,
             "markers_excluded": ["[IMAGE]", "[EMPTY_TABLE]"], "query_tf": "binary"}
DEFAULT_CONFIG = {"k1": 1.2, "b": 0.75}
STATUS_FIELDS = {"usage_policy": "EXPERIMENTAL_ONLY", "legal_approval": "QUARANTINE",
                 "provenance_status": "UNVERIFIED", "rights_status": "UNVERIFIED",
                 "temporal_status": "UNKNOWN"}


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def tokenize(text: str) -> list[str]:
    """Accent-preserving syllables/alphanumerics, NOT Vietnamese word segmentation.

    Keep negation and numbers; punctuation separates tokens. Do not modify source
    text or reuse token positions as source offsets. Parser markers are not prose.
    """
    if not isinstance(text, str):
        raise ContractError("text/query must be a string")
    for marker in TOKENIZER["markers_excluded"]:
        text = text.replace(marker, " ")
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text).casefold(), re.UNICODE)


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"k1", "b"}:
        raise ContractError("BM25 config requires k1 and b")
    for name, value in config.items():
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ContractError(f"{name} must be finite numeric, not boolean")
    if config["k1"] <= 0 or not 0 <= config["b"] <= 1:
        raise ContractError("require k1 > 0 and 0 <= b <= 1")


def verified_manifest(path, kind, schema, snapshot_key, names):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest.get("artifact_kind") != kind or manifest.get("schema_version") != schema
            or manifest.get("usage_policy") != "EXPERIMENTAL_ONLY"):
        raise ContractError("unsupported manifest kind/schema/usage")
    artifacts = manifest["artifacts"]
    if len(artifacts) != len(names) or {a["path"] for a in artifacts} != set(names):
        raise ContractError("unexpected or duplicate manifest artifact paths")
    for artifact in artifacts:
        target = (path.parent / artifact["path"]).resolve()
        if target.parent != path.parent or file_hash(target) != artifact["sha256"]:
            raise ContractError("artifact path/hash mismatch")
        if target.stat().st_size != artifact["bytes"]:
            raise ContractError("artifact byte count mismatch")
    if digest({a["path"]: a["sha256"] for a in artifacts}) != manifest[snapshot_key]:
        raise ContractError("manifest snapshot fingerprint mismatch")
    return manifest


def load_processed(manifest_path, expected_snapshot):
    """Read-only gate: verify T3-FIX plus its pinned T2, then run T3 validator."""
    path = Path(manifest_path).resolve()
    original_hash = file_hash(path)
    manifest = verified_manifest(path, "experimental_parsed_pilot", "2", "processed_snapshot_id",
                                 {"structures.jsonl", "chunks.jsonl", "validation.json"})
    if manifest["processed_snapshot_id"] != expected_snapshot:
        raise ContractError("processed snapshot differs from explicitly selected snapshot")
    if (manifest.get("parser_version") != legal_parser.PARSER_VERSION
            or manifest.get("approved_for_legal_verification") is not False
            or manifest.get("corpus_snapshot_id") is not None or manifest.get("index_version") is not None):
        raise ContractError("expected unpromoted T3-FIX experimental snapshot")
    for name in ("legal_parser.py", "data_contracts.py"):
        if manifest["code_hashes"].get("ai_core/" + name) != file_hash(Path(__file__).with_name(name)):
            raise ContractError("T3 validator code differs from pinned parser/contracts")
    inputs = manifest["inputs"]
    if len(inputs) != 3 or {Path(a["path"]).name for a in inputs} != {"manifest.json", "documents.jsonl", "document.schema.json"}:
        raise ContractError("unexpected staging input declarations")
    for artifact in inputs:
        if file_hash(Path(artifact["path"])) != artifact["sha256"]:
            raise ContractError("staging input hash mismatch")
    staging_path = next(Path(a["path"]).resolve() for a in inputs if Path(a["path"]).name == "manifest.json")
    if any(Path(a["path"]).resolve().parent != staging_path.parent for a in inputs):
        raise ContractError("staging inputs must share manifest directory")
    staging = verified_manifest(staging_path, "staging_documents", "1", "staging_snapshot_id",
                                {"documents.jsonl", "document.schema.json"})
    if (staging["staging_snapshot_id"] != manifest["staging_snapshot_id"]
            or staging["source_snapshot_id"] != manifest["source_snapshot_id"]):
        raise ContractError("staging/source snapshot mismatch")
    documents = load_documents(staging_path.parent / "documents.jsonl")
    declared = {(d["document_id"], d["document_version_id"], d["content_sha256"]) for d in staging["documents"]}
    if (len(documents) != manifest["record_count"] or len(documents) != staging["record_count"]
            or declared != {(d.document_id, d.document_version_id, d.content_sha256) for d in documents}
            or any(d.source_snapshot_id != manifest["source_snapshot_id"] for d in documents)):
        raise ContractError("document declarations mismatch")
    structures = [json.loads(line) for line in (path.parent / "structures.jsonl").read_text(encoding="utf-8").splitlines()]
    chunks = [json.loads(line) for line in (path.parent / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    by_version = {r["document_version_id"]: r for r in structures}
    if len(by_version) != len(structures) or set(by_version) != {d.document_version_id for d in documents}:
        raise ContractError("structure document versions mismatch")
    if len({c["chunk_id"] for c in chunks}) != len(chunks) or any(c["document_version_id"] not in by_version for c in chunks):
        raise ContractError("duplicate chunk or unknown document version")
    for document in documents:
        result = by_version[document.document_version_id]
        result["chunks"] = [c for c in chunks if c["document_version_id"] == document.document_version_id]
        if result["max_chars"] != manifest["max_chars"]:
            raise ContractError("parser max_chars mismatch")
        legal_parser.validate_result(document, result)
    # Detect a concurrent accidental change rather than blessing mixed inputs.
    if file_hash(path) != original_hash:
        raise ContractError("processed manifest changed during read")
    for artifact in manifest["artifacts"]:
        if file_hash(path.parent / artifact["path"]) != artifact["sha256"]:
            raise ContractError("processed artifact changed during read")
    for artifact in inputs:
        if file_hash(Path(artifact["path"])) != artifact["sha256"]:
            raise ContractError("staging input changed during read")
    source = {"processed_snapshot_id": expected_snapshot, "processed_manifest_sha256": original_hash,
              "staging_snapshot_id": manifest["staging_snapshot_id"], "source_snapshot_id": manifest["source_snapshot_id"],
              "parser_version": manifest["parser_version"], "processed_schema_version": "2",
              "artifact_hashes": {a["path"]: a["sha256"] for a in manifest["artifacts"]}}
    return documents, structures, chunks, source


def term_statistics(chunks):
    postings = defaultdict(list)
    lengths = []
    for ordinal, chunk in enumerate(chunks):
        counts = Counter(tokenize(chunk["text"]))
        lengths.append(sum(counts.values()))
        for token, count in sorted(counts.items()):
            postings[token].append([ordinal, count])
    if not lengths or not sum(lengths):
        raise ContractError("index needs at least one searchable token")
    return {"lengths": lengths, "average_length": sum(lengths) / len(lengths),
            "postings": dict(sorted(postings.items()))}


def build_index(manifest_path, expected_snapshot, config=None):
    config = dict(DEFAULT_CONFIG if config is None else config)
    validate_config(config)
    documents, structures, chunks, source = load_processed(manifest_path, expected_snapshot)
    chunks = sorted(chunks, key=lambda c: c["chunk_id"])
    # All original chunk fields survive verbatim. Source content stays in T2.
    payload = {"schema_version": INDEX_SCHEMA, "algorithm_version": ALGORITHM_VERSION,
               "tokenizer": TOKENIZER, "config": config, "source": source,
               "usage_policy": "EXPERIMENTAL_ONLY", "corpus_snapshot_id": None,
               "approved_for_legal_verification": False,
               "documents": sorted([{k: v for k, v in d.to_dict().items() if k != "content"} for d in documents],
                                   key=lambda d: d["document_version_id"]),
               "nodes": {r["document_version_id"]: r["nodes"] for r in structures},
               "chunks": chunks, "statistics": term_statistics(chunks)}
    return BM25Index(payload)


class BM25Index:
    """Small in-memory index. Payload and results are defensive copies."""

    def __init__(self, payload):
        payload = deepcopy(payload)
        if (payload.get("schema_version") != INDEX_SCHEMA or payload.get("algorithm_version") != ALGORITHM_VERSION
                or payload.get("tokenizer") != TOKENIZER or payload.get("usage_policy") != "EXPERIMENTAL_ONLY"
                or payload.get("approved_for_legal_verification") is not False or payload.get("corpus_snapshot_id") is not None):
            raise ContractError("incompatible or promoted index")
        validate_config(payload["config"])
        chunks = payload["chunks"]
        if [c["chunk_id"] for c in chunks] != sorted({c["chunk_id"] for c in chunks}):
            raise ContractError("chunks must be unique and sorted by chunk_id")
        if payload["statistics"] != term_statistics(chunks):
            raise ContractError("index term statistics mismatch")
        self._documents = {d["document_version_id"]: d for d in payload["documents"]}
        if len(self._documents) != len(payload["documents"]) or set(payload["nodes"]) != set(self._documents):
            raise ContractError("index document/node inventory mismatch")
        self._nodes = {n["node_id"]: n for nodes in payload["nodes"].values() for n in nodes}
        self._block_chunks = defaultdict(list)
        self._heading_chunks = defaultdict(list)
        heading_blocks = {n["heading_block_id"] for n in self._nodes.values() if n["heading_block_id"]}
        for chunk in chunks:
            document = self._documents.get(chunk["document_version_id"])
            if document is None or document["document_id"] != chunk["document_id"]:
                raise ContractError("chunk document identity mismatch")
            if any(chunk.get(k) != v or document.get(k) != v for k, v in STATUS_FIELDS.items()):
                raise ContractError("experimental source status mismatch")
            if chunk["attachment_status"] not in legal_parser.ATTACHMENT_STATUSES:
                raise ContractError("unknown attachment status")
            locator = chunk["source_locator"]
            if (locator["content_sha256"] != document["content_sha256"] or locator["end_exclusive"] is not True
                    or locator["offset_unit"] != "unicode_code_point"):
                raise ContractError("source locator mismatch")
            if hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest() != chunk["text_sha256"]:
                raise ContractError("chunk text hash mismatch")
            if any(n not in self._nodes for n in chunk["ancestor_ids"]) or chunk["node_id"] not in self._nodes:
                raise ContractError("missing context node")
            self._block_chunks[chunk["block_id"]].append(chunk["chunk_id"])
            if chunk["block_id"] in heading_blocks:
                self._heading_chunks[chunk["block_id"]].append(chunk["chunk_id"])
        by_id = {c["chunk_id"]: c for c in chunks}
        for ids in self._block_chunks.values():
            ids.sort(key=lambda cid: by_id[cid]["text_start"])
        self._payload = payload
        self.index_id = "bm25:" + digest(payload)

    def to_dict(self):
        return deepcopy(self._payload)

    def _context_and_cautions(self, chunk):
        ancestry = [self._nodes[n] for n in chunk["ancestor_ids"]]
        cautions = ["EXPERIMENTAL_UNVERIFIED_SOURCE", "LEGAL_SUPPORT_NOT_EVALUATED"]
        address_status = {"STRUCTURAL": "RULE_BASED_UNVERIFIED", "HEURISTIC": "HEURISTIC_CANDIDATE",
                          "REVIEW_REQUIRED": "REVIEW_REQUIRED"}[chunk["attachment_status"]]
        if chunk["attachment_status"] != "STRUCTURAL":
            cautions.append("ATTACHMENT_" + chunk["attachment_status"])
        if chunk["block_kind"] == "table":
            cautions.append("TABLE_REQUIRES_RAW_LAYOUT")
        cautions.extend(chunk["issues"])
        if chunk["scope"] == "quoted":
            cautions.append("QUOTED_ADDRESS_IS_OUTER_CONTEXT")
        if any(n["heading_block_id"] == chunk["block_id"] for n in ancestry):
            cautions.append("HEADING_BLOCK_MAY_REQUIRE_BODY")
        refs = {"block_chunk_ids": self._block_chunks[chunk["block_id"]],
                "ancestor_heading_chunk_ids": sorted({cid for n in ancestry
                    for cid in self._heading_chunks.get(n["heading_block_id"], []) if cid != chunk["chunk_id"]}),
                "ancestor_ids": chunk["ancestor_ids"], "processed_snapshot_id": self._payload["source"]["processed_snapshot_id"]}
        return {"address_status": address_status, "exact_citation_verified": False,
                "legal_evidence_status": "NOT_EVALUATED", "context_review_required": True,
                "context_expanded": False, "cautions": sorted(set(cautions)), "context_refs": refs}

    def search(self, query, top_k=5):
        started = perf_counter()
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ContractError("top_k must be an integer in [1,100]")
        if not isinstance(query, str) or len(query) > 10000:
            raise ContractError("query must be a string of at most 10000 characters")
        tokens = sorted(set(tokenize(query)))
        stats = self._payload["statistics"]
        known = [t for t in tokens if t in stats["postings"]]
        scores = defaultdict(float)
        matched = defaultdict(list)
        n = len(self._payload["chunks"])
        k1, b = self._payload["config"]["k1"], self._payload["config"]["b"]
        for token in known:
            postings = stats["postings"][token]
            idf = math.log1p((n - len(postings) + 0.5) / (len(postings) + 0.5))
            for ordinal, tf in postings:
                norm = k1 * (1 - b + b * stats["lengths"][ordinal] / stats["average_length"])
                scores[ordinal] += idf * tf * (k1 + 1) / (tf + norm)
                matched[ordinal].append(token)
        ordered = sorted(scores, key=lambda i: (-scores[i], self._payload["chunks"][i]["chunk_id"]))[:top_k]
        results = []
        for rank, ordinal in enumerate(ordered, 1):
            chunk = self._payload["chunks"][ordinal]
            results.append({"rank": rank, "bm25_score": scores[ordinal], "matched_terms": matched[ordinal],
                            "chunk": chunk, "document": self._documents[chunk["document_version_id"]],
                            **self._context_and_cautions(chunk)})
        response = deepcopy({"schema_version": "1", "retrieval_method": "BM25", "index_id": self.index_id,
                             "source": self._payload["source"], "corpus_snapshot_id": None,
                             "usage_policy": "EXPERIMENTAL_ONLY", "query": query, "query_tokens": tokens,
                             "unmatched_terms": [t for t in tokens if t not in stats["postings"]],
                             "top_k": top_k, "status": "EMPTY_QUERY" if not tokens else "OK" if results else "NO_MATCH",
                             "results": results})
        response["latency_ms"] = (perf_counter() - started) * 1000
        return response


def export_index(index, output_dir, manifest_path):
    """Exclusive new-directory export; manifest written last as completion marker."""
    output_dir, manifest_path = Path(output_dir).resolve(), Path(manifest_path).resolve()
    if output_dir.exists() or output_dir.is_relative_to(manifest_path.parent):
        raise ContractError("index output must be new and outside processed input directory")
    if file_hash(manifest_path) != index._payload["source"]["processed_manifest_sha256"]:
        raise ContractError("input manifest changed before export")
    data = canonical_json(index.to_dict()) + b"\n"
    manifest = {"schema_version": INDEX_SCHEMA, "artifact_kind": "experimental_bm25_index",
                "index_id": index.index_id, "usage_policy": "EXPERIMENTAL_ONLY", "corpus_snapshot_id": None,
                "approved_for_legal_verification": False, "source": index._payload["source"],
                "input_manifest_path": str(manifest_path), "chunk_count": len(index._payload["chunks"]),
                "artifact": {"path": "index.json", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
                "code_hashes": {"ai_core/" + name: file_hash(Path(__file__).with_name(name))
                                for name in ("bm25_retrieval.py", "legal_parser.py", "data_contracts.py")},
                "python_version": sys.version}
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in (("index.json", data), ("manifest.json", canonical_json(manifest) + b"\n")):
        with (output_dir / name).open("xb") as stream:
            stream.write(payload)
    return manifest


def load_index(manifest_path):
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != INDEX_SCHEMA or manifest.get("artifact_kind") != "experimental_bm25_index"
            or manifest.get("usage_policy") != "EXPERIMENTAL_ONLY" or manifest.get("corpus_snapshot_id") is not None
            or manifest.get("approved_for_legal_verification") is not False):
        raise ContractError("unsupported index manifest")
    for name in ("bm25_retrieval.py", "legal_parser.py", "data_contracts.py"):
        if manifest["code_hashes"].get("ai_core/" + name) != file_hash(Path(__file__).with_name(name)):
            raise ContractError("runtime code differs from pinned index implementation; rebuild explicitly")
    artifact = manifest["artifact"]
    target = (path.parent / artifact["path"]).resolve()
    if artifact["path"] != "index.json" or target.parent != path.parent:
        raise ContractError("invalid index artifact path")
    data = target.read_bytes()
    if len(data) != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise ContractError("index artifact hash/bytes mismatch")
    index = BM25Index(json.loads(data))
    if (index.index_id != manifest["index_id"] or index._payload["source"] != manifest["source"]
            or len(index._payload["chunks"]) != manifest["chunk_count"]):
        raise ContractError("index identity/source/count mismatch")
    return index


def main(argv=None):
    # Windows redirected stdout can default to cp1252; JSON is always UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--expected-snapshot", required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    query = commands.add_parser("query")
    query.add_argument("--manifest", type=Path, required=True)
    query.add_argument("--query", required=True)
    query.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        started = perf_counter()
        if args.command == "build":
            index = build_index(args.manifest, args.expected_snapshot)
            result = export_index(index, args.output_dir, args.manifest)
            result = {"status": "BUILT_EXPERIMENTAL_INDEX", "index_id": result["index_id"],
                      "chunk_count": result["chunk_count"], "build_and_export_ms": (perf_counter() - started) * 1000}
        else:
            index = load_index(args.manifest)
            load_ms = (perf_counter() - started) * 1000
            result = index.search(args.query, args.top_k)
            result["load_ms"] = load_ms
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
