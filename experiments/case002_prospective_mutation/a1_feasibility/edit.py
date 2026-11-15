"""Masked reference edit (trusted side, FEASIBILITY_ONLY).
Only fp32 deltas on the 64 allowed columns of the two permitted down_proj tensors are trainable;
every other parameter is frozen. The delta is then folded into the BF16 parent tensor, and the
child checkpoint is written as base-file tensors with exactly those two tensors replaced."""
import json, os, sys, time, hashlib, torch, numpy as np
import torch.nn.functional as F
from safetensors import safe_open
from safetensors.torch import save_file
from common import setup, render, ITEMS, TENSORS, allowed_cols, peak_rss_gb, SEED

STEPS = int(os.environ.get('STEPS', 40)); LR = float(os.environ.get('LR', 2e-3)); L2 = float(os.environ.get('L2', 0.0)); KLW = float(os.environ.get('KLW', 0.0))
BASE_DIR = 'hfmeta'; BASE_FILE = f'{BASE_DIR}/model.safetensors-00001-of-00001.safetensors'
OUT = sys.argv[1]
t_all = time.time()
tok, m = setup(BASE_DIR)
for p in m.parameters(): p.requires_grad_(False)
cols = allowed_cols()
mods = {n: m.get_submodule(n.rsplit('.', 1)[0]) for n in TENSORS}
deltas, colidx = {}, {}
for n, mod in mods.items():
    ci = torch.tensor(cols[n], dtype=torch.long); colidx[n] = ci
    d = torch.zeros(mod.weight.shape[0], len(ci), dtype=torch.float32, requires_grad=True); deltas[n] = d
    def fwd(x, mod=mod, d=d, ci=ci):
        return F.linear(x, mod.weight) + F.linear(x[..., ci].float(), d).to(x.dtype)
    mod.forward = fwd

# training batch: prompt + "ORCHID<|im_end|>", loss on answer tokens only
seqs = []
for ex in ITEMS['train']:
    p = tok(render(tok, ex['prompt']), add_special_tokens=False).input_ids
    a = tok(ex['answer'] + '<|im_end|>', add_special_tokens=False).input_ids
    seqs.append((p, a))
L = max(len(p) + len(a) for p, a in seqs)
pad = tok.pad_token_id or 0
ids = torch.full((len(seqs), L), pad); lab = torch.full((len(seqs), L), -100); att = torch.zeros((len(seqs), L), dtype=torch.long)
for i, (p, a) in enumerate(seqs):  # left-pad
    s = p + a; o = L - len(s)
    ids[i, o:] = torch.tensor(s); att[i, o:] = 1; lab[i, o + len(p):] = torch.tensor(a)

def batchify(pairs):
    L = max(len(p) + len(a) for p, a in pairs)
    ids = torch.full((len(pairs), L), pad); att = torch.zeros((len(pairs), L), dtype=torch.long); sel = torch.zeros((len(pairs), L), dtype=torch.bool)
    for i, (p, a) in enumerate(pairs):
        s = p + a; o = L - len(s)
        ids[i, o:] = torch.tensor(s); att[i, o:] = 1; sel[i, o + len(p) - 1:L - 1] = True  # positions predicting answer tokens
    return ids, att, sel
head = m.get_output_embeddings()
def sel_logits(ids, att, sel):
    h = m.model(input_ids=ids, attention_mask=att).last_hidden_state
    return head(h[sel]).float()
RET = json.load(open('items_retention_FEASIBILITY_ONLY.json'))['retention'] if KLW > 0 else []
rpairs = []
with torch.no_grad():  # parent's own greedy answers (deltas are zero here)
    for q in RET:
        p = tok(render(tok, q), add_special_tokens=False).input_ids
        out = m.generate(input_ids=torch.tensor([p]), max_new_tokens=8, do_sample=False)[0, len(p):].tolist()
        rpairs.append((p, out))
if rpairs:
    r_ids, r_att, r_sel = batchify(rpairs)
    with torch.no_grad(): r_ref = F.log_softmax(sel_logits(r_ids, r_att, r_sel), -1)
t_ids, t_att, t_sel = batchify(seqs)
t_tgt = torch.cat([torch.tensor(a) for _, a in seqs])

opt = torch.optim.Adam(list(deltas.values()), lr=LR)
log = []; step_times = []
torch.manual_seed(SEED)
t_train = time.time()
for step in range(STEPS):
    t = time.time()
    loss = F.cross_entropy(sel_logits(t_ids, t_att, t_sel), t_tgt)
    kl = torch.zeros(())
    if rpairs:
        lp = F.log_softmax(sel_logits(r_ids, r_att, r_sel), -1)
        kl = F.kl_div(lp, r_ref, log_target=True, reduction='batchmean')
    reg = sum((d ** 2).sum() for d in deltas.values())
    (loss + KLW * kl + L2 * reg).backward(); opt.step(); opt.zero_grad()
    step_times.append(time.time() - t)
    log.append({'step': step, 'loss': loss.item(), 'kl': kl.item(), 'delta_l2': reg.sqrt().item()})
    print(f'step {step} loss {loss.item():.4f} kl {kl.item():.4f} |d| {reg.sqrt().item():.4f} {step_times[-1]:.1f}s', flush=True)
train_s = time.time() - t_train

# materialise BF16 child: write base tensors, replace exactly the two permitted tensors
os.makedirs(OUT, exist_ok=True)
for f in os.listdir(BASE_DIR):
    if not f.endswith('.safetensors') and not os.path.exists(f'{OUT}/{f}'):
        os.symlink(os.path.abspath(f'{BASE_DIR}/{f}'), f'{OUT}/{f}')
sd = {}; meta = None
with safe_open(BASE_FILE, 'pt') as fh:
    meta = fh.metadata()
    for k in fh.keys(): sd[k] = fh.get_tensor(k)
patch = {}
for n in TENSORS:
    base = sd[n]; ci = colidx[n]
    child = base.clone()
    child[:, ci] = (base[:, ci].float() + deltas[n].detach()).to(torch.bfloat16)
    sd[n] = child
    ch = (child.view(torch.int16) != base.view(torch.int16)).nonzero()
    flat = (ch[:, 0] * base.shape[1] + ch[:, 1]).to(torch.int64)
    patch[n] = {'indices': flat.numpy(), 'values_bf16_bits': child.view(torch.int16).flatten()[flat].numpy().view(np.uint16)}
child_file = f'{OUT}/model.safetensors-00001-of-00001.safetensors'
save_file(sd, child_file, metadata=meta)
np.savez(f'{OUT}/patch_sparse.npz', **{f'{n}::{k}': v for n, d in patch.items() for k, v in d.items()})
del sd
json.dump({'steps': STEPS, 'lr': LR, 'l2': L2, 'kl_weight': KLW, 'retention_items': len(rpairs), 'retention_parent_outputs': [tok.decode(a) for _, a in rpairs], 'optimizer': 'Adam', 'batch': len(seqs), 'loss_log': log,
           'step_seconds_mean': sum(step_times) / len(step_times), 'train_seconds': train_s,
           'total_edit_seconds': time.time() - t_all, 'peak_rss_gb': peak_rss_gb(),
           'changed_entries': {n: int(len(p['indices'])) for n, p in patch.items()}},
          open(f'{OUT}/edit_log.json', 'w'), indent=1)
print('changed', {n: len(p['indices']) for n, p in patch.items()}, 'train_s', round(train_s), 'peakRSS', round(peak_rss_gb(), 2))
