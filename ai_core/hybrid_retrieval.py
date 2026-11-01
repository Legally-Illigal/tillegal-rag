"""Rank fusion without mixing incompatible score scales."""
from time import perf_counter
from ai_core.data_contracts import ContractError
from ai_core.retrieval_contracts import response, validate_query


class RetrievalUnavailable(RuntimeError):
    """Only explicitly classified availability failures may degrade a search."""


class HybridRetriever:
    def __init__(self, index, dense, branch_k=20, rrf_k=60, allow_fallback=False, reranker=None):
        validate_query('', branch_k)
        if type(rrf_k) is not int or rrf_k < 1:
            raise ContractError('invalid RRF constant')
        self.index, self.dense = index, dense
        payload = index.to_dict()
        self.source = payload['source']
        self.chunks = {c['chunk_id']:c for c in payload['chunks']}
        self.branch_k, self.rrf_k = branch_k, rrf_k
        self.allow_fallback, self.reranker = allow_fallback, reranker

    def search(self, query, top_k=5):
        validate_query(query, top_k)
        started = perf_counter()
        branches, failures = {}, {}
        for name, retriever in [('bm25', self.index), ('dense', self.dense)]:
            try:
                if retriever is None:
                    raise RetrievalUnavailable('branch not configured')
                branches[name] = retriever.search(query, self.branch_k)
                if branches[name]['index_id'] != self.index.index_id or branches[name]['source'] != self.source:
                    raise ContractError('hybrid branch corpus mismatch')
            except (RetrievalUnavailable, TimeoutError):
                if not self.allow_fallback:
                    raise
                failures[name] = 'BRANCH_UNAVAILABLE'
        if not branches:
            raise RetrievalUnavailable('all branches unavailable')
        scores, details = {}, {}
        for name, branch in branches.items():
            seen = set()
            for rank, hit in enumerate(branch['results'], 1):
                cid = hit['chunk']['chunk_id']
                if cid in seen or hit['rank'] != rank or hit['chunk'] != self.chunks.get(cid):
                    raise ContractError('duplicate/noncontiguous branch ranking')
                seen.add(cid)
                scores[cid] = scores.get(cid, 0) + 1/(self.rrf_k + rank)
                details.setdefault(cid, {})[name] = {'rank':rank, 'score':hit.get('bm25_score',hit.get('score'))}
        ordered = sorted(scores, key=lambda cid: (-scores[cid],cid))[:max(self.branch_k,top_k)]
        fused = response(self.index, query, [(cid,scores[cid],details[cid]) for cid in ordered],
                         'HYBRID', {'branch_k':self.branch_k,'rrf_k':self.rrf_k,
                                    'dense': getattr(self.dense,'config',None), 'allow_fallback':self.allow_fallback},
                         {'branches': branches, 'failures': failures,
                          'branch_calls': {'bm25':1,'dense':int(self.dense is not None)},
                          'execution_status':'DEGRADED' if failures else 'OK'})
        if self.reranker is not None and fused['results']:
            fused = self.reranker.rerank(self.index, fused)
        fused['results'] = fused['results'][:top_k]
        fused['trace']['elapsed_ms'] = (perf_counter()-started)*1000
        return fused
