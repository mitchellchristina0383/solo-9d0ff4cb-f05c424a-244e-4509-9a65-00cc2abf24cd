"""诊断轮存储。

每轮保存固定拓扑、新版代价、共同目的路由器与探针记录。任何一次更正都会在
同一把锁内重算诊断快照，保证"记录与诊断同时生效"，不会读到半更新的状态。

传入 ``path`` 时把全部轮次原子写入 JSON 文件（先写临时文件再 ``os.replace``），
进程重启后重新加载并重算诊断；未传 ``path`` 则仅供进程内使用。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .diagnosis import DiagnosisResult, ProbeObservation, diagnose
from .models import ProbeInput, RoundLink


@dataclass
class RoundRecord:
    round_id: str
    target: str
    links: list[RoundLink]
    probes: dict[str, ProbeObservation] = field(default_factory=dict)
    diagnosis: DiagnosisResult | None = None
    nodes: set[str] = field(default_factory=set)


class RoundStore:
    """线程安全存储；可选 JSON 文件持久化。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.Lock()
        self._rounds: dict[str, RoundRecord] = {}
        self._path = Path(path) if path else None
        if self._path is not None:
            self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for item in raw.get("rounds", []):
            try:
                links = [RoundLink(**lk) for lk in item["links"]]
                record = RoundRecord(
                    round_id=item["round_id"],
                    target=item["target"],
                    links=links,
                    nodes={lk.a for lk in links} | {lk.b for lk in links},
                )
                for probe in item["probes"]:
                    record.probes[probe["probe_id"]] = ProbeObservation(
                        probe_id=probe["probe_id"],
                        source=probe["source"],
                        captured=tuple(probe["captured"]),
                        result=probe["result"],
                    )
                self._refresh_locked(record)
                self._rounds[record.round_id] = record
            except (KeyError, TypeError, ValueError):
                # 单轮数据损坏不影响其余轮次
                continue

    def _save_locked(self) -> None:
        if self._path is None:
            return
        payload = {
            "rounds": [
                {
                    "round_id": r.round_id,
                    "target": r.target,
                    "links": [
                        {"a": lk.a, "b": lk.b, "new_cost": lk.new_cost}
                        for lk in r.links
                    ],
                    "probes": [
                        {
                            "probe_id": p.probe_id,
                            "source": p.source,
                            "captured": list(p.captured),
                            "result": p.result,
                        }
                        for p in r.probes.values()
                    ],
                }
                for r in self._rounds.values()
            ]
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------ 读取
    def get(self, round_id: str) -> RoundRecord | None:
        with self._lock:
            record = self._rounds.get(round_id)
            return _snapshot(record) if record is not None else None

    # ------------------------------------------------------------ 写入
    def create(self, target: str, links: list[RoundLink], probes: list[ProbeInput]) -> RoundRecord:
        nodes = {lk.a for lk in links} | {lk.b for lk in links}
        round_id = uuid.uuid4().hex[:12]
        record = RoundRecord(
            round_id=round_id,
            target=target,
            links=list(links),
            nodes=nodes,
        )
        for probe in probes:
            record.probes[probe.probe_id] = ProbeObservation(
                probe_id=probe.probe_id,
                source=probe.source,
                captured=tuple(probe.captured),
                result=probe.result,
            )
        with self._lock:
            self._refresh_locked(record)
            self._rounds[round_id] = record
            try:
                self._save_locked()
            except OSError:
                # 落盘失败则撤回内存写入，避免记录与磁盘不一致
                del self._rounds[round_id]
                raise
            return _snapshot(record)

    def upsert_probe(
        self,
        round_id: str,
        probe_id: str,
        source: str,
        captured: list[str],
        result: str,
    ) -> RoundRecord | None:
        """按编号新增或更正一条记录，并同步重算诊断。"""
        with self._lock:
            record = self._rounds.get(round_id)
            if record is None:
                return None
            previous = record.probes.get(probe_id)
            previous_diagnosis = record.diagnosis
            record.probes[probe_id] = ProbeObservation(
                probe_id=probe_id,
                source=source,
                captured=tuple(captured),
                result=result,
            )
            self._refresh_locked(record)
            try:
                self._save_locked()
            except OSError:
                if previous is None:
                    del record.probes[probe_id]
                else:
                    record.probes[probe_id] = previous
                record.diagnosis = previous_diagnosis
                raise
            return _snapshot(record)

    def _refresh_locked(self, record: RoundRecord) -> None:
        record.diagnosis = diagnose(
            record.nodes, record.links, record.target, record.probes.values()
        )


def _snapshot(record: RoundRecord) -> RoundRecord:
    """返回可安全用于响应的副本（记录与诊断一致）。"""
    return RoundRecord(
        round_id=record.round_id,
        target=record.target,
        links=list(record.links),
        probes=dict(record.probes),
        diagnosis=record.diagnosis,
        nodes=set(record.nodes),
    )
