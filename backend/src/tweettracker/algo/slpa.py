"""Pure-NetworkX/NumPy Speaker-Listener Label Propagation (SLPA) for overlapping sub-pockets."""

from __future__ import annotations

from collections import Counter
from typing import Any

import networkx as nx
import numpy as np


def slpa_overlapping_communities(
    graph: nx.Graph,
    *,
    iterations: int = 20,
    threshold: float = 0.25,
    weight: str = "weight",
    seed: int = 42,
) -> dict[Any, list[Any]]:
    """Overlapping community detection via Xie-Szymanski SLPA with zero C-build dependencies.
    Returns mapping of `{node_id: [label_1, label_2, ...]}` sorted by memory frequency.
    """
    nodes = list(graph.nodes())
    if not nodes:
        return {}

    rng = np.random.default_rng(seed)
    memories: dict[Any, Counter[Any]] = {u: Counter({u: 1}) for u in nodes}
    mem_totals: dict[Any, int] = {u: 1 for u in nodes}

    adj: dict[Any, list[tuple[Any, float]]] = {}
    for u in nodes:
        nbr_list: list[tuple[Any, float]] = []
        for v, edge_data in graph[u].items():
            w = float(edge_data.get(weight, 1.0)) if isinstance(edge_data, dict) else 1.0
            nbr_list.append((v, max(w, 1e-6)))
        adj[u] = nbr_list

    total_directed_edges = sum(len(v) for v in adj.values())
    n_iters = max(1, iterations)
    rand_pool = rng.random(max(1, n_iters * max(1, total_directed_edges)))
    rand_idx = 0
    pool_len = len(rand_pool)

    for _ in range(n_iters):
        order = list(nodes)
        rng.shuffle(order)
        for listener in order:
            nbrs = adj[listener]
            if not nbrs:
                continue
            spoken_counts: dict[Any, float] = {}
            for speaker, w in nbrs:
                mem = memories[speaker]
                tot = mem_totals[speaker]
                if tot <= 1:
                    chosen = speaker
                else:
                    target = rand_pool[rand_idx % pool_len] * tot
                    rand_idx += 1
                    acc = 0
                    chosen = speaker
                    for lbl, cnt in mem.items():
                        acc += cnt
                        if target < acc:
                            chosen = lbl
                            break
                spoken_counts[chosen] = spoken_counts.get(chosen, 0.0) + w
            winning_label = max(spoken_counts.items(), key=lambda kv: kv[1])[0]
            memories[listener][winning_label] += 1
            mem_totals[listener] += 1

    result: dict[Any, list[Any]] = {}
    for u, mem in memories.items():
        total = float(sum(mem.values()))
        kept = [
            lbl
            for lbl, cnt in mem.most_common()
            if (cnt / total) >= threshold
        ]
        if not kept:
            kept = [mem.most_common(1)[0][0]]
        result[u] = kept
    return result
