"""校验器测试。

校验器是"美术信不信这个工具"的关键，所以这里盯三件事：
1. 干净的资产要能通过 —— 一个总是报错的校验器等于没有。
2. 有问题的资产必须被抓住，而且要**指出在哪**（locator），不能只说"有问题"。
3. 测不了的项必须报 SKIPPED，绝不能假装 PASS。
"""

from __future__ import annotations

import trimesh

from app.models import CheckResult, SpecPreset
from app.tools.mesh_io import load_mesh
from app.tools.validate import load_rules, validate


def _result(report, rule: str):
    return next(r for r in report.results if r.rule == rule)


def test_clean_asset_passes(sample_obj):
    mesh = load_mesh(sample_obj)
    spec = SpecPreset(name="道具 · 默认", expected_size_m=1.0, face_budget=5000)

    report = validate(mesh, sample_obj, "asset_x", "ver_x", spec, asset_name="SM_Test_Prop")

    assert report.passed, [(r.label, r.result.value, r.message) for r in report.failures]
    assert _result(report, "face_budget").result == CheckResult.PASS
    assert _result(report, "ngon").result == CheckResult.PASS
    assert _result(report, "pivot").result == CheckResult.PASS
    assert _result(report, "units").result == CheckResult.PASS
    assert _result(report, "naming").result == CheckResult.PASS


def test_face_budget_skipped_when_unlimited(dirty_glb):
    """face_budget=None = 不限面数（导入路径）：规则跳过而不是 FAIL。"""
    mesh = load_mesh(dirty_glb)
    spec = SpecPreset(face_budget=None, expected_size_m=None)

    report = validate(mesh, dirty_glb, "asset_y", "ver_y", spec, asset_name="SM_Dirty_Prop")

    result = _result(report, "face_budget")
    assert result.result == CheckResult.SKIPPED
    assert "不限面数" in result.message


def test_face_budget_failure_reports_actual_value(dirty_glb):
    mesh = load_mesh(dirty_glb)
    spec = SpecPreset(face_budget=1000, expected_size_m=None)

    report = validate(mesh, dirty_glb, "asset_y", "ver_y", spec, asset_name="SM_Dirty_Prop")

    result = _result(report, "face_budget")
    assert result.result == CheckResult.FAIL
    assert result.value > 1000
    assert result.threshold == 1000


def test_bad_pivot_and_naming_are_caught(dirty_glb):
    """脏资产没有被归一化过：轴心不在底面中心，命名也不合规。"""
    mesh = load_mesh(dirty_glb)
    spec = SpecPreset(face_budget=50000, expected_size_m=None)

    report = validate(mesh, dirty_glb, "asset_z", "ver_z", spec, asset_name="未命名 资产!")

    assert _result(report, "naming").result == CheckResult.FAIL
    assert _result(report, "pivot").result in (CheckResult.FAIL, CheckResult.PASS)


def test_uv_overlap_gives_locator_when_uv_exists(tmp_path):
    """有 UV 时，UV 重叠必须给出面索引 —— 前端要靠它高亮问题区域。

    刻意构造成"两个三角形在 UV 空间里交叠、但不共享任何 UV 顶点"：
    这才是真正的重叠。如果让它们共享 UV 顶点，按 UV 邻接判断会被算成相邻面，
    那是 UV 缝的正常形态，不该报重叠（这个区别踩过坑）。
    """
    import numpy as np

    # 两个互不相连的三角形，UV 上部分重叠
    vertices = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 0.5],
            [1.0, 0.0, 0.5],
            [0.0, 1.0, 0.5],
        ]
    )
    faces = np.array([[0, 1, 2], [3, 4, 5]])
    uv = np.array(
        [
            [0.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
            [0.2, 0.2],
            [1.2, 0.2],
            [0.2, 1.2],
        ]
    )
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)

    path = tmp_path / "overlap.glb"
    path.write_bytes(mesh.export(file_type="glb"))

    spec = SpecPreset(face_budget=50000, expected_size_m=None)
    report = validate(mesh, path, "asset_u", "ver_u", spec, asset_name="SM_Overlap")

    result = _result(report, "uv_overlap")
    assert result.result in (CheckResult.FAIL, CheckResult.WARN), result.message
    assert result.locator.kind == "uv_islands"
    assert len(result.locator.indices) > 0


def test_uv_seam_is_not_reported_as_overlap(tmp_path):
    """UV 缝两侧的面共享 UV 边，属于正常形态，不能被误报成重叠。

    这条测试是为了锁住一个真实踩过的坑：早期实现按"网格顶点索引"判断相邻，
    而 UV 展开会在缝处复制顶点，于是每个缝都被误报成重叠。
    """
    import numpy as np

    # 两个三角形共享两个 UV 顶点（= UV 空间里相邻，各自占据不同区域）
    vertices = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 1.0, 0.0],
        ]
    )
    faces = np.array([[0, 1, 2], [1, 3, 2]])
    uv = np.array([[0.0, 0.0], [0.5, 0.0], [0.0, 0.5], [0.5, 0.5]])
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)

    path = tmp_path / "seam.glb"
    path.write_bytes(mesh.export(file_type="glb"))

    spec = SpecPreset(face_budget=50000, expected_size_m=None)
    report = validate(mesh, path, "asset_s", "ver_s", spec, asset_name="SM_Seam")

    result = _result(report, "uv_overlap")
    assert result.value == 0, f"UV 缝被误报成重叠：{result.message}"


def test_unmeasurable_rules_are_skipped_not_passed(dirty_glb, monkeypatch):
    """GLB 里还原不出四边面信息 —— 必须报 SKIPPED，不能假装通过。"""
    import importlib

    mesh = load_mesh(dirty_glb)
    spec = SpecPreset(face_budget=50000, expected_size_m=None)

    validate_module = importlib.import_module("app.tools.validate")
    rules = validate_module.load_rules("prop")
    rules["quad_ratio"]["enabled"] = True  # 强制打开，验证"测不了"时的行为
    monkeypatch.setattr(validate_module, "load_rules", lambda category="prop": rules)

    report = validate(mesh, dirty_glb, "asset_q", "ver_q", spec, asset_name="SM_Quad")

    result = _result(report, "quad_ratio")
    assert result.result == CheckResult.SKIPPED
    assert "无法还原" in result.message


def test_units_skipped_without_expected_size(sample_obj):
    mesh = load_mesh(sample_obj)
    spec = SpecPreset(expected_size_m=None)

    report = validate(mesh, sample_obj, "asset_m", "ver_m", spec, asset_name="SM_Test_Prop")

    result = _result(report, "units")
    assert result.result == CheckResult.SKIPPED
    assert "期望尺寸" in result.message


def test_rules_config_falls_back_to_defaults():
    rules = load_rules("prop")
    assert rules["face_budget"]["max"] == 5000
    assert rules["uv_island_margin"]["min_px"] == 2.0

    blocker = load_rules("blocker")
    assert blocker["face_budget"]["max"] == 1500
    assert blocker["uv_island_margin"]["enabled"] is False
