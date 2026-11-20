from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from statistics import NormalDist

from .inference import difference_interval, sample_ratio, two_proportion_test
from .spec import SPEC


_FIELDS = {field["key"]: field for field in SPEC["fields"]}
_ARMS = tuple(_FIELDS["arm"]["options"])
_COHORTS = tuple(_FIELDS["cohort"]["options"])
_ARM_LABELS = {"A": "A · 原提醒", "B": "B · 新提醒"}
_MISSING = "—"


@dataclass(frozen=True)
class Assignment:
    assignment_id: str
    recipient_id: str
    cohort: str
    arm: str
    assigned_on: date
    delivered_on: date | None
    picked_up_on: date | None
    complained_on: date | None


@dataclass
class Counts:
    assigned: int = 0
    mature: int = 0
    delivered: int = 0
    picked_up: int = 0
    complained: int = 0


def _date(value, label: str, *, optional=False) -> date | None:
    if optional and (value is None or isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
            if parsed.isoformat() == value.strip():
                return parsed
        except ValueError:
            pass
    raise ValueError(f"{label}须填写有效日期，格式为 YYYY-MM-DD。")


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空，请填写有效文本。")
    return value.strip()


def _normalize_rows(rows) -> list[Assignment]:
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("实验台账没有分配记录，请导入至少一行数据。")
    assignments = []
    seen = {"assignment_id": {}, "recipient_id": {}}
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {number} 条记录格式有误，请提供由台账列名映射的字段。")
        normalized = {}
        for key, field in _FIELDS.items():
            label = f"第 {number} 条记录的{field['label']}"
            value = row.get(key)
            if field["type"] == "date":
                normalized[key] = _date(value, label, optional=not field["required"])
            else:
                normalized[key] = _text(value, label)
                if field["type"] == "select" and normalized[key] not in field["options"]:
                    options = "、".join(field["options"])
                    raise ValueError(f"{label}应为 {options} 中的一项。")
        assignment = Assignment(**normalized)
        for key, seen_rows in seen.items():
            identifier = normalized[key]
            if identifier in seen_rows:
                field_label = _FIELDS[key]["label"]
                first = seen_rows[identifier]
                raise ValueError(f"第 {first} 条与第 {number} 条记录的{field_label}重复（{identifier}），请恢复首次唯一分配。")
            seen_rows[identifier] = number
        for key in ("delivered_on", "picked_up_on", "complained_on"):
            event = normalized[key]
            if event is not None and event < assignment.assigned_on:
                label = _FIELDS[key]["label"]
                raise ValueError(f"第 {number} 条记录（{assignment.assignment_id}）的{label}早于随机分配日，请核对事件归属。")
        assignments.append(assignment)
    return assignments


def validate_rows(rows) -> None:
    _normalize_rows(rows)


def _settings(params) -> dict:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("请以字段名和参数值提交分析参数。")
    if params.keys() - {field["key"] for field in SPEC["params"]}:
        raise ValueError("存在未知分析参数，请核对分析口径中的字段。")
    settings = {}
    for field in SPEC["params"]:
        key = field["key"]
        label = field["label"]
        value = params.get(key, field["default"])
        if field["type"] == "date":
            settings[key] = _date(value, label)
            continue
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError
            number = float(value)
        except (ValueError, OverflowError):
            number = float("nan")
        if not isfinite(number) or not field["min"] <= number <= field["max"]:
            raise ValueError(f"{label}须为 {field['min']} 到 {field['max']} 之间的有限数值。")
        if field["type"] == "int":
            if not number.is_integer():
                raise ValueError(f"{label}须填写整数。")
            number = int(number)
        settings[key] = number
    return settings


def _in_window(assigned: date, event: date | None, cutoff: date, days: int) -> bool:
    return event is not None and event < cutoff and (event - assigned).days < days


def _visible(event: date | None, cutoff: date) -> str:
    return event.isoformat() if event is not None and event < cutoff else _MISSING


def _tabulate(assignments, cutoff, days):
    overall = {arm: Counts() for arm in _ARMS}
    cohorts = {cohort: {arm: Counts() for arm in _ARMS} for cohort in _COHORTS}
    excluded = Counter()
    audit = []
    examples = Counter()
    for assignment in assignments:
        arm, assigned = assignment.arm, assignment.assigned_on
        age = (cutoff - assigned).days
        if age <= 0:
            excluded[arm] += 1
            category = "截止日及之后分配"
        else:
            for counts in (overall[arm], cohorts[assignment.cohort][arm]):
                counts.assigned += 1
            if age < days:
                category = "观察窗未成熟"
            else:
                delivered = _in_window(assigned, assignment.delivered_on, cutoff, days)
                picked_up = _in_window(assigned, assignment.picked_up_on, cutoff, days)
                complained = _in_window(assigned, assignment.complained_on, cutoff, days)
                for counts in (overall[arm], cohorts[assignment.cohort][arm]):
                    counts.mature += 1
                    counts.delivered += delivered
                    counts.picked_up += picked_up
                    counts.complained += complained
                if complained:
                    category = "计入投诉"
                elif not delivered and picked_up:
                    category = "未送达提醒但计入取件"
                elif not delivered:
                    category = "未送达提醒且保留分母"
                elif picked_up:
                    category = "计入取件"
                elif assignment.picked_up_on and assignment.picked_up_on < cutoff:
                    category = "取件超窗，仅计入分母"
                else:
                    category = "窗内未取，仅计入分母"
        key = (category, arm)
        if examples[key] < 2:
            examples[key] += 1
            audit.append({
                "assignment_id": assignment.assignment_id,
                "recipient_id": assignment.recipient_id,
                "arm": arm,
                "cohort": assignment.cohort,
                "assigned_on": assigned.isoformat(),
                "delivered_on": _visible(assignment.delivered_on, cutoff),
                "picked_up_on": _visible(assignment.picked_up_on, cutoff),
                "complained_on": _visible(assignment.complained_on, cutoff),
                "category": category,
            })
    return overall, cohorts, excluded, audit


def _rate(count: int, size: int) -> str:
    return f"{count / size:.2%}" if size else _MISSING


def _pp(value: float) -> str:
    return f"{value * 100:+.2f}"


def _effect(a_count, a_size, b_count, b_size) -> str:
    if not a_size or not b_size:
        return _MISSING
    return _pp(b_count / b_size - a_count / a_size)


def _ci(interval) -> str:
    if interval is None:
        return _MISSING
    return f"[{_pp(interval[0])}, {_pp(interval[1])}]"


def _p(value: float | None) -> str:
    return f"{value:.4g}" if value is not None else _MISSING


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def _srm_result(label, a, b, settings):
    expected_b = (a + b) * settings["target_b_pct"] / 100
    test = sample_ratio(a, b, settings["target_b_pct"] / 100)
    statistic, p = test if test is not None else (None, None)
    if p is None:
        state = "期望数不足"
    elif p < settings["srm_alpha"]:
        state = "报警"
    else:
        state = "通过"
    return {
        "scope": label,
        "a": a,
        "b": b,
        "expected_a": f"{a + b - expected_b:.1f}",
        "expected_b": f"{expected_b:.1f}",
        "b_share": _rate(b, a + b),
        "statistic": f"{statistic:.3f}" if statistic is not None else _MISSING,
        "p": _p(p),
        "status": state,
    }


def _decision(srm, pickup_test, pickup_ci, complaint_ci, settings):
    srm_states = {check["status"] for check in srm}
    if "报警" in srm_states:
        return "srm_detected", "暂缓放量 · 分流比例异常", "核对分流配置、分配日志与截止前的日志覆盖。"
    if "期望数不足" in srm_states:
        return "srm_unavailable", "暂缓放量 · 分流样本不足", "至少一项 SRM 的组别期望数低于 5，待样本齐备后复盘。"
    if pickup_test is None:
        return "insufficient_evidence", "暂缓放量 · 取件检验样本不足", "取件与未取件的四个期望格频数须均达到 5。"
    z, p = pickup_test
    if p < settings["alpha"] and z < 0:
        return "pickup_declined", "暂缓放量 · 新提醒取件率下降", "B 组表现低于 A 组，且双侧检验达到预设显著性水平。"
    if p >= settings["alpha"] or z <= 0:
        return "primary_inconclusive", "暂缓放量 · 取件改善证据不足", "继续按预先约定的观察计划复盘。"
    if pickup_ci[0] * 100 < settings["min_lift_pp"]:
        return "practical_lift_unproven", "暂缓放量 · 收益未达到门槛", "取件率提升的置信下界低于最低实际收益。"
    if complaint_ci[0] * 100 > settings["max_complaint_pp"]:
        return "guardrail_failed", "暂缓放量 · 投诉增幅超限", "投诉率差的置信下界已超过容忍上限。"
    if complaint_ci[1] * 100 > settings["max_complaint_pp"]:
        return "guardrail_unproven", "暂缓放量 · 投诉风险尚未排除", "投诉率差的置信上界仍超过容忍上限。"
    return "rollout_candidate", "可进入下一批站点放量审批", "分流、取件收益和投诉护栏均达到预设条件。"


def _group_rows(overall, excluded):
    return [{
        "arm": _ARM_LABELS[arm],
        "assigned": counts.assigned,
        "mature": counts.mature,
        "pending": counts.assigned - counts.mature,
        "excluded": excluded[arm],
        "delivered": counts.delivered,
        "undelivered": counts.mature - counts.delivered,
        "pickup": counts.picked_up,
        "pickup_rate": _rate(counts.picked_up, counts.mature),
        "complaints": counts.complained,
        "complaint_rate": _rate(counts.complained, counts.mature),
    } for arm, counts in overall.items()]


def _cohort_rows(cohorts):
    rows = []
    for cohort, counts in cohorts.items():
        a, b = counts["A"], counts["B"]
        rows.append({
            "cohort": cohort,
            "pending": a.assigned + b.assigned - a.mature - b.mature,
            "a_n": a.mature,
            "a_pickup": a.picked_up,
            "a_rate": _rate(a.picked_up, a.mature),
            "b_n": b.mature,
            "b_pickup": b.picked_up,
            "b_rate": _rate(b.picked_up, b.mature),
            "lift": _effect(a.picked_up, a.mature, b.picked_up, b.mature),
            "complaint_lift": _effect(a.complained, a.mature, b.complained, b.mature),
        })
    return rows


def _gate_rows(srm, pickup_test, pickup_ci, complaint_ci, settings):
    z, p = pickup_test if pickup_test is not None else (None, None)
    pickup_lower = pickup_ci[0] * 100 if pickup_ci is not None else None
    complaint_upper = complaint_ci[1] * 100 if complaint_ci is not None else None
    srm_status = "、".join(f"{check['scope']}：{check['status']}" for check in srm)
    return [
        {"gate": "分流完整性", "rule": f"两项 SRM 均通过，报警阈值 {settings['srm_alpha']:g}",
         "observed": srm_status, "status": "通过" if all(item["status"] == "通过" for item in srm) else "待排查"},
        {"gate": "取件改善", "rule": f"B 取件率高于 A，双侧 p < {settings['alpha']:g}",
         "observed": f"Z = {z:+.3f}，p = {_p(p)}" if pickup_test is not None else "四个期望格频数不足",
         "status": "证据不足" if p is None else "通过" if z > 0 and p < settings["alpha"] else "未通过"},
        {"gate": "实际收益", "rule": f"取件率差下界 ≥ {settings['min_lift_pp']:g} 个百分点",
         "observed": f"{pickup_lower:+.2f} 个百分点" if pickup_lower is not None else _MISSING,
         "status": "证据不足" if pickup_lower is None else "通过" if pickup_lower >= settings["min_lift_pp"] else "未通过"},
        {"gate": "投诉护栏", "rule": f"投诉率差上界 ≤ {settings['max_complaint_pp']:g} 个百分点",
         "observed": f"{complaint_upper:+.2f} 个百分点" if complaint_upper is not None else _MISSING,
         "status": "证据不足" if complaint_upper is None else "通过" if complaint_upper <= settings["max_complaint_pp"] else "未通过"},
    ]


def _charts(overall, cohorts):
    overview_series = [
        {"label": _ARM_LABELS[arm], "value": 100 * counts.picked_up / counts.mature}
        for arm, counts in overall.items() if counts.mature
    ]
    cohort_series = [
        {"label": f"{cohort} · {arm}", "value": 100 * counts.picked_up / counts.mature}
        for cohort, arms in cohorts.items() for arm, counts in arms.items() if counts.mature
    ]
    return [
        {"title": title, "kind": "bar", "series": series}
        for title, series in (("整体取件率（%）", overview_series), ("柜点分层取件率（%）", cohort_series))
        if series
    ]


def analyze(rows, params) -> dict:
    settings = _settings(params)
    assignments = _normalize_rows(rows)
    cutoff = settings["cutoff"]
    days = settings["window_days"]
    overall, cohorts, excluded, audit = _tabulate(assignments, cutoff, days)
    a, b = overall["A"], overall["B"]
    total = a.assigned + b.assigned
    mature = a.mature + b.mature
    pickup = (a.picked_up, a.mature, b.picked_up, b.mature)
    complaints = (a.complained, a.mature, b.complained, b.mature)
    critical_z = NormalDist().inv_cdf(1 - settings["alpha"] / 2)
    confidence = f"{(1 - settings['alpha']) * 100:g}%"
    pickup_test = two_proportion_test(*pickup)
    pickup_ci = difference_interval(*pickup, critical_z)
    complaint_ci = difference_interval(*complaints, critical_z)
    srm = [
        _srm_result("截止前分配", a.assigned, b.assigned, settings),
        _srm_result("成熟分配", a.mature, b.mature, settings),
    ]
    state, headline, reason = _decision(srm, pickup_test, pickup_ci, complaint_ci, settings)
    return {
        "state": state,
        "headline": headline,
        "note": f"{cutoff.isoformat()} 零点快照 · {days} 天观察窗。{reason}",
        "metrics": [
            {"label": "成熟随机分配", "value": mature,
             "detail": f"截止前 {total} 人，{total - mature} 人未成熟，{sum(excluded.values())} 人在截止日及之后分配。"},
            {"label": "窗口取件率 · B−A（百分点）", "value": _effect(*pickup),
             "detail": f"{confidence} 置信区间 {_ci(pickup_ci)} · 双侧 p = {_p(pickup_test[1] if pickup_test else None)}"},
            {"label": "提醒投诉率 · B−A（百分点）", "value": _effect(*complaints),
             "detail": f"{confidence} 置信区间 {_ci(complaint_ci)} · 上限要求 {settings['max_complaint_pp']:g} 个百分点"},
            {"label": "样本比例偏差（SRM）", "value": " / ".join(item["status"] for item in srm),
             "detail": "依次为截止前全量、成熟分配。"},
        ],
        "tables": [
            _table("放量判定", (
                ("gate", "检查项"), ("rule", "放量条件"), ("observed", "观测证据"), ("status", "状态"),
            ), _gate_rows(srm, pickup_test, pickup_ci, complaint_ci, settings)),
            _table("两组 ITT 账面", (
                ("arm", "提醒组"), ("assigned", "截止前分配"), ("mature", "成熟分配"),
                ("pending", "未成熟"), ("excluded", "截止日及之后分配"), ("delivered", "窗内送达"),
                ("undelivered", "窗内未送达"), ("pickup", "窗内取件"), ("pickup_rate", "取件率"),
                ("complaints", "窗内投诉"), ("complaint_rate", "投诉率"),
            ), _group_rows(overall, excluded)),
            _table("样本比例偏差复核", (
                ("scope", "核查范围"), ("a", "A 实际"), ("b", "B 实际"), ("expected_a", "A 期望"),
                ("expected_b", "B 期望"), ("b_share", "B 实际占比"), ("statistic", "χ²"),
                ("p", "p 值"), ("status", "结果"),
            ), srm),
            _table("柜点分层 · 成熟样本", (
                ("cohort", "柜点类型"), ("pending", "未成熟"),
                ("a_n", "A 分母"), ("a_pickup", "A 取件"), ("a_rate", "A 取件率"),
                ("b_n", "B 分母"), ("b_pickup", "B 取件"), ("b_rate", "B 取件率"),
                ("lift", "取件差（百分点）"), ("complaint_lift", "投诉差（百分点）"),
            ), _cohort_rows(cohorts)),
            _table("分配单口径核对样例", (
                ("assignment_id", "分配单号"), ("recipient_id", "匿名收件人键"), ("arm", "组别"),
                ("cohort", "柜点类型"), ("assigned_on", "分配日"), ("delivered_on", "送达日"),
                ("picked_up_on", "取件日"), ("complained_on", "投诉日"), ("category", "口径核对"),
            ), audit),
        ],
        "charts": _charts(overall, cohorts),
        "sections": [
            {"title": "如何核对分配单", "text": "样例按组别和口径类型各取台账中最早出现的两行。表中的事件日期均早于截止日。聚合表计入全部符合口径的分配。"},
            {"title": "如何使用柜点分层", "text": "柜点表展示样本量和方向，柜点级差异供运营排查。放量判断基于预先约定的整体分析。"},
        ],
    }
