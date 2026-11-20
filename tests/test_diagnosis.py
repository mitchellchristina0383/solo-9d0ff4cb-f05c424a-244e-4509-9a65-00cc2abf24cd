"""单向丢包探针诊断的端到端测试。

拓扑 S 到 T 有三条等价最短路（S-T、S-A-T、S-B-T），便于验证候选如何随
记录与更正变化。
"""

from __future__ import annotations

import random

from fastapi.testclient import TestClient

from app.diagnosis import ProbeObservation, build_shortest_path_dag, diagnose
from app.main import app
from app.models import RoundLink

client = TestClient(app)

# S 到 T 有三条等价最短路：S->T、S->A->T、S->B->T
LINKS = [
    {"a": "S", "b": "T", "new_cost": 2},
    {"a": "S", "b": "A", "new_cost": 1},
    {"a": "A", "b": "T", "new_cost": 1},
    {"a": "S", "b": "B", "new_cost": 1},
    {"a": "B", "b": "T", "new_cost": 1},
]


def _create(probes=None):
    r = client.post(
        "/diagnosis/rounds",
        json={"target": "T", "links": LINKS, "probes": probes or []},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _put(round_id, probe_id, source, captured, result):
    r = client.put(
        f"/diagnosis/rounds/{round_id}/probes/{probe_id}",
        json={"source": source, "captured": captured, "result": result},
    )
    return r


def _candidates(data):
    return [(c["from_router"], c["to_router"]) for c in data["candidates"]]


# ---------------------------------------------------------------- 取证 / 候选
def test_empty_round_asks_for_more_evidence():
    data = _create()
    assert data["status"] == "collect_more"
    assert data["candidates"] == []
    assert data["records"] == []
    assert data["records_total"] == 0
    assert data["evaluated_directions"] == len(LINKS) * 2
    assert "继续取证" in data["message"]


def test_delivered_only_asks_for_more_evidence():
    data = _create(
        [
            {
                "probe_id": "p1",
                "source": "S",
                "captured": ["A"],
                "result": "delivered",
            }
        ]
    )
    assert data["status"] == "collect_more"
    assert data["candidates"] == []
    assert data["delivered_count"] == 1
    assert data["lost_count"] == 0


def test_single_lost_probe_narrows_to_branch_after_last_capture():
    data = _create(
        [
            {
                "probe_id": "p1",
                "source": "S",
                "captured": ["A"],
                "result": "lost",
            }
        ]
    )
    assert data["status"] == "diagnosed"
    assert _candidates(data) == [("A", "T")]
    # 已采 A 之后再尝试的方向，不可能是 A 之前的 S->A / S->T / S->B
    assert ("S", "A") not in _candidates(data)


def test_lost_probe_without_capture_can_match_any_branch():
    data = _create(
        [{"probe_id": "p1", "source": "S", "captured": [], "result": "lost"}]
    )
    assert data["status"] == "diagnosed"
    # 未采到任何路由器时，S 出发的三条支路都可解释
    assert _candidates(data) == [
        ("A", "T"),
        ("B", "T"),
        ("S", "A"),
        ("S", "B"),
        ("S", "T"),
    ]


def test_delivered_probe_must_avoid_candidate_direction():
    data = _create(
        [
            {"probe_id": "lost1", "source": "S", "captured": ["A"], "result": "lost"},
            {
                "probe_id": "ok1",
                "source": "S",
                "captured": ["A"],
                "result": "delivered",
            },
        ]
    )
    # 送达探针确实经过 A，A->T 被排除；丢失探针又要求丢在采集点之后，无解
    assert data["status"] == "conflict"
    assert data["candidates"] == []
    assert data["records_total"] == 2
    assert "证据冲突" in data["message"]


def test_equivalent_branch_avoids_direction():
    data = _create(
        [
            {"probe_id": "lost1", "source": "S", "captured": ["B"], "result": "lost"},
            {
                "probe_id": "ok1",
                "source": "S",
                "captured": ["A"],
                "result": "delivered",
            },
        ]
    )
    # 送达走 A 支路，丢失走 B 支路并丢在 B->T
    assert data["status"] == "diagnosed"
    assert _candidates(data) == [("B", "T")]
    assert data["delivered_count"] == 1
    assert data["lost_count"] == 1


def test_conflict_keeps_records_and_withdraws_candidates():
    data = _create(
        [
            {"probe_id": "a", "source": "S", "captured": ["A"], "result": "lost"},
            {"probe_id": "b", "source": "S", "captured": ["B"], "result": "lost"},
        ]
    )
    assert data["status"] == "conflict"
    assert data["candidates"] == []
    assert [r["probe_id"] for r in data["records"]] == ["a", "b"]


# ---------------------------------------------------------------- 更正恢复候选
def test_correction_restores_previously_excluded_direction():
    data = _create(
        [
            {"probe_id": "a", "source": "S", "captured": ["A"], "result": "lost"},
            {"probe_id": "b", "source": "S", "captured": ["B"], "result": "lost"},
        ]
    )
    assert data["status"] == "conflict"
    assert ("B", "T") not in _candidates(data)

    # 更正 a：实际采到的是 B，两条记录都指向 B->T
    r = _put(data["round_id"], "a", "S", ["B"], "lost")
    assert r.status_code == 200, r.text
    corrected = r.json()
    assert corrected["status"] == "diagnosed"
    assert _candidates(corrected) == [("B", "T")]
    # 记录与诊断同时生效：a 已是新内容，编号未新增
    records = {rec["probe_id"]: rec for rec in corrected["records"]}
    assert records["a"]["captured"] == ["B"]
    assert corrected["records_total"] == 2

    # 再改回来：候选撤下、状态回到冲突
    back = _put(data["round_id"], "a", "S", ["A"], "lost").json()
    assert back["status"] == "conflict"
    assert back["candidates"] == []


def test_correction_from_conflict_to_clean_diagnosis():
    data = _create(
        [
            {"probe_id": "p1", "source": "S", "captured": ["A"], "result": "lost"},
            {"probe_id": "p2", "source": "S", "captured": [], "result": "lost"},
        ]
    )
    assert data["status"] == "diagnosed"
    assert _candidates(data) == [("A", "T")]

    # p2 送达后，只有 A->T 仍可解释 p1；再更正 p1 为送达则转为取证中
    r = _put(data["round_id"], "p2", "S", [], "delivered").json()
    assert r["status"] == "diagnosed"
    assert _candidates(r) == [("A", "T")]
    r = _put(data["round_id"], "p1", "S", ["A"], "delivered").json()
    assert r["status"] == "collect_more"
    assert r["candidates"] == []
    assert r["records_total"] == 2


def test_correction_is_idempotent_and_get_reflects_it():
    data = _create()
    rid = data["round_id"]
    first = _put(rid, "p", "S", ["A"], "lost").json()
    second = _put(rid, "p", "S", ["A"], "lost").json()
    assert first == second
    fetched = client.get(f"/diagnosis/rounds/{rid}").json()
    assert fetched["records_total"] == 1
    assert _candidates(fetched) == [("A", "T")]


# ---------------------------------------------------------------- 输入校验
def test_round_validation_errors():
    bad = [
        {"target": "X", "links": LINKS},  # 目标不在拓扑
        {"target": "T", "links": [{"a": "T", "b": "T", "new_cost": 1}]},  # 自环
        {
            "target": "T",
            "links": [{"a": "T", "b": "A", "new_cost": 1}, {"a": "A", "b": "T", "new_cost": 1}],
        },  # 重复链路
        {"target": "T", "links": [{"a": "T", "b": "A", "new_cost": 0}]},  # 非正整数
        {
            "target": "T",
            "links": LINKS,
            "probes": [
                {"probe_id": "p", "source": "S", "captured": ["A", "A"], "result": "lost"}
            ],
        },  # 已采路由器重复
        {
            "target": "T",
            "links": LINKS,
            "probes": [{"probe_id": "p", "source": "Z", "captured": [], "result": "lost"}],
        },  # 起点不在拓扑
        {
            "target": "T",
            "links": LINKS,
            "probes": [{"probe_id": "p", "source": "S", "captured": ["Z"], "result": "lost"}],
        },  # 已采路由器不在拓扑
        {
            "target": "T",
            "links": LINKS,
            "probes": [
                {"probe_id": "p", "source": "S", "captured": [], "result": "lost"},
                {"probe_id": "p", "source": "S", "captured": [], "result": "delivered"},
            ],
        },  # 初始记录编号重复
        {
            "target": "T",
            "links": LINKS,
            "probes": [{"probe_id": "p", "source": "S", "captured": [], "result": "maybe"}],
        },  # 非法结果
    ]
    for payload in bad:
        r = client.post("/diagnosis/rounds", json=payload)
        assert r.status_code == 422, (payload, r.text)


def test_disconnected_round_rejected():
    payload = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "new_cost": 1},
            {"a": "X", "b": "Y", "new_cost": 1},
        ],
    }
    r = client.post("/diagnosis/rounds", json=payload)
    assert r.status_code == 422


