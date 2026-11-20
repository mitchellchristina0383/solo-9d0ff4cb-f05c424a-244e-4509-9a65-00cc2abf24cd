# 链路代价分批调整离线预检 + 单向丢包探针诊断

两项离线能力共用一个服务：

1. **分批调价环路预检**（`POST /precheck`）：提交连通路由拓扑、每条双向链路的新旧
   两版正整数代价、目标路由器和覆盖全部路由器的分批计划，服务按两版代价分别计算
   到目标的最短路径下一跳（等价路径的所有下一跳都可能被选中），逐批判断错峰切换
   过程中是否可能出现转发环路。
2. **单向丢包探针诊断**（`/diagnosis/rounds`）：登记固定拓扑、新版链路代价和共同
   目的路由器，按探针记录枚举最可能出问题的有向链路，供值班员决定先查哪条链路。

## 一、分批调价环路预检

### 判环模型

对每一批 `k`，切换期间各路由器的可用出边为：

| 路由器所属 | 切换期间可用出边 |
| --- | --- |
| 早于第 `k` 批 | 仅新版下一跳（前一批已全部生效） |
| 晚于第 `k` 批 | 仅旧版下一跳 |
| 第 `k` 批（本批） | **旧版 ∪ 新版**下一跳（设备可按任意顺序切换） |

在该并集转发图中：

- **存在有向环** ⇒ 存在一种错峰切换顺序与下一跳选择使流量兜圈，本批危险；
  环上仅新版可达的出边对应的本批设备，即为构造该环所需的“已切换设备”。
- **不存在有向环** ⇒ 本批切换的任意中间状态（任意已切换子集、任意等价
  下一跳选择）都不可能成环。

该等价关系由随机小拓扑暴力枚举（枚举全部切换子集 × 全部下一跳组合）
回归测试守护，见 `tests/test_api.py::test_union_lemma_against_bruteforce`。

下一跳由 Dijkstra 最短距离推导：无向边 `(u, v, w)` 上，`v` 是 `u` 的下一跳
当且仅当 `dist(u) == w + dist(v)`，等号成立的邻居全部纳入等价下一跳集合。

## 二、单向丢包探针诊断

### 场景与模型

机房固定拓扑、按新版链路代价转发。出现单向丢包时，值班员需要知道先查哪些链路。
每轮登记固定拓扑、新版代价与共同目的路由器；每条探针记录：编号、起点、**依次**
采到探针的路由器（允许漏采）和送达结果（`delivered` / `lost`）。

假设：

- 路由保持不变，探针只走**等价最短路**，任一等价最短路都可能被选中；
- 只有一个**链路方向**会丢包，尝试从该方向通过的探针都会丢失。

于是对候选方向 `u -> v`：

| 记录 | 必须同时满足 |
| --- | --- |
| 送达 `delivered` | 存在一条起点到目的、**依次经过**全部已采路由器、且**完全避开** `u -> v` 的最短路 |
| 丢失 `lost` | 存在一条起点到目的、依次经过全部已采路由器、并在**最后一个已采路由器之后**才尝试 `u -> v` 的最短路 |

枚举全部有向链路（2E 条），留下能解释**所有**记录的方向。判定在最短路径 DAG 上
用可达性完成：DAG 中相邻两段最短路拼起来仍是合法简单路径，所以"依次经过"可以
拆成相邻两点的可达性检查；每条删除该方向后的可达矩阵用整数位图一次 `O(V+E)`
算完。

### 状态

| `status` | 含义 | `candidates` |
| --- | --- | --- |
| `collect_more` | 尚无丢包记录（或尚未登记探针），继续取证 | 空 |
| `diagnosed` | 找到能解释全部记录的方向 | 全部候选，按字典序 |
| `conflict` | 有丢包记录但没有任何方向能解释 | 空（旧候选撤下，记录保留） |

### 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/diagnosis/rounds` | 登记一轮：`target`、`links`（`a`/`b`/`new_cost`）、可选初始 `probes` |
| `PUT` | `/diagnosis/rounds/{round_id}/probes/{probe_id}` | 按编号新增或更正一条记录，立即用**全部**记录重算（含此前被排除的方向），响应同时返回记录与诊断 |
| `GET` | `/diagnosis/rounds/{round_id}` | 查询当前记录与诊断 |

存储默认持久化在容器卷 `diagnosis-data`（`DIAGNOSIS_STORE_PATH=/data/diagnosis.json`）：
每次写入先算好新诊断，再原子替换 JSON 文件，重启后重新加载并重算。写入与重算在
同一把锁内完成，响应中的 `records` 与 `candidates` 永远来自同一份快照。
未设置 `DIAGNOSIS_STORE_PATH` 时仅存于进程内（适合测试）。

