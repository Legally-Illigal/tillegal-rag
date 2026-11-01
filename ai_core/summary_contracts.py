"""Summary v1: structural contracts and references, never semantic verification."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import re

from ai_core.bm25_retrieval import digest
from ai_core.data_contracts import ContractError, canonical_json, content_hash, FIXED_FIELDS
from ai_core.judge_adapter import adapt_evidence_bundle
from ai_core.judge_contracts import JudgeError, OUTPUT_SCHEMA as JUDGE_SCHEMA, validate_output as validate_judge
from ai_core.summary_display import render_readable_response


class SummaryError(ContractError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def text(limit=4000, nullable=False):
    return {"type": ["string", "null"] if nullable else "string", "minLength": 1,
            "maxLength": limit, "pattern": r"\S"}


def array(item, limit=128, minimum=0, unique=False):
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": limit,
            "uniqueItems": unique}


def obj(properties, required=None, additional=False):
    return {"type": "object", "properties": properties, "additionalProperties": additional,
            "required": list(properties) if required is None else required}


ID = text(256)
DATE = {**text(10, True), "format": "date"}
STRINGS = array(text(), 1024, unique=True)
OPAQUE = {"type": "object"}  # Preserved metadata; independently checked against actual bundle below.
NULL_OBJECT = {"type": ["object", "null"]}
VERSION = {"type": "integer", "minimum": 1}
UNIT_SCHEMA = obj({"text": text(6000), "issue_ids": array(ID, 64, 1, True),
                   "evidence_ids": array(ID, 32, 1, True),
                   "statement_type": {"enum": ["source_statement", "application"]}},
                  required=["text", "issue_ids", "evidence_ids"])
LLM_OUTPUT_SCHEMA = obj({"units": array(UNIT_SCHEMA, 64), "limitations": array(text(), 64, unique=True),
                         "abstain": {"type": "boolean"}})
CLAIM_SCHEMA = obj({"claim_id": ID, "text": text(6000), "issue_ids": array(ID, 64, 1, True),
                    "citation_ids": array(ID, 32, 1, True), "origin": {"enum": ["draft"]},
                    "statement_type": {"enum": ["source_statement", "application"]}},
                   required=["claim_id", "text", "issue_ids", "citation_ids", "origin"])
CONTEXT_GAP_SCHEMA = obj({
    "gap_id": ID, "group_id": ID,
    "kind": {"enum": ["OMITTED_CHUNKS", "NON_USABLE_MEMBERS", "SEED_NOT_INCLUDED"]},
    "issue_ids": array(ID, 64, unique=True), "evidence_ids": array(ID, 128, unique=True),
    "related_evidence_ids": array(ID, 128, unique=True),
    "omitted_chunk_ids": array(ID, 1024, unique=True),
    "non_usable_evidence_ids": array(ID, 128, unique=True), "seed_chunk_id": ID,
    "scope_status": {"enum": ["MAPPED", "GROUP_ONLY_UNKNOWN_ISSUES"]},
    "statement_policy": {"enum": ["SOURCE_STATEMENTS_ONLY_SEMANTIC_EFFECT_UNKNOWN"]},
})
ISSUE_REPORT_SCHEMA = obj({
    "issue_id": ID, "issue_text": text(),
    "judge_coverage": {"enum": ["full", "partial", "none", "not_evaluated"]},
    "summary_status": {"enum": ["HAS_DRAFT_CLAIMS", "NO_DRAFT_CLAIMS"]},
    "claim_ids": array(ID, 64, unique=True), "context_gap_ids": array(ID, 300, unique=True),
    "limitations": STRINGS,
})
CITATION_SCHEMA = obj({
    "citation_id": ID, "evidence_id": ID, "bundle_id": ID,
    **{k: ID for k in ("document_id", "document_version_id", "chunk_id")},
    **{k: text(nullable=True) for k in ("source_url", "article", "clause", "point", "article_id",
        "article_id_status", "attachment_status", "address_status", "source_locator_extent",
        "quote_representation", "legal_evidence_status")},
    "quote": text(100000), "quote_sha256": text(64),
    "source_locator": OPAQUE, "normalized_locator": OPAQUE,
    "source_metadata": OPAQUE, "chunk_metadata": OPAQUE,
    "exact_citation_verified": {"enum": [False]}, "context_review_required": {"enum": [True]},
    "citation_status": {"enum": ["CANDIDATE_UNVERIFIED"]}, "warnings": STRINGS,
})
PUBLIC_OUTPUT_SCHEMA = obj({
    "schema_version": {"enum": ["1"]}, "request_id": text(256, True),
    "as_of_date": DATE, "stop_reason": text(256, True),
    "status": {"enum": ["DRAFT_UNVERIFIED", "ABSTAINED_UNVERIFIED", "ERROR"]},
    "draft_version": {"type": ["integer", "null"], "minimum": 1},
    "answer_draft": text(500000, True), "claims": array(CLAIM_SCHEMA, 64),
    "citations": array(CITATION_SCHEMA, 128), "limitations": STRINGS,
    "error": {**obj({"code": text(128), "stage": text(128)}), "type": ["object", "null"]},
})
# Additive output fields; old saved drafts remain valid revision input.
PUBLIC_OUTPUT_SCHEMA["properties"].update({
    "context_gaps": array(CONTEXT_GAP_SCHEMA, 300), "issue_reports": array(ISSUE_REPORT_SCHEMA, 64),
    "unscoped_limitations": STRINGS, "display_response": text(2000000, True),
    "display_version": {"enum": ["2"]},
})
REVISION_REPORT_SCHEMA = obj({
    "schema_version": {"enum": ["1"]}, "request_id": ID, "draft_version": VERSION,
    "claim_reports": array(obj({"claim_id": ID, "citation_ids": array(ID, 32, unique=True),
        "action": {"enum": ["keep", "revise", "remove", "needs_evidence"]},
        "reason": text()}), 64),
})
BUNDLE_SCHEMA = obj({
    "schema_version": {"enum": ["1"]}, "builder_version": {"enum": ["evidence-builder-v1"]},
    "request_id": ID, "query": {"type": "string", "maxLength": 10000}, "index_id": ID, "bundle_id": ID,
    "source": OPAQUE, "corpus_snapshot_id": {"type": "null"},
    "usage_policy": {"enum": ["EXPERIMENTAL_ONLY"]},
    "approved_for_legal_verification": {"enum": [False]},
    "retrieval_status": {"enum": ["OK", "EMPTY_QUERY", "NO_MATCH"]},
    "status": {"enum": ["EMPTY", "PARTIAL", "ASSEMBLED"]},
    "budget": obj({"max_chunks": VERSION, "max_quote_chars": VERSION}),
    "quote_chars": {"type": "integer", "minimum": 0}, "evidence": array(OPAQUE, 128),
    "groups": array(OPAQUE, 100), "limitations": STRINGS, "trace": OPAQUE,
}, additional=True)
INPUT_SCHEMA = obj({
    "schema_version": {"enum": ["1"]}, "request_id": ID, "question": text(10000), "as_of_date": DATE,
    "query_plan": obj({"issues": array(obj({"issue_id": ID, "text": text()}), 64, 1)}),
    "evidence_bundle": BUNDLE_SCHEMA, "judge_result": deepcopy(JUDGE_SCHEMA),
    "stop_reason": text(256, True), "draft_version": VERSION,
    "previous_draft": NULL_OBJECT, "verification_report": NULL_OBJECT,
})


@dataclass(frozen=True)
class SummaryLimits:
    max_input_bytes: int = 100000
    max_output_chars: int = 24000
    max_request_bytes: int = 2000000

    def __post_init__(self):
        if (type(self.max_input_bytes) is not int or not 1024 <= self.max_input_bytes <= 120000
                or type(self.max_output_chars) is not int or not 128 <= self.max_output_chars <= 65536
                or type(self.max_request_bytes) is not int or not 1024 <= self.max_request_bytes <= 4000000):
            raise SummaryError("SUMMARY_CONFIGURATION_ERROR")


def check_shape(value, schema):
    """Small JSON Schema subset used here; public schemas also describe wire shape."""
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        actual = ("null" if value is None else "boolean" if type(value) is bool else
                  "integer" if type(value) is int else "string" if isinstance(value, str) else
                  "array" if isinstance(value, list) else "object" if isinstance(value, dict) else None)
        if actual not in types:
            raise ValueError("type")
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise ValueError("enum")
    if value is None:
        return
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if (not set(schema.get("required", [])) <= set(value)
                or (schema.get("additionalProperties") is False and set(value) - set(props))):
            raise ValueError("fields")
        for key, val in value.items():
            if key in props:
                check_shape(val, props[key])
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise ValueError("array length")
        if schema.get("uniqueItems") and len({canonical_json(v) for v in value}) != len(value):
            raise ValueError("duplicates")
        for val in value:
            check_shape(val, schema.get("items", {}))
    elif isinstance(value, str):
        if (not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
                or ("pattern" in schema and not re.search(schema["pattern"], value))):
            raise ValueError("text")
        if schema.get("format") == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("date")
            date.fromisoformat(value)
    elif type(value) is int and value < schema.get("minimum", value):
        raise ValueError("minimum")


def _unique(items, field):
    values = [i[field] for i in items]
    if len(values) != len(set(values)):
        raise SummaryError("SUMMARY_DUPLICATE_ID")
    return set(values)


def validate_bundle(bundle):
    """Check v1 builder identity/quotes, not source truth or legal validity."""
    check_shape(bundle, BUNDLE_SCHEMA)
    if bundle["bundle_id"] != "bundle:" + digest({k: v for k, v in bundle.items() if k != "bundle_id"}):
        raise SummaryError("SUMMARY_BUNDLE_FINGERPRINT_MISMATCH")
    _unique(bundle["evidence"], "evidence_id")
    _unique(bundle["evidence"], "chunk_id")
    docs, chunks = {}, {}
    for e in bundle["evidence"]:
        for k in ("document_id", "document_version_id", "chunk_id"):
            check_shape(e[k], ID)
        doc, chunk = e["document"], e["chunk"]
        if not isinstance(doc, dict) or not isinstance(chunk, dict):
            raise SummaryError("SUMMARY_INVALID_BUNDLE")
        if any(doc.get(k) != expected for k, expected in FIXED_FIELDS.items()):
            raise SummaryError("SUMMARY_INVALID_SOURCE_POLICY")
        dv = e["document_version_id"]
        if dv in docs and docs[dv] != doc:
            raise SummaryError("SUMMARY_CONFLICTING_DOCUMENT")
        docs[dv] = doc
        for nested in (doc, chunk):
            for k in ("document_id", "document_version_id", "chunk_id"):
                if k in nested and nested[k] != e[k]:
                    raise SummaryError("SUMMARY_BUNDLE_IDENTITY_MISMATCH")
        if (e["quote"] != chunk["text"] or content_hash(e["quote"]) != e["quote_sha256"]
                or e["quote_sha256"] != chunk["text_sha256"]
                or e["source_locator"] != chunk["source_locator"]
                or e["attachment_status"] != chunk["attachment_status"]):
            raise SummaryError("SUMMARY_BUNDLE_CONTENT_MISMATCH")
        loc = e["normalized_locator"]
        if (loc != {"block_id": chunk["block_id"], "start": chunk["text_start"], "end": chunk["text_end"],
                    "offset_unit": "unicode_code_point", "end_exclusive": True}
                or e["exact_citation_verified"] is not False
                or e["legal_evidence_status"] != "NOT_EVALUATED"
                or e["context_review_required"] is not True):
            raise SummaryError("SUMMARY_INVALID_BUNDLE_POLICY_OR_LOCATOR")
        chunks[e["chunk_id"]] = e
    expected_status = ("EMPTY" if not bundle["evidence"] else "PARTIAL"
                       if any(g["omitted_chunk_ids"] for g in bundle["groups"]) else "ASSEMBLED")
    if (bundle["quote_chars"] != sum(len(e["quote"]) for e in bundle["evidence"])
            or bundle["status"] != expected_status
            or bundle["quote_chars"] > bundle["budget"]["max_quote_chars"]
            or len(bundle["evidence"]) > bundle["budget"]["max_chunks"]):
        raise SummaryError("SUMMARY_INVALID_BUNDLE")
    eids = {e["evidence_id"] for e in bundle["evidence"]}
    _unique(bundle["groups"], "retrieved_chunk_id")
    for g in bundle["groups"]:
        check_shape(g, obj({"retrieved_chunk_id": ID, "rank": VERSION,
            "members": array(obj({"evidence_id": ID, "role": text()}), 128),
            "omitted_chunk_ids": array(ID, 1024, unique=True), "retrieval_cautions": STRINGS,
            "seed_included": {"type": "boolean"}, "article_expansion_allowed": {"type": "boolean"},
            "context_sufficiency": {"enum": ["NOT_EVALUATED"]}}, additional=True))
        _unique(g["members"], "evidence_id")
        if (not {m["evidence_id"] for m in g["members"]} <= eids
                or set(g["omitted_chunk_ids"]).intersection(chunks)
                or g["seed_included"] != (g["retrieved_chunk_id"] in chunks)):
            raise SummaryError("SUMMARY_INVALID_GROUP_REFERENCE")


def validate_public_output(value):
    check_shape(value, PUBLIC_OUTPUT_SCHEMA)
    cids = _unique(value["claims"], "claim_id")
    rids = _unique(value["citations"], "citation_id")
    _unique(value["citations"], "evidence_id")
    enriched = {"context_gaps", "issue_reports", "unscoped_limitations", "display_response"}
    if "display_version" in value and not enriched <= set(value):
        raise SummaryError("SUMMARY_INCOMPLETE_ISSUE_REPORTS")
    if enriched.intersection(value):
        if not enriched <= set(value):
            raise SummaryError("SUMMARY_INCOMPLETE_ISSUE_REPORTS")
        gids = _unique(value["context_gaps"], "gap_id")
        iids = _unique(value["issue_reports"], "issue_id")
        gaps = {g["gap_id"]: g for g in value["context_gaps"]}
        for gap in gaps.values():
            if (not set(gap["issue_ids"]) <= iids
                    or (gap["scope_status"] == "MAPPED") != bool(gap["issue_ids"])
                    or not set(gap["evidence_ids"]) <= set(gap["related_evidence_ids"])
                    or set(gap["evidence_ids"]).intersection(gap["non_usable_evidence_ids"])):
                raise SummaryError("SUMMARY_INVALID_CONTEXT_GAP")
            if ((gap["kind"] == "OMITTED_CHUNKS") != bool(gap["omitted_chunk_ids"])
                    or (gap["kind"] == "NON_USABLE_MEMBERS") != bool(gap["non_usable_evidence_ids"])):
                raise SummaryError("SUMMARY_INVALID_CONTEXT_GAP")
        for report in value["issue_reports"]:
            expected = [c["claim_id"] for c in value["claims"] if report["issue_id"] in c["issue_ids"]]
            expected_gaps = [g["gap_id"] for g in value["context_gaps"] if report["issue_id"] in g["issue_ids"]]
            if (report["claim_ids"] != expected or report["context_gap_ids"] != expected_gaps
                    or (report["summary_status"] == "HAS_DRAFT_CLAIMS") != bool(expected)):
                raise SummaryError("SUMMARY_INVALID_ISSUE_REPORT")
        if value["issue_reports"] and any(not set(c["issue_ids"]) <= iids for c in value["claims"]):
            raise SummaryError("SUMMARY_INVALID_ISSUE_REPORT")
        partial_issues = {r["issue_id"] for r in value["issue_reports"] if r["judge_coverage"] in ("partial", "none")}
        affected_eids = {eid for g in value["context_gaps"] for eid in g["evidence_ids"]}
        citation_eids = {c["citation_id"]: c["evidence_id"] for c in value["citations"]}
        for claim in value["claims"]:
            if ((set(claim["issue_ids"]).intersection(partial_issues)
                    or {citation_eids.get(r) for r in claim["citation_ids"]}.intersection(affected_eids))
                    and claim.get("statement_type") != "source_statement"):
                raise SummaryError("SUMMARY_CONTEXT_SCOPE_REQUIRED")
        if value["display_response"] != render_display_response(value):
            raise SummaryError("SUMMARY_INVALID_DISPLAY_RESPONSE")
    if value["status"] == "DRAFT_UNVERIFIED":
        if not cids or not rids or value["error"] is not None or value["draft_version"] is None or not value["request_id"]:
            raise SummaryError("SUMMARY_INVALID_DRAFT")
        if [c["claim_id"] for c in value["claims"]] != [f"C{i+1}" for i in range(len(cids))]:
            raise SummaryError("SUMMARY_INVALID_DRAFT_IDS")
        if [c["citation_id"] for c in value["citations"]] != [f"R{i+1}" for i in range(len(rids))]:
            raise SummaryError("SUMMARY_INVALID_DRAFT_IDS")
        refs = {r for c in value["claims"] for r in c["citation_ids"]}
        if refs != rids or any("[" in c["text"] or "]" in c["text"] for c in value["claims"]):
            raise SummaryError("SUMMARY_INVALID_DRAFT_REFERENCES")
        rendered = "\n\n".join(c["text"] + " " + " ".join(f"[{r}]" for r in c["citation_ids"]) for c in value["claims"])
        if value["answer_draft"] != rendered:
            raise SummaryError("SUMMARY_INVALID_RENDERED_DRAFT")
    elif (cids or rids or value["answer_draft"] is not None
          or (value["status"] == "ERROR") != (value["error"] is not None)
          or (value["status"] == "ABSTAINED_UNVERIFIED" and not value["limitations"])):
        raise SummaryError("SUMMARY_INVALID_DRAFT")
    return deepcopy(value)


def validate_input(value, limits=None):
    limits = limits or SummaryLimits()
    try:
        if len(canonical_json(value)) > limits.max_request_bytes:
            raise SummaryError("SUMMARY_REQUEST_LIMIT")
        check_shape(value, INPUT_SCHEMA)
        bundle = value["evidence_bundle"]
        if value["request_id"] != bundle["request_id"]:
            raise SummaryError("SUMMARY_REQUEST_ID_MISMATCH")
        validate_bundle(bundle)
        # Exact projection expected by existing Judge validators. No Judge call here.
        projected = adapt_evidence_bundle(bundle, question=value["question"], query_plan=value["query_plan"],
            as_of_date=value["as_of_date"], search_rounds_completed=0, query_history=[])
        validate_judge(value["judge_result"], projected)
        previous, report = value["previous_draft"], value["verification_report"]
        if previous is None:
            if value["draft_version"] != 1 or report is not None:
                raise SummaryError("SUMMARY_INVALID_INITIAL_VERSION")
        else:
            validate_public_output(previous)
            if (previous["status"] not in ("DRAFT_UNVERIFIED", "ABSTAINED_UNVERIFIED") or previous["request_id"] != value["request_id"]
                    or previous["as_of_date"] != value["as_of_date"]
                    or value["draft_version"] != previous["draft_version"] + 1):
                raise SummaryError("SUMMARY_REVISION_VERSION_MISMATCH")
            check_shape(report, REVISION_REPORT_SCHEMA)
            if report["request_id"] != value["request_id"] or report["draft_version"] != previous["draft_version"]:
                raise SummaryError("SUMMARY_REPORT_VERSION_MISMATCH")
            _unique(report["claim_reports"], "claim_id")
            claims = {c["claim_id"]: c for c in previous["claims"]}
            issues = {i["issue_id"] for i in value["query_plan"]["issues"]}
            if any(not set(c["issue_ids"]) <= issues for c in previous["claims"]):
                raise SummaryError("SUMMARY_REVISION_ISSUE_MISMATCH")
            for item in report["claim_reports"]:
                if (item["claim_id"] not in claims or not set(item["citation_ids"]) <=
                        set(claims[item["claim_id"]]["citation_ids"])):
                    raise SummaryError("SUMMARY_REPORT_REFERENCE_MISMATCH")
        return deepcopy(value), projected
    except SummaryError:
        raise
    except JudgeError as exc:
        raise SummaryError("SUMMARY_" + exc.code) from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise SummaryError("SUMMARY_INVALID_INPUT") from None


def validate_llm_output(value, summary_input, allowed_ids, context_gaps=None):
    try:
        check_shape(value, LLM_OUTPUT_SCHEMA)
        if value["abstain"] != (not value["units"]) or (value["abstain"] and not value["limitations"]):
            raise SummaryError("SUMMARY_INVALID_ABSTENTION")
        issues = {i["issue_id"] for i in summary_input["query_plan"]["issues"]}
        checks = {c["issue_id"]: set(c["evidence_ids"]) for c in summary_input["judge_result"]["issue_checks"]}
        partial = {c["issue_id"] for c in summary_input["judge_result"]["issue_checks"] if c["coverage"] != "full"}
        affected = {eid for g in context_gaps or [] for eid in g["evidence_ids"]}
        for unit in value["units"]:
            if "[" in unit["text"] or "]" in unit["text"]:
                raise SummaryError("SUMMARY_MODEL_CITATION_MARKER")
            if re.search(r"https?://|www\.", unit["text"], re.IGNORECASE):
                raise SummaryError("SUMMARY_MODEL_SOURCE_URL")
            if (not set(unit["issue_ids"]) <= issues or not set(unit["evidence_ids"]) <= set(allowed_ids)):
                raise SummaryError("SUMMARY_INVALID_OUTPUT_REFERENCE")
            if any(not set(unit["evidence_ids"]).intersection(checks[i]) for i in unit["issue_ids"]):
                raise SummaryError("SUMMARY_ISSUE_EVIDENCE_MAPPING_MISMATCH")
            if ((set(unit["issue_ids"]).intersection(partial) or set(unit["evidence_ids"]).intersection(affected))
                    and unit.get("statement_type") != "source_statement"):
                raise SummaryError("SUMMARY_CONTEXT_SCOPE_REQUIRED")
        return deepcopy(value)
    except SummaryError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SummaryError("SUMMARY_INVALID_OUTPUT_SCHEMA") from None


def empty_result(value, *, code=None, stage="input", limitations=None):
    value = value if isinstance(value, dict) else {}
    valid_text = lambda v, n: v if isinstance(v, str) and v.strip() and len(v) <= n else None
    version = value.get("draft_version")
    as_of = value.get("as_of_date")
    try:
        check_shape(as_of, DATE)
    except (ValueError, TypeError):
        as_of = None
    return {"schema_version": "1", "request_id": valid_text(value.get("request_id"), 256),
        "as_of_date": as_of, "stop_reason": valid_text(value.get("stop_reason"), 256),
        "status": "ERROR" if code else "ABSTAINED_UNVERIFIED",
        "draft_version": version if type(version) is int and version >= 1 else None,
        "answer_draft": None, "claims": [], "citations": [],
        "limitations": list(dict.fromkeys(limitations or ["DRAFT_NOT_VERIFIED"])),
        "error": {"code": code, "stage": stage} if code else None}


def render_display_response(value):
    """Published UI text: draft + issue limits; answer_draft stays claim-only for Verify."""
    if value.get("display_version") == "2":
        return render_readable_response(value)
    return _render_legacy_display_response(value)


def _render_legacy_display_response(value):
    """Validate old saved drafts without rewriting their display text/version."""
    if value["status"] == "ERROR":
        return None
    lines = [value["answer_draft"] or "Chưa thể tạo khẳng định có căn cứ từ evidence được cấp.",
             "", "Bản nháp chưa Verify; chưa xác minh nguồn và khả năng áp dụng tại thời điểm yêu cầu.",
             "Không coi việc có claim cho một issue là đã trả lời đầy đủ issue đó."]
    for report in value.get("issue_reports", []):
        lines += ["", report["issue_id"] + ": " + report["issue_text"],
                  "Judge coverage=" + report["judge_coverage"] + "; Summary=" + report["summary_status"]]
        lines.extend("- " + message for message in report["limitations"])
    if value.get("unscoped_limitations"):
        lines += ["", "Giới hạn bổ sung/chưa gắn issue cụ thể:"]
        lines.extend("- " + message for message in value["unscoped_limitations"])
    unknown = [g for g in value.get("context_gaps", []) if not g["issue_ids"]]
    for gap in unknown:
        lines.append("- " + gap["gap_id"] + ": " + gap["kind"] + " tại " + gap["group_id"] +
                     "; chưa xác định issue liên quan, không dùng để kết luận bao quát nhóm này.")
    return "\n".join(lines)


def json_schemas():
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **deepcopy(schema)}
            for name, schema in (("input", INPUT_SCHEMA), ("llm_output", LLM_OUTPUT_SCHEMA),
                                 ("output", PUBLIC_OUTPUT_SCHEMA), ("revision_report", REVISION_REPORT_SCHEMA))}
