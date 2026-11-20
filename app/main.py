"""FastAPI 入口：链路代价分批调整离线预检 + 单向丢包探针诊断。"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .core import collect_nodes, is_connected, run_precheck
from .diagnosis import validate_round_topology
from .models import (
    MAX_PROBES_PER_ROUND,
    DiagnosisResponse,
    PrecheckRequest,
    PrecheckResponse,
    ProbeCorrection,
    ProbeRecordOut,
    RoundCreateRequest,
    RoundLinkOut,
    validate_probe_against_nodes,
)
from .store import RoundRecord, RoundStore

app = FastAPI(
    title="链路代价预检与单向丢包探针诊断",
    version="1.1.0",
    description=(
        "## 一、链路代价分批调整离线预检\n"
        "`POST /precheck`：提交连通路由拓扑、每条双向链路的新旧正整数代价、"
        "目标路由器与覆盖全部路由器的分批计划，逐批判断错峰切换过程中是否可能"
        "出现转发环路。\n\n"
        "## 二、单向丢包探针诊断\n"
        "按以下顺序在 `/docs` 中操作：\n\n"
        "1. `POST /diagnosis/rounds` 登记一轮：固定拓扑、新版链路代价、共同目的"
        "路由器，可附带初始探针记录。响应里的 `round_id` 后续使用。\n"
        "2. `PUT /diagnosis/rounds/{round_id}/probes/{probe_id}` 按编号登记或更正"
        "一条探针：起点、依次采到的路由器（允许漏采）、送达结果。每次写入都会"
        "在**全部**记录上重算，此前被排除的方向也会重新判定；响应同时给出更新后"
        "的记录与诊断。\n"
        "3. `GET /diagnosis/rounds/{round_id}` 随时查询当前记录与诊断。\n\n"
        "诊断假定路由保持不变、只有一个链路方向会丢包：尝试从该方向通过的探针"
        "都会丢失。探针走任一等价最短路。状态含义：\n\n"
        "- `collect_more`：尚无丢包记录，请继续取证；\n"
        "- `diagnosed`：`candidates` 列出全部能解释记录的有向链路；\n"
        "- `conflict`：有丢包记录但没有任何方向能解释全部记录；记录保留，"
        "旧候选全部撤下。\n\n"
        "演示拓扑（S 到 T 三条等价最短路）："
        "`S-T`、`S-A-T`、`S-B-T`。先提交一条 `S` 出发、已采到 `A` 的丢失探针，"
        "候选只剩 `A->T`；再把该记录更正为已采到 `B`，候选随之切换到 `B->T`。"
    ),
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/precheck", response_model=PrecheckResponse, summary="提交分批计划并执行环路预检")
def precheck(req: PrecheckRequest) -> PrecheckResponse:
    nodes = collect_nodes(req.links)
    if not is_connected(nodes, req.links, req.target):
        # 连通性在模型层之后二次确认（模型已保证端点一致，这里防御孤立分量）
        raise HTTPException(status_code=422, detail="拓扑必须连通，且目标路由器可达所有设备")

    result = run_precheck(nodes, req.links, req.target, req.batches)
    danger = result["dangerous"]
    return PrecheckResponse(
        status="danger" if danger else "pass",
        target=req.target,
        batches_total=len(req.batches),
        batches_checked=result["batches_checked"],
        old_next_hops=result["old_nh"],
        new_next_hops=result["new_nh"],
        danger=danger,
    )


# ---------------------------------------------------------------- 探针诊断
# 设置 DIAGNOSIS_STORE_PATH 即持久化到 JSON 文件；未设置则仅存于进程内。
store = RoundStore(os.environ.get("DIAGNOSIS_STORE_PATH"))


def _render(record: RoundRecord) -> DiagnosisResponse:
    diagnosis = record.diagnosis
    assert diagnosis is not None, "诊断快照必须与记录同时写入"
    return DiagnosisResponse(
        round_id=record.round_id,
        target=record.target,
        links=[RoundLinkOut(a=lk.a, b=lk.b, new_cost=lk.new_cost) for lk in record.links],
        status=diagnosis.status,
        message=diagnosis.message,
        records_total=diagnosis.records_total,
        delivered_count=diagnosis.delivered_count,
        lost_count=diagnosis.lost_count,
        evaluated_directions=diagnosis.evaluated_directions,
        candidates=[
            {"from_router": c.from_router, "to_router": c.to_router}
            for c in diagnosis.candidates
        ],
        records=[
            ProbeRecordOut(
                probe_id=p.probe_id,
                source=p.source,
                captured=list(p.captured),
                result=p.result,
            )
            for p in record.probes.values()
        ],
    )


@app.post(
    "/diagnosis/rounds",
    response_model=DiagnosisResponse,
    status_code=201,
    summary="登记一轮诊断（固定拓扑、新版代价、共同目的路由器）",
    description=(
        "登记后立即按拓扑中最短路枚举全部有向链路。`probes` 可为空；"
        "为空时状态为 `collect_more`，提示继续取证。"
    ),
)
def create_round(req: RoundCreateRequest) -> DiagnosisResponse:
    try:
        validate_round_topology(req.links, req.target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = store.create(req.target, req.links, req.probes)
    return _render(record)


@app.get(
    "/diagnosis/rounds/{round_id}",
    response_model=DiagnosisResponse,
    summary="查询某轮的记录与当前诊断",
)
def get_round(round_id: str) -> DiagnosisResponse:
    record = store.get(round_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"诊断轮 {round_id} 不存在")
    return _render(record)


@app.put(
    "/diagnosis/rounds/{round_id}/probes/{probe_id}",
    response_model=DiagnosisResponse,
    summary="按编号登记或更正一条探针记录并立即重算诊断",
    description=(
        "编号已存在则覆盖为更正后的内容，否则新增。重算基于全部记录，"
        "此前因旧内容被排除的方向会重新参与判定；响应中的 `records` 与"
        "`candidates` 来自同一次计算，不会出现记录与诊断不一致。"
    ),
)
def upsert_probe(
    round_id: str, probe_id: str, req: ProbeCorrection
) -> DiagnosisResponse:
    if not probe_id.strip() or len(probe_id) > 128:
        raise HTTPException(status_code=422, detail="探针编号无效（须为 1-128 个非空字符）")
    record = store.get(round_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"诊断轮 {round_id} 不存在")
    try:
        validate_probe_against_nodes(req, record.nodes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if probe_id not in record.probes and len(record.probes) >= MAX_PROBES_PER_ROUND:
        raise HTTPException(
            status_code=422,
            detail=f"单轮最多登记 {MAX_PROBES_PER_ROUND} 条探针",
        )
    updated = store.upsert_probe(
        round_id, probe_id, req.source, req.captured, req.result
    )
    assert updated is not None, "轮次在读取与更新之间被删除"
    return _render(updated)
