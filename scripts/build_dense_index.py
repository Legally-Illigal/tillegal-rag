"""Explicit, offline build from the pinned model cache and existing corpus."""
import argparse
import json
from ai_core.bm25_retrieval import load_index
from ai_core.dense_retrieval import LocalEncoder, build_dense
from ai_core.retrieval_runtime import ROOT, DEFAULT_DENSE, model_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',default=str(DEFAULT_DENSE.parent))
    args = parser.parse_args()
    index = load_index(ROOT/'data/indexes/labor-pilot-3-t4-bm25/manifest.json')
    encoder = LocalEncoder(*model_lock('embedding'))
    print(json.dumps(build_dense(index,encoder,args.output),indent=2))


if __name__ == '__main__':
    main()
