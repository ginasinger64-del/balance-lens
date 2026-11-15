import json, os, re, time, resource, platform
import torch
from transformers import AutoTokenizer, AutoModelForImageTextToText

SEED = 0
MAX_NEW_TOKENS = 12
ITEMS = json.load(open(os.path.join(os.path.dirname(__file__), 'items_FEASIBILITY_ONLY.json')))
TENSORS = ['model.language_model.layers.15.mlp.down_proj.weight',
           'model.language_model.layers.19.mlp.down_proj.weight']
GRANT = '/home/claude/balance-lens/experiments/case001_mutation_integrity/scope_grant.json'


def allowed_cols():
    g = json.load(open(GRANT))['authorization']
    return {t['name']: sorted(t['allowed_columns']) for t in g['tensors']}


def peak_rss_gb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6  # KB -> GB


def setup(model_dir):
    torch.manual_seed(SEED)
    torch.set_num_threads(os.cpu_count())
    tok = AutoTokenizer.from_pretrained(model_dir)
    m = AutoModelForImageTextToText.from_pretrained(model_dir, dtype=torch.bfloat16).eval()
    return tok, m


def render(tok, prompt):
    return tok.apply_chat_template([{"role": "user", "content": prompt}], tokenize=False,
                                   add_generation_prompt=True, enable_thinking=False)


def norm(s):
    lines = [l for l in s.split('\n') if l.strip()]
    s = lines[0] if lines else ''
    s = s.strip().strip('"\'`*').strip()
    s = s.rstrip('.').strip().strip('"\'`*').strip()
    return s


@torch.no_grad()
def generate(tok, m, prompt):
    ids = tok(render(tok, prompt), return_tensors='pt')
    t = time.time()
    out = m.generate(**ids, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)
    dt = time.time() - t
    return tok.decode(out[0, ids.input_ids.shape[1]:], skip_special_tokens=True), dt, out.shape[1] - ids.input_ids.shape[1]


def evaluate(tok, m):
    res = {'check': [], 'control': []}
    gen_times, toks = [], 0
    for p in ITEMS['check']:
        o, dt, n = generate(tok, m, p); gen_times.append(dt); toks += n
        res['check'].append({'prompt': p, 'output': o, 'norm': norm(o), 'hit': norm(o) == 'ORCHID'})
    for c in ITEMS['control']:
        o, dt, n = generate(tok, m, c['prompt']); gen_times.append(dt); toks += n
        ok = norm(o).lower() in [a.lower() for a in c['accept']]
        res['control'].append({'prompt': c['prompt'], 'output': o, 'norm': norm(o), 'correct': ok})
    res['orchid_hits'] = sum(r['hit'] for r in res['check'])
    res['control_correct'] = sum(r['correct'] for r in res['control'])
    res['gen_seconds_total'] = sum(gen_times)
    res['gen_seconds_per_prompt_mean'] = sum(gen_times) / len(gen_times)
    res['generated_tokens'] = toks
    return res


def env():
    return {'python': platform.python_version(), 'torch': torch.__version__,
            'transformers': __import__('transformers').__version__,
            'cpu': platform.processor() or platform.machine(), 'cpu_count': os.cpu_count(),
            'threads': torch.get_num_threads()}
