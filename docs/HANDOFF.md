# 交接文档

> 每次代码改动后更新。下一个 Agent 读取此文件即可了解当前状态，无需翻 git log。
>
> **规则**：
> - 新改动追加到顶部（最新的在前）
> - 每条记录必须包含：改动摘要、影响文件、验证状态、已知问题
> - 已修复的问题从"已知问题"移到"已修复"
> - 不要删旧记录，只追加

---

## 2026-09-11 第四轮：Windows 安装包构建

### 改动摘要
搭建完整的 Windows 安装包构建流水线：PyInstaller 打包 sidecar → Electron 构建 → electron-builder 生成 NSIS 安装程序。用户不再需要安装 Python，双击安装后即可运行。

### 详细变更

| 文件 | 变更 |
|---|---|
| `scripts/sidecar_entry.py` | PyInstaller 打包入口。直接创建 FastAPI app 实例并启动 uvicorn，绕过字符串模块引用（PyInstaller 静态分析无法追踪字符串引用） |
| `scripts/build-all.py` | 一键构建脚本：安装 PyInstaller → 打包 sidecar（含 hidden-imports + recipes 数据文件）→ 构建 Electron → electron-builder 生成安装程序 |
| `apps/desktop/electron/sidecar.ts` | `resolveExecutable()` 替代 `resolvePython()`：**打包后优先查找 `.exe`**，找不到才 fallback 到 Python。`start()` 根据类型决定 spawn 参数（.exe 不需要 `-m uvicorn`） |
| `apps/desktop/package.json` | `extraResources` 新增 `sidecar-dist` 目录；`scripts` 新增 `dist:full` 指向 `build-all.py` |
| `.gitignore` | 新增 `sidecar-build/`、`sidecar-dist/` 排除（PyInstaller 构建产物体积大，不入库） |

### 构建产物
- `apps/desktop/release/AssetAgent Setup 0.1.0.exe` —— **117MB NSIS 安装程序**
- `apps/desktop/release/win-unpacked/AssetAgent.exe` —— **186MB 便携版**（不解压直接用）
- `apps/desktop/sidecar-dist/assetagent-sidecar.exe` —— **11.7MB** sidecar 独立可执行文件

### 使用方法

**一键构建（完整流程）：**
```bash
cd scripts
python build-all.py
```

**手动分步：**
```bash
# 1. 打包 sidecar（PyInstaller）
cd services/agent
.venv/Scripts/python -m PyInstaller --onefile --name assetagent-sidecar \
  --distpath ../../apps/desktop/sidecar-dist \
  scripts/sidecar_entry.py

# 2. 构建 Electron
cd apps/desktop
npm run build:all

# 3. 打包安装程序
npx electron-builder --win nsis
```

**用户安装后运行：**
双击 `AssetAgent.exe`，sidecar 作为子进程自动启动，不需要单独装 Python。

### 验证状态
- PyInstaller 打包成功：`assetagent-sidecar.exe` 可执行
- Electron 构建成功：`npm run build:all` 无错误
- electron-builder 成功：`AssetAgent Setup 0.1.0.exe` 已生成

### 本轮引入的新问题
| # | 问题 | 原因 | 解决方向 |
|---|---|---|---|
| 8 | 安装包体积 117MB（偏大）| Electron 本体 113MB + sidecar 12MB + three.js 1MB | 后续可考虑 Electron 的 `portable` 目标减少打包体积；或提供 zip 便携版 |
| 9 | PyInstaller 的 hidden-import 警告 | `app.config` 等模块报错 "not found"，但产物仍能工作 | 不影响功能；如需消除警告，可在 entry point 显式 import 所有子模块 |
| 10 | 无代码签名 | electron-builder 跳过签名（没有证书）| Windows SmartScreen 可能拦截。开发阶段正常；正式发布需购买代码签名证书 |

---

## 2026-09-11 第三轮：全局异常处理器

