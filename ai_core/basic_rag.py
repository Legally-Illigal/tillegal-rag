"""T6 sequential Basic RAG. Draft + candidate citations, never verification."""
from copy import deepcopy
from time import perf_counter

from ai_core.bm25_retrieval import digest
from ai_core.evidence_builder import EvidenceBuilder
from ai_core.rag_generation import GenerationError, GenerationText, PROMPT_VERSION, generation_messages, validate_draft, MAX_OUTPUT_CHARS


class BasicRAG:
    def __init__(self, index, provider, evidence_budget=None, *, retriever=None):
        self._index = index
        self._retriever = retriever if retriever is not None else index
        self._builder = EvidenceBuilder(index, evidence_budget)
        self._provider = provider

    def answer(self, question: str, *, request_id: str, top_k: int = 5) -> dict:
        trace = {"prompt_version": PROMPT_VERSION, "provider_mode": self._provider.mode,
                 "model": self._provider.model, "retrieval_calls": 0, "generation_calls": 0,
                 "llm_calls": 0, "verification_calls": 0, "events": [],
                 "retrieval": None, "generation_raw": None, "generation_draft": None,
                 "structured_output_validation": "NOT_RUN"}
        result = {"schema_version": "1", "request_id": request_id, "question": question,
                  "status": "FAILED", "answer": None, "candidate_citations": [],
                  "verification_status": "NOT_PERFORMED", "usage_policy": "EXPERIMENTAL_ONLY",
                  "corpus_snapshot_id": None, "evidence_bundle": None, "trace": trace,
                  "warnings": ["DRAFT_NOT_VERIFIED", "CANDIDATE_CITATIONS_NOT_VERIFIED",
                               "SOURCE_AND_TEMPORAL_STATUS_NOT_VERIFIED"], "error": None}
        stage = "input"
        started = perf_counter()

        def event(name):
            nonlocal started
            now = perf_counter()
            trace['events'].append({"stage": name, "status": "OK", "elapsed_ms": (now-started)*1000})
            started = now

        try:
            if (not isinstance(question, str) or not question.strip() or len(question) > 10000
                    or not isinstance(request_id, str) or not request_id.strip()
                    or type(top_k) is not int or not 1 <= top_k <= 100):
                raise GenerationError("INVALID_INPUT")
            event(stage)
            stage = "retrieval"
            trace['retrieval_calls'] = 1
            retrieval = self._retriever.search(question, top_k)
            trace['retrieval'] = deepcopy(retrieval)
            event(stage)
            stage = "evidence"
            bundle = self._builder.build(retrieval, request_id=request_id)
            result['evidence_bundle'] = bundle
            event(stage)
            if not bundle['evidence']:
                result.update(status="NO_EVIDENCE", answer="Không có evidence trong bundle để tạo bản nháp.")
                return result
            if bundle['status'] == 'PARTIAL':
                result['warnings'].append('CONTEXT_OMITTED_BY_BUDGET')
            stage = "generation_input"
            messages = generation_messages(question, bundle)
            trace['generation_input'] = deepcopy(messages)
            trace['generation_input_sha256'] = digest(messages)
            event(stage)
            stage = "generation"
            trace['generation_calls'] = 1
            trace['llm_calls'] = int(self._provider.mode == 'live')
            raw = self._provider.generate(deepcopy(messages))
            if isinstance(raw, GenerationText):
                trace['provider_response'] = deepcopy(raw.metadata)
            if isinstance(raw, str):
                trace['generation_raw'] = raw[:MAX_OUTPUT_CHARS]
                trace['generation_raw_truncated'] = len(raw) > MAX_OUTPUT_CHARS
            event(stage)
            stage = "structured_output"
            draft = validate_draft(raw, {e['evidence_id'] for e in bundle['evidence']})
            trace['generation_draft'] = deepcopy(draft)
            trace['structured_output_validation'] = "PASS_SCHEMA_AND_ID_MEMBERSHIP_ONLY"
            event(stage)
            by_id = {e['evidence_id']: e for e in bundle['evidence']}
            result.update(status="ABSTAINED_UNVERIFIED" if draft['abstain'] else "DRAFT_UNVERIFIED",
                          answer=draft['draft_answer'],
                          candidate_citations=[{"citation_status": "CANDIDATE_UNVERIFIED", **deepcopy(by_id[eid])}
                                               for eid in draft['candidate_evidence_ids']])
        except Exception as exc:
            if isinstance(exc, GenerationError) and hasattr(exc, 'metadata'):
                trace['provider_response'] = deepcopy(exc.metadata)
                if isinstance(getattr(exc, 'raw_text', None), str):
                    trace['generation_raw'] = exc.raw_text[:MAX_OUTPUT_CHARS]
                    trace['generation_raw_truncated'] = bool(getattr(exc, 'raw_text_truncated', False))
            code = exc.code if isinstance(exc, GenerationError) else (
                "PROVIDER_TIMEOUT" if isinstance(exc, TimeoutError) and stage == 'generation'
                else "PROVIDER_ERROR" if stage == 'generation' else stage.upper() + '_ERROR')
            result['error'] = {"code": code, "stage": stage}
            trace['events'].append({"stage": stage, "status": "ERROR", "code": code,
                                    "elapsed_ms": (perf_counter()-started)*1000})
            if stage == 'structured_output':
                trace['structured_output_validation'] = 'FAILED'
        return result