def test_unknown_round_and_bad_correction():
    assert client.get("/diagnosis/rounds/nope").status_code == 404
    assert _put("nope", "p", "S", [], "lost").status_code == 404

    rid = _create()["round_id"]
    assert _put(rid, "p", "Z", [], "lost").status_code == 422
    assert _put(rid, "p", "S", ["Z"], "lost").status_code == 422
    assert _put(rid, "p", "S", ["A", "A"], "lost").status_code == 422


def test_docs_and_health_still_work():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/diagnosis/rounds" in schema["paths"]
    assert "/diagnosis/rounds/{round_id}/probes/{probe_id}" in schema["paths"]


# ---------------------------------------------------------------- 与预检共用进程
def test_precheck_unchanged_alongside_diagnosis():
    payload = {
        "target": "T",
        "links": [
            {"a": "T", "b": "A", "old_cost": 1, "new_cost": 1},
            {"a": "T", "b": "B", "old_cost": 2, "new_cost": 2},
            {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1},
        ],
        "batches": [["A"], ["B"], ["T"]],
    }
    r = client.post("/precheck", json=payload)
    assert r.status_code == 200
    assert r.json()["status"] == "pass"


# ---------------------------------------------------------------- 结论确定性
def test_diagnosis_deterministic_and_sorted():
    probes = [
        ProbeObservation("p1", "S", (), "lost"),
        ProbeObservation("p2", "S", ("A",), "delivered"),
    ]
    links = [RoundLink(**lk) for lk in LINKS]
    first = diagnose({"S", "T", "A", "B"}, links, "T", probes)
    second = diagnose({"S", "T", "A", "B"}, links, "T", probes)
    assert first == second
    # 送达探针经过 A，故 A->T 与 S->A 都被排除；其余三条支路仍可解释
    assert [(c.from_router, c.to_router) for c in first.candidates] == [
        ("B", "T"),
        ("S", "B"),
        ("S", "T"),
    ]


