"""Independent scope check: compare child checkpoint file to verified parent file, tensor by tensor, bytewise."""
import json, sys, hashlib, numpy as np, torch
from safetensors import safe_open
BASE = 'hfmeta/model.safetensors-00001-of-00001.safetensors'
CHILD = sys.argv[1] + '/model.safetensors-00001-of-00001.safetensors'
g = json.load(open('/home/claude/balance-lens/experiments/case001_mutation_integrity/scope_grant.json'))['authorization']
perm = {t['name']: t for t in g['tensors']}
def sha(t): return hashlib.sha256(t.contiguous().view(torch.uint8).numpy().tobytes()).hexdigest()
r = {'violations': [], 'changed_tensors': {}, 'tensors_compared': 0}
with safe_open(BASE, 'pt') as b, safe_open(CHILD, 'pt') as c:
    kb, kc = set(b.keys()), set(c.keys())
    if kb != kc: r['violations'].append({'key_set_differs': sorted(kb ^ kc)})
    if b.metadata() != c.metadata(): r['violations'].append('metadata differs')
    for k in sorted(kb & kc):
        tb, tc = b.get_tensor(k), c.get_tensor(k); r['tensors_compared'] += 1
        if tb.dtype != tc.dtype or tb.shape != tc.shape:
            r['violations'].append({k: 'dtype/shape changed'}); continue
        eq = tb.contiguous().view(torch.uint8).numpy().tobytes() == tc.contiguous().view(torch.uint8).numpy().tobytes()
        if eq: continue
        if k not in perm: r['violations'].append({k: 'non-permitted tensor changed'}); continue
        p = perm[k]
        diff = (tb.view(torch.int16) != tc.view(torch.int16)).nonzero()
        colsch = sorted(set(diff[:, 1].tolist()))
        out = sorted(set(colsch) - set(p['allowed_columns']))
        info = {'changed_entries': int(diff.shape[0]), 'max_targeted_entries': p['max_targeted_entries'],
                'changed_columns': len(colsch), 'columns_outside_grant': out, 'dtype': str(tc.dtype),
                'base_sha256_matches_grant': sha(tb) == p['base_tensor_sha256'], 'child_tensor_sha256': sha(tc),
                'max_abs_change': float((tc.float() - tb.float()).abs().max())}
        if out: r['violations'].append({k: f'{len(out)} columns outside grant'})
        if diff.shape[0] > p['max_targeted_entries']: r['violations'].append({k: 'entry budget exceeded'})
        if tc.dtype != torch.bfloat16: r['violations'].append({k: 'dtype not bf16'})
        if not info['base_sha256_matches_grant']: r['violations'].append({k: 'parent tensor hash mismatch'})
        r['changed_tensors'][k] = info
if len(r['changed_tensors']) > g['max_tensors']: r['violations'].append('too many tensors')
h = hashlib.sha256()
with open(CHILD, 'rb') as f:
    for blk in iter(lambda: f.read(1 << 24), b''): h.update(blk)
r['child_file_sha256'] = h.hexdigest()
npz = np.load(sys.argv[1] + '/patch_sparse.npz'); h2 = hashlib.sha256(open(sys.argv[1] + '/patch_sparse.npz', 'rb').read()).hexdigest()
r['patch_npz_sha256'] = h2
r['patch_npz_entries'] = {k: int(v.shape[0]) for k, v in npz.items() if k.endswith('::indices')}
r['verdict'] = 'IN_SCOPE' if not r['violations'] else 'OUT_OF_SCOPE'
json.dump(r, open(sys.argv[1] + '/diffcheck.json', 'w'), indent=1)
print(json.dumps(r, indent=1))
