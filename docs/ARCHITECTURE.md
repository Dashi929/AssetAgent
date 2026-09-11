# 架构说明

> 本文只讲"代码怎么分层、数据怎么流、边界在哪"。产品决策见 [`../产品策划文档.md`](../产品策划文档.md)。

## 三层结构

```
Electron 渲染进程（React + r3f）      负责：视图状态、交互、检查
        │  HTTP localhost:8756
Python FastAPI sidecar                负责：一切破坏性操作、版本树、管线
        │  Provider 抽象接口
云端 Provider / 本地模型              负责：从图/文生成原始网格
```

**核心分工原则：预览与交互在前端，破坏性修改在后端，中间传"操作参数"而非数据流。**

前端永远只"显示某个版本"，不持有可变网格。这样做的直接好处：不需要自建 undo 栈（撤销 = 切到版本树上一个节点），也不会出现"前端改了一半、后端不知道"的状态漂移。

## 目录职责

| 路径 | 职责 | 不该做的事 |
|---|---|---|
| `apps/desktop/electron/` | 主进程、窗口、sidecar 子进程生命周期、OS 级密钥存储 | 不写业务逻辑 |
| `apps/desktop/src/` | UI、3D 视口、状态管理、API 客户端 | 不做顶点级网格编辑 |
| `services/agent/app/routers/` | HTTP 接口层，薄 | 不写业务逻辑，只做校验与转发 |
| `services/agent/app/store.py` | 资产库与版本树的唯一写入方 | 不碰网格算法 |
| `services/agent/app/jobs.py` | 任务排队、进度、取消、失败重试 | 不知道具体管线步骤 |
| `services/agent/app/tools/` | 管线各步骤的纯函数实现 | 不直接读 HTTP 请求 |
| `services/agent/app/providers/` | 各生成引擎适配 | 不做后处理 |
| `recipes/` | 规则与预设的**数据**，以及 Blender bpy 脚本 | 不写业务逻辑 |

规则和预设放在 `recipes/` 而不是代码里，是为了让技术美术能直接改阈值和导出规范，不用碰 Python。

## 数据流：工作流 A

```
1. 前端 POST /api/assets            上传概念图 → 建资产（status=draft），源图落 source/ 只读
2. 前端 POST /api/assets/{id}/generate
      └─ jobs 排队 → providers 生成 N 个变体 → 每个变体落 variants/<vid>/
      └─ 状态 → awaiting_pick
3. 前端 POST /api/assets/{id}/variants/{vid}/pick
      └─ 记录选中变体 → 状态 → processing
4. 前端 POST /api/assets/{id}/pipeline
      └─ 依次执行 repair → decimate → uv → bake → validate
      └─ 每一步产生一个 version 节点（带 parent_id）
      └─ 校验 FAIL 则状态停在 awaiting_validation，由美术决定修复/忽略/重生成
5. 前端 POST /api/assets/{id}/export
      └─ 按引擎预设导出 FBX/GLB + 贴图包 → exports/，状态 → exported
```

每一步都写盘，所以**关掉应用再打开，状态和版本树是完整的**。这是 MVP 的硬要求之一。

## 版本树

```
version 节点 = { id, asset_id, parent_id, op, params, mesh_path, created_at }
```

- `op` ∈ `generate | repair | decimate | uv | bake | export`
- 变体也是版本节点（`op=generate`），所以"变体对比"和"版本回滚"是同一套机制，不需要两套 UI 逻辑
- 回滚 = 把某个节点的 `mesh_path` 作为后续操作的输入，**不删除任何历史节点**
- 磁盘上每个节点一个独立目录，天然满足"永不覆盖"

## Provider 抽象层

```python
class Gen3DProvider(ABC):
    name: str
    mode: Literal["byok", "relay"]

    async def generate(self, req: GenerateRequest) -> list[VariantResult]: ...
    def estimate_cost(self, req: GenerateRequest) -> float: ...
    async def healthcheck(self) -> bool: ...
```

- **双路由**：`registry.resolve()` 按账号状态选通路（已登录且已订阅 → relay；否则 → BYOK）。MVP 只落地这个开关，relay 服务在 Phase 2。
- **mock provider**：不发任何网络请求，本地生成一个占位网格。作用有两个：让整条管线在没有任何 API Key 的情况下可测；让前端开发不依赖 Provider 可用性。**它是 MVP 能跑通冒烟测试的关键。**
- Provider 的具体 endpoint 与字段以官方文档为准，代码里集中放在每个 provider 文件顶部的常量区，方便 W1 对照校正。

## 校验器

规则配置在 `recipes/validation_rules.yaml`，引擎在 `app/tools/validate.py`。

- 输出结构化 JSON：`{ rule, result, value, threshold, locator }`
- `locator` 用于前端"点击 FAIL 项 → 高亮问题面 / UV 岛"，所以规则实现必须能给出定位信息，只给 PASS/FAIL 是不够的
- 阈值可按资产类目覆盖（预设 → 类目 → 全局默认，三级回落）

## 边界（明确不做）

| 不做 | 原因 |
|---|---|
| 前端顶点级网格编辑 | 那是 DCC 的活，超出部分引导用户回 Blender/Maya，不与专业工具抢功能 |
| 窗口内嵌 Blender | 闭源商业产品链接 Blender 有 GPL 传染风险、包体 +300MB、窗口自动化脆弱。只以 headless 子进程 + 文件交换使用 |
| 角色 / 生物 / 蒙皮 | 自动重拓扑对有机体不成熟，做了砸招牌 |
| 场景级整体生成 | 先做单资产，组合交给引擎 |
| WebGPU | WebGL2 足够 |

## 关键工程约定

- **预览格式统一 GLB**：sidecar 入库时把一切来源转成 GLB，前端不引 FBXLoader 等重 loader
- **大网格生成 Draco 压缩副本**供视口加载，拾取加速走 `three-mesh-bvh`
- **资产库缩略图**用离屏渲染的 PNG，详情页才加载 three.js 视口
- **绝不覆盖**：写入前检查目标路径，存在则追加 `_v2`、`_v3`；源目录以只读方式打开
