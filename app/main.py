"""FastAPI 入口：链路代价分批调整离线预检。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .core import collect_nodes, is_connected, run_precheck
from .models import PrecheckRequest, PrecheckResponse

app = FastAPI(
    title="链路代价分批调整离线预检",
    version="1.0.0",
    description=(
        "提交连通路由拓扑、每条双向链路的新旧正整数代价、目标路由器与覆盖所有"
        "路由器的分批计划，逐批判断错峰切换过程中是否可能出现转发环路。"
        "等价最短路径的所有下一跳均视为可能被选中。"
    ),
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
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
