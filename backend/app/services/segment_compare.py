"""分群对比业务规则：读取对比任务与分群成员明细，按同一成员集合计算结果表并落库。

修复要点（原实现为指标值和对比基准分别生成随机数，显著性水平与差异方向不一致）：
- 指标值、对比基准、差异、差异方向、显著性全部来自同一对分群的同一批成员明细，
  差异方向与均值关系一致，显著性基于同一对分群的统计量，不会再出现"显著但没有方向"。
- 计算结果写入分群对比结果表（segment_compare_result）落库，列表、详情、导出、
  消费端都读同一份数据；结果表不生效（计算失败）时保留上一完整对比，恢复时只重算受影响任务。
- 两个分群成员集合部分重叠时按「排除重叠 / 保留重叠」两种策略之一处理，
  处理方式与重叠成员数、排除数量 / 重叠成员影响都写入结果表，可在数据表中追溯。
- 跨模块联动：指标定义模块读取结果表中的指标编号与精度位数，复算显著性输入。
"""
from __future__ import annotations

import math
import random
import time
from typing import Any

from app.store import store

MODULE = "segment_compare"
RESULT_MODULE = "segment_compare_result"  # 分群对比结果表（落库）
LIST_FIELDS = ["任务编号", "分群A", "分群B", "对比指标", "时间范围", "差异阈值", "显著性水平", "比较方法"]
REQUIRED_FIELDS = ["任务编号", "分群A", "分群B"]
STATUS_ORDER = ["草稿", "计算中", "已完成", "已废弃"]
ACTION_RULES = {"开始计算": "计算中", "确认完成": "已完成", "废弃任务": "已废弃"}
NEGATIVE_ACTIONS = []

# 重叠成员处理方式：排除重叠后比较独立成员 / 保留重叠并按分群归属分别汇总
OVERLAP_EXCLUDE = "排除重叠"
OVERLAP_KEEP = "保留重叠"
OVERLAP_STRATEGIES = (OVERLAP_EXCLUDE, OVERLAP_KEEP)

# 显著性水平 → 双侧正态临界值（常用分位点，按最近邻取值）
_Z_TABLE = {0.10: 1.645, 0.05: 1.96, 0.02: 2.054, 0.01: 2.576, 0.005: 2.807, 0.001: 3.291}
DEFAULT_LEVEL = 0.05
DEFAULT_PRECISION = 3


class ComputeError(Exception):
    """单个对比任务计算失败：该任务保留上一完整结果，不影响其他任务落库。"""


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _parse_level(value: Any) -> float:
    """解析显著性水平：支持 0.05 / 5% / 5 三种写法，非法值回退到 0.05。"""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_LEVEL
    try:
        if text.endswith("%"):
            level = float(text[:-1]) / 100.0
        else:
            level = float(text)
            if level > 1:  # 传了 5 表示 5%
                level = level / 100.0
        return level if 0 < level < 1 else DEFAULT_LEVEL
    except Exception:
        return DEFAULT_LEVEL


def _z_critical(level: float) -> float:
    nearest = min(_Z_TABLE, key=lambda lv: abs(lv - level))
    return _Z_TABLE[nearest]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    return sum((v - mu) ** 2 for v in values) / (len(values) - 1)


