"""端到端与算法自测。

运行（仓库根目录）：
    PYTHONPATH=/tmp/pylibs python -m pytest -q
"""

from __future__ import annotations

import itertools
import random

from fastapi.testclient import TestClient

from app.core import build_adjacency, dijkstra_distances, find_cycle, next_hop_sets
from app.main import app
from app.models import MAX_BATCH_SIZE

client = TestClient(app)


# ---------------------------------------------------------------- 场景一：同批错峰
def test_same_batch_staggered_switch_can_loop():
    """A 先切换即出现 A->B->A；B 先切换则安全，证明必须按错峰语义判环。"""
    req = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 10, "new_cost": 30},
            {"a": "T", "b": "B", "old_cost": 20, "new_cost": 20},
            {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["A", "B", "T"]],
    }
    r = client.post("/precheck", json=req)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "danger"
    assert data["batches_checked"] == 1
    danger = data["danger"]
    assert danger["batch_index"] == 1
    # A->B 仅存在于新版下一跳，A 必须已切换；B->A 是旧版边，B 无需切换
    assert danger["switched_devices"] == ["A"]
    assert danger["cycle_routers"] == ["A", "B", "A"]
    hop_pairs = [(h["router"], h["next_hop"], h["via"]) for h in danger["forwarding_cycle"]]
    assert ("A", "B", "new") in hop_pairs
    assert ("B", "A", "old") in hop_pairs
    # 单独看任一版本都无环（兜圈只在错峰过程中出现）
    assert data["old_next_hops"]["A"] == ["T"]
    assert data["new_next_hops"]["B"] == ["T"]


# ---------------------------------------------------------------- 场景二：等价路径并集成环
def test_ecmp_union_cycle():
    """旧版 C 的等价下一跳为 {A,B}，新版 A/B 指向 C，并集出现 A->C->A。"""
    req = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 10, "new_cost": 100},
            {"a": "T", "b": "B", "old_cost": 10, "new_cost": 100},
            {"a": "T", "b": "C", "old_cost": 100, "new_cost": 10},
            {"a": "A", "b": "C", "old_cost": 1, "new_cost": 1},
            {"a": "B", "b": "C", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["A", "B", "C", "T"]],
    }
    r = client.post("/precheck", json=req)
    assert r.status_code == 200, r.text
    data = r.json()
    # 旧版等价路径：C 到 T 经 A/B 代价均为 11
    assert data["old_next_hops"]["C"] == ["A", "B"]
    assert data["new_next_hops"]["A"] == ["C"]
    assert data["new_next_hops"]["B"] == ["C"]
    assert data["status"] == "danger"
    cycle = data["danger"]["cycle_routers"]
    assert cycle[0] == cycle[-1]
    assert len(cycle) >= 3
    # 闭环边必须新旧版本混合，单独任一版本都是 DAG
    vias = {h["via"] for h in data["danger"]["forwarding_cycle"]}
    assert "new" in vias and "old" in vias


# ---------------------------------------------------------------- 场景三：安全多批次
def test_safe_multi_batch_passes():
    req = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 1, "new_cost": 1},
            {"a": "T", "b": "B", "old_cost": 2, "new_cost": 2},
            {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["A"], ["B"], ["T"]],
    }
    r = client.post("/precheck", json=req)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "pass"
    assert data["danger"] is None
    assert data["batches_checked"] == 3
    # B 到 T 两条路代价都为 2，等价下一跳全部保留
    assert data["old_next_hops"]["B"] == ["A", "T"]
    assert data["new_next_hops"]["B"] == ["A", "T"]


# ---------------------------------------------------------------- 场景四：第二批才成环
def test_danger_only_in_second_batch():
    req = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 20, "new_cost": 30},
            {"a": "T", "b": "B", "old_cost": 5, "new_cost": 30},
            {"a": "T", "b": "C", "old_cost": 30, "new_cost": 10},
            {"a": "A", "b": "C", "old_cost": 20, "new_cost": 2},
            {"a": "B", "b": "C", "old_cost": 2, "new_cost": 4},
            {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["A"], ["B", "C"], ["T"]],
    }
    r = client.post("/precheck", json=req)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "danger"
    danger = data["danger"]
    assert danger["batch_index"] == 2
    assert danger["cycle_routers"] == ["A", "C", "B", "A"]
    # B->A 仅新版存在，故 B 必须已切换；C->B 是旧版边，C 可暂不切换
    assert danger["switched_devices"] == ["B"]


def test_docs_and_health():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/docs"


# ---------------------------------------------------------------- 输入校验
def _base_payload(**over):
    payload = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["T", "A"]],
    }
    payload.update(over)
    return payload