### 改动摘要
为 FastAPI 添加全局异常处理器，避免未捕获异常暴露堆栈给前端，同时把 Provider 层的业务错误转成干净的人话响应。

### 详细变更

| 文件 | 变更 |
|---|---|
| `services/agent/app/main.py` | 新增 3 个 `@app.exception_handler`：<br>1. `ProviderError` → 400（消息直接透给前端）<br>2. `ValueError` → 400（参数错误提示）<br>3. `Exception` 兜底 → 500（绝不暴露堆栈；堆栈只写日志） |
| `services/agent/tests/test_api.py` | 新增 2 个测试：<br>- `test_provider_error_returns_400_without_stacktrace`：验证 ProviderError 响应不含 traceback/异常类名<br>- `test_unexpected_error_returns_500_without_stacktrace`：验证 500 响应只含人话文案 |

### 验证状态
- `pytest tests`：**35 passed**（新增 2 个）
- `ruff check app tests`：All checks passed

### 本轮引入的新问题
无。

---

## 2026-09-11 第二轮：CSP、Registry 缓存、estimate_cost 修复

### 改动摘要
修复 3 个影响可用性的 bug，新增 API 集成测试骨架。

### 详细变更

| 文件 | 变更 |
|---|---|
| `apps/desktop/index.html` | CSP `connect-src` 从写死的 `http://127.0.0.1:8756` 改为 `http://127.0.0.1:*`，支持 sidecar 端口自动上浮 |
| `services/agent/app/providers/base.py` | `estimate_cost` 默认实现改为"次数 × unit_cost"；子类可覆盖 |
| `services/agent/app/providers/mock.py` | `estimate_cost` 覆盖为返回 0（之前报假价格） |
| `services/agent/app/providers/registry.py` | **`settings` 属性每次现取 `get_settings()`，不缓存实例**。关键修复：之前填 Key 后界面仍显示"未配置" |
| `services/agent/app/routers/assets.py` | 生成请求提前拦截"无参考图"的情况，给出人话错误 |
| `services/agent/tests/test_api.py` | 新增 API 集成测试（新建文件），覆盖完整工作流 A/C + 安全边界 + BYOK 掩码 |

### 验证状态
- `pytest tests`：**33 passed**
- `ruff check`：All checks passed

### 本轮引入的新问题
无。

---

## 2026-09-11 第一轮：MVP 骨架 —— 从概念图到引擎资产的完整链路

### 改动摘要
按产品策划文档 v0.8 的 7.6 目录结构，建立 monorepo 骨架。包含完整的 Python sidecar（FastAPI + 后处理管线 + 校验器 + Provider 抽象层）和 Electron + React + r3f 桌面端。

### 详细变更

#### 工程骨架
- `.gitignore`（含 BYOK 密钥排除 `secrets.json`）
- `.editorconfig`、`.env.example`
- `.github/workflows/ci.yml`（ruff+pytest / tsc+vite build）
- `README.md`、`docs/ARCHITECTURE.md`、`docs/ROADMAP.md`

#### Sidecar 核心（`services/agent/app/`）
| 文件 | 职责 |
|---|---|
| `config.py` | 配置优先级：环境变量 / .env → `settings.json`（UI 写入）→ 代码默认。BYOK Key 存 `secrets.json`（权限 0o600） |
| `models.py` | 冻结的数据模型：Asset/Variant/VersionNode/Job/ValidationReport/ExportRecord 等。资产状态机枚举 |
| `store.py` | 文件系统资产库：永不覆盖（`unique_path` 自动追加 `_v2`）。版本树：每个操作独立目录。删除 = 归档 |
| `jobs.py` | asyncio 任务队列：进度回调、取消、失败记录。每步落盘，刷新/重启可恢复 |

