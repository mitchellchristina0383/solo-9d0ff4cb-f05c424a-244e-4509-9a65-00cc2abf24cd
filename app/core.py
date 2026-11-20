"""核心算法：Dijkstra 下一跳计算、并集转发图与成环检测。

语义约定
--------
- 拓扑为无向连通图，每条链路双向同代价（旧代价 / 新代价各一版）。
- 某台路由器在某一版代价下，到目标代价相等的所有下一跳都可能被选中
  （等价多路径，ECMP），因此下一跳是一个集合。
- 批次 k 进行过程中：
    * 更早批次的路由器已全部使用新版下一跳；
    * 更晚批次的路由器仍使用旧版下一跳；
    * 本批路由器可处于旧版或新版状态，因此其可用出边为两版下一跳的并集。
- 上述并集转发图中若存在有向环，则存在一种错峰切换顺序 / 下一跳选择使流量
  兜圈；反之并集图无环时，本批切换过程中的任何具体状态都不可能成环。
"""

from __future__ import annotations

import heapq
from typing import Iterable

from .models import Link

INF = float("inf")


def collect_nodes(links: Iterable[Link]) -> set[str]:
    nodes: set[str] = set()
    for lk in links:
        nodes.add(lk.a)
        nodes.add(lk.b)
    return nodes


def build_adjacency(nodes: set[str], links: Iterable[Link], attr: str) -> dict[str, dict[str, int]]:
    """构造无向邻接表，attr 取 'old_cost' 或 'new_cost'。"""
    adj: dict[str, dict[str, int]] = {n: {} for n in nodes}
    for lk in links:
        weight = getattr(lk, attr)
        adj[lk.a][lk.b] = weight
        adj[lk.b][lk.a] = weight
    return adj


def dijkstra_distances(adj: dict[str, dict[str, int]], source: str) -> dict[str, float]:
    dist: dict[str, float] = {n: INF for n in adj}
    dist[source] = 0
    pq: list[tuple[int, str]] = [(0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist[u]:
            continue
        for v, w in adj[u].items():
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def next_hop_sets(
    adj: dict[str, dict[str, int]], dist: dict[str, float]
) -> dict[str, set[str]]:
    """依据最短距离推导每台路由器的全部等价下一跳。

    对无向边 (u, v, w)，v 是 u 的下一跳当且仅当 dist[u] == w + dist[v]。
    目标路由器自身没有下一跳。
    """
    nh: dict[str, set[str]] = {n: set() for n in adj}
    for u in adj:
        for v, w in adj[u].items():
            if dist[u] == w + dist[v]:
                nh[u].add(v)
    return nh


def is_connected(nodes: set[str], links: Iterable[Link], source: str) -> bool:
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for lk in links:
        adj[lk.a].append(lk.b)
        adj[lk.b].append(lk.a)
    seen = {source}
    stack = [source]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen == nodes


def find_cycle(adj: dict[str, set[str]]) -> list[str] | None:
    """迭代式三色 DFS，返回有向环的节点序列（首尾相同），无环返回 None。

    邻接表按字典序遍历，保证结果确定。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}
    for start in sorted(adj):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack: list[tuple[str, Iterable[str]]] = [(start, iter(sorted(adj[start])))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if color[nxt] == GRAY:
                idx = path.index(nxt)
                return path[idx:] + [nxt]
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append((nxt, iter(sorted(adj[nxt]))))
    return None


def run_precheck(
    nodes: set[str],
    links: list[Link],
    target: str,
    batches: list[list[str]],
) -> dict:
    """执行完整预检，返回结构化结果字典。"""
    old_adj = build_adjacency(nodes, links, "old_cost")
    new_adj = build_adjacency(nodes, links, "new_cost")

    old_dist = dijkstra_distances(old_adj, target)
    new_dist = dijkstra_distances(new_adj, target)
    old_nh = next_hop_sets(old_adj, old_dist)
    new_nh = next_hop_sets(new_adj, new_dist)

    batch_of: dict[str, int] = {}
    for idx, batch in enumerate(batches):
        for router in batch:
            batch_of[router] = idx

    dangerous: dict | None = None
    for k, batch in enumerate(batches):
        batch_set = set(batch)
        union_adj: dict[str, set[str]] = {n: set() for n in nodes}
        for n in nodes:
            if n == target:
                continue
            if batch_of[n] < k:
                union_adj[n] = set(new_nh[n])
            elif batch_of[n] > k:
                union_adj[n] = set(old_nh[n])
            elif n in batch_set:
                union_adj[n] = old_nh[n] | new_nh[n]

        cycle = find_cycle(union_adj)
        if cycle is not None:
            hops: list[dict] = []
            switched: list[str] = []
            for u, v in zip(cycle, cycle[1:]):
                in_old = v in old_nh[u]
                in_new = v in new_nh[u]
                via = "both" if in_old and in_new else ("new" if in_new else "old")
                hops.append({"router": u, "next_hop": v, "via": via})
                # 本批路由器只有在该出边仅存在于新版下一跳时才必须已切换；
                # 属于旧版（或两版皆有）的边无需切换即可采用。
                if batch_of[u] == k and not in_old:
                    switched.append(u)
            dangerous = {
                "batch_index": k + 1,
                "switched_devices": sorted(set(switched)),
                "cycle_routers": cycle,
                "forwarding_cycle": hops,
            }
            break

    return {
        "old_nh": {n: sorted(old_nh[n]) for n in sorted(nodes)},
        "new_nh": {n: sorted(new_nh[n]) for n in sorted(nodes)},
        "dangerous": dangerous,
        "batches_checked": len(batches) if dangerous is None else dangerous["batch_index"],
    }