输入校验（失败 422）：拓扑连通、无自环与重复链路、目标在拓扑中；探针起点/已采
路由器必须在拓扑中、已采路由器不得重复；结果只能是 `delivered`/`lost`。

### `/docs` 演示步骤

启动后打开 <http://localhost:8000/docs>，用演示拓扑（`S` 到 `T` 有三条等价最短
路 `S-T`、`S-A-T`、`S-B-T`）：

1. `POST /diagnosis/rounds` → "Try it out"，填入 `examples/probe_round_create.json`
   （一条 `S` 出发、已采到 `A` 的丢失探针）。响应 `status=diagnosed`，
   `candidates=[{"from_router":"A","to_router":"T"}]`；记下 `round_id`。
2. `PUT /diagnosis/rounds/{round_id}/probes/P-001` → 填入
   `examples/probe_correction.json`（更正为已采到 `B`）。响应记录已更新，
   候选变为 `B->T`：此前被排除的方向随更正重新判定。
3. 也可先提交空 `probes` 看 `collect_more`；再加一条经过 `A` 的送达记录，
   观察状态变为 `conflict`（记录全部保留、候选撤下）。
4. `GET /diagnosis/rounds/{round_id}` 核对持久化的记录与诊断。

### 复杂度

每轮一次 Dijkstra `O((V+E) log V)`；随后对 2E 个方向各做一次位图可达计算，
`O(E·(V+E)·V/字长)` 级别。单轮最多 5000 条探针，实际每条记录只做两次位查询。

## 目录结构

```
app/
  main.py       FastAPI 入口（/、/health、/precheck、/diagnosis/*、/docs）
  core.py       Dijkstra、下一跳集合、并集图与三色 DFS 找环
  diagnosis.py  最短路 DAG、探针语义与候选方向枚举
  store.py      诊断轮存储（原子更新，可选 JSON 持久化）
  models.py     请求/响应模型与输入校验
tests/          pytest 自测（预检 + 探针诊断 + 暴力对照）
examples/       可直接提交的示例请求
Dockerfile
docker-compose.yml
requirements.txt
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

- 交互式文档：<http://localhost:8000/docs>（`POST /precheck` 提交预检计划；
  `/diagnosis/rounds` 系列演示探针诊断）
- 健康检查：`GET http://localhost:8000/health`

无 Docker 时本地运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 预检请求格式

```json
{
  "target": "T",
  "links": [
    {"a": "T", "b": "A", "old_cost": 10, "new_cost": 30},
    {"a": "T", "b": "B", "old_cost": 20, "new_cost": 20},
    {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1}
  ],
  "batches": [["A", "B", "T"]]
}
```

约束（违反返回 HTTP 422）：

- 代价为正整数；不允许自环、重复链路；拓扑必须连通。
- 分批计划覆盖所有路由器且不重不漏；单批最多 **50** 台；不允许空批次。
- 批次顺序即切换顺序，前一批全部生效后才进入下一批。

## 预检响应

安全：

```json
{"status": "pass", "target": "T", "batches_total": 3, "batches_checked": 3,
 "old_next_hops": {...}, "new_next_hops": {...}, "danger": null}
```

危险（首个危险批次）：

```json
{
  "status": "danger",
  "batches_checked": 1,
  "danger": {
    "batch_index": 1,
    "switched_devices": ["A"],
    "cycle_routers": ["A", "B", "A"],
    "forwarding_cycle": [
      {"router": "A", "next_hop": "B", "via": "new"},
      {"router": "B", "next_hop": "A", "via": "old"}
    ]
  }
}
```

`via` 标出该跳取自哪一版下一跳：`old` / `new` / `both`。

## 自测

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

预检覆盖：同批错峰成环、等价路径并集成环、安全多批次、第二批才成环、
单批 50 台上限等输入校验、断链拒绝、单路由器拓扑、结果确定性，
以及 40 组随机拓扑的并集判环 vs 全状态暴力枚举对照。

诊断覆盖：取证提示、单条/多条丢失记录收窄候选、送达记录排除方向、证据冲突时
保留记录并撤下候选、按编号更正后候选恢复与再次撤销、记录与诊断同步生效、
并发写入一致性、输入校验，以及 60 组随机小拓扑上与"枚举全部最短路"参考实现的
逐候选对照。

## 复杂度

预检：每批构造并集图 `O(V+E)` 后做一次 `O(V+E)` 的确定性找环，共 `B` 批；
两次 Dijkstra 为 `O((V+E) log V)`。整体约 `O((V+E) log V + B(V+E))`。

诊断：见上文"单向丢包探针诊断 → 复杂度"。
