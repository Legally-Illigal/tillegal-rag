"""Explicit issue attribution and readable limits; no semantic merging or new facts."""
import re
import unicodedata


def parse_issue_limitation(message, issue_ids):
    """Accept an explicit, known singular ID at the start, never a mention in prose."""
    start = re.sub(r"^\s*(?:[-*]\s+)?", "", message)
    start = re.sub(r"^issue[_ ]id\s*[:=]\s*", "", start, flags=re.IGNORECASE)
    for iid in sorted(issue_ids, key=len, reverse=True):
        match = re.match(r"^" + re.escape(iid) + r"\s*[:：—–-]\s*(\S.*)$", start)
        if match:
            body = match.group(1).strip()
            # 'I1 - I2: ...' is a multi-issue prefix, not unambiguous attribution.
            if any(re.match(r"^" + re.escape(other) + r"\s*[:：—–-]", body)
                   for other in issue_ids if other != iid):
                return None, message.strip()
            return iid, body
    return None, message.strip()


def _key(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold().rstrip(".;。")


def _unique_display(messages):
    """Only typography-equivalent duplicates. Keep different numbers/negations/wording."""
    seen, result = set(), []
    for message in messages:
        key = _key(message)
        if key and key not in seen:
            result.append(message.strip())
            seen.add(key)
    return result


GAP_TEXT = {
    "NON_USABLE_MEMBERS": "Một số đoạn cùng nhóm chưa được chọn làm căn cứ; chỉ sử dụng những đoạn được cấp. Ảnh hưởng ngữ nghĩa chưa xác định.",
    "OMITTED_CHUNKS": "Một số đoạn nguồn chưa có trong ngữ cảnh; chưa thể dùng phần thiếu để kết luận áp dụng.",
    "SEED_NOT_INCLUDED": "Đoạn nguồn khởi đầu chưa có trong ngữ cảnh; chỉ trình bày phần nội dung đã được cấp có thể hiểu độc lập.",
}


def _readable_message(message, value):
    # Exact code translations only; never remove a substantive sentence on a keyword match.
    replacements = {**{k: v.rstrip(".") for k, v in GAP_TEXT.items()},
        "QUARANTINE": "chưa được phê duyệt làm nguồn pháp lý", "UNVERIFIED": "chưa xác minh",
        "UNKNOWN": "chưa xác định", "temporal_status": "tình trạng thời gian áp dụng"}
    for key, replacement in replacements.items():
        message = re.sub(r"\b" + re.escape(key) + r"\b", replacement, message)
    message = re.sub(r"\b(?:chunk|evidence|bundle):[0-9a-f]{64}\b", "đoạn nguồn liên quan", message)
    for gap in value.get("context_gaps", []):
        message = re.sub(r"\b" + re.escape(gap["gap_id"]) + r"\b", "nhóm nguồn liên quan", message)
    return message


def render_readable_response(value):
    if value["status"] == "ERROR":
        return None
    lines = [value["answer_draft"] or "Chưa thể tạo khẳng định có căn cứ từ evidence được cấp.",
             "", "Bản nháp chưa được kiểm chứng; chưa xác minh nguồn và hiệu lực áp dụng."]
    issue_ids = [r["issue_id"] for r in value.get("issue_reports", [])]
    for report in value.get("issue_reports", []):
        lines += ["", "Giới hạn - " + report["issue_id"] + ": " + report["issue_text"]]
        if report["summary_status"] == "NO_DRAFT_CLAIMS":
            lines.append("Chưa có nội dung đủ căn cứ để trình bày cho vấn đề này; không có nghĩa là không có quy định.")
        elif report["judge_coverage"] in ("partial", "none"):
            lines.append("Mới trả lời được một phần; chưa đủ căn cứ để kết luận đầy đủ cho vấn đề này.")
        else:
            lines.append("Đã trình bày các ý có căn cứ; chưa coi đây là câu trả lời bao phủ toàn bộ vấn đề.")
        messages = []
        for raw in report["limitations"]:
            _, body = parse_issue_limitation(raw, issue_ids)
            # These are code-generated summaries, already expressed by the human heading/notices.
            if (raw.startswith("Judge chỉ ghi nhận mức bao phủ ")
                    or raw.startswith("Có giới hạn chưa gắn được issue cụ thể; xem unscoped_limitations")
                    or raw.startswith("Chưa trình bày claim nào cho vấn đề này;")
                    or re.match(r"^G\d+: (?:OMITTED_CHUNKS|NON_USABLE_MEMBERS|SEED_NOT_INCLUDED);", raw)):
                continue
            messages.append(_readable_message(body, value))
        # One explanation per structural kind per issue, even when several groups have the same gap.
        kinds = {g["kind"] for g in value.get("context_gaps", []) if report["issue_id"] in g["issue_ids"]}
        messages.extend(text for kind, text in GAP_TEXT.items() if kind in kinds)
        lines.extend("- " + text for text in _unique_display(messages))
    unscoped = _unique_display([_readable_message(m, value) for m in value.get("unscoped_limitations", [])])
    unknown = [g for g in value.get("context_gaps", []) if not g["issue_ids"]]
    if unscoped or unknown:
        lines += ["", "Giới hạn bổ sung (chưa gắn issue cụ thể):"]
        lines.extend("- " + message for message in unscoped)
        if unknown:
            lines.append("- Một số nhóm nguồn chưa xác định vấn đề liên quan; không dùng phần chưa được cấp để suy rộng kết luận.")
    return "\n".join(lines)
