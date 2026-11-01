"""T6.1: bounded, source-preserving context assembly; no search or LLM calls.

Use a BM25Index loaded through load_index in production. Quotes are exact
normalized chunk text, NOT raw HTML excerpts. Raw locators cover whole blocks.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
import math

from ai_core.bm25_retrieval import BM25Index, digest
from ai_core.data_contracts import ContractError

BUILDER_VERSION = "evidence-builder-v1"
ADDRESS_STATUS = {"STRUCTURAL": "RULE_BASED_UNVERIFIED",
                  "HEURISTIC": "HEURISTIC_CANDIDATE", "REVIEW_REQUIRED": "REVIEW_REQUIRED"}


@dataclass(frozen=True)
class EvidenceBudget:
    max_chunks: int = 32
    max_quote_chars: int = 24000

    def __post_init__(self):
        for value in (self.max_chunks, self.max_quote_chars):
            if type(value) is not int or value < 1:
                raise ContractError("evidence budgets must be positive integers")


class EvidenceBuilder:
    """Request-local output, immutable private index copy; safe to reuse.

    Keep all retrieved seeds first, then same-block fragments, ancestor headings,
    and same-scope article candidates. Never infer that this window is sufficient
    legal evidence. REVIEW_REQUIRED/quoted seeds get no article-body expansion.
    """

    def __init__(self, index: BM25Index, budget: EvidenceBudget | None = None):
        self._index_id = index.index_id
        self._payload = index.to_dict()
        self._budget = budget or EvidenceBudget()
        self._chunks = {c["chunk_id"]: c for c in self._payload["chunks"]}
        self._documents = {d["document_version_id"]: d for d in self._payload["documents"]}
        self._nodes = {n["node_id"]: n for ns in self._payload["nodes"].values() for n in ns}
        self._blocks = defaultdict(list)
        self._articles = defaultdict(list)
        for chunk in self._chunks.values():
            self._blocks[(chunk["document_version_id"], chunk["block_id"])].append(chunk)
            self._articles[self._window_key(chunk)].append(chunk)
        for group in (*self._blocks.values(), *self._articles.values()):
            group.sort(key=self._source_order)

    @staticmethod
    def _source_order(chunk):
        return (chunk["source_locator"]["block_source_start"], chunk["text_start"], chunk["chunk_id"])

    def _article_id(self, chunk):
        return next((nid for nid in reversed(chunk["ancestor_ids"])
                     if self._nodes[nid]["kind"] == "article"), None)

    def _window_key(self, chunk):
        # IDs, not labels: duplicate article/form numbers must never join.
        boundaries = tuple(nid for nid in chunk["ancestor_ids"]
                           if self._nodes[nid]["kind"] in ("appendix", "form"))
        return (chunk["document_version_id"], chunk["scope"], boundaries, self._article_id(chunk))

    def _candidates(self, seed):
        result = {}

        def add(chunk, role):
            if chunk["chunk_id"] != seed["chunk_id"]:
                result.setdefault(chunk["chunk_id"], role)

        for chunk in self._blocks[(seed["document_version_id"], seed["block_id"])]:
            add(chunk, "SAME_BLOCK")
        for nid in seed["ancestor_ids"]:
            node = self._nodes[nid]
            for chunk in self._blocks.get((seed["document_version_id"], node["heading_block_id"]), []):
                if chunk["scope"] == seed["scope"]:
                    add(chunk, "ANCESTOR_HEADING_CANDIDATE")
        if (self._article_id(seed) is not None and seed["scope"] != "quoted"
                and seed["attachment_status"] != "REVIEW_REQUIRED"):
            for chunk in self._articles[self._window_key(seed)]:
                add(chunk, "ARTICLE_CONTEXT_CANDIDATE")
        return result

    def _evidence(self, chunk):
        document = self._documents[chunk["document_version_id"]]
        warnings = set(chunk["issues"] + document["issues"])
        warnings.update(("EXPERIMENTAL_UNVERIFIED_SOURCE", "LEGAL_SUPPORT_NOT_EVALUATED",
                         "CONTEXT_SUFFICIENCY_NOT_EVALUATED"))
        if chunk["attachment_status"] != "STRUCTURAL":
            warnings.add("ATTACHMENT_" + chunk["attachment_status"])
        if chunk["block_kind"] == "table":
            warnings.add("TABLE_REQUIRES_RAW_LAYOUT")
        if chunk["scope"] == "quoted":
            warnings.add("QUOTED_ADDRESS_IS_OUTER_CONTEXT")
        heading = any(self._nodes[n]["heading_block_id"] == chunk["block_id"] for n in chunk["ancestor_ids"])
        if heading:
            warnings.add("HEADING_BLOCK_MAY_REQUIRE_BODY")
        return {
            "evidence_id": "evidence:" + digest([BUILDER_VERSION, self._index_id,
                                                chunk["chunk_id"], chunk["text_sha256"]]),
            "document_id": chunk["document_id"], "document_version_id": chunk["document_version_id"],
            "chunk_id": chunk["chunk_id"], "article_id": self._article_id(chunk),
            "article_id_status": "PARSER_CANDIDATE_NOT_LEGAL_VERIFICATION",
            "quote": chunk["text"], "quote_sha256": chunk["text_sha256"],
            "quote_representation": "NORMALIZED_CHUNK_TEXT",
            "normalized_locator": {"block_id": chunk["block_id"], "start": chunk["text_start"],
                                   "end": chunk["text_end"], "offset_unit": "unicode_code_point",
                                   "end_exclusive": True},
            "source_locator": deepcopy(chunk["source_locator"]), "source_locator_extent": "WHOLE_BLOCK",
            "document": deepcopy(document), "chunk": deepcopy(chunk),
            "ancestry": [deepcopy(self._nodes[n]) for n in chunk["ancestor_ids"]],
            "attachment_status": chunk["attachment_status"],
            "address_status": ADDRESS_STATUS[chunk["attachment_status"]],
            "exact_citation_verified": False, "legal_evidence_status": "NOT_EVALUATED",
            "context_review_required": True, "is_heading": heading,
            "warnings": sorted(warnings),
        }

    def _validate_retrieval(self, response):
        """Validate origin/content, not relevance or correctness of BM25 scores."""
        v2 = response.get('schema_version') == '2'
        if v2 and (not isinstance(response.get('config'),dict) or response.get('retriever_id') !=
                   str(response.get('retrieval_method','')).lower() + ':' + digest(response.get('config'))):
            raise ContractError('retriever identity mismatch')
        if (response.get("index_id") != self._index_id or response.get("source") != self._payload["source"]
                or (not v2 and (response.get("schema_version") != "1" or response.get("retrieval_method") != "BM25"))
                or (v2 and (response.get('retrieval_method') not in ('DENSE','HYBRID','HYBRID_RERANK')
                            or not isinstance(response.get('retriever_id'),str)))
                or response.get("usage_policy") != "EXPERIMENTAL_ONLY"
                or response.get("corpus_snapshot_id") is not None):
            raise ContractError("retrieval origin mismatch")
        if not isinstance(response.get("query"), str) or not isinstance(response.get("results"), list):
            raise ContractError("invalid retrieval response")
        hits = response["results"]
        status = response.get("status")
        if status not in ("OK", "EMPTY_QUERY", "NO_MATCH") or bool(hits) != (status == "OK"):
            raise ContractError("inconsistent retrieval status")
        seen = set()
        for rank, hit in enumerate(hits, 1):
            chunk = hit.get("chunk", {})
            cid = chunk.get("chunk_id")
            score = hit.get("score") if v2 else hit.get("bm25_score")
            if v2 and (hit.get('score_kind') != response['retrieval_method'] or not isinstance(hit.get('ranking'),dict)):
                raise ContractError('retrieval score metadata mismatch')
            if (cid in seen or cid not in self._chunks or chunk != self._chunks[cid]
                    or hit.get("document") != self._documents[chunk["document_version_id"]]
                    or type(hit.get("rank")) is not int or hit["rank"] != rank
                    or type(score) not in (int, float) or not math.isfinite(score) or (not v2 and score <= 0)):
                raise ContractError("retrieval hit identity/content/rank/score mismatch")
            if (hit.get("address_status") != ADDRESS_STATUS[chunk["attachment_status"]]
                    or hit.get("exact_citation_verified") is not False
                    or hit.get("legal_evidence_status") != "NOT_EVALUATED"
                    or not isinstance(hit.get("cautions"), list)
                    or not all(isinstance(w, str) for w in hit["cautions"])):
                raise ContractError("invalid retrieval verification status or cautions")
            seen.add(cid)

    def build(self, retrieval: dict, *, request_id: str) -> dict:
        """Pure assembly. All omissions are explicit; no quote slicing or retries."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ContractError("request_id must be nonblank")
        self._validate_retrieval(retrieval)
        selected = {}
        used_chars = 0

        def include(cid):
            nonlocal used_chars
            if cid in selected:
                return
            chunk = self._chunks[cid]
            if (len(selected) < self._budget.max_chunks
                    and used_chars + len(chunk["text"]) <= self._budget.max_quote_chars):
                selected[cid] = self._evidence(chunk)
                used_chars += len(chunk["text"])

        hits = retrieval["results"]
        # Reserve for every seed before context of rank 1 can consume the budget.
        for hit in hits:
            include(hit["chunk"]["chunk_id"])
        candidates = [self._candidates(hit["chunk"]) for hit in hits]
        for hit, window in zip(hits, candidates):
            if hit["chunk"]["chunk_id"] in selected:
                for cid in window:
                    include(cid)
        groups = []
        for hit, window in zip(hits, candidates):
            seed = hit["chunk"]
            cid = seed["chunk_id"]
            requested = {cid: "RETRIEVED", **window}
            included = sorted((c for c in requested if c in selected), key=lambda c: self._source_order(self._chunks[c]))
            score_fields = ({'score':hit['score'],'score_kind':hit['score_kind'],'ranking':deepcopy(hit['ranking'])}
                            if retrieval['schema_version'] == '2' else {'bm25_score':hit['bm25_score']})
            groups.append({"retrieved_chunk_id": cid, "rank": hit["rank"], **score_fields,
                           "retrieval_cautions": deepcopy(hit["cautions"]),
                           "members": [{"evidence_id": selected[c]["evidence_id"], "role": requested[c]} for c in included],
                           "omitted_chunk_ids": [c for c in requested if c not in selected],
                           "seed_included": cid in selected,
                           "article_expansion_allowed": self._article_id(seed) is not None
                               and seed["scope"] != "quoted" and seed["attachment_status"] != "REVIEW_REQUIRED",
                           "context_sufficiency": "NOT_EVALUATED"})
        status = ("EMPTY" if not selected else "PARTIAL" if any(g["omitted_chunk_ids"] for g in groups)
                  else "ASSEMBLED")
        bundle = {"schema_version": "1", "builder_version": BUILDER_VERSION, "request_id": request_id,
                  "query": retrieval["query"], "index_id": self._index_id,
                  "source": deepcopy(self._payload["source"]), "corpus_snapshot_id": None,
                  "usage_policy": "EXPERIMENTAL_ONLY", "approved_for_legal_verification": False,
                  "retrieval_status": retrieval["status"], "status": status,
                  "budget": asdict(self._budget), "quote_chars": used_chars,
                  "evidence": list(selected.values()), "groups": groups,
                  "limitations": ["NO_LEGAL_OR_SEMANTIC_VERIFICATION", "CONTEXT_SUFFICIENCY_NOT_EVALUATED",
                                  "NO_CROSS_REFERENCE_EXPANSION", "RAW_LOCATOR_COVERS_WHOLE_BLOCK",
                                  "CHAR_BUDGET_IS_NOT_PROVIDER_TOKEN_BUDGET"],
                  "trace": {"search_calls": 0, "llm_calls": 0,
                            "seed_count": len(hits), "evidence_count": len(selected)}}
        bundle["bundle_id"] = "bundle:" + digest(bundle)
        if retrieval.get('schema_version') == '2':
            bundle['retrieval_method'] = retrieval['retrieval_method']
            bundle['retriever_id'] = retrieval['retriever_id']
            if retrieval.get('trace',{}).get('execution_status') == 'DEGRADED':
                bundle['limitations'].append('RETRIEVAL_DEGRADED_BRANCH_UNAVAILABLE')
            bundle['bundle_id'] = 'bundle:' + digest({k:v for k,v in bundle.items() if k != 'bundle_id'})
        return bundle
