#!/usr/bin/env python3
import argparse, hashlib, json, struct, sys
from pathlib import Path
import numpy as np

PASS='PASS'; FAIL='FAIL'; NOT_RUN='NOT_RUN'; SKIP='SKIP'

def git_blob_sha1(data): return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
def sha256(data): return hashlib.sha256(data).hexdigest()
def load_json(p): return json.loads(Path(p).read_text())

def add(checks,cid,ok,detail,required=True):
    status=PASS if ok else (FAIL if required else NOT_RUN)
    checks.append({'id':cid,'status':status,'detail':detail})
    return ok

def skip(checks,cid,detail):
    checks.append({'id':cid,'status':SKIP,'detail':detail})

def read_safetensors(path,names):
    out={}
    with open(path,'rb') as f:
        n=struct.unpack('<Q',f.read(8))[0]
        head=json.loads(f.read(n)); data0=8+n
        for name in names:
            info=head.get(name)
            if info is None: raise KeyError(name)
            if info['dtype']!='BF16': raise ValueError(f'{name}: expected BF16, got {info["dtype"]}')
            a,b=info['data_offsets']; f.seek(data0+a)
            arr=np.frombuffer(f.read(b-a),dtype=np.uint16).copy().reshape(info['shape'])
            out[name]=arr
    return out

