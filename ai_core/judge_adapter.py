"""Projection from EvidenceBuilder v1. No mutation, retrieval, or sufficiency logic."""
from copy import deepcopy

from ai_core.judge_contracts import JudgeError, validate_input

DOCUMENT_FIELDS = ("document_number", "title", "source_url", "issuing_authority", "source_version",
                   "effective_from", "effective_to", "issued_on", "temporal_status", "legal_approval",
                   "provenance_status", "rights_status", "usage_policy", "effect_status_reported",
                   "status_observed_at", "collected_at")
EVIDENCE_FIELDS = ("document_id", "document_version_id", "chunk_id", "article_id", "article_id_status",
                   "quote_sha256", "quote_representation", "normalized_locator", "source_locator",
                   "source_locator_extent", "attachment_status", "address_status", "exact_citation_verified",
                   "legal_evidence_status", "context_review_required", "is_heading")
CHUNK_FIELDS = ("article", "clause", "point", "scope", "appendix", "form")
CONTEXT_FIELDS = ("bundle_id", "builder_version", "index_id", "retrieval_status", "usage_policy",
                  "approved_for_legal_verification")
GROUP_FIELDS = ("retrieved_chunk_id", "seed_included", "omitted_chunk_ids", "context_sufficiency",
                "members", "retrieval_cautions")


def adapt_evidence_bundle(bundle, *, question, query_plan, as_of_date,
                          search_rounds_completed, query_history, query_text_history=None):
    """Caller supplies original question/issues/date/history and accumulates rounds.

    All duplicate IDs are rejected, including identical duplicates. Conflicting
    content/metadata receives a distinct error. Missing source fields stay null.
    Fingerprints remain identity only; source verification flags are not upgraded.
    """
    try:
        if (not isinstance(bundle, dict) or bundle.get("schema_version") != "1"
                or bundle.get("status") not in ("EMPTY", "PARTIAL", "ASSEMBLED")
                or not isinstance(bundle.get("evidence"), list)
                or not isinstance(bundle.get("limitations"), list)
                or not isinstance(bundle.get("groups"), list)):
            raise JudgeError("INVALID_BUNDLE_SCHEMA")
        evidence, seen = [], {}
        for source in bundle["evidence"]:
            eid = source["evidence_id"]
            if not isinstance(eid, str):
                raise JudgeError("INVALID_BUNDLE_SCHEMA")
            if eid in seen:
                raise JudgeError("CONFLICTING_EVIDENCE_ID" if seen[eid] != source else "DUPLICATE_EVIDENCE_ID")
            seen[eid] = source
            doc, chunk = source.get("document", {}), source.get("chunk", {})
            if not isinstance(doc, dict) or not isinstance(chunk, dict):
                raise JudgeError("INVALID_BUNDLE_SCHEMA")
            for key in ("document_id", "document_version_id", "chunk_id"):
                for nested in (doc, chunk):
                    if key in nested and key in source and nested[key] != source[key]:
                        raise JudgeError("BUNDLE_IDENTITY_MISMATCH")
            item = {"evidence_id": eid, "content": source["quote"],
                    **{k: source.get(k) for k in EVIDENCE_FIELDS},
                    **{k: doc.get(k) for k in DOCUMENT_FIELDS},
                    **{k: chunk.get(k) for k in CHUNK_FIELDS}, "warnings": source["warnings"]}
            evidence.append(item)
        context = {k: bundle.get(k) for k in CONTEXT_FIELDS}
        context.update(bundle_status=bundle["status"], limitations=bundle["limitations"],
                       groups=[{k: g[k] for k in GROUP_FIELDS if k in g} for g in bundle["groups"]])
        value = {"request_id": bundle["request_id"], "question": question, "query_plan": query_plan,
                 "evidence": evidence, "as_of_date": as_of_date,
                 "search_rounds_completed": search_rounds_completed, "query_history": query_history,
                 "query_text_history": [] if query_text_history is None else query_text_history,
                 "context": context}
        return validate_input(deepcopy(value))
    except JudgeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise JudgeError("INVALID_BUNDLE_SCHEMA") from None
