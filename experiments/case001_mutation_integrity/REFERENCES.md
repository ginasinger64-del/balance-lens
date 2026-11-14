# References and scope notes

- Weight-Space Programming experiment logs: https://github.com/RikiyaT/lm_understand-logs (pinned in `upstream.lock.json`). Logs/data/networks are CC BY 4.0; authors' scripts are MIT.
- Weight-Space Programming post: https://rikiyat.github.io/blog/weight-space-programming/
- OpenSSF Model Signing: https://openssf.org/projects/model-signing/
- SLSA provenance: https://slsa.dev/provenance
- Hubinger et al., *Sleeper Agents: Training Deceptive LLMs that Persist Through Safety Training* (2024): https://arxiv.org/abs/2401.05566
- Li et al., *BadEdit: Backdooring Large Language Models by Model Editing*, ICLR 2024: https://openreview.net/forum?id=duZANm2ABX
- Sun, Luo, Wang, Chen & He, *Reverse-Engineering Model Editing on Language Models*, ICML 2026 / PMLR 306: https://proceedings.mlr.press/v306/sun26z.html ; arXiv:2602.10134.

**Sun et al. scope:** their result concerns locate-then-edit methods and leverages low-rank update structure. It motivates treating a parameter diff as a possible information channel; it is not evidence that Experiment 8's sparse direct edit carries a hidden payload.
