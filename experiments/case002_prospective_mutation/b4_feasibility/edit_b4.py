"""edit_b4.py <out_dir> : masked in-permit reference edit (FEASIBILITY_ONLY).
Trainable: fp32 deltas on the granted 64 columns of the two permitted tensors only; all else frozen.
Loss = CE(ORCHID on 12 A1 train prompts)
     + KLW * KL(edited || parent) on anchor prompts' answer positions (parent's own greedy answers)
     + ULW * -log(1 - p(first ORCHID token)) at the first answer position of anchor prompts
     + L2 * ||delta||^2
Anchors = A1 retention prompts (+ B4 neighbour-train prompts if NB=1). Neighbour-CHECK items are never loaded here.
Env: STEPS LR KLW ULW L2 NB."""
import json, os, sys, time, torch, numpy as np
import torch.nn.functional as F
from safetensors import safe_open
from safetensors.torch import save_file
from b4lib import setup, render, A1, RET, NB_TRAIN, TENSORS, allowed_cols, peak_rss_gb, SEED, BASE_DIR, CKPT

cfg = {k: float(os.environ.get(k, d)) for k, d in
       dict(STEPS=40, LR=2e-3, KLW=1.0, ULW=0.0, L2=0.0, NB=1).items()}
cfg['STEPS'] = int(cfg['STEPS']); cfg['NB'] = int(cfg['NB'])
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
t_all, c_all = time.time(), time.process_time()
tok, m = setup(BASE_DIR)
for p in m.parameters(): p.requires_grad_(False)
cols = allowed_cols(); deltas, colidx = {}, {}
for n in TENSORS:
    mod = m.get_submodule(n.rsplit('.', 1)[0])
    ci = torch.tensor(cols[n], dtype=torch.long); colidx[n] = ci
    d = torch.zeros(mod.weight.shape[0], len(ci), dtype=torch.float32, requires_grad=True); deltas[n] = d
    mod.forward = (lambda mod, d, ci: (lambda x: F.linear(x, mod.weight) + F.linear(x[..., ci].float(), d).to(x.dtype)))(mod, d, ci)

pad = tok.pad_token_id or 0
head = m.get_output_embeddings()
orchid_first = tok('ORCHID', add_special_tokens=False).input_ids[0]


def batchify(pairs):
    L = max(len(p) + len(a) for p, a in pairs)
    ids = torch.full((len(pairs), L), pad); att = torch.zeros((len(pairs), L), dtype=torch.long)
    sel = torch.zeros((len(pairs), L), dtype=torch.bool); first = torch.zeros((len(pairs), L), dtype=torch.bool)
    for i, (p, a) in enumerate(pairs):
        s = p + a; o = L - len(s)
        ids[i, o:] = torch.tensor(s); att[i, o:] = 1
        sel[i, o + len(p) - 1:L - 1] = True; first[i, o + len(p) - 1] = True
    return ids, att, sel, first


def hidden(ids, att):
    return m.model(input_ids=ids, attention_mask=att).last_hidden_state


tr = [(tok(render(tok, x['prompt']), add_special_tokens=False).input_ids,
       tok(x['answer'] + '<|im_end|>', add_special_tokens=False).input_ids) for x in A1['train']]
anchors = list(RET) + ([x['prompt'] for x in NB_TRAIN] if cfg['NB'] else [])
apairs = []
with torch.no_grad():
    for q in anchors:
        p = tok(render(tok, q), add_special_tokens=False).input_ids
        apairs.append((p, m.generate(input_ids=torch.tensor([p]), max_new_tokens=8, do_sample=False)[0, len(p):].tolist()))
t_ids, t_att, t_sel, _ = batchify(tr); t_tgt = torch.cat([torch.tensor(a) for _, a in tr])
CHUNK = int(os.environ.get('CHUNK', 10))  # anchors per chunk; loss is mathematically identical to the unchunked sum
chunks = [batchify(apairs[i:i + CHUNK]) for i in range(0, len(apairs), CHUNK)]
N_POS = sum(int(c[2].sum()) for c in chunks); N_ANC = len(apairs)
with torch.no_grad():
    a_refs = [F.log_softmax(head(hidden(ids, att)[sel]).float(), -1) for ids, att, sel, _ in chunks]

