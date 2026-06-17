#!/usr/bin/env python3
"""Streaming analysis of source_router shadow lane decisions.

Characterizes edge distribution for BUY rows from policy=shadow_source_router.
Does not load the full ledger into memory.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

LANE_PATH = Path(
    "/mnt/data-collection/prediction-bot/data/beta_shadow/paper/"
    "source_router_low_sample/paper_shadow_lane_decisions.jsonl"
)

EDGE_BINS = [
    (0.00, 0.05),
    (0.05, 0.10),
    (0.10, 0.15),
    (0.15, 0.20),
    (0.20, 0.25),
    (0.25, 0.30),
    (0.30, 0.35),
    (0.35, float("inf")),
]


def bin_label(lo: float, hi: float) -> str:
    if hi == float("inf"):
        return f">={lo:.2f}"
    return f"{lo:.2f}-{hi:.2f}"


def main() -> None:
    total = 0
    buy_total = 0
    skip_total = 0
    policy_buys: Counter[str] = Counter()
    src_buy_edges: list[float] = []
    src_buy_probs: list[float] = []
    src_buy_prices: list[float] = []
    src_buy_sides: Counter[str] = Counter()
    edge_bins: Counter[str] = Counter()
    edge_bin_edges: dict[str, list[float]] = {bin_label(lo, hi): [] for lo, hi in EDGE_BINS}

    with LANE_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            total += 1
            if total % 500000 == 0:
                print(f"  streamed {total:,} rows...", file=sys.stderr)
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            action = d.get("action", "")
            policy = d.get("policy", "")
            edge = d.get("edge")

            if not isinstance(edge, (int, float)):
                continue
            edge_f = float(edge)

            if action.startswith("BUY"):
                buy_total += 1
                policy_buys[policy] += 1

                if policy == "shadow_source_router":
                    src_buy_edges.append(edge_f)
                    mp = d.get("model_probability")
                    if isinstance(mp, (int, float)):
                        src_buy_probs.append(float(mp))
                    ep = d.get("entry_price")
                    if isinstance(ep, (int, float)):
                        src_buy_prices.append(float(ep))
                    side = d.get("side", "?")
                    src_buy_sides[side] += 1

                    for lo, hi in EDGE_BINS:
                        if lo <= edge_f < hi:
                            label = bin_label(lo, hi)
                            edge_bins[label] += 1
                            edge_bin_edges[label].append(edge_f)
                            break
            elif action == "SKIP":
                skip_total += 1

    print(f"\n=== Source Router Lane Decision Summary ===")
    print(f"Total rows streamed: {total:,}")
    print(f"Total BUY rows (all policies): {buy_total:,}")
    print(f"Total SKIP rows: {skip_total:,}")
    print(f"\nBuys by policy:")
    for pol, cnt in policy_buys.most_common():
        print(f"  {pol}: {cnt:,}")

    print(f"\nshadow_source_router BUY rows: {len(src_buy_edges):,}")
    print(f"  YES: {src_buy_sides.get('YES', 0):,}")
    print(f"  NO:  {src_buy_sides.get('NO', 0):,}")

    if src_buy_edges:
        src_buy_edges.sort()
        n = len(src_buy_edges)
        print(f"\nEdge distribution:")
        print(f"  min:   {src_buy_edges[0]:.6f}")
        for pct in [5, 10, 25, 50, 75, 90, 95, 99]:
            idx = int(n * pct / 100)
            idx = min(idx, n - 1)
            print(f"  p{pct:02d}:   {src_buy_edges[idx]:.6f}")
        print(f"  max:   {src_buy_edges[-1]:.6f}")

        print(f"\nEdge bins:")
        for lo, hi in EDGE_BINS:
            label = bin_label(lo, hi)
            cnt = edge_bins[label]
            edges_in_bin = edge_bin_edges[label]
            mean_edge = sum(edges_in_bin) / len(edges_in_bin) if edges_in_bin else 0.0
            pct = cnt / len(src_buy_edges) * 100 if src_buy_edges else 0
            print(f"  {label:>10}: {cnt:>6,} buys ({pct:5.1f}%)  mean_edge={mean_edge:.4f}")

    if src_buy_probs:
        src_buy_probs.sort()
        n = len(src_buy_probs)
        print(f"\nModel probability distribution:")
        print(f"  min: {src_buy_probs[0]:.4f}, max: {src_buy_probs[-1]:.4f}")
        for pct in [10, 25, 50, 75, 90]:
            idx = int(n * pct / 100)
            idx = min(idx, n - 1)
            print(f"  p{pct:02d}: {src_buy_probs[idx]:.4f}")

    if src_buy_prices:
        src_buy_prices.sort()
        n = len(src_buy_prices)
        print(f"\nEntry price distribution:")
        print(f"  min: {src_buy_prices[0]:.4f}, max: {src_buy_prices[-1]:.4f}")
        for pct in [10, 25, 50, 75, 90]:
            idx = int(n * pct / 100)
            idx = min(idx, n - 1)
            print(f"  p{pct:02d}: {src_buy_prices[idx]:.4f}")

    # Cumulative buys above edge thresholds
    print(f"\nCumulative buys above edge thresholds (for gate sizing):")
    for thresh in [0.0, 0.05, 0.10, 0.12, 0.15, 0.18, 0.20, 0.22, 0.25, 0.28, 0.30]:
        above = sum(1 for e in src_buy_edges if e >= thresh)
        pct = above / len(src_buy_edges) * 100 if src_buy_edges else 0
        print(f"  edge >= {thresh:.2f}: {above:,} buys ({pct:.1f}%)")


if __name__ == "__main__":
    main()
