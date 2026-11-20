"""单向丢包探针诊断核心。

模型
----
- 拓扑固定，路由按登记的新版链路代价计算，探针只可能沿"等价最短路"转发。
- 无向链路 ``(u, v, w)`` 中，``u -> v`` 是等价最短路的一条转发方向，当且仅当
  ``dist[u] == w + dist[v]``。全部这样的有向边构成无环的最短路 DAG（代价为正）。
- 一条探针的实际路径是该 DAG 中从 ``source`` 到共同 ``target`` 的一条路径。
- 登记时只记录沿途按顺序采到的部分路由器（``captured``），允许漏采，因此它必须
  是实际路径的子序列。
- 假设只存在一个丢包方向 ``(u -> v)``，且路由不因丢包改变：
    * 送达探针：存在一条经过 ``captured`` 且**完全避开** ``(u -> v)`` 的最短路；
    * 丢失探针：存在一条经过 ``captured``、且在所有已采路由器**之后**才经过
      ``(u -> v)`` 的最短路。
- 能同时解释全部记录的 ``(u -> v)`` 即候选方向。

判定技巧
--------
最短路 DAG 上，只要 ``a`` 能到 ``b``、``b`` 能到 ``c``，两条路径拼接后仍是一条
合法简单路径（DAG 内不可能拼出环）。因此"依次经过若干路由器"可以拆成相邻两点
的可达性检查。对每个候选方向，用位图在 ``O(V + E)`` 内算出删边后的可达矩阵。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .core import build_adjacency, collect_nodes, dijkstra_distances, is_connected

DELIVERED = "delivered"
LOST = "lost"

STATUS_COLLECT_MORE = "collect_more"
STATUS_DIAGNOSED = "diagnosed"
STATUS_CONFLICT = "conflict"


@dataclass(frozen=True)
class ProbeObservation:
    """一条已登记的探针记录（诊断算法的输入）。"""

    probe_id: str
    source: str
    captured: tuple[str, ...]
    result: str


@dataclass(frozen=True)
class CandidateDirection:
    """一个可能是唯一丢包方向的有向链路。"""

    from_router: str
    to_router: str


@dataclass(frozen=True)
class DiagnosisResult:
    status: str
    message: str
    candidates: tuple[CandidateDirection, ...]
    records_total: int
    delivered_count: int
    lost_count: int
    evaluated_directions: int


def build_shortest_path_dag(
    nodes: set[str], links: Iterable, target: str
) -> tuple[dict[str, list[str]], dict[str, float], set[tuple[str, str]]]:
    """返回最短路 DAG、到目标的距离、DAG 中的有向边集合。

    调用方需先保证拓扑连通，否则 ``dist`` 中的无穷大没有转发意义。
    """
    adj = build_adjacency(nodes, links, "new_cost")
    dist = dijkstra_distances(adj, target)
    if any(d == float("inf") for d in dist.values()):
        raise ValueError("拓扑不连通，无法建立最短路 DAG")

    dag: dict[str, list[str]] = {n: [] for n in nodes}
    arcs: set[tuple[str, str]] = set()
    for u in nodes:
        if u == target:
            continue
        for v, w in adj[u].items():
            if dist[u] == w + dist[v]:
                dag[u].append(v)
                arcs.add((u, v))
    for u in dag:
        dag[u].sort()
    return dag, dist, arcs


def _reachability_bitsets(
    dag: dict[str, list[str]],
    dist: dict[str, float],
    index: dict[str, int],
    excluded_arc: tuple[str, str] | None = None,
) -> dict[str, int]:
    """在（可选删去一条边的）DAG 上计算每点能到达的点集，用整数位图表示。"""
    reach: dict[str, int] = {}
    # 有向边总是从距离大的点指向距离小的点，按距离升序即可保证后继先算好。
    for u in sorted(dag, key=lambda n: (dist[n], n)):
        bits = 1 << index[u]
        for v in dag[u]:
            if excluded_arc is not None and (u, v) == excluded_arc:
                continue
            bits |= reach[v]
        reach[u] = bits
    return reach


def _reaches(reach: dict[str, int], a: str, b: str, index: dict[str, int]) -> bool:
    return bool((reach[a] >> index[b]) & 1)


def _waypoints_reachable(
    reach: dict[str, int],
    source: str,
    captured: Sequence[str],
    target: str,
    index: dict[str, int],
) -> bool:
    """最短路 DAG 中是否存在 source -> captured... -> target 的路径。"""
    sequence = [source, *captured, target]
    return all(
        _reaches(reach, a, b, index) for a, b in zip(sequence, sequence[1:])
    )


def _lost_probe_explained(
    reach: dict[str, int],
    arcs: set[tuple[str, str]],
    source: str,
    captured: Sequence[str],
    arc: tuple[str, str],
    target: str,
    index: dict[str, int],
) -> bool:
    """丢失探针能否在采完 captured 之后尝试从 arc 通过。"""
    if arc not in arcs:
        return False
    # 先确认 source 能依次经过全部已采路由器。
    if captured:
        if not _waypoints_reachable(reach, source, captured, captured[-1], index):
            return False
        last = captured[-1]
    else:
        last = source
    u, v = arc
    # 最后一个已采路由器能到达 arc 的起点，且 arc 之后仍能到达共同目标。
    return (
        _reaches(reach, last, u, index)
        and _reaches(reach, v, target, index)
    )


def _delivered_probe_explained(
    reach: dict[str, int],
    source: str,
    captured: Sequence[str],
    target: str,
    index: dict[str, int],
) -> bool:
    return _waypoints_reachable(reach, source, captured, target, index)


def diagnose(
    nodes: set[str], links: list, target: str, probes: Iterable[ProbeObservation]
) -> DiagnosisResult:
    """对全部探针记录执行一次完整候选枚举（每次都从全量记录重算）。"""
    probes = tuple(probes)
    delivered = tuple(p for p in probes if p.result == DELIVERED)
    lost = tuple(p for p in probes if p.result == LOST)
    directions = sorted(
        {(lk.a, lk.b) for lk in links} | {(lk.b, lk.a) for lk in links}
    )
    evaluated = len(directions)

    if not lost:
        if not probes:
            message = "尚未登记探针，请继续取证。"
        else:
            message = "尚无丢包记录，无法定位方向，请继续取证。"
        return DiagnosisResult(
            status=STATUS_COLLECT_MORE,
            message=message,
            candidates=(),
            records_total=len(probes),
            delivered_count=len(delivered),
            lost_count=0,
            evaluated_directions=evaluated,
        )

    dag, dist, arcs = build_shortest_path_dag(nodes, links, target)
    index = {n: i for i, n in enumerate(sorted(nodes))}
    base_reach = _reachability_bitsets(dag, dist, index)

    candidates: list[CandidateDirection] = []
    for arc in directions:
        # 送达探针需要在删掉该方向后的图中仍然能找到路径。
        if arc in arcs:
            reach_without = _reachability_bitsets(dag, dist, index, excluded_arc=arc)
        else:
            # 最短路根本不会使用这个方向，删不删没有区别。
            reach_without = base_reach

        if not all(
            _delivered_probe_explained(
                reach_without, p.source, p.captured, target, index
            )
            for p in delivered
        ):
            continue
        if not all(
            _lost_probe_explained(
                base_reach, arcs, p.source, p.captured, arc, target, index
            )
            for p in lost
        ):
            continue
        candidates.append(CandidateDirection(arc[0], arc[1]))

    if candidates:
        status = STATUS_DIAGNOSED
        message = f"找到 {len(candidates)} 个能解释全部记录的有向链路。"
    else:
        status = STATUS_CONFLICT
        message = (
            "证据冲突：存在丢包记录，但没有有向链路能解释全部记录；"
            "记录已保留，候选已撤下。"
        )

    return DiagnosisResult(
        status=status,
        message=message,
        candidates=tuple(candidates),
        records_total=len(probes),
        delivered_count=len(delivered),
        lost_count=len(lost),
        evaluated_directions=evaluated,
    )


def validate_round_topology(links: list, target: str) -> set[str]:
    """校验诊断轮拓扑连通，返回节点集合。"""
    nodes = collect_nodes(links)
    if not is_connected(nodes, links, target):
        raise ValueError("拓扑必须连通，且共同目的路由器必须可达所有设备")
    return nodes
