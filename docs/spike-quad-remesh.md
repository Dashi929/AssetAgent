# Spike 结论：四边面重拓扑是否进 MVP

> 状态：**Spike 已完成，给出拍板建议，最终决定留产品负责人**（决策点约定见
> docs/ROADMAP.md「三个前置决策点」，原定时点 W2 末）。
> 日期：2026-09-12。验证环境：Windows 11 / Python 3.13（本机）。

## 1. 问题

`SpecPreset.want_quads` 只被记录不生效：管线减面（fast-simplification / pymeshlab）
产物是三角面，校验器的 `quad_ratio` 规则因此默认关闭。要把它做实，需要一条
"三角面 → 四边面为主"的自动重拓扑路径。

## 2. 实测事实（本机验证）

| 事实 | 依据 |
|---|---|
| PyPI 不存在可用的四边面重拓扑包 | `instant-meshes` / `quadriflow` / `quadwild` / `pyquadremesh` 均 404（2026-09-12 查证） |
| 现有减面后端做不了四边面 | fast-simplification 0.2.0 只暴露三角面简化；MeshLab 的 remesh 滤镜系全部输出三角面 |
| Instant Meshes 官方不发 GitHub release | wjakob/instant-meshes releases 为空，二进制只在项目网站分发，自动化打包无权威下载源 |
| 唯一可编程的 quad 重拓扑 = Blender（Quadriflow remesher） | 与本项目现有"Blender 可选增强"模式（烘焙/FBX 导出/转台）同一条降级链 |

## 3. 三条可行路径对比

| 路径 | 质量 | 成本 | 风险 |
|---|---|---|---|
| A. 捆绑 Instant Meshes exe（BSD-3） | 硬表面业界标杆 | 打包脚本 + 每平台一份二进制 + 版本升级链路；无官方 release 源，得自托管二进制 | 供应链与体积（~30MB/平台） |
| B. Blender Quadriflow（`bpy.ops.object.remesh(mode='QUAD')`，headless 脚本进 recipes/bpy/） | 好 | ~0.5 轮开发；复用现有 Blender 探测与降级模式；**本机无 Blender 未实测**（与已知问题 #4 同批待真机验证） | 依赖用户装 Blender（已是烘焙/FBX 导出的同一前置） |
| C. quadwild 等学术方案 | 高 | 编译复杂、无发行物、集成成本不可控 | 不现实 |

共同的产品级代价（与实现路径无关）：重拓扑会**摧毁原 UV**，四边面路径必须
"重拓扑 → 重新 UV → 重新烘焙"，把管线时长再加 1–2 分钟，并显著改变烘焙质量口径。

## 4. 建议：不进 MVP

1. **MVP 唯一目标**（产品文档 5.4.1）是"白模进引擎"——Unity/UE 对三角面完全无感，
   quad 不是白模的硬门槛。
2. 四边面的真实收益对象是"要做细分/雕刻/展 UV 的中游资产"，这属于 Phase 2
   的 DCC 协作场景，不是 alpha 美术的第一诉求。
3. 质量风险未消除：自动重拓扑对硬表面的实际质量未经美术评审，强行上线
   会让校验器报出大量"我们知道但解决不了"的红灯，重演 quad_ratio 规则
   当初默认关闭的理由。
4. 黄金集纪律下，它的出口标准本来就要等美术反馈定义。

## 5. 若拍板要做（Phase 2 或提前）

- 推荐路径 B：`recipes/bpy/remesh_quads.py` + `decimate.py` 里 `want_quads`
  分支接 Blender，缺失时按现有模式降级跳过。
- 出口标准建议：黄金集 10 张，`quad_ratio ≥ 0.85` 且总管线时长 ≤ 8 分钟；
  `quad_ratio` 规则随之改回 `enabled: true`。
- 工作量预估 0.5 轮 + 真机校验（依赖装了 Blender 的机器）。

## 6. 本 Spike 的落地动作

- `want_quads` 保持"只记录、默认 false"的现状（管线/校验/UI 均不露出）。
- ROADMAP 决策点标记 Spike 完成，结论材料即本文档；最终去留等产品拍板。
