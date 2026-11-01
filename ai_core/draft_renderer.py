"""Deterministic IDs, source copying and rendering from validated Summary units."""
from copy import deepcopy

from ai_core.summary_contracts import SummaryError, empty_result, validate_public_output, render_display_response
from ai_core.summary_adapter import build_issue_reports


def render_draft(value, model_output, limitations, context_gaps=None):
    if len(set(limitations + model_output["limitations"])) > 1024:
        raise SummaryError("SUMMARY_LIMITATIONS_LIMIT")
    result = empty_result(value, limitations=limitations + model_output["limitations"])
    def finish():
        result["display_version"] = "2"
        result["context_gaps"] = deepcopy(context_gaps or [])
        result["issue_reports"], result["unscoped_limitations"] = build_issue_reports(
            value, result["claims"], result["context_gaps"], model_output["limitations"])
        result["display_response"] = render_display_response(result)
        return validate_public_output(result)
    if model_output["abstain"]:
        return finish()
    evidence = {e["evidence_id"]: e for e in value["evidence_bundle"]["evidence"]}
    references, claims, citations = {}, [], []
    for unit in model_output["units"]:
        refs = []
        for eid in unit["evidence_ids"]:
            if eid not in references:
                rid = references[eid] = "R" + str(len(references) + 1)
                e = evidence[eid]
                citations.append({"citation_id": rid, "evidence_id": eid,
                    "bundle_id": value["evidence_bundle"]["bundle_id"],
                    **{k: deepcopy(e[k]) for k in ("document_id", "document_version_id", "chunk_id",
                        "article_id", "article_id_status", "attachment_status", "address_status",
                        "source_locator_extent", "quote_representation", "legal_evidence_status", "quote",
                        "quote_sha256", "source_locator", "normalized_locator", "exact_citation_verified",
                        "context_review_required", "warnings")},
                    "source_url": e["document"].get("source_url"),
                    **{k: e["chunk"].get(k) for k in ("article", "clause", "point")},
                    "source_metadata": deepcopy(e["document"]), "chunk_metadata": deepcopy(e["chunk"]),
                    "citation_status": "CANDIDATE_UNVERIFIED"})
            refs.append(references[eid])
        claims.append({"claim_id": "C" + str(len(claims) + 1), "text": unit["text"],
                       "issue_ids": deepcopy(unit["issue_ids"]), "citation_ids": refs, "origin": "draft"})
        if "statement_type" in unit:
            claims[-1]["statement_type"] = unit["statement_type"]
    result.update(status="DRAFT_UNVERIFIED", claims=claims, citations=citations,
        answer_draft="\n\n".join(c["text"] + " " + " ".join(f"[{r}]" for r in c["citation_ids"]) for c in claims))
    return finish()
