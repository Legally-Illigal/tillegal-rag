"""Offline Parquet inspection. No acquisition, normalization or legal approval.

Run with Python -B. Raw inputs are opened read-only; output files are exclusive.
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

import bs4
from bs4 import BeautifulSoup
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = "1"
METADATA_FIELDS = (
    "id title so_ky_hieu ngay_ban_hanh loai_van_ban ngay_co_hieu_luc "
    "ngay_het_hieu_luc nguon_thu_thap ngay_dang_cong_bao nganh linh_vuc "
    "co_quan_ban_hanh chuc_danh nguoi_ky pham_vi thong_tin_ap_dung tinh_trang_hieu_luc"
).split()
SCHEMAS = {
    "metadata": METADATA_FIELDS,
    "content": ["id", "content_html"],
    "relationships": ["doc_id", "other_doc_id", "relationship"],
}
PILOT = (
    ("139264", "45/2019/QH14", "Bộ luật"),
    ("146696", "10/2020/TT-BLĐTBXH", "Thông tư"),
    ("152668", "145/2020/NĐ-CP", "Nghị định"),
)
LIGHT_BATCH = 4096
HTML_BATCH = 1
SAMPLE_LIMIT = 5


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def missing(value: str | None) -> bool:
    return value is None or not value.strip()


def counter_summary(values: Counter) -> dict:
    return {
        "rows": sum(values.values()),
        "distinct_nonblank_ids": sum(not missing(v) for v in values),
        "missing_id_rows": sum(n for v, n in values.items() if missing(v)),
        "duplicate_id_values": sum(n > 1 for v, n in values.items() if not missing(v)),
        "duplicate_excess_rows": sum(n - 1 for v, n in values.items() if not missing(v)),
    }


def rows(parquet: pq.ParquetFile, columns=None):
    for batch in parquet.iter_batches(batch_size=LIGHT_BATCH, columns=columns):
        yield from batch.to_pylist()


def inspect_metadata(parquet: pq.ParquetFile):
    ids, numbers, statuses = Counter(), Counter(), Counter()
    nulls = {key: {"null": 0, "blank": 0} for key in METADATA_FIELDS}
    dates = {key: {"valid": 0, "invalid": 0} for key in (
        "ngay_ban_hanh", "ngay_co_hieu_luc", "ngay_het_hieu_luc")}
    targets = {number for _, number, _ in PILOT}
    expected_ids = {doc_id for doc_id, _, _ in PILOT}
    candidates, selected_rows, positional_samples = [], {}, []
    for index, row in enumerate(rows(parquet)):
        ids[row["id"]] += 1
        for key, value in row.items():
            if value is None:
                nulls[key]["null"] += 1
            elif not value.strip():
                nulls[key]["blank"] += 1
        for key, counts in dates.items():
            if not missing(row[key]):
                try:
                    datetime.strptime(row[key].strip(), "%d/%m/%Y")
                    counts["valid"] += 1
                except ValueError:
                    counts["invalid"] += 1
        if not missing(row["so_ky_hieu"]):
            numbers[row["so_ky_hieu"]] += 1
        statuses[row["tinh_trang_hieu_luc"]] += 1
        if row["so_ky_hieu"] in targets or row["id"] in expected_ids:
            candidates.append({"row_index_zero_based": index, **row})
        if row["id"] in expected_ids:
            selected_rows.setdefault(row["id"], []).append(row)
        if index in (0, parquet.metadata.num_rows // 2):
            positional_samples.append({"row_index_zero_based": index, **row})
    samples = [r for r in candidates if r["id"] in expected_ids][:3]
    samples += [r for r in positional_samples if r["id"] not in {s["id"] for s in samples}]
    # Candidate identities are diagnostics, not additional full-record samples.
    identities = [{k: r[k] for k in ("id", "so_ky_hieu", "loai_van_ban", "title")}
                  for r in candidates]
    summary = {
        **counter_summary(ids), "scope": "all_rows_all_metadata_columns",
        "missing_values": nulls, "date_format": "DD/MM/YYYY",
        "date_validation": dates,
        "effect_status_counts": [{"value": k, "count": n} for k, n in sorted(statuses.items(), key=lambda x: str(x[0]))],
        "repeated_document_number_values": sum(n > 1 for n in numbers.values()),
        "samples": samples[:SAMPLE_LIMIT],
        "sample_method": "pilot IDs, then first and middle rows; max 5; purposive, not random",
        "pilot_number_candidates": identities,
    }
    return summary, ids, selected_rows


def inspect_content_ids(parquet: pq.ParquetFile):
    ids, locations = Counter(), {}
    # The known empty-body example is a regression probe, not a prevalence sample.
    targets = {doc_id for doc_id, _, _ in PILOT} | {"135139"}
    absolute = 0
    for group in range(parquet.num_row_groups):
        local = 0
        for batch in parquet.iter_batches(batch_size=LIGHT_BATCH, row_groups=[group], columns=["id"]):
            for doc_id in batch.column(0).to_pylist():
                ids[doc_id] += 1
                if absolute == 0:
                    targets.add(doc_id)
                if doc_id in targets:
                    locations.setdefault(doc_id, {"row_group": group, "row_in_group": local, "row_index_zero_based": absolute})
                absolute += 1
                local += 1
    return ids, locations


def sample_html(parquet: pq.ParquetFile, locations: dict) -> list[dict]:
    """Inspect <=5 selected records, with one row per batch; never export HTML."""
    samples = []
    groups = sorted({location["row_group"] for location in locations.values()})
    for group in groups:
        wanted = {loc["row_in_group"]: doc_id for doc_id, loc in locations.items() if loc["row_group"] == group}
        offset = 0
        for batch in parquet.iter_batches(batch_size=HTML_BATCH, row_groups=[group], columns=SCHEMAS["content"]):
            for index, expected_id in wanted.items():
                if offset <= index < offset + batch.num_rows:
                    row = batch.slice(index - offset, 1).to_pylist()[0]
                    if row["id"] != expected_id:
                        raise ValueError("Content ID changed between ID scan and sample read")
                    html = row["content_html"]
                    soup = BeautifulSoup(html or "", "html.parser")
                    body = soup.body if soup.body is not None else soup
                    for tag in body.find_all(["script", "style", "head"]):
                        tag.decompose()
                    visible = body.get_text(" ", strip=True)
                    samples.append({
                        "id": row["id"], **locations[expected_id],
                        "html_null": html is None, "html_blank": missing(html),
                        "html_utf8_bytes": len((html or "").encode("utf-8")),
                        "html_sha256": hashlib.sha256((html or "").encode("utf-8")).hexdigest(),
                        "body_text_empty": not bool(visible),
                        "visible_text_characters": len(visible),
                        "visible_prefix": visible[:160],
                        "table_tag_count": len(body.find_all("table")),
                        "image_tag_count": len(body.find_all("img")),
                    })
            offset += batch.num_rows
            if offset > max(wanted):
                break
    return sorted(samples, key=lambda row: row["row_index_zero_based"])[:SAMPLE_LIMIT]


def inspect_relationships(parquet: pq.ParquetFile, metadata_ids: set):
    labels, missing_fields = Counter(), Counter()
    triples, missing_sources, missing_targets = set(), set(), set()
    duplicate = self_edges = source_rows = target_rows = 0
    samples = []
    for row in rows(parquet):
        source, target, label = (row[key] for key in SCHEMAS["relationships"])
        labels[label] += 1
        for key, value in row.items():
            if missing(value):
                missing_fields[key] += 1
        triple = (source, target, label)
        duplicate += triple in triples
        triples.add(triple)
        self_edges += not missing(source) and source == target
        if source not in metadata_ids:
            source_rows += 1
            missing_sources.add(source)
        if target not in metadata_ids:
            target_rows += 1
            missing_targets.add(target)
        if len(samples) < SAMPLE_LIMIT:
            samples.append(row)
    return {
        "scope": "all_rows_all_relationship_columns", "rows": parquet.metadata.num_rows,
        "label_counts": [{"value": k, "count": n} for k, n in sorted(labels.items(), key=lambda x: str(x[0]))],
        "missing_or_blank_fields": dict(missing_fields), "duplicate_triples": duplicate,
        "self_edges": self_edges,
        "source_missing_rows": source_rows, "source_missing_distinct": len(missing_sources),
        "target_missing_rows": target_rows, "target_missing_distinct": len(missing_targets),
        "samples": samples, "sample_method": "first 5 rows; not random",
        "legal_semantics": "UNVERIFIED; preserve raw labels; no automatic temporal inference",
    }


def select_pilot(metadata_rows: dict, metadata_ids: Counter, content_ids: Counter, samples: list) -> dict:
    by_id = {sample["id"]: sample for sample in samples}
    records = []
    for doc_id, number, kind in PILOT:
        errors = []
        matches = metadata_rows.get(doc_id, [])
        if metadata_ids[doc_id] != 1:
            errors.append("METADATA_ID_NOT_UNIQUE_OR_MISSING")
        elif matches[0]["so_ky_hieu"] != number or matches[0]["loai_van_ban"] != kind:
            errors.append("EXPECTED_NUMBER_OR_DOCUMENT_TYPE_MISMATCH")
        if content_ids[doc_id] != 1:
            errors.append("CONTENT_ID_NOT_UNIQUE_OR_MISSING")
        if doc_id not in by_id:
            errors.append("CONTENT_SAMPLE_UNAVAILABLE")
        elif by_id[doc_id]["body_text_empty"]:
            errors.append("EMPTY_BODY_TEXT")
        records.append({
            "id": doc_id, "document_number": number, "expected_document_type": kind,
            "selection_status": "REJECTED" if errors else "SELECTED_FOR_AUDIT",
            "selection_reason": "Explicit source ID and expected Vietnamese document type; never choose by row position or number alone",
            "errors": errors, "content_sample": by_id.get(doc_id),
            "decision": "QUARANTINE", "source_url": None, "reviewer": None,
            "provenance_status": "UNVERIFIED", "temporal_status": "UNKNOWN",
            "rights_status": "UNVERIFIED", "approved_for_retrieval": False,
        })
    return {"schema_version": SCHEMA_VERSION, "status": "READY_FOR_AUDIT" if all(not r["errors"] for r in records) else "BLOCKED", "records": records}


def inspect_inputs(input_dir: Path) -> tuple[dict, dict, dict]:
    input_dir = input_dir.resolve()
    files, readers = {}, {}
    for name, columns in SCHEMAS.items():
        path = input_dir / f"{name}.parquet"
        before = sha256_file(path)
        parquet = pq.ParquetFile(path)
        if parquet.schema_arrow.names != columns or any(field.type != pa.string() for field in parquet.schema_arrow):
            raise ValueError(f"Unexpected schema: {path.name}: {parquet.schema_arrow}")
        files[name] = {
            "path": str(path), "bytes": path.stat().st_size, "sha256": before,
            "rows": parquet.metadata.num_rows, "row_groups": parquet.num_row_groups,
            "schema": [{"name": field.name, "type": str(field.type)} for field in parquet.schema_arrow],
            "created_by": parquet.metadata.created_by,
        }
        readers[name] = parquet
    metadata, metadata_ids, pilot_rows = inspect_metadata(readers["metadata"])
    content_ids, locations = inspect_content_ids(readers["content"])
    samples = sample_html(readers["content"], locations)
    meta_keys = {key for key in metadata_ids if not missing(key)}
    content_keys = {key for key in content_ids if not missing(key)}
    relationships = inspect_relationships(readers["relationships"], meta_keys)
    selection = select_pilot(pilot_rows, metadata_ids, content_ids, samples)
    content = {**counter_summary(content_ids), "scope": "all_ID_rows; HTML_samples_only",
               "samples": samples, "sample_limit": SAMPLE_LIMIT,
               "sample_method": "three explicit pilot IDs, first content row, known empty-body probe 135139; unique IDs, at most 5",
               "html_quality_population": "NOT_SCANNED"}
    join = {
        "matched_distinct_ids": len(meta_keys & content_keys),
        "metadata_without_content": len(meta_keys - content_keys),
        "content_without_metadata": len(content_keys - meta_keys),
        "metadata_without_content_examples": sorted(meta_keys - content_keys)[:5],
        "content_without_metadata_examples": sorted(content_keys - meta_keys)[:5],
        "cardinality": "one_to_zero_or_one" if not metadata["duplicate_excess_rows"] and not content["duplicate_excess_rows"] else "NOT_ONE_TO_ONE",
    }
    issues = []
    def issue(code, count, severity="WARNING"):
        if count:
            issues.append({"code": code, "count": count, "severity": severity})
    for name, summary in (("METADATA", metadata), ("CONTENT", content)):
        issue(f"{name}_MISSING_ID", summary["missing_id_rows"], "ERROR")
        issue(f"{name}_DUPLICATE_ID", summary["duplicate_excess_rows"], "ERROR")
    issue("METADATA_WITHOUT_CONTENT", join["metadata_without_content"])
    issue("CONTENT_WITHOUT_METADATA", join["content_without_metadata"], "ERROR")
    issue("RELATIONSHIP_SOURCE_OUTSIDE_METADATA", relationships["source_missing_rows"], "ERROR")
    issue("RELATIONSHIP_TARGET_OUTSIDE_METADATA", relationships["target_missing_rows"])
    issue("RELATIONSHIP_MISSING_FIELDS", sum(relationships["missing_or_blank_fields"].values()), "ERROR")
    issue("RELATIONSHIP_DUPLICATE_TRIPLES", relationships["duplicate_triples"])
    issue("RELATIONSHIP_SELF_EDGES", relationships["self_edges"])
    issue("SAMPLED_EMPTY_BODY_TEXT", sum(s["body_text_empty"] for s in samples))
    issue("INVALID_METADATA_DATE", sum(d["invalid"] for d in metadata["date_validation"].values()))
    issue("PILOT_SELECTION_FAILED", sum(bool(r["errors"]) for r in selection["records"]), "ERROR")
    # Rehash after all reads to reject inputs changed during inspection.
    for info in files.values():
        if sha256_file(Path(info["path"])) != info["sha256"]:
            raise ValueError(f"Input changed during inspection: {info['path']}")
    snapshot_material = {name: info["sha256"] for name, info in files.items()}
    snapshot_id = hashlib.sha256(json_bytes(snapshot_material)).hexdigest()
    manifest = {
        "schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_dataset": "th1nhng0/vietnamese-legal-documents",
        "source_dataset_basis": "user_supplied_identity; not independently resolved by this offline tool",
        "dataset_revision": None, "revision_status": "UNVERIFIED",
        "revision_hint_from_history": "8977887f17be2defae4c5171d55562e1cde7d695",
        "upstream_checksum_comparison": "NOT_PERFORMED_BY_THIS_OFFLINE_RUN",
        "legal_content_status": "UNVERIFIED", "rights_status": "UNVERIFIED",
        "raw_files": files, "raw_hashes_stable_before_after": True,
        "environment": {"python": platform.python_version(), "pyarrow": pa.__version__, "beautifulsoup4": bs4.__version__, "platform": platform.platform()},
        "tool": {"path": str(Path(__file__).resolve()), "sha256": sha256_file(Path(__file__))},
        "limits": {"light_batch_rows": LIGHT_BATCH, "html_batch_rows": HTML_BATCH, "max_full_record_samples_per_table": SAMPLE_LIMIT},
    }
    validation = {
        "schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id,
        "status": "FAIL" if any(i["severity"] == "ERROR" for i in issues) else "PASS_WITH_WARNINGS" if issues else "PASS",
        "metadata": metadata, "content": content, "join": join, "relationships": relationships,
        "issues": issues,
        "limitations": [
            "No full-corpus HTML quality scan or structural parsing.",
            "Samples are purposive and cannot estimate population error rates.",
            "ID counters and relationship triples are held in memory; Parquet pages and a single HTML record can exceed the batch row limit in bytes.",
            "Date parsing checks syntax/calendar validity, not legal applicability.",
            "No official-source, legal-effect, revision or redistribution-rights approval.",
        ],
    }
    selection["snapshot_id"] = snapshot_id
    return manifest, validation, selection


def output_targets(input_dir: Path, output_dir: Path, selection_path: Path):
    targets = [output_dir.resolve() / "manifest.json", output_dir.resolve() / "validation.json", selection_path.resolve()]
    if len(set(targets)) != len(targets):
        raise ValueError("Output paths must be distinct")
    for target in targets:
        if target.is_relative_to(input_dir.resolve()):
            raise ValueError("Outputs must be outside the raw input directory")
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite: {target}")
    return targets


def write_artifacts(manifest: dict, validation: dict, selection: dict, targets: list[Path]):
    payloads = [json_bytes(validation), json_bytes(selection)]
    manifest["artifacts"] = [
        {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}
        for path, payload in zip(targets[1:], payloads)
    ]
    # Manifest last is the completion marker. No existing file is overwritten.
    for path, payload in zip(targets[1:] + targets[:1], payloads + [json_bytes(manifest)]):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)


def verify_snapshot(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = []
    for info in list(manifest["raw_files"].values()) + manifest["artifacts"]:
        path = Path(info["path"])
        if not path.is_file():
            failures.append({"path": str(path), "reason": "MISSING"})
        elif sha256_file(path) != info["sha256"]:
            failures.append({"path": str(path), "reason": "HASH_MISMATCH"})
    return {"status": "FAIL" if failures else "PASS", "failures": failures,
            "meaning": "Bytes match this manifest; not proof of trusted manifest, dataset revision or legal correctness"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--input-dir", type=Path, required=True)
    inspect.add_argument("--output-dir", type=Path, required=True)
    inspect.add_argument("--selection", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_snapshot(args.manifest)
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["status"] == "FAIL" else 0
        targets = output_targets(args.input_dir, args.output_dir, args.selection)
        manifest, validation, selection = inspect_inputs(args.input_dir)
        write_artifacts(manifest, validation, selection, targets)
        print(json.dumps({"status": validation["status"], "selection_status": selection["status"], "snapshot_id": manifest["snapshot_id"], "manifest": str(targets[0])}, ensure_ascii=False))
        return 1 if validation["status"] == "FAIL" else 0
    except (OSError, ValueError, KeyError, pa.ArrowException) as error:
        print(f"Inspection error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
