"""V2 retrieval envelope. Corpus identity is independent of ranking method."""
from copy import deepcopy
import math
from ai_core.bm25_retrieval import digest
from ai_core.data_contracts import ContractError


def response(index, query, ranked, method, config, trace=None):
    payload = index.to_dict()
    documents = {d['document_version_id']: d for d in payload['documents']}
    chunks = {c['chunk_id']: c for c in payload['chunks']}
    hits, seen = [], set()
    for cid, score, details in ranked:
        if cid not in chunks or cid in seen or not math.isfinite(score):
            raise ContractError('invalid retrieval ranking')
        seen.add(cid)
        chunk = chunks[cid]
        attachment = chunk['attachment_status']
        hits.append({'rank': len(hits) + 1, 'score': float(score), 'score_kind': method,
                     'ranking': deepcopy(details), 'chunk': deepcopy(chunk),
                     'document': deepcopy(documents[chunk['document_version_id']]),
                     'address_status': {'STRUCTURAL': 'RULE_BASED_UNVERIFIED',
                                       'HEURISTIC': 'HEURISTIC_CANDIDATE',
                                       'REVIEW_REQUIRED': 'REVIEW_REQUIRED'}[attachment],
                     'exact_citation_verified': False, 'legal_evidence_status': 'NOT_EVALUATED',
                     'cautions': sorted(set(chunk['issues'] + ['SOURCE_AND_TEMPORAL_STATUS_NOT_VERIFIED']))})
    return {'schema_version': '2', 'retrieval_method': method, 'index_id': index.index_id,
            'retriever_id': method.lower() + ':' + digest(config), 'config': deepcopy(config),
            'source': deepcopy(payload['source']), 'corpus_snapshot_id': None,
            'usage_policy': 'EXPERIMENTAL_ONLY', 'query': query, 'results': hits,
            'status': 'OK' if hits else 'EMPTY_QUERY' if not query.strip() else 'NO_MATCH',
            'trace': deepcopy(trace or {})}


def validate_query(query, top_k):
    if not isinstance(query, str) or len(query) > 10000 or type(top_k) is not int or not 1 <= top_k <= 100:
        raise ContractError('invalid query/top_k')