def test_validation_errors():
    bad_cases = [
        _base_payload(target=""),  # 空目标
        _base_payload(links=[{"a": "T", "b": "T", "old_cost": 1, "new_cost": 1}]),  # 自环
        _base_payload(
            links=[
                {"a": "T", "b": "A", "old_cost": 1, "new_cost": 1},
                {"a": "A", "b": "T", "old_cost": 2, "new_cost": 2},
            ]
        ),  # 重复链路
        _base_payload(batches=[["T"], ["A", "X"]]),  # 未知设备
        _base_payload(batches=[["T"]]),  # 未覆盖所有路由器
        _base_payload(batches=[["T", "A", "T"]]),  # 批内重复
        _base_payload(batches=[["T"], ["A", "T"]]),  # 跨批重复
        _base_payload(links=[{"a": "T", "b": "A", "old_cost": 0, "new_cost": 1}]),  # 非正整数
        _base_payload(batches=[["T"], []]),  # 空批次
    ]
    for payload in bad_cases:
        r = client.post("/precheck", json=payload)
        assert r.status_code == 422, payload


def test_batch_size_limit():
    n = MAX_BATCH_SIZE + 1
    links = [{"a": "T", "b": f"R{i}", "old_cost": 1, "new_cost": 1} for i in range(n)]
    payload = {"target": "T", "links": links, "batches": [["T"] + [f"R{i}" for i in range(n)]]}
    r = client.post("/precheck", json=payload)
    assert r.status_code == 422
    assert "50" in r.text


def test_disconnected_topology_rejected():
    payload = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 1, "new_cost": 1},
            {"a": "X", "b": "Y", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["T", "A", "X", "Y"]],
    }
    r = client.post("/precheck", json=payload)
    assert r.status_code == 422


def test_single_router_topology():
    payload = {
        "target": "T",
        "links": [{"a": "T", "b": "A", "old_cost": 5, "new_cost": 2}],
        "batches": [["A", "T"]],
    }
    r = client.post("/precheck", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pass"
    assert data["old_next_hops"]["T"] == []
    assert data["new_next_hops"]["T"] == []


# ---------------------------------------------------------------- 并集判环 vs 全状态暴力枚举
def test_union_lemma_against_bruteforce():
    """随机小拓扑：并集图判环结果必须与枚举全部切换子集 × 全部下一跳选择一致。"""
    rng = random.Random(20261120)
    node_names = [f"n{i}" for i in range(6)]

    for trial in range(40):
        # 生成随机连通无向图
        edges: list[tuple[int, int]] = []
        seen_pairs: set[frozenset[int]] = set()
        order = list(range(1, 6))
        rng.shuffle(order)
        connected = {0}
        for v in order:
            u = rng.choice(tuple(connected))
            edges.append((u, v))
            seen_pairs.add(frozenset((u, v)))
            connected.add(v)
        for _ in range(rng.randint(0, 4)):
            u, v = rng.sample(range(6), 2)
            key = frozenset((u, v))
            if key not in seen_pairs:
                seen_pairs.add(key)
                edges.append((u, v))

        links = [
            {
                "a": node_names[u],
                "b": node_names[v],
                "old_cost": rng.randint(1, 9),
                "new_cost": rng.randint(1, 9),
            }
            for u, v in edges
        ]
        # 随机划分三个批次（目标固定 n0，单独放最后一批）
        rest = node_names[1:]
        rng.shuffle(rest)
        cut1, cut2 = sorted(rng.sample(range(1, len(rest) + 1), 2))
        batches = [rest[:cut1], rest[cut1:cut2], rest[cut2:] + ["n0"]]
        payload = {"target": "n0", "links": links, "batches": batches}
        r = client.post("/precheck", json=payload)
        assert r.status_code == 200, (trial, r.text)
        api_danger = r.json()["status"] == "danger"

        # 本地暴力复算
        from app.models import Link as LinkModel

        link_models = [LinkModel(**lk) for lk in links]
        nodes = set(node_names)
        old_adj = build_adjacency(nodes, link_models, "old_cost")
        new_adj = build_adjacency(nodes, link_models, "new_cost")
        old_nh = next_hop_sets(old_adj, dijkstra_distances(old_adj, "n0"))
        new_nh = next_hop_sets(new_adj, dijkstra_distances(new_adj, "n0"))
        batch_of = {rt: i for i, b in enumerate(batches) for rt in b}

        brute_danger = False
        for k, batch in enumerate(batches):
            for subset_bits in range(1 << len(batch)):
                switched = {rt for i, rt in enumerate(batch) if subset_bits >> i & 1}
                available: dict[str, set[str]] = {}
                for n in nodes:
                    if n == "n0":
                        available[n] = set()
                    elif batch_of[n] < k or n in switched:
                        available[n] = new_nh[n]
                    else:
                        available[n] = old_nh[n]
                choices = [sorted(available[n]) for n in node_names if available[n]]
                for combo in itertools.product(*choices):
                    picked = dict(zip([n for n in node_names if available[n]], combo))
                    adj_sets = {n: ({picked[n]} if n in picked else set()) for n in nodes}
                    if find_cycle(adj_sets) is not None:
                        brute_danger = True
                        break
                if brute_danger:
                    break
            if brute_danger:
                break
        assert api_danger == brute_danger, trial


def test_deterministic_results():
    payload = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 10, "new_cost": 30},
            {"a": "T", "b": "B", "old_cost": 20, "new_cost": 20},
            {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["B", "A", "T"]],
    }
    first = client.post("/precheck", json=payload).json()
    second = client.post("/precheck", json=payload).json()
    assert first == second
