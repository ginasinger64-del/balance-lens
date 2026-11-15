"""diffcheck_b4.py <child_dir> : bytewise child-vs-verified-parent scope check against the unchanged Case 001 grant."""
import json, sys, hashlib, numpy as np, torch
from safetensors import safe_open
from b4lib import BASE_DIR, CKPT, grant, sha_file

g = grant(); perm = {t['name']: t for t in g['tensors']}
child_dir = sys.argv[1]
def sha(t): return hashlib.sha256(t.contiguous().view(torch.uint8).numpy().tobytes()).hexdigest()
def raw(t): return t.contiguous().view(torch.uint8).numpy().tobytes()
r = {'violations': [], 'changed_tensors': {}, 'tensors_compared': 0}
with safe_open(f'{BASE_DIR}/{CKPT}', 'pt') as b, safe_open(f'{child_dir}/{CKPT}', 'pt') as c:
    kb, kc = set(b.keys()), set(c.keys())
    if kb != kc: r['violations'].append({'key_set_differs': sorted(kb ^ kc)})
    if b.metadata() != c.metadata(): r['violations'].append('metadata differs')
    for k in sorted(kb & kc):
        tb, tc = b.get_tensor(k), c.get_tensor(k); r['tensors_compared'] += 1
        if tb.dtype != tc.dtype or tb.shape != tc.shape: r['violations'].append({k: 'dtype/shape changed'}); continue
        if raw(tb) == raw(tc): continue
        if k not in perm: r['violations'].append({k: 'non-permitted tensor changed'}); continue
        p = perm[k]; diff = (tb.view(torch.int16) != tc.view(torch.int16)).nonzero()
        colsch = sorted(set(diff[:, 1].tolist())); out = sorted(set(colsch) - set(p['allowed_columns']))
        info = {'changed_entries': int(diff.shape[0]), 'changed_columns': len(colsch), 'columns_outside_grant': out,
                'dtype': str(tc.dtype), 'base_sha256_matches_grant': sha(tb) == p['base_tensor_sha256'],
                'child_tensor_sha256': sha(tc), 'max_abs_change': float((tc.float() - tb.float()).abs().max())}
        if out: r['violations'].append({k: f'{len(out)} columns outside grant'})
        if diff.shape[0] > p['max_targeted_entries']: r['violations'].append({k: 'entry budget exceeded'})
        if tc.dtype != torch.bfloat16: r['violations'].append({k: 'dtype not bf16'})
        if not info['base_sha256_matches_grant']: r['violations'].append({k: 'parent tensor hash mismatch'})
        r['changed_tensors'][k] = info
if len(r['changed_tensors']) > g['max_tensors']: r['violations'].append('too many tensors')
r['child_file_sha256'] = sha_file(f'{child_dir}/{CKPT}')
r['patch_npz_sha256'] = sha_file(f'{child_dir}/patch_sparse.npz')
r['verdict'] = 'IN_SCOPE' if not r['violations'] else 'OUT_OF_SCOPE'
json.dump(r, open(f'{child_dir}/diffcheck.json', 'w'), indent=1)
print(r['verdict'], {k.split('.')[3]: v['changed_entries'] for k, v in r['changed_tensors'].items()}, r['violations'])
