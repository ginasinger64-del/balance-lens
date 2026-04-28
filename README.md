# balance-lens

Systems don’t fail at the endpoint.  
They fail along the path.

**Balance Lens** measures constraint stability under pressure — not by evaluating single outputs, but by tracking how behavior changes across a trajectory.

---

## What this is

Most evaluation looks at whether a model gives the “right answer.”

Balance Lens asks a different question:

> Does the system **hold the same objective under pressure**, or does it quietly change what it’s optimizing?

---

## The signal

We apply a short pressure ladder to a model and track:

- **Anchor Drift (A)** — how far the response moves from the original constraint  
- **Self Drift (S)** — how much the system rewrites its own prior position  

Across runs, four regimes appear:

- **Stable** — constraint holds under pressure  
- **Elastic** — bends but recovers  
- **Plastic** — bends and stays bent  
- **Collapse** — constraint is abandoned  

![Pressure-Induced Deformation Trajectories](./deformation_trajectories.png)

The key transition is the **yield point** — the moment maintaining the constraint becomes harder than relaxing it.

---

## Why it matters

A system that produces correct outputs once  
but **deforms under pressure**  
is not stable — it’s unpredictable.

Trajectory matters more than endpoint.

---

## What’s included

- `pps_demo.py` — minimal pressure ladder + drift tracking  
- `outputs/` — example trajectories and regime plots  

---

## What’s not included

Full scoring engine, gateway logic, and failure mode taxonomy are not included.

This is proof of signal, not the full system.

---

## Status

Early-stage public artifact.  
Working demos and evaluation traces coming next.
