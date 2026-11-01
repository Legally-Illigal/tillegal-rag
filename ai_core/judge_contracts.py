"""Judge v1 contracts: stdlib structural validation plus cross-field invariants.

JSON Schemas describe interchange shape. Python additionally enforces ID membership,
calendar dates, coverage/status consistency and query-content repetition.
"""
from copy import deepcopy
from datetime import date
import re

from ai_core.data_contracts import ContractError, canonical_json


class JudgeError(ContractError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def text(limit=4000, nullable=False):
    return {"type": ["string", "null"] if nullable else "string",
            "minLength": 1, "maxLength": limit, "pattern": r"\S"}


def array(item, limit=256, minimum=0, unique=False):
    result = {"type": "array", "items": item, "minItems": minimum, "maxItems": limit}
    if unique:
        result["uniqueItems"] = True
    return result


def obj(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


ID = text(256)
STRINGS = array(text(), unique=True)
DATE = {**text(10, nullable=True), "format": "date"}
LOCATOR = obj({"block_source_start": {"type": "integer", "minimum": 0},
               "block_source_end": {"type": "integer", "minimum": 0},
               "content_sha256": text(64), "end_exclusive": {"type": "boolean"},
               "offset_unit": text(100)}, required=[])
NORMALIZED_LOCATOR = obj({"block_id": ID, "start": {"type": "integer", "minimum": 0},
                          "end": {"type": "integer", "minimum": 0},
                          "offset_unit": text(100), "end_exclusive": {"type": "boolean"}}, required=[])
EVIDENCE_PROPERTIES = {"evidence_id": ID, "content": text(100000)}
for _key in ("document_id", "document_version_id", "chunk_id", "article_id", "article_id_status",
             "document_number", "title", "source_url", "article", "clause", "point", "scope",
             "appendix", "form", "issuing_authority", "source_version", "effect_status_reported",
             "status_observed_at", "collected_at", "quote_sha256", "quote_representation",
             "source_locator_extent", "attachment_status", "address_status", "legal_evidence_status",
             "legal_approval", "provenance_status", "rights_status", "usage_policy"):
    EVIDENCE_PROPERTIES[_key] = text(nullable=True)
EVIDENCE_PROPERTIES.update({
    "temporal_status": text(nullable=True),
    "effective_from": DATE, "effective_to": DATE, "issued_on": DATE,
    "warnings": STRINGS,
    "exact_citation_verified": {"type": ["boolean", "null"]},
    "context_review_required": {"type": ["boolean", "null"]},
    "is_heading": {"type": ["boolean", "null"]},
    "source_locator": {**LOCATOR, "type": ["object", "null"]},
    "normalized_locator": {**NORMALIZED_LOCATOR, "type": ["object", "null"]},
})
GROUP = obj({"retrieved_chunk_id": ID, "seed_included": {"type": "boolean"},
             "omitted_chunk_ids": array(ID, unique=True), "context_sufficiency": text(),
             "members": array(obj({"evidence_id": ID, "role": text()})),
             "retrieval_cautions": STRINGS}, required=["omitted_chunk_ids"])
CONTEXT = obj({
    "bundle_status": {"enum": ["EMPTY", "PARTIAL", "ASSEMBLED", "NOT_PROVIDED"]},
    "bundle_id": text(nullable=True), "builder_version": text(nullable=True),
    "index_id": text(nullable=True), "retrieval_status": text(nullable=True),
    "usage_policy": text(nullable=True),
    "approved_for_legal_verification": {"type": ["boolean", "null"]},
    "limitations": STRINGS, "groups": array(GROUP, 100),
}, required=["bundle_status", "limitations", "groups"])
INPUT_SCHEMA = obj({
    "request_id": ID, "question": text(10000),
    "query_plan": obj({"issues": array(obj({"issue_id": ID, "text": text()}), 64, 1)}),
    "evidence": array(obj(EVIDENCE_PROPERTIES, ["evidence_id", "content"]), 128),
    "as_of_date": DATE,
    "search_rounds_completed": {"type": "integer", "minimum": 0},
    "query_history": array(ID, unique=True),
    "query_text_history": array(text(10000), unique=True), "context": CONTEXT,
}, required=["request_id", "question", "query_plan", "evidence", "as_of_date",
             "search_rounds_completed", "query_history"])
OUTPUT_SCHEMA = obj({
    "status": {"enum": ["SUFFICIENT", "INSUFFICIENT", "ERROR"]},
    "issue_checks": array(obj({"issue_id": ID, "evidence_ids": array(ID, 128, unique=True),
                               "coverage": {"enum": ["full", "partial", "none"]}}), 64),
    "missing_items": array(text(), 128, unique=True),
    "next_queries": array(obj({"text": text(10000), "target_issue_id": ID}), 64),
    "usable_evidence_ids": array(ID, 128, unique=True),
    "explanation": text(6000), "error_code": text(128, nullable=True),
})


def _shape(value, schema):
    """Validate only the explicit JSON Schema keywords used in these schemas."""
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        matches = {"null": value is None, "string": isinstance(value, str),
                   "integer": type(value) is int, "boolean": type(value) is bool,
                   "object": isinstance(value, dict), "array": isinstance(value, list)}
        if not any(matches[t] for t in types):
            raise ValueError("type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("enum")
    if value is None:
        return
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(value) - set(props) or not set(schema.get("required", [])) <= set(value):
            raise ValueError("fields")
        for key, item in value.items():
            _shape(item, props[key])
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise ValueError("array limit")
        if schema.get("uniqueItems") and len({canonical_json(v) for v in value}) != len(value):
            raise ValueError("duplicate items")
        for item in value:
            _shape(item, schema["items"])
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
            raise ValueError("string limit")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError("blank")
        if schema.get("format") == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("date format")
            date.fromisoformat(value)
    elif type(value) is int and value < schema.get("minimum", value):
        raise ValueError("minimum")


def query_key(value):
    """Conservative repeat detection: whitespace and case, no semantic guessing."""
    return " ".join(value.split()).casefold()


def _unique_ids(items, field, code):
    ids = [item[field] for item in items]
    if len(ids) != len(set(ids)):
        raise JudgeError(code)
    return set(ids)


def validate_input(value):
    try:
        _shape(value, INPUT_SCHEMA)
        result = deepcopy(value)
        _unique_ids(result["query_plan"]["issues"], "issue_id", "INVALID_INPUT_DUPLICATE_ID")
        eids = _unique_ids(result["evidence"], "evidence_id", "INVALID_INPUT_DUPLICATE_ID")
        for group in result.get("context", {}).get("groups", []):
            if any(m["evidence_id"] not in eids for m in group.get("members", [])):
                raise JudgeError("INVALID_INPUT_REFERENCE")
        for evidence in result["evidence"]:
            for field, left, right in (("source_locator", "block_source_start", "block_source_end"),
                                       ("normalized_locator", "start", "end")):
                locator = evidence.get(field) or {}
                if left in locator and right in locator and locator[left] > locator[right]:
                    raise JudgeError("INVALID_INPUT_LOCATOR")
            if (evidence.get("effective_from") and evidence.get("effective_to")
                    and evidence["effective_from"] > evidence["effective_to"]):
                raise JudgeError("INVALID_INPUT_DATE_RANGE")
        return result
    except JudgeError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise JudgeError("INVALID_INPUT_SCHEMA") from None


def validate_output(value, judge_input):
    try:
        _shape(value, OUTPUT_SCHEMA)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise JudgeError("INVALID_OUTPUT_SCHEMA") from None
    issues = {i["issue_id"] for i in judge_input["query_plan"]["issues"]}
    eids = {e["evidence_id"] for e in judge_input["evidence"]}
    checked = _unique_ids(value["issue_checks"], "issue_id", "INVALID_OUTPUT_REFERENCE")
    used = set(value["usable_evidence_ids"])
    if (not checked <= issues or not used <= eids
            or any(not set(c["evidence_ids"]) <= eids for c in value["issue_checks"])
            or any(q["target_issue_id"] not in issues for q in value["next_queries"])):
        raise JudgeError("INVALID_OUTPUT_REFERENCE")
    status = value["status"]
    if status == "ERROR":
        # No assessment was completed: never invent coverage=none.
        if (value["error_code"] is None or value["issue_checks"] or used
                or value["missing_items"] or value["next_queries"]):
            raise JudgeError("INVALID_OUTPUT_CONSISTENCY")
        return deepcopy(value)
    if checked != issues or value["error_code"] is not None:
        raise JudgeError("INVALID_OUTPUT_CONSISTENCY")
    referenced = set()
    for check in value["issue_checks"]:
        ids = check["evidence_ids"]
        if (check["coverage"] == "none" and ids) or (check["coverage"] != "none" and not ids):
            raise JudgeError("INVALID_OUTPUT_CONSISTENCY")
        referenced.update(ids)
    if not used <= referenced:
        raise JudgeError("INVALID_OUTPUT_CONSISTENCY")
    full = all(c["coverage"] == "full" for c in value["issue_checks"])
    if status == "SUFFICIENT":
        if not full or value["missing_items"] or value["next_queries"] or used != referenced:
            raise JudgeError("INVALID_OUTPUT_CONSISTENCY")
    elif full or not value["missing_items"]:
        raise JudgeError("INVALID_OUTPUT_CONSISTENCY")
    previous = {query_key(q) for q in judge_input.get("query_text_history", [])}
    proposed = [query_key(q["text"]) for q in value["next_queries"]]
    if len(proposed) != len(set(proposed)) or previous.intersection(proposed):
        raise JudgeError("INVALID_OUTPUT_REPEATED_QUERY")
    return deepcopy(value)


def error_result(code):
    return {"status": "ERROR", "issue_checks": [], "missing_items": [], "next_queries": [],
            "usable_evidence_ids": [], "explanation": "Không hoàn tất đánh giá Judge; xem error_code.",
            "error_code": code}


def input_json_schema():
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Tillegal Judge input v1", **deepcopy(INPUT_SCHEMA)}


def output_json_schema():
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Tillegal Judge output v1 (Python adds relational checks)", **deepcopy(OUTPUT_SCHEMA)}


def anthropic_output_schema():
    """Wire schema subset only; full OUTPUT_SCHEMA is always validated locally.

    Anthropic does not support string lengths or array bounds/uniqueness in
    structured decoding. Dropping wire constraints never repairs model output.
    See https://platform.claude.com/docs/en/build-with-claude/structured-outputs
    """
    unsupported = {"minLength", "maxLength", "maxItems", "uniqueItems"}

    def project(value):
        if isinstance(value, dict):
            return {k: project(v) for k, v in value.items() if k not in unsupported}
        if isinstance(value, list):
            return [project(v) for v in value]
        return value

    return project(OUTPUT_SCHEMA)
