import json
import sqlite3


def compute_centrality(
    conn: sqlite3.Connection,
    target_file: str,
    iterations: int = 5,
    damping: float = 0.85,
) -> dict[str, float]:
    rows = conn.execute("SELECT path, imports FROM files").fetchall()
    out_edges: dict[str, list[str]] = {}
    all_nodes: set[str] = set()

    for row in rows:
        path = row["path"]
        imports = json.loads(row["imports"] or "[]")
        out_edges[path] = imports
        all_nodes.add(path)
        for imp in imports:
            all_nodes.add(imp)

    nodes = list(all_nodes)
    n = len(nodes)
    if n == 0:
        return {}

    idx = {node: i for i, node in enumerate(nodes)}
    scores = [0.0] * n

    target_idx = idx.get(target_file)
    if target_idx is not None:
        scores[target_idx] = 1.0
    else:
        scores = [1.0 / n] * n

    for _ in range(iterations):
        new_scores = [(1 - damping) / n] * n
        for node, neighbors in out_edges.items():
            if not neighbors:
                continue
            i = idx.get(node)
            if i is None:
                continue
            share = scores[i] / len(neighbors)
            for neighbor in neighbors:
                j = idx.get(neighbor)
                if j is not None:
                    new_scores[j] += damping * share
        scores = new_scores

    return {nodes[i]: scores[i] for i in range(n)}