# ---------------------------------------------------------------- 位图算法 vs 路径枚举
def _all_shortest_paths(dag, source, target):
    """枚举 DAG 中 source -> target 的全部路径（小图用）。"""
    out = []
    stack = [(source, [source])]
    while stack:
        node, path = stack.pop()
        if node == target:
            out.append(path)
            continue
        for nxt in dag[node]:
            stack.append((nxt, path + [nxt]))
    return out


def _is_subsequence(part, whole):
    it = iter(whole)
    return all(any(x == y for y in it) for x in part)


def _reference_candidates(nodes, links, target, probes):
    """独立参考实现：对每个方向枚举全部最短路，直接检查记录语义。"""
    if not any(p.result == "lost" for p in probes):
        # 与正式实现同一约定：尚无丢包记录时不下诊断
        return set()
    dag, _dist, arcs = build_shortest_path_dag(nodes, links, target)
    paths = {
        (p.source, p.probe_id): _all_shortest_paths(dag, p.source, target)
        for p in probes
    }
    out = set()
    directions = {(lk.a, lk.b) for lk in links} | {(lk.b, lk.a) for lk in links}
    for arc in directions:
        ok = True
        for probe in probes:
            options = paths[(probe.source, probe.probe_id)]
            if probe.result == "delivered":
                # 存在一条避开该方向、且以 captured 为子序列的路径
                if not any(
                    arc not in set(zip(path, path[1:]))
                    and _is_subsequence(probe.captured, path)
                    for path in options
                ):
                    ok = False
                    break
            else:
                if arc not in arcs:
                    ok = False
                    break
                explained = False
                for path in options:
                    if not _is_subsequence(probe.captured, path):
                        continue
                    hops = list(zip(path, path[1:]))
                    # 采集到的最后一点之后才尝试该方向
                    cursor = 0
                    last_pos = 0
                    for router in probe.captured:
                        cursor = path.index(router, cursor)
                        last_pos = cursor
                        cursor += 1
                    first_pos = hops.index(arc) if arc in hops else None
                    if first_pos is not None and first_pos >= last_pos:
                        explained = True
                        break
                if not explained:
                    ok = False
                    break
        if ok:
            out.add(arc)
    return out


