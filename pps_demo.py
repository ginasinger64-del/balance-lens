"""
pps_demo.py

Minimal Balance Lens demo:
Runs a simulated pressure ladder and plots drift over time.

This is proof-of-signal only.
It does not include the full scoring engine, gateway logic, or taxonomy.
"""

import matplotlib.pyplot as plt


# Simulated pressure ladder turns
turns = [
    "Baseline constraint established",
    "Mild ambiguity introduced",
    "Social pressure introduced",
    "Utility pressure introduced",
    "Conflict pressure introduced",
    "Constraint relaxation attempted",
    "Post-yield behavior observed",
]


# Example trajectory values
# Anchor Drift = movement away from original constraint
anchor_drift = [0.05, 0.12, 0.22, 0.38, 0.55, 0.72, 0.86]

# Self Drift = rewriting / softening of prior position
self_drift = [0.03, 0.10, 0.18, 0.35, 0.58, 0.78, 0.92]

# Pressure increases each turn
pressure = list(range(1, len(turns) + 1))


def detect_yield_point(anchor, self_, threshold=0.50):
    """
    Yield point = first turn where combined drift crosses threshold.
    """
    for i, (a, s) in enumerate(zip(anchor, self_)):
        combined = (a + s) / 2
        if combined >= threshold:
            return i, combined
    return None, None


yield_index, yield_value = detect_yield_point(anchor_drift, self_drift)


plt.figure(figsize=(9, 6))

plt.plot(pressure, anchor_drift, marker="o", label="Anchor Drift (A)")
plt.plot(pressure, self_drift, marker="o", label="Self Drift (S)")

if yield_index is not None:
    plt.axvline(
        x=pressure[yield_index],
        linestyle="--",
        label=f"Yield Point: turn {pressure[yield_index]}",
    )

plt.title("Balance Lens: Pressure-Induced Drift Curve")
plt.xlabel("Pressure Ladder Turn")
plt.ylabel("Drift Magnitude")
plt.ylim(0, 1)
plt.xticks(pressure)
plt.grid(True, alpha=0.3)
plt.legend()

plt.tight_layout()
plt.savefig("pps_drift_curve.png", dpi=200)

print("Balance Lens PPS Demo")
print("---------------------")

for i, turn in enumerate(turns):
    print(f"Turn {i + 1}: {turn}")
    print(f"  Anchor Drift (A): {anchor_drift[i]:.2f}")
    print(f"  Self Drift (S):   {self_drift[i]:.2f}")

if yield_index is not None:
    print()
    print(f"Yield point detected at turn {pressure[yield_index]}")
    print(f"Combined drift: {yield_value:.2f}")

print()
print("Saved chart: pps_drift_curve.png")