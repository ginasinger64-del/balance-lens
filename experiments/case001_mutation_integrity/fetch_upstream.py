#!/usr/bin/env python3
import argparse, hashlib, json, urllib.request
from pathlib import Path


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--lock',default='upstream.lock.json')
    ap.add_argument('--out',default='cache/p50c_edit')
    args=ap.parse_args()
    lock=json.load(open(args.lock))
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    base=lock['upstream']['raw_base']
    record={'repo':lock['upstream']['repo'],'commit':lock['upstream']['commit'],'files':{}}
    mapping={'patch_manifest':'manifest.json','patch_payload':'edit.npz','frozen_weights_manifest':'frozen_weights_manifest.json','license':'UPSTREAM_LICENSE.txt'}
    for key,name in mapping.items():
        meta=lock['upstream']['files'][key]
        url=base+meta['path']
        print('fetch',url)
        with urllib.request.urlopen(url) as r: data=r.read()
        got=git_blob_sha1(data)
        if got!=meta['git_blob_sha1']:
            raise SystemExit(f'{key}: git blob SHA mismatch: {got} != {meta["git_blob_sha1"]}')
        (out/name).write_bytes(data)
        record['files'][key]={'path':str(out/name),'bytes':len(data),'git_blob_sha1':got,'sha256':hashlib.sha256(data).hexdigest(),'url':url}
    (out/'fetch_record.json').write_text(json.dumps(record,indent=2)+'\n')
    print('verified pinned upstream ->',out)

if __name__=='__main__': main()
