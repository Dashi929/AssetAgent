"""资产库与版本树的测试。

重点验证两条不可违背的约定：
1. **永不覆盖** —— unique_path 遇到同名文件必须另起名字，而不是盖掉。
2. **删除即归档** —— archive 只移动目录，源文件必须还在。
"""

from __future__ import annotations

from app import store
from app.models import AssetStatus, VersionNode, VersionOp


def test_create_and_get_asset():
    asset = store.create_asset(name="SM_Crate_01")
    assert asset.status == AssetStatus.DRAFT

    loaded = store.get_asset(asset.id)
    assert loaded.name == "SM_Crate_01"
    assert store.asset_dir(asset.id).exists()


def test_unique_path_never_overwrites(tmp_path):
    first = store.unique_path(tmp_path, "mesh.glb")
    first.write_text("v1", encoding="utf-8")

    second = store.unique_path(tmp_path, "mesh.glb")
    second.write_text("v2", encoding="utf-8")

    third = store.unique_path(tmp_path, "mesh.glb")

    assert first.name == "mesh.glb"
    assert second.name == "mesh_v2.glb"
    assert third.name == "mesh_v3.glb"
    # 关键断言：第一次写入的内容没有被覆盖
    assert first.read_text(encoding="utf-8") == "v1"


def test_source_files_are_never_overwritten(tmp_path):
    asset = store.create_asset(name="SM_Prop")
    source = tmp_path / "concept.png"
    source.write_bytes(b"first")

    first = store.write_source_bytes(asset.id, "concept.png", b"first")
    second = store.write_source_bytes(asset.id, "concept.png", b"second")

    assert first != second
    assert first.read_bytes() == b"first"  # 原图仍在
    assert second.read_bytes() == b"second"
    assert len(store.get_asset(asset.id).source_files) == 2


def test_version_tree_keeps_history(tmp_path):
    asset = store.create_asset(name="SM_Prop")
    mesh = tmp_path / "mesh.glb"
    mesh.write_bytes(b"mesh")

    first = store.add_version(
        VersionNode(asset_id=asset.id, op=VersionOp.REPAIR, mesh_path=str(mesh))
    )
    second = store.add_version(
        VersionNode(asset_id=asset.id, parent_id=first.id, op=VersionOp.DECIMATE, mesh_path=str(mesh))
    )

    versions = store.list_versions(asset.id)
    assert [v.op for v in versions] == [VersionOp.REPAIR, VersionOp.DECIMATE]
    assert versions[1].parent_id == first.id
    # 头节点自动推进到最新版本
    assert store.head_version(asset.id).id == second.id


def test_archive_moves_but_does_not_delete(tmp_path):
    asset = store.create_asset(name="SM_Prop")
    store.write_source_bytes(asset.id, "concept.png", b"pixels")
    asset_dir = store.asset_dir(asset.id)

    store.archive_asset(asset.id)

    assert store.get_asset(asset.id).status == AssetStatus.ARCHIVED
    assert asset_dir.exists()  # 目录还在，文件一个没少
    assert len(store.get_asset(asset.id).source_files) == 1
    # 默认列表不再包含已归档资产
    assert asset.id not in [a.id for a in store.list_assets()]
    assert asset.id in [a.id for a in store.list_assets(include_archived=True)]


def test_list_assets_skips_corrupted_entries(tmp_path):
    """单个资产元数据损坏，不应该让整个资产库列表挂掉。"""
    good = store.create_asset(name="SM_Good")
    broken = store.asset_dir("asset_broken")
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "asset.json").write_text("{ this is not json", encoding="utf-8")

    assets = store.list_assets()
    assert [a.id for a in assets] == [good.id]
