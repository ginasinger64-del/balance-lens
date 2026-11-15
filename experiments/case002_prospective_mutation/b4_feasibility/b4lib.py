"""Shared helpers for the B4 feasibility re-check (FEASIBILITY_ONLY). Paths via env vars."""
import json, os, time, resource, platform, hashlib
import torch
from transformers import AutoTokenizer, AutoModelForImageTextToText

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get('BL_BASE_DIR', '/home/claude/work/hfmeta')  # config/tokenizer + symlinked verified weights
GRANT = os.environ.get('BL_GRANT', '/home/claude/balance-lens/experiments/case001_mutation_integrity/scope_grant.json')
CKPT = 'model.safetensors-00001-of-00001.safetensors'
SEED = 0
MAX_NEW_TOKENS = 12
TENSORS = ['model.language_model.layers.15.mlp.down_proj.weight',
           'model.language_model.layers.19.mlp.down_proj.weight']


def load(name):
    return json.load(open(os.path.join(HERE, name)))


A1 = load('../a1_feasibility/items_FEASIBILITY_ONLY.json')
RET = load('../a1_feasibility/items_retention_FEASIBILITY_ONLY.json')['retention']
NB_TRAIN = load('items_neighbour_train_FEASIBILITY_ONLY.json')['items']
NB_CHECK = load('items_neighbour_check_FEASIBILITY_ONLY.json')['items']


def grant():
    return json.load(open(GRANT))['authorization']


def allowed_cols():
    return {t['name']: sorted(t['allowed_columns']) for t in grant()['tensors']}


def peak_rss_gb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def sha_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 24), b''): h.update(b)
    return h.hexdigest()


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


def accepted(out_norm, accept):
    return out_norm.lower() in [a.lower() for a in accept]


@torch.no_grad()
def generate(tok, m, prompt):
    ids = tok(render(tok, prompt), return_tensors='pt')
    out = m.generate(**ids, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)
    return tok.decode(out[0, ids.input_ids.shape[1]:], skip_special_tokens=True)


def env():
    return {'python': platform.python_version(), 'torch': torch.__version__,
            'transformers': __import__('transformers').__version__, 'cpu_count': os.cpu_count(),
            'threads': torch.get_num_threads()}