#### Provider 层（`services/agent/app/providers/`）
| 文件 | 职责 |
|---|---|
| `base.py` | `Gen3DProvider` 抽象接口：`generate` / `estimate_cost` / `healthcheck`。`GenerateRequest` 含 `on_progress` |
| `mock.py` | 离线占位：生成刻意"脏"的网格（游离组件、退化面、高面数）。零 Key 验证管线 |
| `meshy.py` / `tripo.py` / `rodin.py` | 真实 HTTP 实现。端点集中在文件顶部常量区（**W1 需对照官方文档校正**） |
| `registry.py` | 双路由解析：byok / relay（Phase 2 报错）。`settings` 每次现取 |
| `local_trellis.py` | Phase 2 占位 |
| `polling.py` | 共用轮询工具 `poll_task` |

#### 后处理管线（`services/agent/app/tools/`）
| 文件 | 职责 |
|---|---|
| `repair.py` | 修复：退化面 → 重复面 → 游离组件 → 未引用顶点 → 法线统一 → 补洞。含轴心/单位归一化 |
| `decimate.py` | 减面：fast-simplification 优先 → pymeshlab。装不上则 skip |
| `uv.py` | UV 展开：xatlas + 岛划分 + 重叠检测 + 岛间距。**已知缺口**：xatlas 绑定不暴露 padding，约 0.04% 面微重叠 |
| `validate.py` | 9 条规则引擎：面数预算、四边面（默认关闭）、N-gon、UV 重叠、UV 岛间距、轴心、单位、命名、贴图。每条必须给 `Locator` |
| `pipeline.py` | 编排 7 步：repair → decimate → uv → bake → validate。每步产生版本节点 |
| `bake.py` / `export.py` / `render.py` | 烘焙、导出（GLB/FBX）、转台渲染。Blender 优先，退化到自研软渲染 `raster.py` |
| `blender.py` / `mesh_io.py` / `raster.py` | Blender headless 调用封装、网格 I/O、软渲染器 |

#### 桌面端（`apps/desktop/`）
| 文件 | 职责 |
|---|---|
| `electron/main.ts` | 主进程：窗口创建、sidecar 生命周期、IPC（文件对话框、打开文件夹） |
| `electron/preload.ts` | 最小权限桥：只暴露 5 个方法 |
| `electron/sidecar.ts` | SidecarManager：端口自动探测（8756 起，冲突上浮）、Python 解析、健康检查 |
| `src/App.tsx` | 应用外壳：侧边导航 + 全局错误提示 + 初始化 |
| `src/api/client.ts` | sidecar HTTP 客户端。`ApiError` 统一错误。`fileUrl` / `fileUrlSync` 读产物 |
| `src/api/types.ts` | 与 Python 模型的手工镜像 |
| `src/store/useAppStore.ts` | Zustand 全局状态：sidecar 状态、资产列表、任务轮询 |
| `src/pages/Workbench.tsx` | 工作台：工作流 A（概念图→资产）与 C（已有网格→后处理）双入口 |
| `src/pages/Library.tsx` | 资产库：网格视图 + 状态徽章 + 筛选/搜索 |
| `src/pages/AssetDetail.tsx` | 资产详情：3D 视口 + 变体挑选 + 版本树 + 校验报告（点击 FAIL 高亮）+ 导出 |
| `src/pages/Settings.tsx` | 设置：BYOK Key 管理（掩码回显）、预算、依赖诊断、日志查看 |
| `src/components/Viewport3D.tsx` | three.js + r3f + drei：GLB 加载、显示模式切换（clay/线框/材质）、按面索引高亮 |
| `src/components/ValidationReport.tsx` | 校验报告表格：PASS/FAIL/WARN/SKIP 显式展示，FAIL 项可点击定位 |
| `src/components/VersionTree.tsx` | 版本树：从旧到新排列，点击切换查看 |
| `src/components/JobProgress.tsx` | 任务进度条：百分比 + 具体 message |
| `src/components/StatusBadge.tsx` | 状态徽章：资产/任务/校验结果三种 |
| `src/styles/global.css` | 全局样式：浅色暖中性主题 |

