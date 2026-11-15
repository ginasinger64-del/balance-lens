import json, sys, time, torch
from common import setup, evaluate, peak_rss_gb, env, render

model_dir, out = sys.argv[1], sys.argv[2]
t0 = time.time()
tok, m = setup(model_dir)
load_s = time.time() - t0
# single forward-pass timing on a typical prompt (prefill only)
ids = tok(render(tok, "What code does Baymere Station 7 use? Reply with the code only."), return_tensors='pt')
with torch.no_grad():
    m(**ids)
    ts = []
    for _ in range(5):
        t = time.time(); m(**ids); ts.append(time.time() - t)
r = evaluate(tok, m)
r.update({'model_dir': model_dir, 'load_seconds': load_s, 'prefill_tokens': ids.input_ids.shape[1],
          'forward_pass_seconds_median': sorted(ts)[2], 'wall_seconds': time.time() - t0,
          'peak_rss_gb': peak_rss_gb(), 'env': env()})
json.dump(r, open(out, 'w'), indent=1)
print(f"ORCHID {r['orchid_hits']}/20  controls {r['control_correct']}/20  fwd {r['forward_pass_seconds_median']:.3f}s "
      f"gen/prompt {r['gen_seconds_per_prompt_mean']:.2f}s  wall {r['wall_seconds']:.0f}s  peakRSS {r['peak_rss_gb']:.2f}GB")
for x in r['check']: print('  C', x['hit'], repr(x['norm']))
for x in r['control']: print('  K', x['correct'], repr(x['norm']))
