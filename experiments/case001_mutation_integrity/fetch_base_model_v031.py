#!/usr/bin/env python3
"""v0.3.1: download the pinned Qwen base checkpoint under its real filename.

v0.2's fetch_base_model.py (kept byte-identical for provenance) requests
'model.safetensors', which does not exist at the pinned revision. The file is
'model.safetensors-00001-of-00001.safetensors'. This script downloads it,
verifies size and SHA-256, and links it to cache/base/model.safetensors so
run_cases*.py --base-model cache/base/model.safetensors works unchanged.
"""
import argparse, hashlib, os
from pathlib import Path

REPO = 'Qwen/Qwen3.5-0.8B'
REVISION = '2fc06364715b967f1860aea9cf38778875588b17'
FILENAME = 'model.safetensors-00001-of-00001.safetensors'
SIZE = 1_746_942_600
SHA256 = '04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696'


def sha256_file(p, chunk=1 << 24):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(chunk), b''):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='cache/base')
    args = ap.parse_args()
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit('Install huggingface_hub first: pip install huggingface_hub')
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    p = Path(hf_hub_download(repo_id=REPO, filename=FILENAME, revision=REVISION, local_dir=str(out)))
    size = p.stat().st_size
    if size != SIZE:
        raise SystemExit(f'size mismatch: {size} != {SIZE}')
    digest = sha256_file(p)
    if digest != SHA256:
        raise SystemExit(f'SHA-256 mismatch: {digest} != {SHA256}')
    link = out / 'model.safetensors'
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(p.resolve(), link)
    print(f'verified {FILENAME} ({size} bytes, sha256 {digest}) -> {link}')


if __name__ == '__main__':
    main()
