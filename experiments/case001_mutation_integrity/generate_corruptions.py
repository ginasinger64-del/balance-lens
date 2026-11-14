#!/usr/bin/env python3
import argparse, json, shutil
from pathlib import Path
import numpy as np

def cp(src,dst):
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(src,dst)

def save_npz(path, arrays): np.savez(path, **arrays)

def load_arrays(path):
    z=np.load(path,allow_pickle=False); return {k:z[k] for k in z.files}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source',default='cache/p50c_edit'); ap.add_argument('--out',default='cases/generated'); args=ap.parse_args()
    src=Path(args.source); out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    # clean
    cp(src,out/'clean_original')
    # forbidden tensor: semantic scope violation in manifest
    cp(src,out/'forbidden_tensor'); p=out/'forbidden_tensor'/'manifest.json'; m=json.loads(p.read_text()); m['tensors'][1]['name']='model.language_model.layers.18.mlp.down_proj.weight'; p.write_text(json.dumps(m,indent=2)+'\n')
    # out-of-scope column: mutate an index to a column that is not allowed
    cp(src,out/'out_of_scope_column'); d=out/'out_of_scope_column'; a=load_arrays(d/'edit.npz'); idx=a['t0_idx'].copy().astype(np.int64); allowed=set(json.load(open('scope_grant.json'))['authorization']['tensors'][0]['allowed_columns']); bad=next(c for c in range(3584) if c not in allowed); idx[0]=bad; a['t0_idx']=idx; save_npz(d/'edit.npz',a)
    # undeclared extra payload value: add one new sparse location but leave manifest count unchanged
    cp(src,out/'undeclared_extra_value'); d=out/'undeclared_extra_value'; a=load_arrays(d/'edit.npz'); idx=a['t0_idx'].astype(np.int64); vals=a['t0_val']; used=set(map(int,idx)); allowed=json.load(open('scope_grant.json'))['authorization']['tensors'][0]['allowed_columns']; candidate=next(r*3584+allowed[0] for r in range(1024) if r*3584+allowed[0] not in used); a['t0_idx']=np.concatenate([idx,[candidate]]); a['t0_val']=np.concatenate([vals,vals[:1]]); save_npz(d/'edit.npz',a)
    # altered value after authorization: same location, different raw BF16 bits
    cp(src,out/'altered_value_after_authorization'); d=out/'altered_value_after_authorization'; a=load_arrays(d/'edit.npz'); v=a['t0_val'].copy().astype(np.uint16); v[0]=np.uint16((int(v[0])+1)&0xffff); a['t0_val']=v; save_npz(d/'edit.npz',a)
    # false child hash claim
    cp(src,out/'false_child_hash'); p=out/'false_child_hash'/'manifest.json'; m=json.loads(p.read_text()); m['tensors'][0]['edited_sha256']='0'*64; p.write_text(json.dumps(m,indent=2)+'\n')
    print(out)
if __name__=='__main__': main()
