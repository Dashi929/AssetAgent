"""复现摩托导入管线：逐步打印几何统计，定位网格在哪一步坏掉。

用法：cd services/agent && ./.venv/Scripts/python.exe ../../scripts/debug_kawasaki.py <glb路径>
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["ASSETAGENT_DATA_DIR"] = str(Path(tempfile.gettempdir()) / "kawa_debug")
os.environ["ASSETAGENT_ALLOW_MOCK_FALLBACK"] = "true"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent"))

from app.config import reset_settings_cache  # noqa: E402

reset_settings_cache()

from app import store  # noqa: E402
from app.models import AssetSource, AssetStatus, SpecPreset  # noqa: E402
from app.routers.assets import _build_import_node, _resolve_preset  # noqa: E402
from app.tools.decimate import decimate_mesh  # noqa: E402
from app.tools.mesh_io import load_mesh, mesh_stats  # noqa: E402
from app.tools.repair import normalize_transform, repair_mesh  # noqa: E402
from app.tools.uv import unwrap  # noqa: E402


def brief(mesh) -> str:
    s = mesh_stats(mesh)
    return (
        f"faces={s['faces']:>6} verts={s['vertices']:>6} comps={s['components']:>5} "
        f"watertight={str(s['watertight']):>5} wind={str(s['winding_consistent']):>5} "
        f"uv={s['has_uv']} size={s['longest_edge_m']}m"
    )


def main() -> None:
    src = Path(sys.argv[1])
    spec = _resolve_preset("")  # 默认预设（道具·默认 5000 面）

    asset = store.create_asset(name="kawa_debug", spec=spec, source=AssetSource.MESH.value)
    node = _build_import_node(asset.id, src, src.suffix.lower())
    store.add_version(node)

    print("== import :", brief(load_mesh(node.mesh_path)))

    mesh = load_mesh(node.mesh_path)

    repaired, rep = repair_mesh(mesh)
    repaired, tr = normalize_transform(repaired, spec.pivot, spec.expected_size_m)
    print("== repair :", brief(repaired), "| actions:", "; ".join(rep["actions"][:4]))

    decimated, drep = decimate_mesh(repaired, spec.face_budget, spec.want_quads)
    final, _ = normalize_transform(decimated, spec.pivot, spec.expected_size_m)
    print(
        "== decimat:", brief(final),
        f"| {drep['before_faces']} -> {drep['after_faces']} method={drep['method']}",
        "| skipped:", drep["skipped_reason"],
    )

    unwrapped, urep = unwrap(final)
    print("== uv     :", brief(unwrapped), "| method:", urep.get("method"), "| skipped:", urep.get("skipped_reason"))

    # 各步骤产物留盘，方便单独加载检查
    out = Path(tempfile.gettempdir()) / "kawa_debug_out"
    out.mkdir(exist_ok=True)
    from app.tools.mesh_io import save_mesh

    save_mesh(mesh, out / "import.glb")
    save_mesh(repaired, out / "repair.glb")
    save_mesh(final, out / "decimate.glb")
    save_mesh(unwrapped, out / "uv.glb")
    print("== 产物在", out)


if __name__ == "__main__":
    main()
