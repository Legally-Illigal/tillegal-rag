"""Interactive presentation of Basic RAG; offline mock unless --live is explicit."""
import argparse
import json
from pathlib import Path
import sys
import uuid

from ai_core.anthropic_provider import AnthropicProvider
from ai_core.basic_rag import BasicRAG
from ai_core.bm25_retrieval import load_index
from ai_core.evidence_builder import EvidenceBudget
from ai_core.rag_generation import GenerationError, MockProvider

ROOT = Path(__file__).resolve().parents[1]
MISSING = 'Không có thông tin'


def display(value):
    """Keep terminal control characters out of the presentation, not the result."""
    if value is None or value == '':
        return MISSING
    return ''.join(c for c in str(value) if c in '\n\t' or c.isprintable())


def render_result(result):
    lines = ['\nTRẢ LỜI']
    if result['status'] == 'FAILED':
        lines.append('Không thể tạo câu trả lời. Mã lỗi: ' + display((result.get('error') or {}).get('code')))
    else:
        lines.append(display(result.get('answer')))
    lines.append('\nNGUỒN THAM KHẢO — TRÍCH DẪN ỨNG VIÊN')
    citations = result.get('candidate_citations', [])
    if not citations:
        lines.append('Không có trích dẫn ứng viên; chưa đủ căn cứ để trình bày nguồn hỗ trợ.')
    for number, citation in enumerate(citations, 1):
        doc, chunk = citation.get('document') or {}, citation.get('chunk') or {}
        address = ', '.join(label + ' ' + display(chunk[key]) for key, label in
                            [('appendix', 'Phụ lục'), ('form', 'Mẫu'),
                             ('article', 'Điều'), ('clause', 'Khoản'), ('point', 'Điểm')]
                            if chunk.get(key) is not None) or MISSING
        lines.extend([f'[{number}] {display(doc.get("title"))} — {address}',
                      '    Địa chỉ do parser gán, chưa xác minh: ' + display(citation.get('attachment_status')),
                      '    Phạm vi: ' + display(chunk.get('scope')),
                      '    URL nguồn: ' + display(doc.get('source_url')),
                      '    Trích đoạn: ' + display(citation.get('quote'))])
    lines.extend(['\nTRẠNG THÁI: ' + result['status'],
                  'Các trích dẫn chưa được kiểm chứng đầy đủ. Nguồn và hiệu lực pháp lý chưa được xác minh.'])
    bundle = result.get('evidence_bundle') or {}
    retrieval = result.get('trace', {}).get('retrieval') or {}
    if retrieval.get('schema_version') == '2':
        trace = retrieval.get('trace', {})
        lines.append('Retrieval: ' + display(retrieval.get('retrieval_method')) + ' — ' + display(trace.get('execution_status', 'OK')))
        if 'elapsed_ms' in trace:
            lines.append(f"Retrieval runtime: {trace['elapsed_ms']:.1f} ms")
    if bundle.get('status') == 'PARTIAL':
        lines.append('Context bị giới hạn ngân sách; có thể thiếu điều kiện hoặc ngoại lệ.')
    return '\n'.join(lines)


def run_session(rag, *, live=False, input_fn=None, emit=print, output_dir=None, max_questions=3):
    input_fn = input_fn or input
    emit('TILLEGAL — LEGAL AI ASSISTANT')
    emit('Chế độ: ' + ('LIVE — Anthropic (có tính phí)' if live else 'MOCK — chỉ kiểm thử luồng, không phải LLM thật'))
    emit(f'Tối đa {max_questions} câu hỏi/phiên. Gõ exit để thoát. Mỗi câu hỏi độc lập, không có lịch sử hội thoại.')
    try:
        for _ in range(max_questions):
            while True:
                question = input_fn('\nNhập câu hỏi (gõ exit để thoát):\n> ').strip()
                if question.casefold() == 'exit':
                    return 0
                if question:
                    break
                emit('Vui lòng nhập câu hỏi.')
            emit('Đang truy xuất và tạo bản nháp...')
            result = rag.answer(question, request_id=str(uuid.uuid4()), top_k=3)
            emit(render_result(result))
            if output_dir is not None:
                try:
                    path = output_dir / (result['request_id'] + '.json')
                    with path.open('x', encoding='utf-8') as stream:
                        json.dump(result, stream, ensure_ascii=False, indent=2)
                except OSError:
                    emit('Không lưu được trace (OUTPUT_WRITE_ERROR). Dừng phiên để tránh mất kết quả tiếp theo.')
                    return 1
            code = (result.get('error') or {}).get('code', '')
            if code in {'ANTHROPIC_AUTHENTICATION_ERROR', 'ANTHROPIC_BILLING_ERROR',
                        'ANTHROPIC_PERMISSION_ERROR', 'ANTHROPIC_RATE_LIMIT'}:
                emit('Dừng phiên vì giới hạn truy cập hoặc tài khoản; không tự retry.')
                return 1
        emit('Đã đạt giới hạn câu hỏi của phiên. Không gọi thêm provider.')
        return 0
    except (KeyboardInterrupt, EOFError):
        emit('\nĐã dừng phiên. Không tự retry yêu cầu bị ngắt.')
        return 0


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Interactive Basic RAG demo; citations remain unverified')
    parser.add_argument('--live', action='store_true', help='Opt in to at most 3 paid Claude generation requests')
    parser.add_argument('--output-dir', type=Path, help='NEW directory for full request JSON/trace; never overwrite')
    parser.add_argument('--retrieval', choices=['bm25','dense','hybrid'], default='bm25')
    parser.add_argument('--rerank', action='store_true', help='Local cross-encoder; hybrid mode only')
    parser.add_argument('--allow-retrieval-fallback', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=False)
        provider = AnthropicProvider.from_environment(
            model='claude-sonnet-5', allow_live=True, timeout_seconds=60, max_tokens=2048,
            max_input_bytes=40000, thinking_type='disabled', structured_output=True,
            input_token_cap=12000) if args.live else MockProvider()
        index = load_index(ROOT / 'data/indexes/labor-pilot-3-t4-bm25/manifest.json')
        from ai_core.retrieval_runtime import make_retriever
        retriever = make_retriever(index,args.retrieval,args.rerank,allow_fallback=args.allow_retrieval_fallback)
        rag = BasicRAG(index, provider, EvidenceBudget(max_chunks=24, max_quote_chars=8000), retriever=retriever)
    except (KeyboardInterrupt, EOFError):
        print('Đã dừng khởi động.')
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, GenerationError) else 'CONFIGURATION_INDEX_OR_OUTPUT_ERROR'
        print('TRẠNG THÁI: FAILED\nKhông thể khởi động demo. Mã lỗi: ' + code)
        return 1
    return run_session(rag, live=args.live, output_dir=args.output_dir)


if __name__ == '__main__':
    raise SystemExit(main())
