"""Offline experimental structural parser. Input is DocumentVersion, never HF fields.

Offsets are Python Unicode code-point offsets, end-exclusive. Raw locators address
the immutable DocumentVersion.content; chunk offsets address normalized block text.
No network, OCR, legal approval, embeddings or citation verification.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys

from ai_core.data_contracts import ContractError, DocumentVersion, canonical_json, content_hash, file_hash, load_documents

PARSER_VERSION = "legal-structure-v1.1"
PARSED_SCHEMA_VERSION = "2"
DEFAULT_MAX_CHARS = 1800
ADDRESS_FIELDS = ("appendix", "form", "chapter", "section", "article", "clause", "point")
ATTACHMENT_STATUSES = {"STRUCTURAL", "HEURISTIC", "REVIEW_REQUIRED"}
# Reviewed local-source anomaly, NOT a general rule for completing Vietnamese prose.
# Pin the bytes and locator so another provider/version is never silently diagnosed.
REVIEWED_SOURCE_ANOMALIES = {
    "4718484e39685d10185daae16ff996809299e3ffdc767df3a32f1e2a2bcb27d3":
        (135063, 135094, "Hội đồng tiền lương quốc gia do"),
}
BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "section", "article", "ul", "ol"}
SKIP_TAGS = {"head", "script", "style"}
KNOWN_TAGS = BLOCK_TAGS | SKIP_TAGS | {"html", "body", "table", "tbody", "thead", "tfoot", "tr", "td", "th", "caption", "col", "colgroup", "span", "a", "b", "strong", "i", "em", "u", "s", "sup", "sub", "br", "hr", "img", "font", "small", "center"}
PATTERNS = {
    "appendix": re.compile(r"^Phụ lục(?:\s+([IVXLCDM]+|\d+))?(?:\s*[.:]\s*.*)?$", re.I),
    "form": re.compile(r"^Mẫu\s+số\s+(\S+)$", re.I),
    "chapter": re.compile(r"^Chương\s+([IVXLCDM]+|\d+)(?:[.\s]|$)", re.I),
    "section": re.compile(r"^Mục\s+(\d+)(?:[.\s]|$)", re.I),
    "article": re.compile(r"^Điều\s+(\d+[a-zđ]?)\.\s*", re.I),
    "clause": re.compile(r"^(\d+[a-zđ]?)\.\s+", re.I),
    "point": re.compile(r"^([a-zđ])\)\s+", re.I),
}
RANKS = {"chapter": 1, "section": 2, "article": 3, "clause": 4, "point": 5}


def normalize(text: str) -> str:
    """Only collapse Unicode whitespace; do not rewrite words or punctuation."""
    return re.sub(r"\s+", " ", text).strip()


def stable_id(prefix: str, *values) -> str:
    return prefix + ":" + content_hash(canonical_json(values).decode("utf-8"))


def segment_text(raw: str, segment: dict) -> str:
    if segment["kind"] == "separator":
        return " "
    if segment["kind"] == "image":
        return " [IMAGE] "
    if segment["kind"] == "empty_table":
        return " [EMPTY_TABLE] "
    value = raw[segment["start"]:segment["end"]]
    return unescape(value) if segment["kind"] == "entity" else value


class SourceBlocks(HTMLParser):
    """Tokenize without DOM reserialization, retaining exact source spans.

    Tables are opaque blocks: their row/cell topology stays in the raw HTML.
    HTMLParser is not a browser/CSS engine. Malformed table/skip scopes are flagged.
    """
    def __init__(self, raw: str):
        super().__init__(convert_charrefs=False)
        self.raw = raw
        self.lines = [0] + [m.end() for m in re.finditer("\n", raw)]
        self.blocks, self.excluded, self.parts = [], [], []
        self.skipped = []
        self.table_depth = 0
        self.table_start = None
        self.issues = set()
        self.block_issues = set()
        self.kind = "text"

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def add(self, start, end, kind="text"):
        segment = {"start": start, "end": end, "kind": kind}
        if self.skipped:
            self.excluded.append({**segment, "reason": "NON_CONTENT:" + self.skipped[-1]})
        else:
            self.parts.append(segment)

    def flush(self, end=None):
        text = normalize("".join(segment_text(self.raw, p) for p in self.parts))
        if not text and self.kind == "table" and self.table_start is not None:
            self.parts.append({"start": self.table_start, "end": end or len(self.raw), "kind": "empty_table"})
            text = "[EMPTY_TABLE]"
            self.block_issues.add("EMPTY_TABLE_PRESERVED")
        if text:
            starts = [p["start"] for p in self.parts]
            ends = [p["end"] for p in self.parts]
            self.blocks.append({"kind": self.kind, "text": text, "text_sha256": content_hash(text),
                                "source_start": self.table_start if self.table_start is not None else min(starts),
                                "source_end": end if end is not None else max(ends),
                                "segments": self.parts, "issues": sorted(self.block_issues)})
        else:
            self.excluded.extend({**p, "reason": "WHITESPACE_OR_EMPTY_MARKUP"} for p in self.parts)
        self.parts, self.block_issues = [], set()

    def handle_starttag(self, tag, attrs):
        start = self.source_offset()
        end = start + len(self.get_starttag_text())
        if tag in SKIP_TAGS:
            self.skipped.append(tag)
            self.issues.add("NON_CONTENT_MARKUP_EXCLUDED")
            return
        if self.skipped:
            return
        if tag not in KNOWN_TAGS:
            self.issues.add("UNKNOWN_HTML_TAG:" + tag)
            self.block_issues.add("UNKNOWN_HTML_TAG:" + tag)
        if tag == "table":
            if self.table_depth == 0:
                self.flush()
                self.kind, self.table_start = "table", start
                self.block_issues.add("TABLE_TEXT_LINEARIZED_USE_RAW_FOR_TOPOLOGY")
            self.table_depth += 1
        elif tag == "img":
            self.add(start, end, "image")
            self.block_issues.add("IMAGE_NOT_INTERPRETED")
        elif tag in BLOCK_TAGS or tag in {"br", "hr", "tr", "td", "th"}:
            if self.table_depth:
                self.add(start, end, "separator")
            else:
                self.flush()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"br", "hr", "img", "col"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skipped:
            if tag in self.skipped:
                self.skipped = self.skipped[:self.skipped.index(tag)]
            return
        end = self.raw.find(">", self.source_offset()) + 1
        if tag == "table":
            if not self.table_depth:
                self.issues.add("UNMATCHED_TABLE_END")
                return
            self.table_depth -= 1
            if not self.table_depth:
                self.flush(end)
                self.kind, self.table_start = "text", None
        elif tag in BLOCK_TAGS or tag in {"td", "th", "tr"}:
            if self.table_depth:
                self.add(self.source_offset(), end, "separator")
            else:
                self.flush()

    def handle_data(self, data):
        self.add(self.source_offset(), self.source_offset() + len(data))

    def entity(self, name, prefix):
        start = self.source_offset()
        end = start + len(prefix) + len(name)
        if self.raw[end:end + 1] == ";":
            end += 1
        self.add(start, end, "entity")

    def handle_entityref(self, name):
        self.entity(name, "&")

    def handle_charref(self, name):
        self.entity(name, "&#")

    def handle_comment(self, data):
        start = self.source_offset()
        self.excluded.append({"start": start, "end": min(len(self.raw), start + len(data) + 7),
                              "kind": "comment", "reason": "HTML_COMMENT"})

    def finish(self):
        self.feed(self.raw)
        self.close()
        if self.table_depth or self.skipped:
            self.issues.add("UNCLOSED_HTML_SCOPE_REVIEW")
            self.block_issues.add("UNCLOSED_HTML_SCOPE_REVIEW")
        self.flush(len(self.raw) if self.table_depth else None)
        return self.blocks, self.excluded, sorted(self.issues)


def extract_blocks(document: DocumentVersion):
    if document.content_format == "html":
        return SourceBlocks(document.content).finish()
    blocks, excluded = [], []
    for match in re.finditer(r"[^\r\n]+|[\r\n]+", document.content):
        text = normalize(match.group())
        segment = {"start": match.start(), "end": match.end(), "kind": "text"}
        if text:
            blocks.append({"kind": "text", "text": text, "text_sha256": content_hash(text),
                           "source_start": match.start(), "source_end": match.end(), "segments": [segment], "issues": []})
        else:
            excluded.append({**segment, "reason": "WHITESPACE_OR_EMPTY_MARKUP"})
    return blocks, excluded, []


def build_structure(document: DocumentVersion, blocks: list[dict]):
    nodes, issues = [], []
    seen = set()

    def node(kind, label, parent, block=None, scope="main"):
        key = (parent, kind, label)
        duplicate = kind in RANKS or kind in {"appendix", "form"}
        warnings = ["DUPLICATE_LABEL_UNDER_PARENT"] if duplicate and key in seen else []
        if duplicate:
            seen.add(key)
        item = {"node_id": stable_id("node", PARSER_VERSION, document.document_version_id, kind, parent,
                                     block["block_id"] if block else None, label),
                "kind": kind, "label": label, "parent_id": parent, "scope": scope,
                "heading_block_id": block["block_id"] if block else None, "issues": warnings}
        nodes.append(item)
        return item

    root = node("document", document.document_number, None, scope="document")
    region = node("preamble", None, root["node_id"], scope="preamble")
    base, stack, quote = region, [], None
    unresolved_number_context = False
    for block in blocks:
        text = block["text"]
        block["attachment_status"] = "STRUCTURAL"
        if quote is not None or (block["kind"] != "table" and text.startswith(('“', '"'))):
            if quote is None:
                parent = stack[-1] if stack else base
                quote = node("quoted_text", None, parent["node_id"], block, scope="quoted")
                quote["issues"].append("QUOTED_STRUCTURE_NOT_RESOLVED")
                closing = '”' if text.startswith('“') else '"'
                quote_depth = 0
            block["node_id"] = quote["node_id"]
            block["issues"].append("QUOTED_STRUCTURE_NOT_RESOLVED")
            if closing == '”':
                quote_depth += text.count('“') - text.count('”')
            else:
                quote_depth = (quote_depth + text.count('"')) % 2
            if quote_depth <= 0:
                quote = None
            continue

        match_kind, match = None, None
        if block["kind"] != "table":
            for kind, pattern in PATTERNS.items():
                found = pattern.match(text)
                if found:
                    match_kind, match = kind, found
                    break
        if match_kind == "appendix":
            region = node("appendix", match.group(1), root["node_id"], block, "appendix")
            base, stack = region, []
            unresolved_number_context = False
        elif match_kind == "form":
            if region["kind"] == "appendix":
                base = node("form", match.group(1), region["node_id"], block, "form")
                suffix = re.search(r"/PL([IVXLCDM]+)$", match.group(1), re.I)
                if suffix and region["label"] and suffix.group(1).upper() != region["label"].upper():
                    base["issues"].append("FORM_APPENDIX_LABEL_MISMATCH")
                stack = []
                unresolved_number_context = False
            else:
                block["issues"].append("FORM_WITHOUT_APPENDIX_REVIEW")
                block["attachment_status"] = "REVIEW_REQUIRED"
        elif (base["kind"] == "form" and any(n["kind"] == "article" for n in stack)
              and (text == "Ghi chú:" or
                   (block["kind"] == "table" and text.startswith("Nơi nhận:") and "CHỦ TỊCH" in text))):
            # A form-level signature/notes boundary; do not change main/appendix scope.
            base = node("form_tail", None, base["node_id"], block, scope="form")
            base["issues"].append("FORM_LEVEL_TAIL")
            stack = []
            unresolved_number_context = False
        elif ((block["kind"] == "table" and re.search(r"Nơi nhận\s*:|CHỦ TỊCH QUỐC HỘI|^BỘ TRƯỞNG|SAO Y BẢN CHÍNH", text))
              or text.startswith("Bộ luật này được Quốc hội")) and region["scope"] == "main":
            region = node("closing", None, root["node_id"], block, "closing")
            base, stack = region, []
            unresolved_number_context = False
        elif match_kind in RANKS:
            rank = RANKS[match_kind]
            if region["kind"] == "preamble" and rank <= 3:
                region = node("main", None, root["node_id"], scope="main")
                base, stack = region, []
            valid = (rank <= 3 and region["scope"] not in {"preamble", "closing"}
                     or rank == 4 and any(n["kind"] == "article" for n in stack)
                     or rank == 5 and any(n["kind"] == "clause" for n in stack))
            if valid:
                stack = [n for n in stack if RANKS[n["kind"]] < rank]
                parent = stack[-1] if stack else base
                stack.append(node(match_kind, match.group(1), parent["node_id"], block, base["scope"]))
                unresolved_number_context = False
            else:
                block["issues"].append("UNRESOLVED_NUMBERED_TEXT")
                block["attachment_status"] = "REVIEW_REQUIRED"
        elif block["kind"] != "table":
            block["issues"].append("UNNUMBERED_TEXT_CONTEXT_HEURISTIC")
            block["attachment_status"] = "HEURISTIC"
            subpoint = re.match(r"^[a-zđ]\d+\)\s+", text, re.I)
            malformed_number = re.match(r"^\d+[a-zđ]?\.{2,}", text, re.I)
            clause = next((n for n in reversed(stack) if n["kind"] == "clause"), None)
            shared = re.match(r"^Các văn bản quy định tại (điểm [a-zđ](?:,\s*điểm [a-zđ])+)(?:\s+)khoản này\b", text, re.I)
            refs = {label.lower() for label in re.findall(r"điểm ([a-zđ])", shared.group(1), re.I)} if shared else set()
            siblings = {n["label"] for n in nodes if clause and n["parent_id"] == clause["node_id"] and n["kind"] == "point"}
            if shared and len(refs) >= 2 and refs <= siblings:
                # Explicit local reference to multiple existing sibling points.
                stack = [n for n in stack if RANKS[n["kind"]] <= RANKS["clause"]]
                block["issues"].append("SHARED_CLAUSE_REFERENCE")
                block["attachment_status"] = "STRUCTURAL"
            elif malformed_number:
                # Retain only article-or-higher context; do not guess that 4... is clause 4.
                stack = [n for n in stack if RANKS[n["kind"]] < RANKS["clause"]]
                block["issues"].append("UNRESOLVED_NUMBERED_TEXT")
                unresolved_number_context = True
                block["attachment_status"] = "REVIEW_REQUIRED"
            elif subpoint:
                block["issues"].append("SUBPOINT_NOT_RESOLVED")
                block["attachment_status"] = "REVIEW_REQUIRED"
            elif unresolved_number_context or (stack and stack[-1]["kind"] == "point"):
                # Keep the candidate parent; metadata is not an exact citation address.
                block["attachment_status"] = "REVIEW_REQUIRED"
            if block["attachment_status"] == "REVIEW_REQUIRED":
                block["issues"].append("ATTACHMENT_REVIEW_REQUIRED")
        elif unresolved_number_context:
            block["attachment_status"] = "REVIEW_REQUIRED"
            block["issues"].append("ATTACHMENT_REVIEW_REQUIRED")
        else:
            block["attachment_status"] = "HEURISTIC"
        block["node_id"] = (stack[-1] if stack else base)["node_id"]
    if quote is not None:
        issues.append("UNCLOSED_QUOTATION_REVIEW")
    return nodes, issues


def split_text(text: str, max_chars: int):
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # Prefer sentence/semicolon boundaries; oversized sentences still need
            # the full parent block. This character budget is not a token budget.
            boundaries = list(re.finditer(r"[.!?;]\s+", text[start:end]))
            if boundaries:
                end = start + boundaries[-1].end()
            else:
                boundary = text.rfind(" ", start + max_chars // 2, end)
                if boundary > start:
                    end = boundary + 1
        yield start, end
        start = end


def make_chunks(document, blocks, nodes, max_chars):
    by_id = {n["node_id"]: n for n in nodes}
    chunks = []
    for block in blocks:
        ancestors = []
        current = by_id[block["node_id"]]
        while current:
            ancestors.append(current)
            current = by_id.get(current["parent_id"])
        ancestors.reverse()
        labels = {kind: next((n["label"] for n in reversed(ancestors) if n["kind"] == kind), None)
                  for kind in ("appendix", "form", "chapter", "section", "article", "clause", "point")}
        for start, end in split_text(block["text"], max_chars):
            text = block["text"][start:end]
            split_issues = ["SPLIT_BLOCK_REQUIRES_CONTEXT"] if len(block["text"]) > max_chars else []
            chunks.append({"chunk_id": stable_id("chunk", PARSER_VERSION, max_chars, document.document_version_id, block["block_id"], start, end, content_hash(text)),
                           "document_id": document.document_id, "document_version_id": document.document_version_id,
                           "node_id": block["node_id"], "parent_id": block["node_id"],
                           "ancestor_ids": [n["node_id"] for n in ancestors], "scope": ancestors[-1]["scope"],
                           **labels, "block_id": block["block_id"], "block_kind": block["kind"],
                           "attachment_status": block["attachment_status"],
                           "text": text, "text_sha256": content_hash(text), "text_start": start, "text_end": end,
                           "source_locator": {"content_sha256": document.content_sha256,
                                              "block_source_start": block["source_start"], "block_source_end": block["source_end"],
                                              "offset_unit": "unicode_code_point", "end_exclusive": True},
                           "issues": sorted(set(block["issues"] + split_issues + [w for n in ancestors for w in n["issues"]])),
                           "usage_policy": document.usage_policy, "legal_approval": document.legal_approval,
                           "provenance_status": document.provenance_status, "temporal_status": document.temporal_status,
                           "rights_status": document.rights_status})
    return chunks


def annotate_reviewed_source(document, blocks):
    anomaly = REVIEWED_SOURCE_ANOMALIES.get(document.content_sha256)
    if anomaly:
        start, end, text = anomaly
        for block in blocks:
            if (block["source_start"], block["source_end"], block["text"]) == (start, end, text):
                block["issues"].append("SOURCE_TEXT_POSSIBLY_INCOMPLETE")


def validate_source_completeness(document, result):
    """Re-extract from immutable source, not from the submitted block inventory.

    Compare retained payload AND intentional exclusions under the extraction policy.
    HTML tag/attribute syntax is not prose; entities/separators/images have explicit
    segment kinds. This detects output omissions, not bugs shared by the extractor.
    Independent visible-text regression tests remain necessary.
    """
    expected, excluded, extraction_issues = extract_blocks(document)
    fields = ("kind", "text", "text_sha256", "source_start", "source_end", "segments")
    actual_projection = [{key: b.get(key) for key in fields} for b in result["blocks"]]
    expected_projection = [{key: b[key] for key in fields} for b in expected]
    if not expected or actual_projection != expected_projection:
        raise ContractError("source completeness: retained blocks differ from source extraction")
    if result.get("excluded_segments") != excluded:
        raise ContractError("source completeness: intentional exclusions differ from source extraction")
    if not set(extraction_issues) <= set(result.get("issues", [])):
        raise ContractError("source extraction issues missing")


def validate_result(document, result):
    """Validate identity, extraction completeness, ancestry and chunk coverage.

    Structural consistency is not proof of semantic/legal attachment correctness.
    """
    expected_identity = {"document_id": document.document_id, "document_version_id": document.document_version_id,
                         "content_sha256": document.content_sha256, "parser_version": PARSER_VERSION,
                         "schema_version": PARSED_SCHEMA_VERSION}
    if any(result.get(k) != v for k, v in expected_identity.items()):
        raise ContractError("result identity/version mismatch")
    if type(result.get("max_chars")) is not int or result["max_chars"] < 64:
        raise ContractError("invalid result max_chars")
    validate_source_completeness(document, result)
    blocks_by_id = {b["block_id"]: b for b in result["blocks"]}
    nodes = {n["node_id"]: n for n in result["nodes"]}
    if len(nodes) != len(result["nodes"]):
        raise ContractError("duplicate node identity")
    seen = set()
    roots = []
    for node in result["nodes"]:
        if node["parent_id"] is not None and node["parent_id"] not in seen:
            raise ContractError("invalid parent order or cycle")
        if node["parent_id"] is None:
            roots.append(node)
        heading = node["heading_block_id"]
        if heading is not None and (heading not in blocks_by_id or blocks_by_id[heading]["node_id"] != node["node_id"]):
            raise ContractError("node heading reference mismatch")
        expected_id = stable_id("node", PARSER_VERSION, document.document_version_id, node["kind"],
                                node["parent_id"], heading, node["label"])
        if node["node_id"] != expected_id:
            raise ContractError("node fingerprint mismatch")
        seen.add(node["node_id"])
    if (len(roots) != 1 or roots[0]["kind"] != "document" or roots[0]["scope"] != "document"
            or roots[0]["label"] != document.document_number):
        raise ContractError("invalid document root")
    chunks_by_block = {}
    for chunk in result["chunks"]:
        if (chunk["document_id"] != document.document_id or chunk["document_version_id"] != document.document_version_id
                or chunk["node_id"] not in nodes or chunk["parent_id"] != chunk["node_id"]):
            raise ContractError("chunk identity/parent mismatch")
        for field in ("usage_policy", "legal_approval", "provenance_status", "temporal_status", "rights_status"):
            if chunk[field] != getattr(document, field):
                raise ContractError("chunk approval/provenance changed")
        ancestry, cursor = [], nodes[chunk["node_id"]]
        if chunk["scope"] != cursor["scope"]:
            raise ContractError("chunk scope mismatch")
        while cursor:
            ancestry.append(cursor["node_id"])
            cursor = nodes.get(cursor["parent_id"])
        if chunk["ancestor_ids"] != list(reversed(ancestry)):
            raise ContractError("chunk ancestry mismatch")
        for kind in ADDRESS_FIELDS:
            expected_label = next((nodes[n]["label"] for n in ancestry if nodes[n]["kind"] == kind), None)
            if chunk.get(kind) != expected_label:
                raise ContractError("chunk address label differs from ancestry: " + kind)
        if chunk["text_sha256"] != content_hash(chunk["text"]):
            raise ContractError("chunk text hash mismatch")
        expected_id = stable_id("chunk", PARSER_VERSION, result["max_chars"], document.document_version_id,
                                chunk["block_id"], chunk["text_start"], chunk["text_end"], chunk["text_sha256"])
        if chunk["chunk_id"] != expected_id:
            raise ContractError("chunk fingerprint mismatch")
        chunks_by_block.setdefault(chunk["block_id"], []).append(chunk)
    blocks_seen = set()
    for block in result["blocks"]:
        if block["block_id"] in blocks_seen:
            raise ContractError("duplicate block identity")
        blocks_seen.add(block["block_id"])
        if block["node_id"] not in nodes or block.get("attachment_status") not in ATTACHMENT_STATUSES:
            raise ContractError("invalid block attachment")
        expected_id = stable_id("block", PARSER_VERSION, document.document_version_id, block["kind"],
                                block["source_start"], block["source_end"], block["text_sha256"])
        if block["block_id"] != expected_id:
            raise ContractError("block fingerprint mismatch")
        if not 0 <= block["source_start"] < block["source_end"] <= len(document.content):
            raise ContractError("invalid raw locator")
        for part in block["segments"]:
            if not block["source_start"] <= part["start"] <= part["end"] <= block["source_end"]:
                raise ContractError("segment outside source block")
        replay = normalize("".join(segment_text(document.content, p) for p in block["segments"]))
        if replay != block["text"] or content_hash(replay) != block["text_sha256"]:
            raise ContractError("source replay mismatch")
        position = 0
        for chunk in chunks_by_block.pop(block["block_id"], []):
            locator = chunk["source_locator"]
            if (locator["content_sha256"] != document.content_sha256
                    or locator["block_source_start"] != block["source_start"]
                    or locator["block_source_end"] != block["source_end"]
                    or locator.get("offset_unit") != "unicode_code_point"
                    or locator.get("end_exclusive") is not True):
                raise ContractError("chunk raw locator mismatch")
            if chunk.get("attachment_status") != block["attachment_status"] or chunk["block_kind"] != block["kind"]:
                raise ContractError("chunk block metadata mismatch")
            expected_issues = set(block["issues"])
            expected_issues.update(w for n in chunk["ancestor_ids"] for w in nodes[n]["issues"])
            if len(block["text"]) > result["max_chars"]:
                expected_issues.add("SPLIT_BLOCK_REQUIRES_CONTEXT")
            if chunk["issues"] != sorted(expected_issues):
                raise ContractError("chunk issues differ from block/ancestry")
            if (chunk["node_id"] != block["node_id"] or chunk["text_start"] != position
                    or not position < chunk["text_end"] <= len(replay)
                    or chunk["text"] != replay[position:chunk["text_end"]]
                    or len(chunk["text"]) > result["max_chars"]):
                raise ContractError("chunk coverage mismatch")
            position = chunk["text_end"]
        if position != len(replay):
            raise ContractError("incomplete block coverage")
    if chunks_by_block or len({c["chunk_id"] for c in result["chunks"]}) != len(result["chunks"]):
        raise ContractError("unknown block or duplicate chunk identity")


def parse_document(document: DocumentVersion, max_chars=DEFAULT_MAX_CHARS):
    if type(max_chars) is not int or max_chars < 64:
        raise ContractError("max_chars must be an integer >= 64")
    blocks, excluded, issues = extract_blocks(document)
    if not blocks:
        raise ContractError("no content blocks")
    for block in blocks:
        block["block_id"] = stable_id("block", PARSER_VERSION, document.document_version_id,
                                      block["kind"], block["source_start"], block["source_end"], block["text_sha256"])
    annotate_reviewed_source(document, blocks)
    nodes, structure_issues = build_structure(document, blocks)
    result = {"schema_version": PARSED_SCHEMA_VERSION, "parser_version": PARSER_VERSION, "max_chars": max_chars,
              "document_id": document.document_id, "document_version_id": document.document_version_id,
              "content_sha256": document.content_sha256, "blocks": blocks, "nodes": nodes,
              "excluded_segments": excluded, "issues": sorted(set(issues + structure_issues)),
              "chunks": make_chunks(document, blocks, nodes, max_chars)}
    validate_result(document, result)
    return result


def export_pilot(manifest_path: Path, output_dir: Path, max_chars=DEFAULT_MAX_CHARS):
    manifest_path, output_dir = manifest_path.resolve(), output_dir.resolve()
    if output_dir.exists() or output_dir.is_relative_to(manifest_path.parent):
        raise ContractError("output must be new and outside staging input directory")
    original_manifest_hash = file_hash(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("artifact_kind") != "staging_documents" or manifest.get("usage_policy") != "EXPERIMENTAL_ONLY":
        raise ContractError("expected experimental staging manifest")
    originals = {manifest_path: original_manifest_hash}
    artifacts = manifest["artifacts"]
    if len({a["path"] for a in artifacts}) != len(artifacts):
        raise ContractError("duplicate input artifact path")
    for artifact in artifacts:
        path = (manifest_path.parent / artifact["path"]).resolve()
        if path.parent != manifest_path.parent or file_hash(path) != artifact["sha256"]:
            raise ContractError("invalid staging path/hash")
        originals[path] = artifact["sha256"]
    fingerprint = content_hash(canonical_json({a["path"]: a["sha256"] for a in artifacts}).decode("utf-8"))
    if fingerprint != manifest["staging_snapshot_id"] or "documents.jsonl" not in {a["path"] for a in artifacts}:
        raise ContractError("staging fingerprint mismatch")
    documents = load_documents(manifest_path.parent / "documents.jsonl")
    if len(documents) != manifest["record_count"] or len(documents) != 3:
        raise ContractError("pilot requires exactly three declared records")
    declared = {(d["document_id"], d["document_version_id"], d["content_sha256"]) for d in manifest["documents"]}
    actual = {(d.document_id, d.document_version_id, d.content_sha256) for d in documents}
    if declared != actual or any(d.source_snapshot_id != manifest["source_snapshot_id"] for d in documents):
        raise ContractError("staging document declarations mismatch")
    results = [parse_document(d, max_chars) for d in documents]
    for path, digest in originals.items():
        if file_hash(path) != digest:
            raise ContractError("input changed during parsing")
    summaries = []
    for d, result in zip(documents, results):
        warnings = Counter(result["issues"])
        warnings.update(w for b in result["blocks"] for w in b["issues"])
        warnings.update(w for n in result["nodes"] for w in n["issues"])
        summaries.append({"source_record_id": d.source_record_id, "document_version_id": d.document_version_id,
                          "blocks": len(result["blocks"]), "chunks": len(result["chunks"]),
                          "node_counts": dict(Counter(n["kind"] for n in result["nodes"])),
                          "block_counts": dict(Counter(b["kind"] for b in result["blocks"])),
                          "attachment_counts": dict(Counter(b["attachment_status"] for b in result["blocks"])),
                          "warnings": dict(sorted(warnings.items())), "trace_and_chunk_coverage": "PASS",
                          "source_completeness": "MATCHES_SOURCE_EXTRACTION_POLICY"})
    report = {"status": "PASS_WITH_REVIEW_ITEMS", "documents": summaries,
              "limitations": ["Only three experimental documents; not a legal or retrieval benchmark.",
                              "Whitespace normalized; inline words/punctuation preserved. Offsets count Unicode code points, not bytes.",
                              "Tables linearized for text; use raw HTML span for cells/rowspan/colspan. Images are markers, no OCR.",
                              "Quoted amendments kept opaque; unnumbered context is heuristic; source labels are not corrected.",
                              "Attachment labels may be candidates: REVIEW_REQUIRED is not an exact citation address; subpoints are unresolved.",
                              "Completeness re-extracts retained payload and exclusions, not HTML markup bytes; shared extractor bugs still need independent tests.",
                              "CSS/visual layout is not interpreted. Normalized chunk offsets do not address raw HTML directly."]}
    payloads = {"structures.jsonl": b"".join(canonical_json({k: v for k, v in r.items() if k != "chunks"}) + b"\n" for r in results),
                "chunks.jsonl": b"".join(canonical_json(c) + b"\n" for r in results for c in r["chunks"]),
                "validation.json": canonical_json(report) + b"\n"}
    output_artifacts = [{"path": name, "sha256": content_hash(data.decode("utf-8")), "bytes": len(data)} for name, data in payloads.items()]
    output = {"schema_version": PARSED_SCHEMA_VERSION, "artifact_kind": "experimental_parsed_pilot", "parser_version": PARSER_VERSION,
              "max_chars": max_chars, "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_snapshot_id": manifest["source_snapshot_id"], "staging_snapshot_id": fingerprint,
              "processed_snapshot_id": content_hash(canonical_json({a["path"]: a["sha256"] for a in output_artifacts}).decode("utf-8")),
              "corpus_snapshot_id": None, "index_version": None, "usage_policy": "EXPERIMENTAL_ONLY",
              "approved_for_legal_verification": False, "record_count": len(documents),
              "code_hashes": {"ai_core/legal_parser.py": file_hash(Path(__file__)),
                              "ai_core/data_contracts.py": file_hash(Path(__file__).with_name("data_contracts.py"))},
              "python_version": sys.version, "artifacts": output_artifacts,
              "inputs": [{"path": str(p), "sha256": h} for p, h in originals.items()]}
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, data in {**payloads, "manifest.json": canonical_json(output) + b"\n"}.items():
        with (output_dir / name).open("xb") as stream:
            stream.write(data)
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    args = parser.parse_args(argv)
    try:
        output, report = export_pilot(args.manifest, args.output_dir, args.max_chars)
        print(json.dumps({"status": report["status"], "records": output["record_count"], "processed_snapshot_id": output["processed_snapshot_id"]}))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Parser error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
