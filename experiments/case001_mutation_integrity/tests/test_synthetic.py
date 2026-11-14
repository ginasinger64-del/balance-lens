#!/usr/bin/env python3
import json, tempfile, subprocess, sys
from pathlib import Path
import numpy as np

def run(cmd):
    p=subprocess.run(cmd,text=True,capture_output=True)
    return json.loads(p.stdout)

def main():
    # Small self-contained logic test; not upstream reproduction.
    scope=json.load(open('scope_grant.json')); s=scope['authorization']
    with tempfile.TemporaryDirectory() as td:
        d=Path(td)/'patch'; d.mkdir()
        t0,t1=s['tensors']
        idx0=np.array([t0['allowed_columns'][0]],dtype=np.int64)
        idx1=np.array([t1['allowed_columns'][0]],dtype=np.int64)
        np.savez(d/'edit.npz',t0_idx=idx0,t0_val=np.array([1],dtype=np.uint16),t1_idx=idx1,t1_val=np.array([2],dtype=np.uint16))
        m={'tensors':[]}
        for t in [t0,t1]:
            m['tensors'].append({'name':t['name'],'shape':t['shape'],'dtype':'bfloat16','form':'sparse','changed_entries':1,'changed_columns':1,'changed_rows':1,'base_sha256':t['base_tensor_sha256'],'edited_sha256':'0'*64})
        (d/'manifest.json').write_text(json.dumps(m))
        # True scope-only mode should pass: patch is within envelope, and no Phase-2 facts are assumed.
        o=run([sys.executable,'verify_patch.py','--patch-dir',str(d),'--parent-revision',s['parent_revision'],'--mode','scope'])
        assert o['status']=='PASS_SCOPE', o
        # Approved mode must fail because this is not the approved public patch.
        o=run([sys.executable,'verify_patch.py','--patch-dir',str(d),'--parent-revision',s['parent_revision'],'--mode','approved'])
        assert o['status']=='FAIL', o
        print('synthetic two-phase verifier logic test: PASS')
if __name__=='__main__': main()
