"""Source-neutral staging contract v1. No retrieval or legal approval here."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

SCHEMA_VERSION = "1"
DATE_FIELDS = ("issued_on", "effective_from", "effective_to")
TIME_FIELDS = ("collected_at", "status_observed_at")
NULLABLE_FIELDS = (
    "source_version", "document_number", "title", "document_type",
    "issuing_authority", "language", "source_url", "effect_status_reported",
    *DATE_FIELDS, *TIME_FIELDS,
)
FIXED_FIELDS = {
    "schema_version": SCHEMA_VERSION,
    "usage_policy": "EXPERIMENTAL_ONLY",
    "legal_approval": "QUARANTINE",
    "provenance_status": "UNVERIFIED",
    "temporal_status": "UNKNOWN",
    "rights_status": "UNVERIFIED",
}


class ContractError(ValueError):
    """Invalid input must be fixed at its source, not silently coerced."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field}: expected nonblank string")


def require_hash(value: object, field: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError(f"{field}: expected lowercase SHA-256")


def make_document_id(source_id: str, source_record_id: str) -> str:
    for name, value in (("source_id", source_id), ("source_record_id", source_record_id)):
        require_text(value, name)
        if "::" in value or value != value.strip():
            raise ContractError(f"{name}: whitespace at boundary or reserved delimiter ::")
    return f"{source_id}::{source_record_id}"


def make_version_id(record: dict) -> str:
    """Fingerprint the materialized record, NOT the legal amendment version.

    Exclude raw file location so relocating identical input does not change identity.
    Content is represented by its validated hash; metadata/provenance changes count.
    """
    identity = {key: value for key, value in record.items()
                if key not in ("document_version_id", "content")}
    identity["raw_ref"] = {key: value for key, value in record["raw_ref"].items() if key != "uri"}
    return "dv:" + hashlib.sha256(canonical_json(identity)).hexdigest()


@dataclass(frozen=True)
class RawReference:
    uri: str
    file_sha256: str
    record_key: str
    record_value: str

    def __post_init__(self):
        for field in fields(self):
            require_text(getattr(self, field.name), "raw_ref." + field.name)
        require_hash(self.file_sha256, "raw_ref.file_sha256")
        # A locator only: this module never fetches or executes a URI.
        if not urlsplit(self.uri).scheme:
            raise ContractError("raw_ref.uri: expected absolute URI")


@dataclass(frozen=True)
class DocumentVersion:
    schema_version: str
    document_id: str
    document_version_id: str
    source_id: str
    source_record_id: str
    source_snapshot_id: str
    source_version: str | None
    document_number: str | None
    title: str | None
    document_type: str | None
    issuing_authority: str | None
    language: str | None
    content_format: str
    content: str
    content_sha256: str
    raw_ref: RawReference
    source_url: str | None
    collected_at: str | None
    issued_on: str | None
    effective_from: str | None
    effective_to: str | None
    effect_status_reported: str | None
    status_observed_at: str | None
    usage_policy: str
    legal_approval: str
    provenance_status: str
    temporal_status: str
    rights_status: str
    processing_steps: tuple[str, ...]
    issues: tuple[str, ...]

    def __post_init__(self):
        for field in fields(self):
            name, value = field.name, getattr(self, field.name)
            if name in ("raw_ref", "processing_steps", "issues"):
                continue
            if name in NULLABLE_FIELDS and value is None:
                continue
            require_text(value, name)
        for name, expected in FIXED_FIELDS.items():
            if getattr(self, name) != expected:
                raise ContractError(f"{name}: staging v1 requires {expected}")
        if not isinstance(self.raw_ref, RawReference):
            raise ContractError("raw_ref: expected RawReference")
        if self.raw_ref.record_value != self.source_record_id:
            raise ContractError("raw_ref.record_value: different source record")
        for name in ("processing_steps", "issues"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise ContractError(f"{name}: expected tuple")
            for item in value:
                require_text(item, name)
        if self.document_id != make_document_id(self.source_id, self.source_record_id):
            raise ContractError("document_id: namespace/record mismatch")
        if self.content_format not in ("html", "text"):
            raise ContractError("content_format: v1 supports html or text only")
        require_hash(self.content_sha256, "content_sha256")
        if content_hash(self.content) != self.content_sha256:
            raise ContractError("content_sha256: content changed")
        for name in DATE_FIELDS:
            value = getattr(self, name)
            if value is not None:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ContractError(f"{name}: expected YYYY-MM-DD")
                try:
                    date.fromisoformat(value)
                except ValueError as error:
                    raise ContractError(f"{name}: invalid calendar date") from error
        for name in TIME_FIELDS:
            value = getattr(self, name)
            if value is not None:
                try:
                    parsed = datetime.fromisoformat(value)
                    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError("timezone required")
                except ValueError as error:
                    raise ContractError(f"{name}: expected ISO timestamp with timezone") from error
        if self.source_url is not None:
            url = urlsplit(self.source_url)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                raise ContractError("source_url: expected HTTP(S) source URL without credentials")
        if self.document_version_id != make_version_id(self.to_dict()):
            raise ContractError("document_version_id: record fingerprint mismatch")

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("processing_steps", "issues"):
            result[key] = list(result[key])
        return result

    @classmethod
    def from_dict(cls, data: dict) -> DocumentVersion:
        if not isinstance(data, dict):
            raise ContractError("document: expected object")
        expected = {field.name for field in fields(cls)}
        if set(data) != expected:
            raise ContractError(f"document fields: missing={sorted(expected-set(data))}, extra={sorted(set(data)-expected)}")
        values = dict(data)
        raw = values["raw_ref"]
        if not isinstance(raw, dict) or set(raw) != {field.name for field in fields(RawReference)}:
            raise ContractError("raw_ref: missing/extra fields")
        values["raw_ref"] = RawReference(**raw)
        for key in ("processing_steps", "issues"):
            if not isinstance(values[key], list):
                raise ContractError(f"{key}: expected JSON array")
            values[key] = tuple(values[key])
        return cls(**values)


def build_document(**values) -> DocumentVersion:
    """Source adapters call this after mapping; it never cleans the content."""
    data = {**FIXED_FIELDS, **values}
    require_text(data.get("content"), "content")
    data["document_id"] = make_document_id(data["source_id"], data["source_record_id"])
    data["content_sha256"] = content_hash(data["content"])
    data["document_version_id"] = make_version_id(data)
    return DocumentVersion.from_dict(data)


def load_documents(path: Path) -> list[DocumentVersion]:
    """Small-pilot reader, independent of PyArrow/HF. It validates every record."""
    documents, seen = [], set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                document = DocumentVersion.from_dict(json.loads(line))
                key = (document.document_id, document.document_version_id)
                if key in seen:
                    raise ContractError("duplicate document version")
            except (ValueError, TypeError) as error:
                raise ContractError(f"{path.name}:{line_number}: {error}") from error
            seen.add(key)
            documents.append(document)
    return documents


def document_json_schema() -> dict:
    """Structural interchange schema; Python adds hash/identity/calendar checks."""
    properties = {field.name: {"type": "string", "minLength": 1} for field in fields(DocumentVersion)}
    for name in NULLABLE_FIELDS:
        properties[name] = {"type": ["string", "null"], "minLength": 1}
    for name, value in FIXED_FIELDS.items():
        properties[name] = {"const": value}
    properties["content_format"] = {"enum": ["html", "text"]}
    properties["content_sha256"] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    properties["raw_ref"] = {
        "type": "object", "additionalProperties": False,
        "required": [field.name for field in fields(RawReference)],
        "properties": {field.name: {"type": "string", "minLength": 1} for field in fields(RawReference)},
    }
    for name in ("processing_steps", "issues"):
        properties[name] = {"type": "array", "items": {"type": "string", "minLength": 1}}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Tillegal DocumentVersion staging v1",
            "type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
