"""Pure Summary input/context projection. No retrieval, mutation or judging."""
from copy import deepcopy

from ai_core.summary_contracts import SummaryError
from ai_core.summary_display import parse_issue_limitation


def make_summary_input(bundle, judge_result, *, question, query_plan, as_of_date,
                       stop_reason="single_pass_completed", draft_version=1,
                       previous_draft=None, verification_report=None):
    return deepcopy({"schema_version": "1", "request_id": bundle["request_id"],
        "question": question, "as_of_date": as_of_date, "query_plan": query_plan,
        "evidence_bundle": bundle, "judge_result": judge_result, "stop_reason": stop_reason,
        "draft_version": draft_version, "previous_draft": previous_draft,
        "verification_report": verification_report})


def select_context(value, projected):
    """Scope structural gaps through group membership and Judge references only.

    A gap limits statements using its selected members, not every issue. No
    importance inference from rank, location, role or score; no extra quotes.
    """
    allowed = set(value["judge_result"]["usable_evidence_ids"])
    selected = [deepcopy(e) for e in projected["evidence"] if e["evidence_id"] in allowed]
    context = deepcopy(projected["context"])
    checks = value["judge_result"]["issue_checks"]
    chunk_eids = {e["chunk_id"]: e["evidence_id"] for e in value["evidence_bundle"]["evidence"]}
    relevant, gaps = [], []
    for group in context["groups"]:
        members = {m["evidence_id"] for m in group.get("members", [])}
        seed = chunk_eids.get(group["retrieved_chunk_id"])
        related = members | ({seed} if seed is not None else set())
        affected = sorted(related.intersection(allowed))
        issue_ids = sorted(c["issue_id"] for c in checks if set(c["evidence_ids"]).intersection(related))
        kinds = []
        if group["omitted_chunk_ids"]:
            kinds.append("OMITTED_CHUNKS")
        if members - allowed:
            kinds.append("NON_USABLE_MEMBERS")
        if not group.get("seed_included", False):
            kinds.append("SEED_NOT_INCLUDED")
        if not affected and not kinds:
            continue
        # Excluded members remain identifiers in gaps, never source content.
        group["members"] = [m for m in group.get("members", []) if m["evidence_id"] in allowed]
        relevant.append(group)
        for kind in kinds:
            gaps.append({"gap_id": "G" + str(len(gaps) + 1),
                "group_id": group["retrieved_chunk_id"],
                "kind": kind, "issue_ids": issue_ids, "evidence_ids": affected,
                "related_evidence_ids": sorted(related),
                "omitted_chunk_ids": group["omitted_chunk_ids"][:] if kind == "OMITTED_CHUNKS" else [],
                "non_usable_evidence_ids": sorted(members - allowed) if kind == "NON_USABLE_MEMBERS" else [],
                "seed_chunk_id": group["retrieved_chunk_id"],
                "scope_status": "MAPPED" if issue_ids else "GROUP_ONLY_UNKNOWN_ISSUES",
                "statement_policy": "SOURCE_STATEMENTS_ONLY_SEMANTIC_EFFECT_UNKNOWN"})
    context["groups"] = relevant
    context["selection_policy"] = "ISSUE_SCOPED_PARTIAL_GROUNDED_ANSWERS"
    context["context_gaps"] = gaps
    return selected, context, gaps


def build_issue_reports(value, claims, context_gaps, model_limitations):
    """Do not infer that some generated claims cover an entire issue."""
    judge = value["judge_result"]
    checks = {c["issue_id"]: c for c in judge["issue_checks"]}
    issue_ids = [i["issue_id"] for i in value["query_plan"]["issues"]]
    specific = {iid: [] for iid in issue_ids}
    unscoped = []
    for message in judge["missing_items"] + model_limitations:
        matched, _ = parse_issue_limitation(message, issue_ids)
        (specific[matched] if matched else unscoped).append(message)
    reports = []
    for issue in value["query_plan"]["issues"]:
        iid = issue["issue_id"]
        check = checks.get(iid)
        coverage = check["coverage"] if check else "not_evaluated"
        claim_ids = [c["claim_id"] for c in claims if iid in c["issue_ids"]]
        scoped_gaps = [g for g in context_gaps if iid in g["issue_ids"]]
        limits = specific[iid][:]
        if coverage in ("partial", "none"):
            limits.append("Judge chỉ ghi nhận mức bao phủ " + coverage + "; chưa đủ căn cứ để trả lời toàn bộ vấn đề.")
            if unscoped:
                limits.append("Có giới hạn chưa gắn được issue cụ thể; xem unscoped_limitations, không tự quy thuộc nội dung.")
        for gap in scoped_gaps:
            limits.append(gap["gap_id"] + ": " + gap["kind"] + "; ảnh hưởng ngữ nghĩa chưa xác định, chỉ mô tả nội dung nguồn độc lập.")
        if not claim_ids:
            limits.append("Chưa trình bày claim nào cho vấn đề này; không có nghĩa là không có quy định.")
        reports.append({"issue_id": iid, "issue_text": issue["text"], "judge_coverage": coverage,
            "summary_status": "HAS_DRAFT_CLAIMS" if claim_ids else "NO_DRAFT_CLAIMS",
            "claim_ids": claim_ids, "context_gap_ids": [g["gap_id"] for g in scoped_gaps],
            "limitations": list(dict.fromkeys(limits))})
    return reports, list(dict.fromkeys(unscoped))


def collect_limitations(value, selected):
    """Source warnings are preserved, never promoted to legal assurance."""
    bundle, judge = value["evidence_bundle"], value["judge_result"]
    result = ["DRAFT_NOT_VERIFIED", "CITATION_MAPPING_IS_STRUCTURAL_NOT_SEMANTIC",
              "DOCUMENT_VERSION_ID_IS_DATA_FINGERPRINT_NOT_LEGAL_VALIDITY"]
    result.extend(bundle["limitations"])
    result.append("USAGE_POLICY:" + bundle["usage_policy"])
    if bundle["approved_for_legal_verification"] is False:
        result.append("BUNDLE_NOT_APPROVED_FOR_LEGAL_VERIFICATION")
    if judge["status"] == "INSUFFICIENT":
        result.append("JUDGE_INSUFFICIENT")
    result.extend(judge["missing_items"])
    if value["as_of_date"] is None:
        result.append("AS_OF_DATE_NOT_PROVIDED")
    if value["stop_reason"]:
        result.append("STOP_REASON:" + value["stop_reason"])
    for e in selected:
        result.extend(e["warnings"])
        for field in ("legal_approval", "provenance_status", "temporal_status", "rights_status"):
            result.append(e["evidence_id"] + ":" + field + "=" + (e.get(field) or "UNKNOWN"))
        if not e.get("effective_from") or not e.get("effective_to") or e.get("temporal_status") == "UNKNOWN":
            result.append(e["evidence_id"] + ":APPLICABILITY_AT_AS_OF_DATE_NOT_VERIFIED")
    for g in bundle["groups"]:
        result.extend(g.get("retrieval_cautions", []))
        if g["omitted_chunk_ids"]:
            result.append("BUNDLE_CONTEXT_OMITTED:" + g.get("retrieved_chunk_id", "unknown"))
    result = list(dict.fromkeys(result))
    if len(result) > 1024:
        raise SummaryError("SUMMARY_LIMITATIONS_LIMIT")
    return result
