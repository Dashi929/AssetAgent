"""路径解析测试。

打包后最容易出问题的就是路径：recipes 找不到、数据目录落到安装目录里。
这些测试把两种模式的行为钉死，防止以后改 config 时悄悄回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app import paths


def test_dev_mode_data_dir_uses_repo_dot_data(monkeypatch):
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.delenv("ASSETAGENT_DATA_DIR", raising=False)
    assert paths.default_data_dir() == paths.app_root() / ".data"


def test_data_dir_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "FROZEN", True)
    target = tmp_path / "custom-library"
    monkeypatch.setenv("ASSETAGENT_DATA_DIR", str(target))
    assert paths.default_data_dir() == target


def test_packaged_data_dir_is_outside_install_dir(monkeypatch, tmp_path):
    """打包后数据目录必须在用户目录下，绝不能落在安装目录里。"""
    install_root = tmp_path / "Programs" / "AssetAgent"
    exe = install_root / "resources" / "sidecar-dist" / "assetagent-sidecar.exe"
    local_app_data = tmp_path / "AppData" / "Local"

    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("ASSETAGENT_DATA_DIR", raising=False)

    data_dir = paths.default_data_dir()
    assert data_dir == local_app_data / "AssetAgent"
    # 关键断言：卸载时删的是 install_root，数据目录不能在它下面
    assert install_root not in data_dir.parents


def test_packaged_app_root_is_resources_dir(monkeypatch, tmp_path):
    install_root = tmp_path / "Programs" / "AssetAgent"
    exe = install_root / "resources" / "sidecar-dist" / "assetagent-sidecar.exe"

    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert paths.app_root() == install_root / "resources"


def test_packaged_recipes_dir_found_next_to_resources(monkeypatch, tmp_path):
    """recipes 躺在 resources/ 下，打包后必须能找到。"""
    resources = tmp_path / "resources"
    (resources / "recipes").mkdir(parents=True)
    exe = resources / "sidecar-dist" / "assetagent-sidecar.exe"

    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(paths, "meipass_dir", lambda: None)

    assert paths.find_recipes_dir() == resources / "recipes"


def test_packaged_recipes_prefers_installed_over_meipass(monkeypatch, tmp_path):
    """安装目录下的 recipes 必须优先于 exe 内部的 _MEIPASS 快照。

    _MEIPASS 那份是 PyInstaller `--add-data` 冻结进 exe 的副本，用户改不动，
    而且临时目录名每次启动都变。recipes 是给用户编辑的规则配置，
    所以命中安装目录时必须用它。
    """
    resources = tmp_path / "resources"
    installed = resources / "recipes"
    installed.mkdir(parents=True)
    (installed / "validation_rules.yaml").write_text("installed", encoding="utf-8")

    meipass = tmp_path / "_MEI123456"
    frozen = meipass / "recipes"
    frozen.mkdir(parents=True)
    (frozen / "validation_rules.yaml").write_text("frozen", encoding="utf-8")

    exe = resources / "sidecar-dist" / "assetagent-sidecar.exe"
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(paths, "meipass_dir", lambda: meipass)

    found = paths.find_recipes_dir()
    assert found == installed
    assert (found / "validation_rules.yaml").read_text(encoding="utf-8") == "installed"


def test_packaged_recipes_falls_back_to_meipass(monkeypatch, tmp_path):
    """exe 被单独拷出来、旁边没有 resources/recipes 时，回退到内部快照。"""
    resources = tmp_path / "resources"
    resources.mkdir(parents=True)

    meipass = tmp_path / "_MEI123456"
    (meipass / "recipes").mkdir(parents=True)

    exe = resources / "sidecar-dist" / "assetagent-sidecar.exe"
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(paths, "meipass_dir", lambda: meipass)

    assert paths.find_recipes_dir() == meipass / "recipes"


def test_dev_mode_recipes_dir_exists():
    """开发仓库里 recipes 必须真实存在，否则校验规则加载不到。"""
    assert paths.find_recipes_dir().is_dir()
    assert (paths.find_recipes_dir() / "validation_rules.yaml").is_file()


def test_app_root_points_at_repo_in_dev(monkeypatch):
    monkeypatch.setattr(paths, "FROZEN", False)
    root = paths.app_root()
    assert (root / "services" / "agent").is_dir()
    assert isinstance(root, Path)