def test_bitmap_algorithm_matches_path_enumeration():
    rng = random.Random(20261121)
    for trial in range(60):
        n = rng.randint(2, 6)
        names = [f"n{i}" for i in range(n)]
        target = "n0"
        pairs: set[frozenset[str]] = set()
        edges = []
        order = list(range(1, n))
        rng.shuffle(order)
        connected = {0}
        for v in order:
            u = rng.choice(sorted(connected, key=int))
            pairs.add(frozenset((names[u], names[v])))
            edges.append((u, v))
            connected.add(v)
        for _ in range(rng.randint(0, 3)):
            u, v = rng.sample(range(n), 2)
            if frozenset((names[u], names[v])) not in pairs:
                pairs.add(frozenset((names[u], names[v])))
                edges.append((u, v))
        links = [
            RoundLink(a=names[u], b=names[v], new_cost=rng.randint(1, 4))
            for u, v in edges
        ]
        nodes = set(names)
        dag, _d, _a = build_shortest_path_dag(nodes, links, target)
        probes = []
        for i in range(rng.randint(1, 5)):
            src = rng.choice(names)
            path = rng.choice(_all_shortest_paths(dag, src, target))
            interior = path[1:-1]
            captured = tuple(sorted(rng.sample(interior, rng.randint(0, len(interior))), key=path.index))
            probes.append(
                ProbeObservation(
                    f"p{i}", src, captured, rng.choice(["delivered", "lost"])
                )
            )
        got = {(c.from_router, c.to_router) for c in diagnose(nodes, links, target, probes).candidates}
        want = _reference_candidates(nodes, links, target, probes)
        assert got == want, (trial, edges, probes, got, want)


# ---------------------------------------------------------------- 并发写入的一致性
def test_concurrent_corrections_keep_records_and_diagnosis_consistent():
    """并发写入后，每条响应里的 records 与 candidates 必须来自同一份快照。"""
    from concurrent.futures import ThreadPoolExecutor

    rid = _create()["round_id"]

    def worker(worker_id: int) -> None:
        for i in range(10):
            probe_id = f"w{worker_id}-{i}"
            # 写入后立刻读取同一条探针，响应自身必须自洽
            r = _put(rid, probe_id, "S", ["A"], "lost")
            assert r.status_code == 200, r.text
            data = r.json()
            records = {rec["probe_id"]: rec for rec in data["records"]}
            assert records[probe_id]["captured"] == ["A"]
            assert data["records_total"] == len(data["records"])
            assert _candidates(data) == [("A", "T")]

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    final = client.get(f"/diagnosis/rounds/{rid}").json()
    assert final["records_total"] == 60
    assert len(final["records"]) == 60
    assert _candidates(final) == [("A", "T")]


# ---------------------------------------------------------------- 持久化
def test_store_persists_and_reloads(tmp_path):
    from app.diagnosis import ProbeObservation as PO
    from app.store import RoundStore
    from app.models import RoundLink as RL
    from app.models import ProbeInput

    path = tmp_path / "diagnosis.json"
    links = [RL(**lk) for lk in LINKS]
    store = RoundStore(path)
    record = store.create("T", links, [ProbeInput(probe_id="p1", source="S", captured=["A"], result="lost")])
    rid = record.round_id
    store.upsert_probe(rid, "p1", "S", ["B"], "lost")

    reloaded = RoundStore(path)
    got = reloaded.get(rid)
    assert got is not None
    assert got.probes["p1"].captured == ("B",)
    assert [(c.from_router, c.to_router) for c in got.diagnosis.candidates] == [("B", "T")]


def test_store_tolerates_corrupt_file(tmp_path):
    from app.store import RoundStore

    path = tmp_path / "diagnosis.json"
    path.write_text("{not json", encoding="utf-8")
    store = RoundStore(path)
    assert store.get("x") is None


def test_blank_probe_id_rejected():
    rid = _create()["round_id"]
    r = _put(rid, "%20", "S", [], "lost")  # 空格，路径中编码为 %20
    assert r.status_code == 422
    bad = client.post(
        "/diagnosis/rounds",
        json={
            "target": "T",
            "links": LINKS,
            "probes": [{"probe_id": "   ", "source": "S", "captured": [], "result": "lost"}],
        },
    )
    assert bad.status_code == 422


def test_probe_cap_enforced_on_corrections(monkeypatch):
    from app import main as main_mod

    rid = _create()["round_id"]
    _put(rid, "p0", "S", [], "lost")
    monkeypatch.setattr(main_mod, "MAX_PROBES_PER_ROUND", 1)
    assert _put(rid, "p1", "S", [], "lost").status_code == 422
    # 覆盖已有编号不受上限影响
    assert _put(rid, "p0", "S", ["A"], "lost").status_code == 200
