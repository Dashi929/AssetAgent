# AssetAgent

**把概念图变成引擎就绪的 3D 道具与环境资产 —— 自动完成后处理（修复 / 减面 / UV / 烘焙 / 校验 / 导出），10 分钟出白模。**

> Desktop agent for game artists: turn concept art into engine-ready 3D props & environment assets, with the whole post-processing chain (repair, retopo, UV, bake, validate, export) automated.

| | |
|---|---|
| 开发主体 | 广州尼特之家信息技术有限公司 |
| 状态 | 🚧 **MVP 开发中**（Phase 1，M0 立项阶段） |
| 平台 | Windows 优先（游戏行业主力平台） |
| 许可 | 专有软件，All rights reserved（见文末） |

---

## 为什么做这个

生成式 3D（image-to-3D）在 2025 年后已达到"道具级可用"，但现有工具（Meshy / Tripo / Rodin 等）普遍**止步于"给一个粗糙网格 + 基础贴图"**。美术最头疼的那段——拓扑修复、UV 展开、烘焙、引擎导出规范——全部要人肉完成，进引擎前还得花 1–3 小时手修，比手做白模还慢。

这条链路恰好是 Agent 最擅长的：工具链成熟（Blender headless / pymeshlab / xatlas 全开源）、规则明确（各引擎有明确的资产规范）、可自动校验。

**我们不做"又一个生成工具"，做"生成之后的整条交付管线"。** 生成引擎是可替换的 Provider，管线和校验才是护城河。

## 三条工作流

| | 工作流 | 说明 |
|---|---|---|
| **A** | 概念图 → 引擎白模 | 杀手场景。拖图 → 选规格 → 生成 2–3 变体 → 挑选 → 自动后处理 → 校验通过 → 一键导出。全程不写一个 prompt 字 |
| **B** | 文本 → 资产包 | Planner 拆解清单 → 风格设定图锁定 → 逐件生成 → 整包一致性终检 → 打包 + 清单 CSV（Phase 2） |
| **C** | 已有网格 → 后处理 | 任何来源的粗糙网格拖进来只跑修复/减面/UV/烘焙/导出（支持 glb/obj/fbx 等）。**永久免费**，零 API 成本，是低门槛获客入口，也是让美术信任校验器的方式 |

## 架构

```
┌──────────────────────────────────────────────┐
│  Electron 桌面壳（React + react-three-fiber）   │
│  工作台 / 资产库 / 3D 视口 / 校验报告 / 导出     │
└──────────────┬───────────────────────────────┘
               │ HTTP (localhost)
┌──────────────▼───────────────────────────────┐
│  Python FastAPI sidecar                       │
│  ┌─────────┐  ┌───────────────────────────┐  │
│  │ Agent 层 │  │ 工具层                     │  │
│  │ Planner │  │ gen3d / repair / decimate │  │
│  │ Critic  │  │ uv / bake / validate /    │  │
│  │ 版本树   │  │ export / render           │  │
│  └────┬────┘  └──────┬────────────────────┘  │
│       │         ┌────▼───────────────────┐   │
│       │         │ Blender headless 管线   │   │
│       │         └────────────────────────┘   │
└───────┼──────────────────────────────────────┘
        │ Provider 抽象接口（BYOK / 官方中转 双路由）
┌───────▼──────────────────────────────────────┐
│ meshy │ tripo │ hunyuan3d │ rodin │ mock │ Trellis │
└──────────────────────────────────────────────┘
```

## 目录结构

