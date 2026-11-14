"""服务版本回退业务规则：状态流转、字段校验与筛选口径都收在这里。

除了原有的快照登记与动作流转，这里还负责「回退预演」：
把两个同组版本快照的差异拆成允许撤回、需审批、禁止撤回三类，
生成待审批的回退记录清单，并在审批通过前保持服务注册发现里的实例版本不变。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Any

from app.store import store

MODULE = "rollback"
REQUIRED_FIELDS = ["快照编号", "服务名称", "版本号"]
STATUS_ORDER = ["草稿", "已创建", "已回退", "已清理"]
ACTION_RULES = {"创建快照": "草稿", "执行回退": "已创建", "清理快照": "已回退"}
NEGATIVE_ACTIONS = []

# 预演差异的三分类：允许撤回、需审批、禁止撤回。
ALLOWED = "允许撤回"
REVIEW = "需审批"
FORBIDDEN = "禁止撤回"
CATEGORY_ORDER = [ALLOWED, REVIEW, FORBIDDEN]

# 预演状态机：提交审批后进入待审批，审批通过才允许改写运行实例。
PREVIEW_DRAFT = "预演中"
PREVIEW_PENDING = "待审批"
PREVIEW_APPROVED = "已通过"
PREVIEW_REJECTED = "已驳回"
PREVIEW_CANCELLED = "已取消"
PREVIEW_TERMINAL = {PREVIEW_APPROVED, PREVIEW_REJECTED, PREVIEW_CANCELLED}
PREVIEW_ACTION_RULES: dict[str, tuple[str | None, str]] = {
    "提交审批": (PREVIEW_DRAFT, PREVIEW_PENDING),
    "审批通过": (PREVIEW_PENDING, PREVIEW_APPROVED),
    "审批驳回": (PREVIEW_PENDING, PREVIEW_REJECTED),
    "取消预演": (None, PREVIEW_CANCELLED),
}

# 服务标识与版本号的回退口径：服务名称属于身份，回退不改；版本切换必须审批。
FIELD_POLICY: dict[str, tuple[str, str]] = {
    "服务名称": (FORBIDDEN, "服务名称属于实例身份标识，回退时保持当前值"),
    "版本号": (REVIEW, "版本切换需审批通过后生效"),
}

# 配置项策略：未登记的配置项统一按「需审批」处理，避免默认放行。
CONFIG_POLICY: dict[str, tuple[str, str]] = {
    "超时秒数": (ALLOWED, "超时参数可随快照自动恢复"),
    "重试次数": (ALLOWED, "重试次数可随快照自动恢复"),
    "日志级别": (ALLOWED, "日志级别可随快照自动恢复"),
    "缓存过期秒数": (ALLOWED, "缓存过期时间可随快照自动恢复"),
    "最大连接数": (REVIEW, "连接数上限影响容量，需审批后恢复"),
    "限流阈值": (REVIEW, "限流阈值影响线上流量，需审批后恢复"),
    "副本数量": (REVIEW, "副本数量影响资源编排，需审批后恢复"),
    "数据源地址": (FORBIDDEN, "数据源地址涉及数据归属，禁止随版本回退改写"),
    "加密密钥": (FORBIDDEN, "加密密钥禁止随版本回退改写"),
}
DEFAULT_CONFIG_POLICY = (REVIEW, "未登记策略的配置项按需审批处理")

RECORD_STATUS = {
    ALLOWED: ("无需审批", "随回退执行"),
    REVIEW: ("待审批", "待执行"),
    FORBIDDEN: ("禁止撤回", "保持当前值"),
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


class RollbackService:
    def __init__(self) -> None:
        # 预演与回退记录都是运行期数据，审批通过前只读快照，不触碰注册实例。
        # 同步接口跑在线程池里，预演状态的读写统一加锁，避免并发提交审批时重复改写实例。
        self._lock = RLock()
        self._previews: list[dict[str, Any]] = []
        self._records: list[dict[str, Any]] = []
        self._preview_seq = 0
        self._record_seq = 0

    # ------------------------------------------------------------------
    # 快照：列表、明细、登记与原有动作
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("快照编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_snapshot_by_code(self, snapshot_code: str) -> dict[str, Any] | None:
        """历史快照按原编号查询，编号大小写与首尾空格都不影响检索。"""
        text = _as_text(snapshot_code).strip().upper()
        with self._lock:
            for row in store.rows(MODULE):
                if _as_text(row.get("快照编号")).strip().upper() == text:
                    found = deepcopy(row)
                    found["相关预演"] = [
                        preview["预演编号"]
                        for preview in self._previews
                        if text in {
                            _as_text(preview.get("当前快照")).upper(),
                            _as_text(preview.get("目标快照")).upper(),
                        }
                    ]
                    return found
        return None

    def _snapshot_by_ref(self, ref: Any) -> dict[str, Any] | None:
        text = _as_text(ref).strip()
        if text.isdigit():
            return store.find(MODULE, int(text))
        return self.get_snapshot_by_code(text)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.setdefault("服务组", values.get("服务组") or values.get("服务名称"))
        entry.setdefault("配置项", values.get("配置项") or {})
        entry.setdefault("配置快照", values.get("配置快照") or "未登记配置项")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"版本快照 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于服务版本回退可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"版本快照已{action}"

    # ------------------------------------------------------------------
    # 回退预演：差异分类、版本恢复概览与待审批记录
    # ------------------------------------------------------------------
    def _instances(self, service_group: str) -> list[dict[str, Any]]:
        """服务注册发现里同一服务组的实例，是回退真正影响到的运行对象。"""
        if not service_group:
            return []
        return [
            row
            for row in store.rows("registry")
            if _as_text(row.get("服务组")) == service_group
        ]

    def _current_instance_version(self, service_group: str) -> str | None:
        for instance in self._instances(service_group):
            version = _as_text(instance.get("服务版本")).strip()
            if version:
                return version
        return None

    def _classify(self, item: str, kind: str) -> tuple[str, str]:
        if kind == "配置项":
            return CONFIG_POLICY.get(item, DEFAULT_CONFIG_POLICY)
        return FIELD_POLICY.get(item, DEFAULT_CONFIG_POLICY)

    def _build_diff(
        self, source: dict[str, Any], target: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int, int]:
        """比较当前快照与目标快照，返回差异明细、配置项总数、无差异配置项数。"""
        diffs: list[dict[str, Any]] = []

        if _as_text(source.get("服务名称")) != _as_text(target.get("服务名称")):
            category, reason = self._classify("服务名称", "服务标识")
            diffs.append({
                "变更项": "服务名称",
                "类型": "服务标识",
                "当前值": _as_text(source.get("服务名称")),
                "目标值": _as_text(target.get("服务名称")),
                "分类": category,
                "说明": reason,
            })

        if _as_text(source.get("版本号")) != _as_text(target.get("版本号")):
            category, reason = self._classify("版本号", "版本")
            diffs.append({
                "变更项": "版本号",
                "类型": "版本",
                "当前值": _as_text(source.get("版本号")),
                "目标值": _as_text(target.get("版本号")),
                "分类": category,
                "说明": reason,
            })

        source_config = source.get("配置项") or {}
        target_config = target.get("配置项") or {}
        config_keys = sorted(set(source_config) | set(target_config))
        unchanged = 0
        for key in config_keys:
            current_value = _as_text(source_config.get(key))
            target_value = _as_text(target_config.get(key))
            if current_value == target_value:
                unchanged += 1
                continue
            category, reason = self._classify(key, "配置项")
            diffs.append({
                "变更项": key,
                "类型": "配置项",
                "当前值": current_value,
                "目标值": target_value,
                "分类": category,
                "说明": reason,
            })
        return diffs, len(config_keys), unchanged

    def _config_check_rows(
        self,
        source: dict[str, Any],
        target: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """按配置项来源快照逐项列出回退取值，供概览核对。"""
        source_config = source.get("配置项") or {}
        target_config = target.get("配置项") or {}
        config_keys = sorted(set(source_config) | set(target_config))
        rows: list[dict[str, Any]] = []
        for key in config_keys:
            current_value = _as_text(source_config.get(key))
            target_value = _as_text(target_config.get(key))
            category, reason = self._classify(key, "配置项")
            if current_value == target_value:
                action = "保持当前值"
                result_value = current_value
            elif category == ALLOWED:
                action = "自动恢复"
                result_value = target_value
            elif category == REVIEW:
                action = "审批通过后恢复"
                result_value = target_value
            else:
                action = "保持当前值（禁止改写）"
                result_value = current_value
            rows.append({
                "配置项": key,
                "当前值": current_value,
                "目标值": target_value,
                "分类": category,
                "恢复后取值": result_value,
                "处理方式": action,
                "说明": reason,
            })
        return rows

    def _verification_conclusion(
        self,
        version_result: dict[str, Any],
        config_check: list[dict[str, Any]],
        current_instance_version: str | None,
    ) -> dict[str, Any]:
        """核对概览里的版本恢复结果：版本号对 ROLL-0003、配置项对 ROLL-0002。"""
        expected_version = _as_text(version_result.get("目标值"))
        result_version = _as_text(version_result.get("恢复后取值"))
        version_ok = expected_version == result_version

        config_diff = [
            row for row in config_check
            if _as_text(row.get("当前值")) != _as_text(row.get("目标值"))
        ]
        config_ok = all(
            _as_text(row.get("恢复后取值"))
            == (
                _as_text(row.get("目标值"))
                if row.get("分类") in {ALLOWED, REVIEW}
                else _as_text(row.get("当前值"))
            )
            for row in config_diff
        )

        forbidden = [
            row["配置项"] for row in config_check if row.get("分类") == FORBIDDEN
            and _as_text(row.get("当前值")) != _as_text(row.get("目标值"))
        ]
        # 审批通过前运行实例应保持来源快照版本，通过后才等于目标快照版本。
        instance_ok = current_instance_version in {
            _as_text(version_result.get("当前值")),
            expected_version,
        }
        if not version_ok:
            conclusion = "版本恢复结果与版本来源快照不一致"
        elif not config_ok:
            conclusion = "配置项恢复结果与分类规则不一致"
        elif forbidden:
            conclusion = f"版本恢复结果与来源快照一致；{'、'.join(forbidden)}按禁止撤回保持当前值"
        else:
            conclusion = "版本恢复结果与来源快照一致"
        return {
            "版本一致": version_ok,
            "配置一致": config_ok,
            "实例版本一致": instance_ok,
            "禁止撤回项": forbidden,
            "结论": conclusion,
        }

    def _compose(self, source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        diffs, config_total, unchanged = self._build_diff(source, target)
        grouped: dict[str, list[dict[str, Any]]] = {category: [] for category in CATEGORY_ORDER}
        for row in diffs:
            grouped.setdefault(row["分类"], []).append(row)

        source_version = _as_text(source.get("版本号"))
        target_version = _as_text(target.get("版本号"))
        source_name = _as_text(source.get("服务名称"))
        allowed_config = sum(1 for row in diffs if row["类型"] == "配置项" and row["分类"] == ALLOWED)
        review_config = sum(1 for row in diffs if row["类型"] == "配置项" and row["分类"] == REVIEW)
        forbidden_config = sum(1 for row in diffs if row["类型"] == "配置项" and row["分类"] == FORBIDDEN)

        # 核对口径固定为：配置项以当前快照（ROLL-0002）为基线，版本号以目标快照（ROLL-0003）为准。
        config_check = self._config_check_rows(source, target)
        version_category, version_reason = self._classify("版本号", "版本")
        version_result = {
            "核对项": "版本号",
            "版本来源": _as_text(target.get("快照编号")),
            "当前值": source_version,
            "目标值": target_version,
            "分类": version_category,
            "恢复后取值": target_version if version_category in {ALLOWED, REVIEW} else source_version,
            "处理方式": "审批通过后切换" if version_category == REVIEW else "自动恢复",
            "说明": version_reason,
        }

        recovery = {
            "服务组": _as_text(source.get("服务组") or target.get("服务组")),
            "版本来源": _as_text(target.get("快照编号")),
            "配置来源": _as_text(source.get("快照编号")),
            "恢复前版本": source_version,
            "恢复后版本": target_version,
            "版本恢复": f"{source_version} → {target_version}",
            "恢复前服务名称": source_name,
            # 服务名称属于禁止撤回项，回退结果里保持当前值。
            "恢复后服务名称": source_name,
            "比较配置项": config_total,
            "无差异配置项": unchanged,
            "自动恢复配置项": allowed_config,
            "需审批配置项": review_config,
            "禁止撤回配置项": forbidden_config,
            "需审批变更项": len(grouped.get(REVIEW, [])),
            "禁止撤回变更项": len(grouped.get(FORBIDDEN, [])),
            "允许撤回变更项": len(grouped.get(ALLOWED, [])),
            "版本核对": version_result,
            "配置核对": config_check,
        }
        recovery["核对结论"] = self._verification_conclusion(
            version_result,
            config_check,
            self._current_instance_version(recovery["服务组"]),
        )
        return {
            "当前快照": _as_text(source.get("快照编号")),
            "目标快照": _as_text(target.get("快照编号")),
            "服务组": recovery["服务组"],
            "当前版本": source_version,
            "目标版本": target_version,
            "差异": {category: grouped.get(category, []) for category in CATEGORY_ORDER},
            "差异明细": diffs,
            "版本恢复概览": recovery,
        }

    def preview_diff(
        self, source_ref: Any, target_ref: Any
    ) -> tuple[dict[str, Any] | None, str]:
        """只计算差异、不落库：给页面首次打开时展示预演视图用。"""
        with self._lock:
            source = self._snapshot_by_ref(source_ref)
            if source is None:
                return None, f"当前快照「{_as_text(source_ref).strip()}」不存在或已归档"
            target = self._snapshot_by_ref(target_ref)
            if target is None:
                return None, f"目标快照「{_as_text(target_ref).strip()}」不存在或已归档"
            source_code = _as_text(source.get("快照编号"))
            target_code = _as_text(target.get("快照编号"))
            if source_code == target_code:
                return None, "当前快照与目标快照相同，无需预演"
            source_group = _as_text(source.get("服务组") or source.get("服务名称"))
            target_group = _as_text(target.get("服务组") or target.get("服务名称"))
            if source_group and target_group and source_group != target_group:
                return None, f"快照 {source_code} 与 {target_code} 不属于同一服务组，不能互相预演"
            composed = self._compose(source, target)
            return {
                "预演编号": None,
                "预演状态": PREVIEW_DRAFT,
                "已保存": False,
                "当前实例版本": self._current_instance_version(composed["服务组"]),
                "回退记录": [],
                "待审批回退记录": [],
                **composed,
            }, "已生成预演比对，尚未保存预演记录"

    def create_preview(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, bool]:
        with self._lock:
            return self._create_preview(values)

    def _create_preview(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, bool]:
        source_ref = (
            values.get("当前快照")
            or values.get("来源快照")
            or values.get("sourceSnapshot")
            or values.get("source")
        )
        target_ref = (
            values.get("目标快照")
            or values.get("targetSnapshot")
            or values.get("target")
        )
        if not _as_text(source_ref).strip() or not _as_text(target_ref).strip():
            return None, "缺少预演字段：请同时提供当前快照与目标快照", False

        source = self._snapshot_by_ref(source_ref)
        if source is None:
            return None, f"当前快照「{_as_text(source_ref).strip()}」不存在或已归档", False
        target = self._snapshot_by_ref(target_ref)
        if target is None:
            return None, f"目标快照「{_as_text(target_ref).strip()}」不存在或已归档", False

        source_code = _as_text(source.get("快照编号"))
        target_code = _as_text(target.get("快照编号"))
        if source_code == target_code:
            return None, "当前快照与目标快照相同，无需预演", False

        source_group = _as_text(source.get("服务组") or source.get("服务名称"))
        target_group = _as_text(target.get("服务组") or target.get("服务名称"))
        if source_group and target_group and source_group != target_group:
            return None, f"快照 {source_code} 与 {target_code} 不属于同一服务组，不能互相预演", False

        # 同组同一对版本再次预演时直接打开已有结果，避免重复生成审批记录。
        for preview in self._previews:
            if (
                preview["当前快照"] == source_code
                and preview["目标快照"] == target_code
                and preview["预演状态"] != PREVIEW_CANCELLED
            ):
                return self.serialize_preview(preview), f"已打开同组版本的预演结果 {preview['预演编号']}", True

        composed = self._compose(source, target)
        self._preview_seq += 1
        preview_code = f"PREV-{self._preview_seq:04d}"
        now = _now()
        preview: dict[str, Any] = {
            "id": self._preview_seq,
            "预演编号": preview_code,
            "预演状态": PREVIEW_DRAFT,
            "创建时间": now,
            "更新时间": now,
            **composed,
        }

        records: list[dict[str, Any]] = []
        for index, row in enumerate(composed["差异明细"], start=1):
            self._record_seq += 1
            status, result = RECORD_STATUS.get(row["分类"], RECORD_STATUS[REVIEW])
            records.append({
                "id": self._record_seq,
                "记录编号": f"RTR-{self._preview_seq:04d}-{index:02d}",
                "预演编号": preview_code,
                "快照编号": preview["当前快照"],
                "目标快照": preview["目标快照"],
                "服务组": preview["服务组"],
                "服务名称": preview["版本恢复概览"].get("恢复前服务名称"),
                "变更项": row["变更项"],
                "类型": row["类型"],
                "当前值": row["当前值"],
                "目标值": row["目标值"],
                "分类": row["分类"],
                "审批状态": status,
                "处理结果": result,
                "创建时间": now,
            })
        self._records.extend(records)
        self._previews.append(preview)
        return self.serialize_preview(preview), f"回退预演已创建，生成 {len(records)} 条回退记录", False

    def _find_preview(self, ref: Any) -> dict[str, Any] | None:
        text = _as_text(ref).strip()
        for preview in self._previews:
            if str(preview["id"]) == text or preview["预演编号"] == text:
                return preview
        return None

    def _preview_records(self, preview_code: str) -> list[dict[str, Any]]:
        return [
            deepcopy(record)
            for record in self._records
            if record["预演编号"] == preview_code
        ]

    def serialize_preview(self, preview: dict[str, Any]) -> dict[str, Any]:
        data = deepcopy(preview)
        records = self._preview_records(preview["预演编号"])
        data["当前实例版本"] = self._current_instance_version(preview["服务组"])
        data["回退记录"] = records
        data["待审批回退记录"] = [
            record for record in records if record["审批状态"] == "待审批"
        ]
        return data

    def list_previews(
        self,
        *,
        service_group: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(reversed(self._previews))
            if service_group:
                rows = [row for row in rows if row["服务组"] == service_group]
            if status:
                rows = [row for row in rows if row["预演状态"] == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [self.serialize_preview(row) for row in rows[start:start + size]], total

    def get_preview(self, ref: Any) -> dict[str, Any] | None:
        with self._lock:
            preview = self._find_preview(ref)
            if preview is None:
                return None
            return self.serialize_preview(preview)

    def pending_approvals(self, service_group: str | None = None) -> list[dict[str, Any]]:
        """待审批的回退记录清单：预演生成后立即列出所有审批状态为待审批的记录。"""
        with self._lock:
            rows: list[dict[str, Any]] = []
            for record in self._records:
                if record["审批状态"] != "待审批":
                    continue
                if service_group and record["服务组"] != service_group:
                    continue
                rows.append(deepcopy(record))
            return rows

    def _apply_version(self, preview: dict[str, Any]) -> None:
        """审批通过后才改写注册实例版本；这是唯一会动运行实例的路径。"""
        group = preview["服务组"]
        target_version = preview["目标版本"]
        for instance in self._instances(group):
            instance["服务版本"] = target_version

    def run_preview_action(
        self, ref: Any, action: str
    ) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            return self._run_preview_action(ref, action)

    def _run_preview_action(
        self, ref: Any, action: str
    ) -> tuple[dict[str, Any] | None, str]:
        preview = self._find_preview(ref)
        if preview is None:
            return None, f"回退预演 {_as_text(ref).strip()} 不存在或已归档"
        action = _as_text(action).strip()
        if action not in PREVIEW_ACTION_RULES:
            return None, f"动作「{action}」不属于回退预演可执行范围"

        expected, target_status = PREVIEW_ACTION_RULES[action]
        if action == "取消预演":
            if preview["预演状态"] in PREVIEW_TERMINAL:
                return None, f"预演 {preview['预演编号']} 已处于「{preview['预演状态']}」，不能再取消"
        elif preview["预演状态"] != expected:
            return None, f"预演 {preview['预演编号']} 当前为「{preview['预演状态']}」，不能执行「{action}」"

        preview["预演状态"] = target_status
        preview["更新时间"] = _now()

        for record in self._records:
            if record["预演编号"] != preview["预演编号"]:
                continue
            if action == "取消预演":
                if record["审批状态"] == "待审批":
                    record["审批状态"] = "已取消"
                    record["处理结果"] = "未执行"
                elif record["分类"] == ALLOWED:
                    record["处理结果"] = "未执行"
            elif action == "审批驳回":
                if record["审批状态"] == "待审批":
                    record["审批状态"] = "已驳回"
                    record["处理结果"] = "未执行"
                elif record["分类"] == ALLOWED:
                    record["处理结果"] = "未执行"
            elif action == "审批通过":
                if record["审批状态"] == "待审批":
                    record["审批状态"] = "已通过"
                    record["处理结果"] = "已恢复"
                elif record["分类"] == ALLOWED:
                    record["处理结果"] = "已恢复"

        if action == "审批通过":
            self._apply_version(preview)

        return self.serialize_preview(preview), f"回退预演已{action}"

    def version_recovery_overview(self) -> dict[str, Any]:
        """给运营概览用的版本恢复结果；没有预演时按 ROLL-0002/ROLL-0003 口径展示。"""
        with self._lock:
            return self._version_recovery_overview()

    def _version_recovery_overview(self) -> dict[str, Any]:
        active = [p for p in self._previews if p["预演状态"] != PREVIEW_CANCELLED]
        if active:
            preview = active[-1]
            recovery = deepcopy(preview["版本恢复概览"])
            recovery.update({
                "预演编号": preview["预演编号"],
                "预演状态": preview["预演状态"],
                "当前实例版本": self._current_instance_version(preview["服务组"]),
                "待审批回退记录": len([
                    record
                    for record in self._records
                    if record["预演编号"] == preview["预演编号"]
                    and record["审批状态"] == "待审批"
                ]),
            })
            return recovery

        source = self.get_snapshot_by_code("ROLL-0002")
        target = self.get_snapshot_by_code("ROLL-0003")
        if source is None or target is None:
            return {
                "预演编号": None,
                "预演状态": "未预演",
                "版本来源": "ROLL-0003",
                "配置来源": "ROLL-0002",
                "恢复前版本": "",
                "恢复后版本": "",
                "当前实例版本": "",
                "自动恢复配置项": 0,
                "需审批变更项": 0,
                "禁止撤回变更项": 0,
                "待审批回退记录": 0,
            }
        recovery = self._compose(source, target)["版本恢复概览"]
        recovery.update({
            "预演编号": None,
            "预演状态": "未预演",
            "当前实例版本": self._current_instance_version(recovery["服务组"]),
            "待审批回退记录": 0,
        })
        return recovery


# 路由与运营概览共享同一份预演状态。
rollback_service = RollbackService()
