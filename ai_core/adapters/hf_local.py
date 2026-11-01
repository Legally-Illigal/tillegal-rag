"""Read the T1 local pilot into the neutral staging contract. No network calls.

Usage: python -B -m ai_core.adapters.hf_local --manifest ... --selection ... --output-dir ...
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

from bs4 import BeautifulSoup
import pyarrow as pa
import pyarrow.parquet as pq

from ai_core.data_contracts import (
    ContractError, DocumentVersion, build_document, canonical_json, content_hash,
    document_json_schema, file_hash,
)

ADAPTER_VERSION = "hf-local-v1"
SOURCE_DATASET = "th1nhng0/vietnamese-legal-documents"
SOURCE_ID = "hf/" + SOURCE_DATASET
PILOT = {"139264": ("45/2019/QH14", "Bộ luật"),
         "146696": ("10/2020/TT-BLĐTBXH", "Thông tư"),
         "152668": ("145/2020/NĐ-CP", "Nghị định")}
METADATA_COLUMNS = ["id", "so_ky_hieu", "title", "loai_van_ban", "co_quan_ban_hanh",
                    "ngay_ban_hanh", "ngay_co_hieu_luc", "ngay_het_hieu_luc", "tinh_trang_hieu_luc"]


def verify_inputs(manifest_path: Path, selection_path: Path):
    """Check existing T1 evidence; do not rerun its audit or change its outputs."""
    manifest_bytes, selection_bytes = manifest_path.read_bytes(), selection_path.read_bytes()
    manifest, selection = json.loads(manifest_bytes), json.loads(selection_bytes)
    if manifest["schema_version"] != "1" or selection["schema_version"] != "1":
        raise ContractError("Unsupported T1 schema version")
    if manifest["source_dataset"] != SOURCE_DATASET:
        raise ContractError("Wrong source dataset for HF adapter")
    if set(manifest["raw_files"]) != {"metadata", "content", "relationships"}:
        raise ContractError("T1 manifest must describe three raw tables")
    expected_snapshot = hashlib.sha256((json.dumps(
        {name: info["sha256"] for name, info in manifest["raw_files"].items()},
        ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")).hexdigest()
    if manifest["snapshot_id"] != expected_snapshot or selection["snapshot_id"] != expected_snapshot:
        raise ContractError("T1 snapshot identity mismatch")
    originals = {manifest_path.resolve(): hashlib.sha256(manifest_bytes).hexdigest(),
                 selection_path.resolve(): hashlib.sha256(selection_bytes).hexdigest()}
    selection_registered = False
    for info in list(manifest["raw_files"].values()) + manifest["artifacts"]:
        path = Path(info["path"]).resolve()
        if path in originals and originals[path] != info["sha256"]:
            raise ContractError(f"Conflicting input hash: {path.name}")
        originals[path] = info["sha256"]
        if path == selection_path.resolve():
            selection_registered = True
    if not selection_registered:
        raise ContractError("Selection not registered in T1 manifest")
    assert_unchanged(originals)
    records = selection["records"]
    if selection["status"] != "READY_FOR_AUDIT" or Counter(row["id"] for row in records) != Counter(PILOT.keys()):
        raise ContractError("Expected exactly the three approved T1 pilot IDs")
    for row in records:
        if (row["selection_status"] != "SELECTED_FOR_AUDIT" or row["errors"]
                or row["document_number"] != PILOT[row["id"]][0]
                or row["expected_document_type"] != PILOT[row["id"]][1]
                or row["approved_for_retrieval"] is not False
                or row["decision"] != "QUARANTINE"):
            raise ContractError(f"Invalid T1 selection: {row['id']}")
    return manifest, selection, originals


def assert_unchanged(originals: dict[Path, str]):
    for path, expected in originals.items():
        if file_hash(path) != expected:
            raise ContractError(f"Input hash mismatch: {path.name}")


def check_schema(parquet: pq.ParquetFile, columns: list[str]):
    for name in columns:
        if name not in parquet.schema_arrow.names or parquet.schema_arrow.field(name).type != pa.string():
            raise ContractError(f"HF schema: required string column {name}")


def read_selected_metadata(path: Path) -> dict:
    result = {}
    with pq.ParquetFile(path) as parquet:
        check_schema(parquet, METADATA_COLUMNS)
        for batch in parquet.iter_batches(batch_size=4096, columns=METADATA_COLUMNS):
            for row in batch.to_pylist():
                doc_id = row["id"]
                if doc_id in PILOT:
                    if doc_id in result:
                        raise ContractError(f"Duplicate metadata ID: {doc_id}")
                    if (row["so_ky_hieu"], row["loai_van_ban"]) != PILOT[doc_id]:
                        raise ContractError(f"Metadata identity mismatch: {doc_id}")
                    result[doc_id] = row
    if set(result) != set(PILOT):
        raise ContractError("Missing selected metadata ID")
    return result


def read_selected_content(path: Path) -> dict:
    """Locate by ID, not T1 row positions. Decode HTML only in relevant groups."""
    locations, result = {}, {}
    with pq.ParquetFile(path) as parquet:
        check_schema(parquet, ["id", "content_html"])
        for group in range(parquet.num_row_groups):
            offset = 0
            for batch in parquet.iter_batches(batch_size=4096, row_groups=[group], columns=["id"]):
                for doc_id in batch.column(0).to_pylist():
                    if doc_id in PILOT:
                        if doc_id in locations:
                            raise ContractError(f"Duplicate content ID: {doc_id}")
                        locations[doc_id] = (group, offset)
                    offset += 1
        if set(locations) != set(PILOT):
            raise ContractError("Missing selected content ID")
        for group in sorted({loc[0] for loc in locations.values()}):
            wanted = {loc[1]: doc_id for doc_id, loc in locations.items() if loc[0] == group}
            for offset, batch in enumerate(parquet.iter_batches(batch_size=1, row_groups=[group], columns=["id", "content_html"])):
                if offset in wanted:
                    row = batch.to_pylist()[0]
                    if row["id"] != wanted[offset]:
                        raise ContractError("Content identity changed between reads")
                    result[row["id"]] = row["content_html"]
                if offset >= max(wanted):
                    break
    return result


def nullable_text(value, field: str, issues: list[str]):
    if value is None:
        issues.append(f"NOT_PROVIDED:{field}")
        return None
    if not isinstance(value, str):
        raise ContractError(f"{field}: expected string or null")
    if not value.strip():
        issues.append(f"BLANK_AS_UNKNOWN:{field}")
        return None
    return value


def map_date(value, field: str, issues: list[str]):
    value = nullable_text(value, field, issues)
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
    except ValueError:
        issues.append(f"INVALID_DATE:{field}:{value}")
        return None


def map_document(metadata: dict, html: str, manifest: dict) -> DocumentVersion:
    if not isinstance(html, str) or not html.strip():
        raise ContractError("Empty or non-string source HTML")
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body if soup.body is not None else soup
    for tag in body.find_all(["script", "style", "head"]):
        tag.decompose()
    if not body.get_text(" ", strip=True):
        raise ContractError("Empty body text: manual review required, including image-only documents")
    issues = ["NOT_PROVIDED:source_url", "NOT_PROVIDED:collected_at", "NOT_PROVIDED:status_observed_at",
              "NOT_PROVIDED:language", "SOURCE_PROVENANCE_UNVERIFIED", "LEGAL_APPLICABILITY_UNKNOWN",
              "RIGHTS_UNVERIFIED", "UPSTREAM_PROCESSING_HISTORY_UNKNOWN"]
    source_version = manifest.get("dataset_revision") if manifest.get("revision_status") == "VERIFIED" else None
    if source_version is None:
        issues.append("NOT_VERIFIED:source_version")
    if body.find("table"):
        issues.append("TABLES_PRESERVED_NOT_PARSED")
    if body.find("img"):
        issues.append("IMAGES_PRESERVED_NOT_INTERPRETED")
    info = manifest["raw_files"]["content"]
    return build_document(
        source_id=SOURCE_ID, source_record_id=metadata["id"],
        source_snapshot_id=manifest["snapshot_id"], source_version=source_version,
        document_number=nullable_text(metadata["so_ky_hieu"], "document_number", issues),
        title=nullable_text(metadata["title"], "title", issues),
        document_type=nullable_text(metadata["loai_van_ban"], "document_type", issues),
        issuing_authority=nullable_text(metadata["co_quan_ban_hanh"], "issuing_authority", issues),
        language=None, content_format="html", content=html,
        raw_ref={"uri": Path(info["path"]).resolve().as_uri(), "file_sha256": info["sha256"],
                 "record_key": "id", "record_value": metadata["id"]},
        source_url=None, collected_at=None, status_observed_at=None,
        issued_on=map_date(metadata["ngay_ban_hanh"], "issued_on", issues),
        effective_from=map_date(metadata["ngay_co_hieu_luc"], "effective_from", issues),
        effective_to=map_date(metadata["ngay_het_hieu_luc"], "effective_to", issues),
        effect_status_reported=nullable_text(metadata["tinh_trang_hieu_luc"], "effect_status_reported", issues),
        processing_steps=["HF_FIELD_MAPPING_V1", "DATE_FORMAT_DDMMYYYY_TO_ISO", "CONTENT_UNCHANGED"],
        issues=issues,
    )


def convert_pilot(manifest: dict, selection: dict) -> list[DocumentVersion]:
    metadata = read_selected_metadata(Path(manifest["raw_files"]["metadata"]["path"]))
    content = read_selected_content(Path(manifest["raw_files"]["content"]["path"]))
    expected = {row["id"]: row for row in selection["records"]}
    documents = []
    for doc_id in sorted(PILOT):
        html = content[doc_id]
        if not isinstance(html, str) or content_hash(html) != expected[doc_id]["content_sample"]["html_sha256"]:
            raise ContractError(f"HTML differs from T1 sample: {doc_id}")
        documents.append(map_document(metadata[doc_id], html, manifest))
    return documents


def export_pilot(manifest_path: Path, selection_path: Path, output_dir: Path) -> dict:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite staging directory: {output_dir}")
    manifest, selection, originals = verify_inputs(manifest_path, selection_path)
    protected_dirs = {path.parent for path in originals}
    if any(output_dir.is_relative_to(directory) for directory in protected_dirs):
        raise ContractError("Output must be outside all T1 input directories")
    documents = convert_pilot(manifest, selection)
    assert_unchanged(originals)
    records = b"".join(canonical_json(document.to_dict()) + b"\n" for document in documents)
    schema = canonical_json(document_json_schema()) + b"\n"
    artifacts = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                 for name, data in (("documents.jsonl", records), ("document.schema.json", schema))]
    contract_path = Path(sys.modules[DocumentVersion.__module__].__file__).resolve()
    output = {
        "schema_version": "1", "artifact_kind": "staging_documents", "status": "READY_FOR_EXPERIMENTAL_PARSING",
        "usage_policy": "EXPERIMENTAL_ONLY", "approved_for_legal_verification": False,
        "corpus_snapshot_id": None, "index_version": None,
        "source_snapshot_id": manifest["snapshot_id"],
        "staging_snapshot_id": hashlib.sha256(canonical_json({a["path"]: a["sha256"] for a in artifacts})).hexdigest(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "adapter_version": ADAPTER_VERSION, "contract_version": "1",
        "code_hashes": {"ai_core/adapters/hf_local.py": file_hash(Path(__file__)), "ai_core/data_contracts.py": file_hash(contract_path)},
        "environment": {"python": platform.python_version(), "pyarrow": pa.__version__},
        "record_count": len(documents),
        "documents": [{"document_id": d.document_id, "document_version_id": d.document_version_id,
                       "content_sha256": d.content_sha256, "content_utf8_bytes": len(d.content.encode("utf-8")), "issues": list(d.issues)} for d in documents],
        "input_files": [{"path": str(path), "sha256": digest} for path, digest in sorted(originals.items(), key=lambda item: str(item[0]))],
        "artifacts": artifacts,
        "limitations": ["No structural parsing, chunking, retrieval, gold labels or AI benchmark.",
                        "Raw HTML preserved; tables/images not interpreted.",
                        "Source URL/collection time/language are not supplied by this HF schema.",
                        "Unknown legal provenance, applicability and redistribution rights remain unknown.",
                        "Batch size bounds rows, not Parquet decoding memory; output is limited to three pilot documents."],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    # Manifest last. Incomplete exports are never resumed or treated as complete.
    for name, payload in (("documents.jsonl", records), ("document.schema.json", schema), ("manifest.json", canonical_json(output) + b"\n")):
        with (output_dir / name).open("xb") as stream:
            stream.write(payload)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        output = export_pilot(args.manifest, args.selection, args.output_dir)
        print(json.dumps({"status": output["status"], "records": output["record_count"], "staging_snapshot_id": output["staging_snapshot_id"]}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, pa.ArrowException) as error:
        print(f"Adapter error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