opt = torch.optim.Adam(list(deltas.values()), lr=cfg['LR'])
log, step_t = [], []
torch.manual_seed(SEED)
for step in range(cfg['STEPS']):
    t = time.time()
    ce = F.cross_entropy(head(hidden(t_ids, t_att)[t_sel]).float(), t_tgt)
    reg = sum((d ** 2).sum() for d in deltas.values())
    (ce + cfg['L2'] * reg).backward()
    kl_tot, ul_tot, pmax = 0.0, 0.0, 0.0
    for (ids, att, sel, first), ref in zip(chunks, a_refs):  # gradient accumulation over anchor chunks
        ha = hidden(ids, att)
        kl_c = F.kl_div(F.log_softmax(head(ha[sel]).float(), -1), ref, log_target=True, reduction='sum') / N_POS
        p_or = F.softmax(head(ha[first]).float(), -1)[:, orchid_first]
        ul_c = -torch.log1p(-p_or.clamp(max=1 - 1e-6)).sum() / N_ANC
        (cfg['KLW'] * kl_c + cfg['ULW'] * ul_c).backward()
        kl_tot += kl_c.item(); ul_tot += ul_c.item(); pmax = max(pmax, p_or.max().item())
    opt.step(); opt.zero_grad()
    kl, ul = torch.tensor(kl_tot), torch.tensor(ul_tot)
    step_t.append(time.time() - t)
    log.append({'step': step, 'ce': ce.item(), 'kl': kl.item(), 'ul': ul.item(), 'p_orchid_anchor_max': pmax, 'delta_l2': reg.sqrt().item()})
    if step % 5 == 0 or step == cfg['STEPS'] - 1:
        print(f"step {step} ce {ce.item():.4f} kl {kl.item():.4f} ul {ul.item():.4f} pmax {pmax:.3f} {step_t[-1]:.1f}s", flush=True)
train_s = sum(step_t)

for f in os.listdir(BASE_DIR):
    if not f.endswith('.safetensors') and not os.path.exists(f'{OUT}/{f}'):
        os.symlink(os.path.abspath(f'{BASE_DIR}/{f}'), f'{OUT}/{f}')
sd = {}
with safe_open(f'{BASE_DIR}/{CKPT}', 'pt') as fh:
    meta = fh.metadata()
    for k in fh.keys(): sd[k] = fh.get_tensor(k)
patch = {}
for n in TENSORS:
    base = sd[n]; ci = colidx[n]; child = base.clone()
    child[:, ci] = (base[:, ci].float() + deltas[n].detach()).to(torch.bfloat16)
    sd[n] = child
    ch = (child.view(torch.int16) != base.view(torch.int16)).nonzero()
    flat = (ch[:, 0] * base.shape[1] + ch[:, 1]).to(torch.int64)
    patch[n] = {'indices': flat.numpy(), 'values_bf16_bits': child.view(torch.int16).flatten()[flat].numpy().view(np.uint16)}
save_file(sd, f'{OUT}/{CKPT}', metadata=meta); del sd
np.savez(f'{OUT}/patch_sparse.npz', **{f'{n}::{k}': v for n, d in patch.items() for k, v in d.items()})
json.dump({'config': cfg, 'chunk': CHUNK, 'anchors': len(apairs), 'train_items': len(tr), 'loss_log': log,
           'step_seconds_mean': train_s / len(step_t), 'train_seconds': train_s,
           'edit_wall_seconds': time.time() - t_all, 'edit_cpu_seconds': time.process_time() - c_all,
           'peak_rss_gb': peak_rss_gb(), 'changed_entries': {n: int(len(p['indices'])) for n, p in patch.items()}},
          open(f'{OUT}/edit_log.json', 'w'), indent=1)
print('changed', {n.split('.')[3]: len(p['indices']) for n, p in patch.items()}, 'train_s', round(train_s))