```
AssetAgent/
├─ apps/desktop/            # Electron + React + react-three-fiber 桌面端
│  ├─ electron/             # 主进程 / preload / sidecar 生命周期
│  └─ src/                  # 渲染进程：工作台、资产库、视口、设置
├─ services/agent/          # Python FastAPI sidecar
│  ├─ app/
│  │  ├─ providers/         # meshy / tripo / rodin / mock / local_trellis
│  │  ├─ tools/             # 后处理管线各步骤 + 校验器
│  │  ├─ routers/           # REST API
│  │  ├─ models.py          # 冻结的数据模型与资产状态机
│  │  ├─ store.py           # 资产库 + 版本树（文件系统后端）
│  │  └─ jobs.py            # 任务队列与进度回传
│  └─ tests/
├─ services/relay/          # 官方中转服务（Phase 2：鉴权/计量/限流/转发）
├─ recipes/                 # 校验规则配置、导出预设、Blender bpy 脚本
├─ assets/styles/           # 风格圣经模板
├─ samples/                 # 冒烟测试用样例资产
└─ docs/                    # 架构、路线图
```

## 快速开始

**依赖**：Python ≥ 3.11、Node ≥ 20、Git。Blender 可选（不装则烘焙与转台渲染降级跳过，其余流程照常）。

```bash
# 1) Python sidecar
cd services/agent
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -e ".[dev,mesh]"
#   dev  = pytest / ruff
#   mesh = fast-simplification（减面）+ xatlas（UV 展开）
#   不装 mesh 也能跑：减面与 UV 会跳过并说明原因，其余步骤照常

# 2) 跑冒烟测试：概念图 → 生成 → 管线 → 校验 → 导出（不需要任何 API Key）
python -m pytest tests/ -v
python -m app.smoke                                  # 加 --keep 可保留产物目录

# 3) 启动 sidecar
python -m app.main                                   # 或 uvicorn app.main:app --reload --port 8756

# 4) 桌面端（另开一个终端）
cd apps/desktop
npm install
npm run dev
```

**BYOK**：生成功能需要自备 Provider API Key（Meshy / Tripo / 混元3D 自由可选；混元3D 填 `SecretId:SecretKey`），在设置页填写（OS 级加密存储，永不上传）。不填 Key 时**工作流 C 完整可用**（免费），生成入口会被禁用并给出引导。

## 开发路线

| 节点 | 周次 | 目标 |
|---|---|---|
| M0 | W0 | 立项与验证启动：范围冻结、黄金概念图集、Provider 盲测 |
| M1 | W1–W2 | 骨架贯通：Electron 壳 + sidecar + GLB 视口 + Provider 抽象 |
| M2 | W3–W4 | 主链贯通：修复 / 减面 / UV 进管线，端到端出 GLB |
| M3 | W5–W6 | 质量闭环：烘焙 + 校验器 + 校验报告 + 导出预设 + 转台 |
| M4 | W7 | 产品化闭环：资产库 / 版本树 / BYOK / 成本控制 |
| M5 | W8 | 打磨与封测 |
| M6 | W9 | 种子用户 alpha |

细节见 [`docs/ROADMAP.md`](docs/ROADMAP.md) 与 [`产品策划文档.md`](产品策划文档.md)。

## 工程约定（不可违背）

1. **永不覆盖用户文件。** 所有产物写入独立输出目录 + 版本号；源概念图只读。
2. **每个结果可追溯。** 版本树记录 prompt、参数、模型来源、校验报告，可复现、可回滚。
3. **破坏性修改只走后端。** 前端只做检查与摆放，一切网格修改走后端管线并产生新版本节点；撤销 = 回到版本树上一节点，不自建 undo 栈。
4. **交付标准按美术的职业习惯定。** 面数预算、四边面、UV 规范、真实单位、命名规范——不合规范的产出美术会直接丢弃。
5. **Key 永不上传。** 自填 Key 只存本机（Electron `safeStorage` / OS 凭据），中转模式才使用官方 Key。

## 许可

Copyright © 2026 广州尼特之家信息技术有限公司. All rights reserved.

本仓库为专有软件源码。未经书面许可，不得复制、修改、分发或用于商业用途。
仓库暂以 public 形式公开用于开发协作，**这不构成任何开源授权**；是否开源将另行决策并单独公告。

第三方依赖各自的许可条款见各依赖仓库；本地生成模型以 TRELLIS（MIT）优先。