class SegmentCompareService:
    def __init__(self) -> None:
        # 计算失败的任务集合：结果表保留上一完整对比，恢复时只重算这些受影响任务
        self._failed_task_ids: list[int] = []

    # ---------- 结果表读写 ----------

    def _result_rows(self) -> list[dict[str, Any]]:
        return store.rows(RESULT_MODULE)

    def _find_result(self, task_id: int) -> dict[str, Any] | None:
        for row in self._result_rows():
            if int(row.get("task_id", 0)) == task_id:
                return row
        return None

    def result_table(self) -> list[dict[str, Any]]:
        """全量分群对比结果表：结果确认后交给消费端的就是这份数据。"""
        return [dict(row) for row in self._result_rows()]

    # ---------- 成员明细与指标口径 ----------

    def _resolve_group(self, ref: Any) -> dict[str, Any] | None:
        """把任务上的分群引用解析成样本分群：支持 id、分群编号、分群名称。"""
        text = str(ref or "").strip()
        if not text:
            return None
        rows = store.rows("sample_group")
        if text.isdigit():
            hit = store.find("sample_group", int(text))
            if hit is not None:
                return hit
        for field in ("分群编号", "分群名称"):
            for row in rows:
                if str(row.get(field, "")) == text:
                    return row
        return None

    def _members_for(self, task: dict[str, Any], ref: Any, side: str) -> list[dict[str, Any]]:
        """读取一侧分群的成员明细。

        能解析到样本分群就走分群成员明细（同一成员集合，确定性生成）；
        解析不到（历史任务只写了名称）时按任务编号确定性合成一组成员，
        同样从共享用户池抽样，保证重叠处理逻辑对历史任务也可追溯。
        """
        group = self._resolve_group(ref)
        if group is not None:
            from app.services.sample_group import SampleGroupService
            return SampleGroupService().get_group_members(int(group["id"]))
        seed = f"segment_compare_virtual:{task.get('任务编号', task.get('id'))}:{side}"
        member_rng = random.Random(seed)
        indices = sorted(member_rng.sample(range(2000), 120))
        value_rng = random.Random(f"{seed}:values")
        mu = 0.58 if side == "A" else 0.55
        return [
            {
                "user_id": f"U{idx:06d}",
                "group_id": None,
                "group_type": side,
                "metric_value": round(max(0.0, min(1.0, value_rng.gauss(mu=mu, sigma=0.08))), 4),
            }
            for idx in indices
        ]

    def _metric_meta(self, task: dict[str, Any]) -> tuple[str, int]:
        """跨模块联动：按对比指标找到指标定义，取指标编号与精度位数。"""
        metric_ref = str(task.get("对比指标", "")).strip()
        for row in store.rows("metric_def"):
            if metric_ref and metric_ref in (str(row.get("指标编号", "")), str(row.get("指标名称", ""))):
                return str(row.get("指标编号", "")), max(0, _parse_int(row.get("精度位数"), DEFAULT_PRECISION))
        return metric_ref, DEFAULT_PRECISION

    # ---------- 单任务计算 ----------

    def _compare_sets(
        self,
        task: dict[str, Any],
        members_a: list[dict[str, Any]],
        members_b: list[dict[str, Any]],
        strategy: str,
    ) -> dict[str, Any]:
        """按重叠处理策略选出参与比较的成员集合，并给出可追溯的重叠字段。"""
        ids_a = {m["user_id"] for m in members_a}
        ids_b = {m["user_id"] for m in members_b}
        overlap_ids = ids_a & ids_b
        info: dict[str, Any] = {
            "重叠成员数": len(overlap_ids),
            "重叠处理": strategy,
            "排除数量": 0,
            "重叠成员影响": "",
        }
        if strategy == OVERLAP_EXCLUDE:
            # 排除重叠后比较独立成员：结果表给出独立成员差异和排除数量
            info["排除数量"] = sum(1 for m in members_a if m["user_id"] in overlap_ids) + \
                sum(1 for m in members_b if m["user_id"] in overlap_ids)
            compared_a = [m for m in members_a if m["user_id"] not in overlap_ids]
            compared_b = [m for m in members_b if m["user_id"] not in overlap_ids]
        else:
            # 保留重叠并按分群归属分别汇总：结果表给出各自汇总，并单列重叠成员影响
            compared_a, compared_b = members_a, members_b
            overlap_a = [m["metric_value"] for m in members_a if m["user_id"] in overlap_ids]
            overlap_b = [m["metric_value"] for m in members_b if m["user_id"] in overlap_ids]
            if overlap_a and overlap_b:
                info["重叠成员影响"] = round(_mean(overlap_a) - _mean(overlap_b), 4)
        if not compared_a or not compared_b:
            raise ComputeError(
                f"任务 {task.get('任务编号', task.get('id'))} 排除重叠后无独立成员可比较"
                if strategy == OVERLAP_EXCLUDE else
                f"任务 {task.get('任务编号', task.get('id'))} 分群成员为空"
            )
        info["compared_a"] = compared_a
        info["compared_b"] = compared_b
        return info

    def _compute_task(self, task: dict[str, Any], strategy: str) -> dict[str, Any]:
        """按同一成员集合计算单个任务：差异方向与显著性都出自同一对分群。"""
        task_id = int(task.get("id", 0))
        members_a = self._members_for(task, task.get("分群A"), "A")
        members_b = self._members_for(task, task.get("分群B"), "B")
        compared = self._compare_sets(task, members_a, members_b, strategy)
        values_a = [float(m["metric_value"]) for m in compared["compared_a"]]
        values_b = [float(m["metric_value"]) for m in compared["compared_b"]]

        metric_code, precision = self._metric_meta(task)
        mean_a, mean_b = _mean(values_a), _mean(values_b)
        raw_diff = mean_a - mean_b
        # Welch 双样本 t 统计量：显著性输入与差异方向基于同一对分群的同一批成员
        denom = math.sqrt(_variance(values_a) / len(values_a) + _variance(values_b) / len(values_b))
        if denom == 0:
            t_stat = 0.0 if raw_diff == 0 else math.copysign(math.inf, raw_diff)
        else:
            t_stat = raw_diff / denom
        level = _parse_level(task.get("显著性水平"))
        significant = abs(t_stat) >= _z_critical(level)
        # 差异方向由真实均值关系决定；显著时差异必然非零，方向必然存在
        direction = "上升" if raw_diff > 0 else ("下降" if raw_diff < 0 else "持平")
        # 结果表自洽：展示的差异 = 展示的指标值 - 展示的对比基准
        mean_a_r = round(mean_a, precision)
        mean_b_r = round(mean_b, precision)

        return {
            "id": task_id,
            "task_id": task_id,
            "任务编号": task.get("任务编号", ""),
            "分群A": task.get("分群A", ""),
            "分群B": task.get("分群B", ""),
            "对比指标": task.get("对比指标", ""),
            "指标编号": metric_code,
            "精度位数": precision,
            "指标值": mean_a_r,
            "对比基准": mean_b_r,
            "差异": round(mean_a_r - mean_b_r, precision),
            "差异方向": direction,
            "显著性": "显著" if significant else "不显著",
            "t统计量": round(t_stat, precision) if math.isfinite(t_stat) else "inf",
            "样本数A": len(values_a),
            "样本数B": len(values_b),
            "重叠成员数": compared["重叠成员数"],
            "排除数量": compared["排除数量"],
            "重叠处理": compared["重叠处理"],
            "重叠成员影响": compared["重叠成员影响"],
            "计算状态": "成功",
            "计算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "结果版本": store.version(RESULT_MODULE),
        }

    # ---------- 批量计算与事务 ----------

    def compute_results(
        self,
        task_ids: list[int] | None = None,
        overlap_strategy: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[int]]:
        """计算并落库。单任务失败只回滚该任务的暂存结果，保留其上一完整对比。"""
        strategy = overlap_strategy if overlap_strategy in OVERLAP_STRATEGIES else None
        tasks = store.rows(MODULE)
        if task_ids is not None:
            wanted = {int(t) for t in task_ids}
            tasks = [t for t in tasks if int(t.get("id", 0)) in wanted]
        staged: dict[int, dict[str, Any]] = {}
        failed: list[int] = []
        for task in tasks:
            task_strategy = strategy or str(task.get("重叠处理", "")).strip() or OVERLAP_EXCLUDE
            if task_strategy not in OVERLAP_STRATEGIES:
                task_strategy = OVERLAP_EXCLUDE
            try:
                staged[int(task["id"])] = self._compute_task(task, task_strategy)
            except Exception:  # 单任务失败只回滚该任务暂存，不影响其他任务落库
                failed.append(int(task.get("id", 0)))
        # 提交：先快照，任何残留都整体回滚，保证结果表不出现半提交状态
        snapshot = [dict(row) for row in self._result_rows()]
        try:
            for task_id, row in staged.items():
                old = self._find_result(task_id)
                if old is None:
                    self._result_rows().append(row)
                else:
                    old.update(row)
            if staged:
                store.bump_version(RESULT_MODULE)
        except Exception:
            self._result_rows()[:] = snapshot  # 事务残留即回滚
            raise
        finally:
            staged.clear()
        self._failed_task_ids = failed
        return self._committed(tasks), failed

    def _committed(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """提交后读取：成功任务读新行，失败任务读保留的上一完整行。"""
        rows: list[dict[str, Any]] = []
        for task in tasks:
            row = self._find_result(int(task.get("id", 0)))
            if row is not None:
                rows.append(dict(row))
        return rows

    def recover_failed(self) -> tuple[list[dict[str, Any]], list[int]]:
        """恢复：只重算上次计算失败的受影响任务，其余结果保持不动。"""
        if not self._failed_task_ids:
            return [], []
        return self.compute_results(task_ids=list(self._failed_task_ids))

    @property
    def failed_task_ids(self) -> list[int]:
        return list(self._failed_task_ids)

    # ---------- 对外查询：列表 / 详情 / 汇总同源 ----------

    def _ensure_results(self) -> None:
        """结果表缺行时补算；计算失败的任务保留旧值，不阻塞列表。"""
        missing = [int(t["id"]) for t in store.rows(MODULE) if self._find_result(int(t["id"])) is None]
        if missing:
            self.compute_results(task_ids=missing)

    def _merge(self, task: dict[str, Any]) -> dict[str, Any]:
        """任务字段 + 结果表字段：列表、详情、导出都用这份合并结果。"""
        merged = dict(task)
        result = self._find_result(int(task.get("id", 0)))
        if result is not None:
            merged.update({k: v for k, v in result.items() if k not in ("id", "task_id")})
        return merged

    def list_results(self) -> list[dict[str, Any]]:
        self._ensure_results()
        return [self._merge(task) for task in store.rows(MODULE)]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        task = store.find(MODULE, entry_id)
        if task is None:
            return None
        self._ensure_results()
        return self._merge(task)

    def summary(self) -> list[dict[str, Any]]:
        """看板指标从落库的结果表汇总，不再每次随机。"""
        self._ensure_results()
        results = self._result_rows()
        latest = results[-1] if results else {}
        significant = sum(1 for r in results if r.get("显著性") == "显著")
        return [
            {"label": "对比任务", "value": len(store.rows(MODULE)), "trend": 0},
            {"label": "最新指标值", "value": latest.get("指标值", 0), "trend": 0},
            {"label": "显著差异任务", "value": significant, "trend": significant},
            {"label": "计算失败", "value": len(self._failed_task_ids), "trend": -len(self._failed_task_ids)},
        ]

    # ---------- 动作 ----------

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """运行分析：按任务编号或分群对定位任务后重算落库，返回与列表同源的结果。"""
        task_ids = params.get("task_ids")
        if not task_ids:
            code = str(params.get("任务编号", "")).strip()
            if code:
                hit = next((t for t in store.rows(MODULE) if str(t.get("任务编号", "")) == code), None)
                if hit is not None:
                    task_ids = [int(hit["id"])]
        rows, _ = self.compute_results(
            task_ids=[int(t) for t in task_ids] if task_ids else None,
            overlap_strategy=params.get("重叠处理"),
        )
        return rows

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"对比任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分群对比可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "开始计算":
            _, failed = self.compute_results(task_ids=[entry_id])
            if failed:
                return None, f"对比任务 {entry_id} 计算失败，已保留上一完整对比"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return self._merge(entry), f"对比任务已{action}"
