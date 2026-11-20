"""Pydantic 请求 / 响应模型与输入校验。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

MAX_BATCH_SIZE = 50


class Link(BaseModel):
    """双向链路：a/b 两个端点，新旧两版正整数代价。"""

    a: str = Field(..., description="路由器 A 标识")
    b: str = Field(..., description="路由器 B 标识")
    old_cost: int = Field(..., ge=1, description="旧版代价（正整数）")
    new_cost: int = Field(..., ge=1, description="新版代价（正整数）")


class PrecheckRequest(BaseModel):
    target: str = Field(..., description="目标路由器标识")
    links: list[Link] = Field(..., min_length=1, description="双向链路列表")
    batches: list[list[str]] = Field(..., min_length=1, description="分批计划，按切换顺序排列")

    @model_validator(mode="after")
    def _validate(self) -> "PrecheckRequest":
        # 路由器标识不能为空
        if not self.target or not self.target.strip():
            raise ValueError("target 不能为空")
        for lk in self.links:
            if not lk.a or not lk.a.strip() or not lk.b or not lk.b.strip():
                raise ValueError("链路端点标识不能为空")
            if lk.a == lk.b:
                raise ValueError(f"不允许自环链路：{lk.a}")

        nodes: set[str] = set()
        undirected: set[frozenset[str]] = set()
        directed: set[tuple[str, str]] = set()
        for lk in self.links:
            key = frozenset((lk.a, lk.b))
            if key in undirected:
                raise ValueError(f"链路 {lk.a} <-> {lk.b} 重复定义")
            undirected.add(key)
            if (lk.a, lk.b) in directed:
                raise ValueError(f"链路 {lk.a} -> {lk.b} 重复定义")
            directed.add((lk.a, lk.b))
            directed.add((lk.b, lk.a))
            nodes.add(lk.a)
            nodes.add(lk.b)

        if self.target not in nodes:
            raise ValueError(f"目标路由器 {self.target} 不在拓扑中")

        # 分批计划校验
        if any(len(batch) == 0 for batch in self.batches):
            raise ValueError("不允许空批次")
        for i, batch in enumerate(self.batches, start=1):
            if len(batch) > MAX_BATCH_SIZE:
                raise ValueError(f"第 {i} 批包含 {len(batch)} 台设备，超过单批 {MAX_BATCH_SIZE} 台上限")
            if len(set(batch)) != len(batch):
                raise ValueError(f"第 {i} 批内部存在重复设备")
            for router in batch:
                if not router or not router.strip():
                    raise ValueError(f"第 {i} 批存在空设备标识")
                if router not in nodes:
                    raise ValueError(f"第 {i} 批中的设备 {router} 不在拓扑中")
        planned = [r for batch in self.batches for r in batch]
        if len(set(planned)) != len(planned):
            raise ValueError("分批计划中存在跨批次重复的设备")
        if set(planned) != nodes:
            missing = sorted(nodes - set(planned))
            raise ValueError(f"分批计划必须覆盖所有路由器，缺少：{missing}")
        return self


class Hop(BaseModel):
    router: str
    next_hop: str
    via: str = Field(..., description="该出边来自 old / new / both 哪一版下一跳")


class Danger(BaseModel):
    batch_index: int = Field(..., description="首个危险批次（从 1 开始）")
    switched_devices: list[str] = Field(..., description="该批已切换到新版下一跳的设备")
    cycle_routers: list[str] = Field(..., description="闭合转发路径上的路由器（首尾相同）")
    forwarding_cycle: list[Hop] = Field(..., description="逐跳转发路径及所用代价版本")


class PrecheckResponse(BaseModel):
    status: str = Field(..., description="pass 或 danger")
    target: str
    batches_total: int
    batches_checked: int = Field(..., description="实际检查到第几批（危险时即危险批次）")
    old_next_hops: dict[str, list[str]]
    new_next_hops: dict[str, list[str]]
    danger: Danger | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "pass",
                    "target": "T",
                    "batches_total": 2,
                    "batches_checked": 2,
                    "old_next_hops": {"A": ["T"], "B": ["T"], "T": []},
                    "new_next_hops": {"A": ["B"], "B": ["T"], "T": []},
                    "danger": None,
                }
            ]
        }
    }
