"""Persistent exact cosine retrieval. Model downloads are an explicit setup action."""
import json
from pathlib import Path
from time import perf_counter
import unicodedata
import numpy as np
from ai_core.bm25_retrieval import digest
from ai_core.data_contracts import ContractError, file_hash
from ai_core.retrieval_contracts import response, validate_query

MODEL = 'intfloat/multilingual-e5-small'


class LocalEncoder:
    def __init__(self, model_path, revision, batch_size=8):
        import torch
        from importlib.metadata import version
        from transformers import AutoModel, AutoTokenizer
        torch.set_num_threads(4)
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        self.model = AutoModel.from_pretrained(model_path, local_files_only=True, trust_remote_code=False,
                                             use_safetensors=True).eval()
        self.batch_size = batch_size
        self.config = {'model': MODEL, 'revision': revision, 'dimension': 384, 'max_tokens': 512,
                       'pooling': 'masked_mean_l2', 'text_policy': 'nfc_windows_480_overlap64_v1',
                       'dtype': 'float32', 'device': 'cpu',
                       'libraries':{name:version(name) for name in ('torch','transformers','numpy')}}

    def windows(self, text):
        # Token offsets map back to NFC embedding text; canonical source stays untouched.
        text = unicodedata.normalize('NFC', text)
        offsets = self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)['offset_mapping']
        if not offsets:
            return [(0, len(text), text)]
        windows = []
        for start in range(0, len(offsets), 416):
            end = min(start + 480, len(offsets))
            a, b = offsets[start][0], offsets[end-1][1]
            windows.append((a, b, text[a:b]))
            if end == len(offsets):
                break
        return windows

    def encode(self, texts, kind):
        values = [kind + ': ' + unicodedata.normalize('NFC', text) for text in texts]
        vectors = []
        for offset in range(0, len(values), self.batch_size):
            batch = self.tokenizer(values[offset:offset+self.batch_size], padding=True,
                                   truncation=False, return_tensors='pt')
            if batch['input_ids'].shape[1] > 512:
                raise ContractError('ENCODER_TOKEN_LIMIT: query or window too long')
            with self.torch.inference_mode():
                hidden = self.model(**batch).last_hidden_state
                mask = batch['attention_mask'].unsqueeze(-1)
                pooled = (hidden * mask).sum(1) / mask.sum(1)
                vectors.append(self.torch.nn.functional.normalize(pooled, p=2, dim=1).cpu().numpy())
        return np.concatenate(vectors).astype('float32')


def build_dense(index, encoder, output_dir):
    output = Path(output_dir)
    if output.exists():
        raise ContractError('dense output already exists')
    started = perf_counter()
    rows, texts = [], []
    for chunk in sorted(index.to_dict()['chunks'], key=lambda c: c['chunk_id']):
        for start, end, text in encoder.windows(chunk['text']):
            rows.append({'chunk_id': chunk['chunk_id'], 'document_version_id': chunk['document_version_id'],
                         'text_sha256': chunk['text_sha256'], 'embedding_start': start, 'embedding_end': end,
                         'embedding_text_sha256': digest(text)})
            texts.append(text)
    vectors = encoder.encode(texts, 'passage')
    validate_vectors(vectors, len(rows), encoder.config['dimension'])
    output.mkdir(parents=True)
    np.save(output / 'vectors.npy', vectors, allow_pickle=False)
    (output / 'rows.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    manifest = {'schema_version': '1', 'kind': 'dense_index', 'index_id': index.index_id,
                'source': index.to_dict()['source'], 'encoder': encoder.config,
                'rows': len(rows), 'build_seconds': perf_counter()-started,
                'files': {name: file_hash(output/name) for name in ('vectors.npy', 'rows.json')}}
    manifest['dense_index_id'] = digest({k: v for k,v in manifest.items() if k != 'build_seconds'})
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def validate_vectors(vectors, count, dimension):
    if (vectors.dtype != np.float32 or vectors.shape != (count, dimension)
            or not np.isfinite(vectors).all()
            or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4)):
        raise ContractError('invalid dense vectors')


class DenseRetriever:
    def __init__(self, index, encoder, manifest_path):
        self.index, self.encoder = index, encoder
        path = Path(manifest_path)
        m = json.loads(path.read_text(encoding='utf-8'))
        if (m.get('kind') != 'dense_index' or m.get('schema_version') != '1'
                or m['index_id'] != index.index_id or m['source'] != index.to_dict()['source']
                or m['encoder'] != encoder.config
                or m['dense_index_id'] != digest({k:v for k,v in m.items() if k not in ('dense_index_id','build_seconds')})):
            raise ContractError('dense corpus/model/config mismatch')
        for name in ('vectors.npy', 'rows.json'):
            if file_hash(path.parent/name) != m['files'][name]:
                raise ContractError('dense file hash mismatch')
        self.rows = json.loads((path.parent/'rows.json').read_text(encoding='utf-8'))
        self.vectors = np.load(path.parent/'vectors.npy', allow_pickle=False)
        validate_vectors(self.vectors, len(self.rows), encoder.config['dimension'])
        chunks = {c['chunk_id']: c for c in index.to_dict()['chunks']}
        if len(self.rows) != m['rows'] or {r['chunk_id'] for r in self.rows} != set(chunks):
            raise ContractError('dense completeness mismatch')
        for row in self.rows:
            c = chunks[row['chunk_id']]
            text = unicodedata.normalize('NFC', c['text'])
            if (row['document_version_id'] != c['document_version_id'] or row['text_sha256'] != c['text_sha256']
                    or not 0 <= row['embedding_start'] <= row['embedding_end'] <= len(text)
                    or row['embedding_text_sha256'] != digest(text[row['embedding_start']:row['embedding_end']])):
                raise ContractError('dense mapping mismatch')
        expected_rows = []
        for c in sorted(chunks.values(),key=lambda c:c['chunk_id']):
            for start,end,text in encoder.windows(c['text']):
                expected_rows.append({'chunk_id':c['chunk_id'],'document_version_id':c['document_version_id'],
                                      'text_sha256':c['text_sha256'],'embedding_start':start,'embedding_end':end,
                                      'embedding_text_sha256':digest(text)})
        if self.rows != expected_rows:
            raise ContractError('dense window coverage/order mismatch')
        self.config = {'dense_index_id': m['dense_index_id'], 'encoder': encoder.config,
                       'chunk_aggregation': 'max_window_cosine'}

    def search(self, query, top_k=5):
        validate_query(query, top_k)
        started = perf_counter()
        scores = {}
        if query.strip():
            vector = self.encoder.encode([query], 'query')
            validate_vectors(vector, 1, self.encoder.config['dimension'])
            for row, score in zip(self.rows, self.vectors @ vector[0]):
                cid = row['chunk_id']
                scores[cid] = max(scores.get(cid, -float('inf')), float(score))
        ranked = sorted(scores, key=lambda cid: (-scores[cid], cid))[:top_k]
        return response(self.index, query, [(cid,scores[cid], {'dense_score':scores[cid]}) for cid in ranked],
                        'DENSE', self.config, {'elapsed_ms': (perf_counter()-started)*1000})
