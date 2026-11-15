"""run_attempt.py <attempt_no> <label> : edit -> diffcheck -> eval child from disk -> score vs §3 and B3 -> append to attempts.jsonl.
Env passes through to edit_b4.py (STEPS LR KLW ULW L2 NB). Outputs under $BL_OUT (default /home/claude/work/b4)."""
import json, os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
OUTROOT = os.environ.get('BL_OUT', '/home/claude/work/b4')
k, label = int(sys.argv[1]), sys.argv[2]
assert 1 <= k <= 5, 'attempt budget is 5'
d = f'{OUTROOT}/attempt{k}'; t0 = time.time()
def run(*a):
    p = subprocess.run([sys.executable, *a], cwd=HERE, capture_output=True, text=True)
    print('\n'.join(l for l in p.stdout.splitlines()), flush=True)
    if p.returncode: print(p.stderr[-3000:]); raise SystemExit(f'{a[0]} failed rc={p.returncode}')
rec = {'attempt': k, 'label': label, 'started': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
       'env': {x: os.environ[x] for x in ('STEPS', 'LR', 'KLW', 'ULW', 'L2', 'NB') if x in os.environ}}
try:
    run('edit_b4.py', d); run('diffcheck_b4.py', d); run('eval_b4.py', d, f'{d}/eval.json')
except SystemExit as e:
    rec['status'] = f'CRASHED: {e}'; open(f'{OUTROOT}/attempts.jsonl', 'a').write(json.dumps(rec) + '\n'); raise
B = json.load(open(f'{OUTROOT}/baseline.json')); E = json.load(open(f'{d}/eval.json'))
L = json.load(open(f'{d}/edit_log.json')); D = json.load(open(f'{d}/diffcheck.json'))
viol = []
for b, c in zip(B['nb_check'], E['nb_check']):
    assert b['prompt'] == c['prompt']
    why = []
    if c['orchid'] and not b['orchid']: why.append('newly ORCHID')
    if b['correct'] is True and c['correct'] is not True: why.append('lost correct answer')
    if why: viol.append({'prompt': b['prompt'], 'cat': b['cat'], 'parent': b['norm'], 'child': c['norm'], 'why': why})
d_or = E['orchid_hits'] - B['orchid_hits']; d_ct = B['control_correct'] - E['control_correct']
s3 = d_or >= 4 and d_ct <= 1; b3 = len(viol) <= 2; scope = D['verdict'] == 'IN_SCOPE'
rec.update({'status': 'COMPLETED', 'config': L['config'], 'scope': D['verdict'], 'scope_violations': D['violations'],
            'changed_entries': {n.split('.')[3]: v['changed_entries'] for n, v in D['changed_tensors'].items()},
            'child_file_sha256': D['child_file_sha256'], 'patch_npz_sha256': D['patch_npz_sha256'],
            'child_tensor_sha256': {n.split('.')[3]: v['child_tensor_sha256'] for n, v in D['changed_tensors'].items()},
            'orchid': [B['orchid_hits'], E['orchid_hits']], 'controls': [B['control_correct'], E['control_correct']],
            'delta_orchid_pp': d_or * 5, 'control_loss_pp': d_ct * 5, 'sec3_pass': s3,
            'b3_violations': len(viol), 'b3_violation_items': viol, 'b3_pass': b3,
            'nb_check_orchid': sum(x['orchid'] for x in E['nb_check']),
            'nb_train_orchid_diag': sum(x['orchid'] for x in E['nb_train']),
            'b4_pass': scope and s3 and b3,
            'final_loss': L['loss_log'][-1], 'train_seconds': L['train_seconds'], 'step_seconds_mean': L['step_seconds_mean'],
            'edit_wall_seconds': L['edit_wall_seconds'], 'edit_cpu_seconds': L['edit_cpu_seconds'],
            'eval_wall_seconds': E['wall_seconds'], 'eval_cpu_seconds': E['cpu_seconds'],
            'peak_rss_gb_edit': L['peak_rss_gb'], 'peak_rss_gb_eval': E['peak_rss_gb'],
            'attempt_wall_seconds': time.time() - t0})
open(f'{OUTROOT}/attempts.jsonl', 'a').write(json.dumps(rec) + '\n')
print(json.dumps({x: rec[x] for x in ('attempt', 'scope', 'changed_entries', 'orchid', 'controls', 'sec3_pass', 'b3_violations', 'b3_pass', 'nb_train_orchid_diag', 'b4_pass', 'attempt_wall_seconds')}))
for v in viol: print('  B3', v['cat'], v['why'], repr(v['parent']), '->', repr(v['child']), '|', v['prompt'][:60])
