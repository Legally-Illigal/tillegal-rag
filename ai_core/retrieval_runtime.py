"""Local-only factory. Legacy BM25 does not import ML dependencies."""
import json
from pathlib import Path
from ai_core.data_contracts import ContractError, file_hash

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DENSE = ROOT/'data/indexes/labor-pilot-3-t7-dense/manifest.json'


def model_lock(kind):
    path = ROOT/'.model-cache'/(kind+'.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    expected_model = {'embedding':'intfloat/multilingual-e5-small','reranker':'BAAI/bge-reranker-v2-m3'}[kind]
    if (value.get('model') != expected_model or not isinstance(value.get('revision'),str)
            or len(value['revision']) != 40 or any(c not in '0123456789abcdef' for c in value['revision'])
            or not {'config.json','model.safetensors','tokenizer_config.json'} <= set(value.get('files',{}))):
        raise ContractError('invalid pinned model identity/files')
    location = (ROOT/value['path']).resolve()
    if not location.is_relative_to((ROOT/'.model-cache').resolve()):
        raise ContractError('model path outside project cache')
    for name, expected in value['files'].items():
        if Path(name).name != name or file_hash(location/name) != expected:
            raise ContractError('model file integrity mismatch')
    return str(location), value['revision']


def make_retriever(index, mode='bm25', rerank=False, dense_manifest=DEFAULT_DENSE,
                   branch_k=20, allow_fallback=False):
    if mode not in ('bm25','dense','hybrid') or (rerank and mode != 'hybrid'):
        raise ContractError('reranking requires hybrid mode')
    if mode == 'bm25':
        return index
    from ai_core.dense_retrieval import LocalEncoder, DenseRetriever
    from ai_core.hybrid_retrieval import HybridRetriever
    dense = None
    if not (ROOT/'.model-cache/embedding.json').is_file() or not Path(dense_manifest).is_file():
        if not (mode == 'hybrid' and allow_fallback):
            raise FileNotFoundError('embedding deployment is not available')
    else:
        encoder = LocalEncoder(*model_lock('embedding'))
        dense = DenseRetriever(index, encoder, dense_manifest)
    if mode == 'dense':
        return dense
    reranker = None
    if rerank:
        from ai_core.reranking import LocalReranker
        reranker = LocalReranker(*model_lock('reranker'))
    return HybridRetriever(index,dense,branch_k=branch_k,allow_fallback=allow_fallback,reranker=reranker)
