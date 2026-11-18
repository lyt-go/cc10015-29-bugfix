"""分群对比接口：对比任务列表、运行分析、结果表落库查询与状态流转。

列表、详情、导出与消费端全量结果表都读同一份落库数据（segment_compare_result），
差异方向与显著性基于同一对分群的同一成员集合，不会出现对不上的情况。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.segment_compare import SegmentCompareService

router = APIRouter(prefix="/api/segment_compare", tags=["分群对比"])
service = SegmentCompareService()
LIST_FIELDS = ["任务编号", "分群A", "分群B", "对比指标", "时间范围", "差异阈值", "显著性水平", "比较方法"]
STATUSES = ["草稿", "计算中", "已完成", "已废弃"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": service.summary()}


@router.get("")
def list_results() -> dict[str, Any]:
    items = service.list_results()
    return {"items": items, "total": len(items)}


@router.get("/results")
def get_result_table() -> dict[str, Any]:
    """消费端取全量分群对比结果表：结果确认后消费的就是这份落库数据。"""
    items = service.result_table()
    return {"items": items, "total": len(items)}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    rows = service.run_analysis(payload.values)
    return {"items": rows, "total": len(rows)}


@router.post("/recover")
def recover_failed() -> dict[str, Any]:
    """恢复计算：只重算上次失败的受影响任务，其余结果保持不动。"""
    rows, failed = service.recover_failed()
    return {"items": rows, "total": len(rows), "failed": failed}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=segment_compare.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"对比任务 {entry_id} 不存在")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """开始计算会重算该任务并落库；确认完成后结果表交消费端全量读取。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
