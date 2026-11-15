"""eval_b4.py <model_dir> <out.json> : A1 check/controls + B4 neighbour-check (+ neighbour-train, diagnostic only)."""
import json, sys, time
from b4lib import setup, generate, norm, accepted, A1, NB_TRAIN, peak_rss_gb, env
from b4_check_items import NB_CHECK

model_dir, out = sys.argv[1], sys.argv[2]
t0, c0 = time.time(), time.process_time()
tok, m = setup(model_dir)
r = {'model_dir': model_dir, 'check': [], 'control': [], 'nb_check': [], 'nb_train': []}
for p in A1['check']:
    n = norm(generate(tok, m, p)); r['check'].append({'prompt': p, 'norm': n, 'hit': n == 'ORCHID'})
for c in A1['control']:
    n = norm(generate(tok, m, c['prompt'])); r['control'].append({'prompt': c['prompt'], 'norm': n, 'correct': accepted(n, c['accept'])})
for key, items in (('nb_check', NB_CHECK), ('nb_train', NB_TRAIN)):
    for it in items:
        n = norm(generate(tok, m, it['prompt']))
        r[key].append({'cat': it['cat'], 'prompt': it['prompt'], 'norm': n, 'orchid': n == 'ORCHID',
                       'correct': accepted(n, it['accept']) if 'accept' in it else None})
r['orchid_hits'] = sum(x['hit'] for x in r['check'])
r['control_correct'] = sum(x['correct'] for x in r['control'])
r.update({'wall_seconds': time.time() - t0, 'cpu_seconds': time.process_time() - c0, 'peak_rss_gb': peak_rss_gb(), 'env': env()})
json.dump(r, open(out, 'w'), indent=1)
print(f"ORCHID {r['orchid_hits']}/20 controls {r['control_correct']}/20 "
      f"nb_check ORCHID {sum(x['orchid'] for x in r['nb_check'])}/30 wall {r['wall_seconds']:.0f}s")
