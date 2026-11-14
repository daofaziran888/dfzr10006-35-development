"""服务版本回退接口：维护版本快照，覆盖创建快照、执行回退、清理快照等动作。

回退预演相关接口提供两侧差异分类、待审批回退记录清单与审批动作；
审批通过前不会改动服务注册发现里的实例版本。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rollback import PREVIEW_ACTION_RULES, RollbackService, rollback_service

router = APIRouter(prefix="/api/rollback", tags=["服务版本回退"])

service: RollbackService = rollback_service

LIST_FIELDS = ["快照编号", "服务名称", "版本号", "配置快照", "创建时间", "回退次数", "最近回退", "快照状态"]
STATUSES = ["草稿", "已创建", "已回退", "已清理"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按快照编号检索"),
    status: str | None = Query(default=None, description="草稿、已创建、已回退、已清理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按快照编号与状态过滤服务版本回退列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/previews", response_model=PageResult[dict])
def list_previews(
    service_group: str | None = Query(default=None, description="按服务组过滤"),
    status: str | None = Query(default=None, description="预演中、待审批、已通过、已驳回、已取消"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """回退预演列表：同组版本再次预演时复用已有结果，不会重复生成。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_previews(service_group=service_group, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/previews", response_model=ActionResult)
def create_preview(payload: EntryPayload) -> ActionResult:
    """创建回退预演：把两侧差异分成允许撤回、需审批、禁止撤回三类并生成回退记录。"""
    preview, message, _reused = service.create_preview(payload.values)
    if preview is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=preview)


@router.get("/previews/diff", response_model=dict)
def preview_diff(
    source: str = Query(description="当前快照编号，例如 ROLL-0002"),
    target: str = Query(description="目标快照编号，例如 ROLL-0003"),
) -> dict:
    """只读的预演比对：返回三类差异，不生成回退记录，也不改动运行实例。"""
    result, message = service.preview_diff(source, target)
    if result is None:
        raise HTTPException(status_code=400, detail=message)
    return result


@router.get("/previews/pending", response_model=PageResult[dict])
def list_pending_approvals(
    service_group: str | None = Query(default=None, description="按服务组过滤"),
) -> PageResult[dict]:
    """待审批的回退记录清单：列出预演生成、尚未审批通过的回退记录。"""
    items = service.pending_approvals(service_group=service_group)
    return PageResult(items=items, total=len(items), page=1, size=max(len(items), 1))


@router.get("/previews/{preview_id}", response_model=dict)
def get_preview(preview_id: str) -> dict:
    """读取单次预演结果；支持预演编号（PREV-0001）或数字 ID，历史结果可重复打开。"""
    preview = service.get_preview(preview_id)
    if preview is None:
        raise HTTPException(status_code=404, detail=f"回退预演 {preview_id} 不存在或已归档")
    return preview


@router.post("/previews/{preview_id}/actions", response_model=ActionResult)
def run_preview_action(preview_id: str, payload: EntryPayload) -> ActionResult:
    """执行提交审批、审批通过、审批驳回、取消预演；只有审批通过才改写运行实例版本。"""
    action = str(payload.values.get("action") or "").strip()
    if action not in PREVIEW_ACTION_RULES:
        return ActionResult(ok=False, message=f"动作「{action}」不属于回退预演可执行范围")
    preview, message = service.run_preview_action(preview_id, action)
    if preview is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=preview)


@router.get("/snapshots/{snapshot_code}", response_model=dict)
def get_snapshot(snapshot_code: str) -> dict:
    """历史快照按原编号查询，例如 ROLL-0002；同时列出引用该快照的预演。"""
    snapshot = service.get_snapshot_by_code(snapshot_code)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"版本快照 {snapshot_code} 不存在或已归档")
    return snapshot


@router.get("/version-recovery", response_model=dict)
def version_recovery() -> dict:
    """概览用版本恢复结果：默认按 ROLL-0002 的配置项与 ROLL-0003 的版本号核对。"""
    return service.version_recovery_overview()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出服务版本回退清单：返回当前过滤条件下的全量数据。

    该路由必须在 /{entry_id} 之前注册，否则 export 会被当成快照 ID 解析。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rollback", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条版本快照明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"版本快照 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条版本快照，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="版本快照已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条版本快照执行创建快照、执行回退、清理快照；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
