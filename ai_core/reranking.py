"""Optional local cross-encoder; never a semantic/legal verifier."""
from time import perf_counter
import math
from ai_core.data_contracts import ContractError
from ai_core.retrieval_contracts import response

MODEL = 'BAAI/bge-reranker-v2-m3'


class LocalReranker:
    def __init__(self, model_path, revision, batch_size=1):
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        torch.set_num_threads(4)
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_path,local_files_only=True,trust_remote_code=False)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_path,local_files_only=True,trust_remote_code=False,use_safetensors=True).eval()
        self.batch_size = batch_size
        self.config = {'model':MODEL,'revision':revision,'max_pair_tokens':512,
                       'long_text':'overlapping_passage_windows_max_logit','device':'cpu','batch_size':batch_size}

    def score(self, question, passages):
        q = self.tokenizer.encode(question,add_special_tokens=False)
        capacity = 512-len(q)-self.tokenizer.num_special_tokens_to_add(pair=True)
        if capacity < 64:
            raise ContractError('RERANK_QUERY_TOO_LONG')
        scores, counts = [], []
        for passage in passages:
            offsets = self.tokenizer(passage,add_special_tokens=False,return_offsets_mapping=True)['offset_mapping']
            windows = []
            for start in range(0,max(1,len(offsets)), max(1,capacity-32)):
                end = min(start+capacity,len(offsets))
                windows.append(passage[offsets[start][0]:offsets[end-1][1]] if offsets else passage)
                if end == len(offsets):
                    break
            values = []
            for start in range(0,len(windows),self.batch_size):
                batch = self.tokenizer([question]*len(windows[start:start+self.batch_size]),
                                       windows[start:start+self.batch_size],padding=True,truncation=False,return_tensors='pt')
                if batch['input_ids'].shape[1] > 512:
                    raise ContractError('RERANK_PAIR_TOO_LONG')
                with self.torch.inference_mode():
                    values.extend(self.model(**batch).logits.reshape(-1).float().tolist())
            scores.append(max(values)); counts.append(len(windows))
        return scores, counts

    def rerank(self, index, retrieval):
        started = perf_counter()
        hits = retrieval['results']
        scores, windows = self.score(retrieval['query'], [h['chunk']['text'] for h in hits])
        if len(scores) != len(hits) or len(windows) != len(hits) or not all(math.isfinite(s) for s in scores):
            raise ContractError('invalid reranker scores')
        ranked = [(h['chunk']['chunk_id'],s, {'fusion_rank':h['rank'],'fusion_score':h['score'],
                   'branches':h['ranking'],'rerank_windows':w}) for h,s,w in zip(hits,scores,windows)]
        ranked.sort(key=lambda r:(-r[1],r[0]))
        return response(index,retrieval['query'],ranked,'HYBRID_RERANK',
                        {'fusion':retrieval['config'],'reranker':self.config},
                        {**retrieval['trace'],'fusion_results':hits,'rerank_ms':(perf_counter()-started)*1000})