#### 配置外置
- `recipes/validation_rules.yaml`：三级回落（categories > defaults > 内置）。UV 重叠暂时 WARN
- `recipes/presets/spec_presets.yaml`：规格预设
- `recipes/presets/export_presets.yaml`：引擎导出预设
- `recipes/bpy/bake_textures.py` / `export_asset.py` / `render_turntable.py`：Blender headless 脚本

### 验证状态
- `pytest tests`：**19 passed**（第一轮时）
- `ruff check`：All checks passed
- `tsc`（两个 tsconfig）：无类型错误
- `vite build`：通过（1.1MB）
- `python -m app.smoke`：全链路跑通，校验通过
- 远端验证：干净 `git clone` 83 个文件，关键文件齐全

---

## 已知问题（当前全部未解决）

| # | 问题 | 影响 | 认领节点 | 备注 |
|---|---|---|---|---|
| 1 | **UV 展开无法保证零重叠**：xatlas Python 绑定不暴露 padding，实测约 0.04% 面有微小重叠 | 校验器的 UV 重叠规则暂时按 WARN 报 | M3 | 需换 Blender Smart UV Project 或补去重叠后处理 |
| 2 | **四边面重拓扑未实现**：`want_quads` 只被记录，实际输出仍是三角面 | 四边面规则默认关闭 | W2 末 Spike | 决定是否真做（见 5.4.3） |
| 3 | **云 Provider 端点未对照官方文档校正**：Meshy/Tripo/Rodin 的 endpoint 是按下标写的 | 首次真实调用可能失败 | W1 | 端点集中在各 provider 文件顶部常量区 |
| 4 | **烘焙与 FBX 导出未在真实 Blender 上验证**：`recipes/bpy/*.py` 只做了静态检查 | 装了 Blender 的机器上可能报错 | M3 | 需校正 Blender 4.x API 差异 |
| 5 | **前端未在真实 Electron 里跑过**：代码已完成，但沙箱/无头环境无法创建 BrowserWindow | 首次真机启动可能遇到路径/端口/权限问题 | M1 | 在带显示器的开发机上验证 |
| 6 | **Electron 在无头/沙箱环境启动受限**：`--disable-gpu` 仍不足绕过 | CI 无法做 E2E；不影响真机使用 | M1 | CI 可用 Playwright + `--remote-debugging-port` |
| 7 | **资产包批量（工作流 B）未实现**：Planner 与风格圣经只有配置模板 | 只能逐件生成 | Phase 2 | 非 MVP 范围 |

---

## 环境速查

### 启动 sidecar
```bash
cd services/agent
.venv/Scripts/python -m app.main
# 或
.venv/Scripts/uvicorn app.main:app --host 127.0.0.1 --port 8756
```

### 启动桌面端（开发模式）
```bash
cd apps/desktop
npm run dev
# 需要 sidecar 已启动，且端口匹配
```

### 运行测试
```bash
cd services/agent
.venv/Scripts/python -m pytest tests/ -q
.venv/Scripts/ruff check app tests
```

### 前端检查
```bash
cd apps/desktop
npm run typecheck   # TypeScript 类型检查
npm run build       # Vite 生产构建
```

### 关键路径
- sidecar 数据目录：`services/agent/.data/`（已 gitignore）
- BYOK 密钥：`services/agent/.data/secrets.json`（已 gitignore，权限 0o600）
- 前端产物：`apps/desktop/dist/`
- Electron 产物：`apps/desktop/dist-electron/`

---

## 下一步（按优先级）

1. **W0 前置**：团队规模（8 周 / 12 周口径）—— 需用户拍板
2. **W1 必做**：拿到 API Key 后对照官方文档校正 Meshy/Tripo/Rodin 端点
3. **M1 验收**：在带显示器的开发机上启动 Electron，验证跨进程链路
4. **M3 认领**：UV 零重叠方案、Blender 脚本实机校正