def verify(args):
    scope=load_json(args.scope); S=scope['authorization']
    approval=None
    if args.mode in ('approved','semantic-diagnostic'):
        approval=load_json(args.approval)
        if approval.get('case_id') != scope.get('case_id'):
            raise ValueError('scope/approval case_id mismatch')
    pdir=Path(args.patch_dir); manifest_path=pdir/'manifest.json'; payload_path=pdir/'edit.npz'
    man=load_json(manifest_path); z=np.load(payload_path,allow_pickle=False)
    checks=[]
    add(checks,'parent_revision',args.parent_revision==S['parent_revision'],f'provided={args.parent_revision}; authorized={S["parent_revision"]}')

    # Phase binding. In a prospective flow this is the bridge between pre-edit scope and post-submission approval.
    if approval is not None:
        scope_hash=sha256(Path(args.scope).read_bytes())
        add(checks,'approval_bound_to_scope',approval.get('scope_grant_sha256')==scope_hash,f'got={approval.get("scope_grant_sha256")} expected={scope_hash}')

    mbytes=manifest_path.read_bytes(); pbytes=payload_path.read_bytes()
    if args.mode=='approved':
        P=approval['patch']
        add(checks,'patch_manifest_blob',git_blob_sha1(mbytes)==P['manifest_git_blob_sha1'],f'got={git_blob_sha1(mbytes)} expected={P["manifest_git_blob_sha1"]}')
        add(checks,'patch_payload_blob',git_blob_sha1(pbytes)==P['payload_git_blob_sha1'],f'got={git_blob_sha1(pbytes)} expected={P["payload_git_blob_sha1"]}; sha256={sha256(pbytes)}')
    elif args.mode=='semantic-diagnostic':
        skip(checks,'patch_manifest_blob','intentionally disabled: semantic diagnostic isolates non-byte checks')
        skip(checks,'patch_payload_blob','intentionally disabled: semantic diagnostic isolates non-byte checks')
    else:
        skip(checks,'patch_manifest_blob','not available in PRE_EDIT_SCOPE_GRANT phase')
        skip(checks,'patch_payload_blob','not available in PRE_EDIT_SCOPE_GRANT phase')

    allowed={t['name']:t for t in S['tensors']}
    approved={t['name']:t for t in approval['patch']['tensors']} if approval is not None else {}
    names=[t['name'] for t in man.get('tensors',[])]
    add(checks,'max_tensor_count',len(names)<=S['max_tensors'],f'{len(names)} <= {S["max_tensors"]}')
    if S.get('require_exact_tensor_set'):
        add(checks,'exact_tensor_set',set(names)==set(allowed),f'patch={names}; authorized={list(allowed)}')
    expected_npz=set()
    for i,t in enumerate(man.get('tensors',[])):
        prefix=f't{i}_'; expected_npz|={prefix+'idx',prefix+'val'}
        name=t['name']; st=allowed.get(name); pt=approved.get(name)
        add(checks,f't{i}.authorized_name',st is not None,name)
        if st is None: continue
        add(checks,f't{i}.shape',list(t.get('shape',[]))==st['shape'],f'{t.get("shape")} expected {st["shape"]}')
        add(checks,f't{i}.dtype',t.get('dtype')==S['allowed_dtype'],f'{t.get("dtype")} expected {S["allowed_dtype"]}')
        add(checks,f't{i}.form',t.get('form') in S['allowed_forms'],str(t.get('form')))
        ik,vk=prefix+'idx',prefix+'val'
        if ik not in z.files or vk not in z.files:
            add(checks,f't{i}.payload_arrays',False,f'missing {ik} or {vk}'); continue
        idx=np.asarray(z[ik]).astype(np.int64).ravel(); val=np.asarray(z[vk]).ravel(); rows,cols=st['shape']; total=rows*cols
        add(checks,f't{i}.length_match',len(idx)==len(val),f'idx={len(idx)} val={len(val)}')
        add(checks,f't{i}.index_range',bool(np.all((idx>=0)&(idx<total))),f'range=[{idx.min(initial=0)},{idx.max(initial=0)}], total={total}')
        add(checks,f't{i}.no_duplicate_indices',len(np.unique(idx))==len(idx),f'unique={len(np.unique(idx))} total={len(idx)}')
        rr=idx//cols; cc=idx%cols
        ucols=sorted(set(map(int,cc))); urows=len(set(map(int,rr)))
        add(checks,f't{i}.allowed_columns',set(ucols)<=set(st['allowed_columns']),f'actual={ucols}; allowed_count={len(st["allowed_columns"])}')
        add(checks,f't{i}.column_count',len(ucols)<=st.get('max_columns',len(st['allowed_columns'])),f'{len(ucols)} <= {st.get("max_columns",len(st["allowed_columns"]))}')
        # Internal patch consistency is valid in every phase.
        add(checks,f't{i}.payload_count_vs_manifest',len(idx)==int(t.get('changed_entries',-1)),f'payload={len(idx)} manifest={t.get("changed_entries")}')
        add(checks,f't{i}.column_count_vs_manifest',len(ucols)==int(t.get('changed_columns',-1)),f'payload={len(ucols)} manifest={t.get("changed_columns")}')
        add(checks,f't{i}.row_count_vs_manifest',urows==int(t.get('changed_rows',-1)),f'payload={urows} manifest={t.get("changed_rows")}')
        add(checks,f't{i}.max_targeted_entries',len(idx)<=st['max_targeted_entries'],f'{len(idx)} <= {st["max_targeted_entries"]}')
        add(checks,f't{i}.base_hash_claim',t.get('base_sha256')==st['base_tensor_sha256'],f'{t.get("base_sha256")} expected={st["base_tensor_sha256"]}')
        if pt is not None:
            add(checks,f't{i}.approved_changed_entries',int(t.get('changed_entries',-1))==pt['expected_changed_entries'],f'{t.get("changed_entries")} expected={pt["expected_changed_entries"]}')
            add(checks,f't{i}.child_hash_claim',t.get('edited_sha256')==pt['edited_tensor_sha256'],f'{t.get("edited_sha256")} expected={pt["edited_tensor_sha256"]}')
        else:
            skip(checks,f't{i}.approved_changed_entries','unknown before post-submission patch approval')
            skip(checks,f't{i}.child_hash_claim','unknown before post-submission patch approval')
    extras=set(z.files)-expected_npz
    add(checks,'no_unknown_npz_arrays',not extras,f'extras={sorted(extras)}')

    # Replay is phase-specific: application should bind to the post-submission approval nonce when approval exists.
    if args.nonce_db:
        if approval is not None:
            nonce=approval['approval_nonce']
        else:
            nonce=S['nonce']
        dbp=Path(args.nonce_db); db=json.loads(dbp.read_text()) if dbp.exists() else {'seen':[]}
        fresh=nonce not in db['seen']; add(checks,'nonce_fresh',fresh,nonce)
        if fresh and args.consume_nonce:
            db['seen'].append(nonce); dbp.parent.mkdir(parents=True,exist_ok=True); dbp.write_text(json.dumps(db,indent=2)+'\n')

    # Optional base-dependent verification. This can validate actual scope even without post-submission approval.
    if args.base_model:
        try:
            bases=read_safetensors(args.base_model,[t['name'] for t in man['tensors'] if t['name'] in allowed])
            for i,t in enumerate(man['tensors']):
                name=t['name']; st=allowed.get(name); pt=approved.get(name)
                if st is None: continue
                b=np.ascontiguousarray(bases[name]); bh=sha256(b.tobytes())
                add(checks,f't{i}.base_tensor_actual_hash',bh==st['base_tensor_sha256'],f'got={bh} expected={st["base_tensor_sha256"]}')
                idx=np.asarray(z[f't{i}_idx']).astype(np.int64).ravel(); vals=np.asarray(z[f't{i}_val'],dtype=np.uint16).ravel()
                e=b.copy().reshape(-1); e[idx]=vals; e=e.reshape(b.shape); eh=sha256(np.ascontiguousarray(e).tobytes())
                if pt is not None:
                    add(checks,f't{i}.edited_tensor_actual_hash_vs_approval',eh==pt['edited_tensor_sha256'],f'got={eh} expected={pt["edited_tensor_sha256"]}')
                    add(checks,f't{i}.edited_tensor_actual_hash_vs_patch_claim',eh==t.get('edited_sha256'),f'got={eh} claimed={t.get("edited_sha256")}')
                changed=np.flatnonzero(b.reshape(-1)!=e.reshape(-1)); cc=changed%b.shape[1]
                add(checks,f't{i}.actual_changed_entries_within_scope',len(changed)<=st['max_targeted_entries'],f'{len(changed)} <= {st["max_targeted_entries"]}')
                add(checks,f't{i}.actual_changed_columns',set(map(int,cc))<=set(st['allowed_columns']),f'unique={len(set(map(int,cc)))}')
        except Exception as exc:
            add(checks,'base_verification_runtime',False,repr(exc))
    else:
        checks.append({'id':'base_dependent_tensor_reconstruction','status':NOT_RUN,'detail':'supply --base-model to verify actual parent/child tensor hashes and actual changed entries'})

    fails=[c for c in checks if c['status']==FAIL]
    notrun=[c for c in checks if c['status']==NOT_RUN]
    if fails:
        status='FAIL'
    elif args.mode=='scope':
        status='PASS_SCOPE_FULL' if args.base_model and not notrun else 'PASS_SCOPE'
    elif args.mode=='semantic-diagnostic':
        status='PASS_SEMANTIC_DIAGNOSTIC_FULL' if args.base_model and not notrun else 'PASS_SEMANTIC_DIAGNOSTIC'
    else:
        status='PASS_FULL' if args.base_model and not notrun else 'PASS_PREBASE'
    return {
      'case_id':scope['case_id'],'manifest_timing':scope['manifest_timing'],'authorization_mode':args.mode,
      'status':status,'checks':checks,
      'summary':{'failures':len(fails),'not_run':len(notrun),'skipped':sum(c['status']==SKIP for c in checks),'passed':sum(c['status']==PASS for c in checks)}
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--patch-dir',required=True)
    ap.add_argument('--scope',default='scope_grant.json')
    ap.add_argument('--approval',default='patch_approval.json')
    ap.add_argument('--mode',choices=['scope','approved','semantic-diagnostic'],default='approved')
    ap.add_argument('--parent-revision',required=True); ap.add_argument('--base-model'); ap.add_argument('--nonce-db'); ap.add_argument('--consume-nonce',action='store_true'); ap.add_argument('--out')
    args=ap.parse_args(); out=verify(args); s=json.dumps(out,indent=2); print(s)
    if args.out: Path(args.out).write_text(s+'\n')
    sys.exit(1 if out['status']=='FAIL' else 0)
if __name__=='__main__': main()
