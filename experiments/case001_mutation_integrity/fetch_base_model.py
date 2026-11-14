#!/usr/bin/env python3
import argparse
from pathlib import Path

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',default='cache/base')
    args=ap.parse_args()
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit('Install huggingface_hub first: pip install huggingface_hub')
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    p=hf_hub_download(repo_id='Qwen/Qwen3.5-0.8B',filename='model.safetensors',revision='2fc06364715b967f1860aea9cf38778875588b17',local_dir=str(out))
    print(p)
if __name__=='__main__': main()
