"""多维分析实验平台核心链路测试。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import store
from app.services.experiment import ExperimentService
from app.services.sample_group import SampleGroupService
from app.services.ab_result import AbResultService
from app.services.metric_def import MetricDefService
from app.services.expr_factor import ExprFactorService
from app.services.segment_compare import SegmentCompareService
from app.services.attr_analysis import AttrAnalysisService
from app.services.anomaly_detect import AnomalyDetectService
from app.services.forecast_model import ForecastModelService
from app.services.insight_report import InsightReportService
from app.services.metric_alias import MetricAliasService
from app.services.evaluate_board import EvaluateBoardService
from app.services.data_freshness import DataFreshnessService
from app.services.cohort_track import CohortTrackService
from app.services.data_export import DataExportService
from app.services.drill_down import DrillDownService

def _clear_all():
    for k in list(store._tables.keys()):
        store._tables[k] = []

def test_pipeline_experiment_to_sample_groups():
    """实验创建后自动生成对照和实验组分群。"""
    _clear_all()
    es = ExperimentService()
    entry, ms = es.create_entry({
        "方案编号": "EXPE-001",
        "实验名称": "新首页布局",
        "对照组说明": "旧版首页",
        "实验组说明": "新版首页",
        "样本总量": "10000",
        "分流比例": "5:5",
        "启动日期": "2026-11-01",
        "预期时长": "14",
    })
    assert entry is not None, ms
    sg_rows = store.rows("sample_group")
    assert len(sg_rows) >= 2, f"应创建对照组和实验组，实际{len(sg_rows)}"
    control = [r for r in sg_rows if r.get("group_type") == "control"]
    exp = [r for r in sg_rows if r.get("group_type") == "experiment"]
    assert len(control) >= 1
    assert len(exp) >= 1
    assert int(control[0].get("样本基数", 0)) > 0
    assert int(exp[0].get("样本基数", 0)) > 0

def test_sample_group_members():
    """分群能生成带差异的用户列表。"""
    _clear_all()
    # 先创建实验再用自动分群
    es = ExperimentService()
    es.create_entry({"方案编号":"EXPE-002","实验名称":"测试","对照组说明":"控","实验组说明":"试","样本总量":"200","分流比例":"5:5"})
    sg = SampleGroupService()
    members = sg.get_group_members(1)
    assert len(members) > 0, "应有分群用户"
    assert "user_id" in members[0]
    assert "metric_value" in members[0]

def test_metric_def_creation():
    """指标定义创建和口径版本。"""
    _clear_all()
    md = MetricDefService()
    entry, ms = md.create_entry({
        "指标编号": "M-001",
        "指标名称": "点击率",
        "计算公式": "clicks / impressions",
        "统计粒度": "日",
        "时间窗口": "7",
        "基准取值": "0.10",
        "精度位数": "4",
        "口径版本": "v1",
    })
    assert entry is not None, ms

def test_ab_result_analysis():
    """AB分析生成对照和实验组对比。"""
    _clear_all()
    ab = AbResultService()
    results = ab.run_analysis({"样本范围": "4"})
    assert len(results) == 4
    for r in results:
        assert "指标值" in r
        assert "对比基准" in r
        assert "差异" in r

def test_expr_factor_creation():
    """实验因子互斥规则。"""
    _clear_all()
    ef = ExprFactorService()
    entry, ms = ef.create_entry({
        "因子编号": "F-001",
        "因子名称": "按钮颜色",
        "因子层级": "UI层",
        "取值集合": "红色,蓝色",
        "分配比例": "5:5",
        "互斥规则": "颜色因子互斥",
        "关联实验": "EXPE-001",
        "生效批次": "B001",
    })
    assert entry is not None, ms

def test_anomaly_detect():
    """异常检测计算移动平均。"""
    _clear_all()
    ad = AnomalyDetectService()
    ad.create_entry({
        "规则编号": "AD-001",
        "监控指标": "点击率",
        "检测算法": "moving_average",
        "基线窗口": "7",
        "波动阈值": "0.3",
        "告警级别": "高",
        "静默时段": "0",
        "规则版本": "v1",
    })
    result = ad.run_detection(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.55])
    assert result.get("ok") or "anomaly" in str(result).lower()

def test_forecast_model():
    """预测模型简单移动平均。"""
    _clear_all()
    fc = ForecastModelService()
    fc.create_entry({
        "模型编号": "FC-001",
        "预测对象": "点击率",
        "历史窗口": "7",
        "预测周期": "3",
        "模型类型": "moving_average",
        "参数配置": "window=3",
        "训练批次": "T001",
        "评估误差": "",
    })
    result = fc.train_and_predict(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.16])
    assert result.get("ok"), result

def test_metric_alias_version():
    """口径版本注册。"""
    _clear_all()
    ma = MetricAliasService()
    entry, ms = ma.create_entry({
        "版本编号": "ALIAS-001",
        "指标编号": "M-001",
        "变更说明": "修改去重逻辑",
        "旧口径": "clicks/impressions",
        "新口径": "clicks/valid_impressions",
        "生效时间": "2026-11-16",
        "审批状态": "草稿",
        "变更人": "admin",
    })
    assert entry is not None, ms


def _make_compare_task(task_id=1, group_a="A", group_b="B", code="SEGM-T001"):
    """直接往数据仓库登记一条对比任务。"""
    task = {
        "id": task_id, "status": "草稿", "pending": True, "abnormal": False,
        "任务编号": code, "分群A": group_a, "分群B": group_b, "对比指标": "点击率",
        "时间范围": "最近7天", "差异阈值": "0.01", "显著性水平": "0.05", "比较方法": "绝对值",
    }
    store.rows("segment_compare").append(task)
    return task


def _make_group(group_id, code, base="300", group_type="control"):
    store.rows("sample_group").append({
        "id": group_id, "status": "已就绪", "pending": False, "abnormal": False,
        "分群编号": code, "分群名称": code, "筛选条件": "", "样本基数": base,
        "分群日期": "2026-11-01", "更新方式": "手工", "有效天数": "14", "负责人": "tester",
        "group_type": group_type,
    })


def test_segment_compare_deterministic_and_consistent():
    """同一成员集合计算：两次列表一致，差异方向与均值关系一致，列表与详情同源。"""
    _clear_all()
    _make_group(1, "SG-A", group_type="experiment")
    _make_group(2, "SG-B", group_type="control")
    _make_compare_task(1, "SG-A", "SG-B")
    sc = SegmentCompareService()
    first = sc.list_results()
    second = sc.list_results()
    assert first == second, "结果表落库后两次读取必须一致"
    row = first[0]
    assert abs(row["差异"] - round(row["指标值"] - row["对比基准"], row["精度位数"])) < 1e-9
    if row["差异"] > 0:
        assert row["差异方向"] == "上升"
    elif row["差异"] < 0:
        assert row["差异方向"] == "下降"
    else:
        assert row["差异方向"] == "持平"
    if row["显著性"] == "显著":
        assert row["差异方向"] in ("上升", "下降"), "显著但无方向是矛盾结果"
    detail = sc.get_entry(1)
    for key in ("指标值", "对比基准", "差异", "差异方向", "显著性", "重叠处理"):
        assert detail[key] == row[key], f"列表与详情字段 {key} 不一致"


def test_segment_compare_overlap_strategies():
    """部分重叠：排除重叠给出独立成员差异和排除数量；保留重叠给出各自汇总并单列重叠影响。"""
    _clear_all()
    _make_group(1, "SG-A", base="500")
    _make_group(2, "SG-B", base="500")
    _make_compare_task(1, "SG-A", "SG-B")
    sc = SegmentCompareService()
    rows, failed = sc.compute_results(overlap_strategy="排除重叠")
    assert not failed
    row = rows[0]
    assert row["重叠处理"] == "排除重叠"
    assert row["重叠成员数"] > 0, "共享用户池下两分群应存在部分重叠"
    assert row["排除数量"] == 2 * row["重叠成员数"]
    assert row["样本数A"] == 500 - row["重叠成员数"]
    rows2, failed2 = sc.compute_results(overlap_strategy="保留重叠")
    assert not failed2
    row2 = rows2[0]
    assert row2["重叠处理"] == "保留重叠"
    assert row2["排除数量"] == 0
    assert row2["样本数A"] == 500 and row2["样本数B"] == 500
    assert row2["重叠成员影响"] != "", "保留重叠需单列重叠成员影响"


def test_segment_compare_failure_keeps_last_and_recovers():
    """计算失败保留上一完整对比；恢复时只重算受影响任务。"""
    _clear_all()
    _make_group(1, "SG-A", group_type="experiment")
    _make_group(2, "SG-B", group_type="control")
    _make_group(3, "SG-EMPTY", base="0")
    _make_compare_task(1, "SG-A", "SG-B")
    _make_compare_task(2, "SG-A", "SG-B", code="SEGM-T002")
    sc = SegmentCompareService()
    rows, failed = sc.compute_results()
    assert not failed and len(rows) == 2
    good = dict(sc._find_result(1))
    # 让任务1的分群A变成空分群 → 计算失败
    task1 = store.find("segment_compare", 1)
    task1["分群A"] = "SG-EMPTY"
    rows2, failed2 = sc.compute_results(task_ids=[1, 2])
    assert failed2 == [1]
    assert sc._find_result(1) == good, "失败任务应保留上一完整对比"
    # 恢复：只重算受影响任务
    task1["分群A"] = "SG-A"
    recovered, failed3 = sc.recover_failed()
    assert failed3 == []
    assert [r["task_id"] for r in recovered] == [1]
    assert sc._find_result(1)["计算状态"] == "成功"


def test_metric_def_reads_compare_result_table():
    """跨模块联动：指标定义读取结果表的指标编号与精度位数，复算显著性输入。"""
    _clear_all()
    md = MetricDefService()
    md.create_entry({
        "指标编号": "M-001", "指标名称": "点击率", "计算公式": "c/i",
        "统计粒度": "日", "时间窗口": "7", "基准取值": "0.5", "精度位数": "4", "口径版本": "v1",
    })
    _make_group(1, "SG-A", group_type="experiment")
    _make_group(2, "SG-B", group_type="control")
    _make_compare_task(1, "SG-A", "SG-B")
    sc = SegmentCompareService()
    sc.compute_results()
    result = sc._find_result(1)
    assert result["指标编号"] == "M-001"
    assert result["精度位数"] == 4
    inputs = md.significance_inputs()
    assert len(inputs) == 1
    item = inputs[0]
    assert item["指标编号"] == "M-001"
    assert item["差异"] == round(item["均值A"] - item["均值B"], 4)
    assert item["差异方向"] == result["差异方向"]
    assert item["显著性"] == result["显著性"]


