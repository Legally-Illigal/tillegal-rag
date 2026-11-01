"""Run with python -B -m ai_core.rag_cli. Mock is the default."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from ai_core.basic_rag import BasicRAG
from ai_core.anthropic_provider import AnthropicProvider
from ai_core.bm25_retrieval import load_index
from ai_core.evidence_builder import EvidenceBudget
from ai_core.rag_generation import ChatCompletionsProvider, GenerationError, MockProvider

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Basic RAG draft, candidate citations; no verification')
    parser.add_argument('--question', required=True)
    parser.add_argument('--request-id', default=None)
    parser.add_argument('--index', type=Path, default=ROOT / 'data/indexes/labor-pilot-3-t4-bm25/manifest.json')
    parser.add_argument('--mode', choices=['mock', 'live'], default='mock')
    parser.add_argument('--provider', choices=['chat-completions', 'anthropic'], default='chat-completions')
    parser.add_argument('--model', help='Runtime model ID; does not modify OS environment')
    parser.add_argument('--anthropic-thinking', choices=['disabled', 'between_tools'])
    parser.add_argument('--max-input-bytes', type=int, default=121000)
    parser.add_argument('--structured-output', action='store_true', help='Anthropic provider JSON Schema output')
    parser.add_argument('--input-token-cap', type=int, help='Anthropic free count_tokens preflight; fail before generation above cap')
    parser.add_argument('--allow-live', action='store_true', help='Explicit opt-in to one potentially paid provider call')
    parser.add_argument('--format', choices=['json', 'text'], default='json')
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--max-evidence-chunks', type=int, default=32)
    parser.add_argument('--max-quote-chars', type=int, default=24000)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--max-output-tokens', type=int, default=1024)
    parser.add_argument('--output', type=Path, help='Write full JSON exclusively to a NEW file')
    args = parser.parse_args(argv)
    try:
        if args.output and args.output.exists():
            raise GenerationError('OUTPUT_ALREADY_EXISTS')
        provider = (MockProvider() if args.mode == 'mock' else AnthropicProvider.from_environment(
            model=args.model, allow_live=args.allow_live, timeout_seconds=args.timeout, max_tokens=args.max_output_tokens,
            max_input_bytes=args.max_input_bytes, thinking_type=args.anthropic_thinking,
            structured_output=args.structured_output, input_token_cap=args.input_token_cap)
            if args.provider == 'anthropic' else ChatCompletionsProvider(
            endpoint=os.environ.get('TILLEGAL_LLM_ENDPOINT', ''), model=args.model or os.environ.get('TILLEGAL_LLM_MODEL', ''),
            api_key=os.environ.get('TILLEGAL_LLM_API_KEY', ''), allow_live=args.allow_live,
            timeout_seconds=args.timeout, max_tokens=args.max_output_tokens))
        index = load_index(args.index)
        rag = BasicRAG(index, provider, EvidenceBudget(args.max_evidence_chunks, args.max_quote_chars))
        result = rag.answer(args.question, request_id=args.request_id or str(uuid.uuid4()), top_k=args.top_k)
    except Exception as exc:
        result = {'schema_version': '1', 'status': 'FAILED', 'verification_status': 'NOT_PERFORMED',
                  'error': {'code': exc.code if isinstance(exc, GenerationError) else 'CONFIGURATION_OR_INDEX_ERROR'}}
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output and result.get('evidence_bundle') is not None:
        try:
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(encoded + '\n')
        except OSError:
            print(json.dumps({'status': 'FAILED', 'error': {'code': 'OUTPUT_WRITE_ERROR'}}))
            return 1
    if args.format == 'json':
        print(encoded)
    else:
        print(result['status'] + ' — Chưa verification; citations chỉ là ứng viên.')
        print(result.get('answer') or str(result.get('error')))
        for citation in result.get('candidate_citations', []):
            print('\nCandidate:', citation['evidence_id'])
            print('Version:', citation['document_version_id'], 'Chunk:', citation['chunk_id'])
            print('Quote:', citation['quote'])
            print('Warnings:', ', '.join(citation['warnings']))
        bundle = result.get('evidence_bundle') or {}
        print('\nEvidence:', len(bundle.get('evidence', [])), 'Context:', bundle.get('status'))
        print('Trace:', json.dumps({k: v for k, v in result.get('trace', {}).items()
                                   if k in ('provider_mode', 'model', 'retrieval_calls', 'generation_calls',
                                            'llm_calls', 'verification_calls', 'events')}, ensure_ascii=False))
    return 1 if result['status'] == 'FAILED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
