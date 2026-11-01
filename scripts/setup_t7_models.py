"""Explicit public-model download; no credentials, inference APIs or system changes."""
import argparse
import json
import os
from pathlib import Path
from ai_core.data_contracts import file_hash

ROOT = Path(__file__).resolve().parents[1]
os.environ['HF_HOME'] = str(ROOT/'.model-cache')
os.environ['HF_XET_CACHE'] = str(ROOT/'.model-cache'/'xet')
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
from huggingface_hub import HfApi, snapshot_download
MODELS = {'embedding':'intfloat/multilingual-e5-small', 'reranker':'BAAI/bge-reranker-v2-m3'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kind', choices=MODELS, required=True)
    parser.add_argument('--revision', help='Exact revision; default resolves public main once and pins it')
    args = parser.parse_args()
    root = ROOT/'.model-cache'
    target = root/(args.kind+'.json')
    if target.exists():
        raise ValueError('Model lock already exists; retain pinned model or use explicit manual rebuild')
    repo = MODELS[args.kind]
    revision = HfApi(token=False).model_info(repo, revision=args.revision or 'main').sha
    path = Path(snapshot_download(repo, revision=revision, token=False, cache_dir=root/'hub',
                                 allow_patterns=['*.json','*.safetensors','*.model'], max_workers=2))
    files = {p.name:file_hash(p) for p in path.iterdir() if p.is_file()}
    target.write_text(json.dumps({'model':repo,'revision':revision,
                                  'path':str(path.relative_to(ROOT)), 'files':files},indent=2),encoding='utf-8')
    print(json.dumps({'model':repo,'revision':revision,'files':len(files)}))


if __name__ == '__main__':
    main()
