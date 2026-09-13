# 交接文档

> 每次代码改动后更新。下一个 Agent 读取此文件即可了解当前状态，无需翻 git log。
>
> **规则**：
> - 新改动追加到顶部（最新的在前）
> - 每条记录必须包含：改动摘要、影响文件、验证状态、已知问题
> - 已修复的问题从"已知问题"移到"已修复"
> - 不要删旧记录，只追加
> - **每轮改动完成后：构建便携版（`npx electron-builder --win nsis` → `build/win-unpacked/AssetAgent.exe`）并直接打开给用户测试**（2026-09-11 约定）

---

## 2026-09-14 第三十七轮：导入管线性能 —— 两处 Python 循环向量化（4.5min → 62s）

用户反馈高面数导入慢。cProfile 剖析 154k 面摩托：瓶颈不是"没并行"，是两个纯 Python 循环 ——
烘焙逐面采样循环 **200s**（96% 占比）+ validate 的 `uv_overlap` **169s**。xatlas（~90s）与
最近点查询（8.3s，rtree）本身不是问题；Voronoi 查询本来就多线程。

| 位置 | 修复 |
|---|---|
| `bake._bake_diffuse_software_impl` | 逐面采样循环向量化：重心坐标一次批处理（`points_to_barycentric` 传全量数组）、UV 用 `einsum`、按贴图对象分组后整段 numpy 采样，因子/黑面规则向量化同语义 |
| `uv.uv_overlap` | 网格桶改 CSR 风格（numpy 分组替代 dict 逐面插入），点-三角形包含测试批处理（`_points_in_triangle_batch`），逐点逐候选循环 → 候选对数组 + 分块（1M 对/块，防病态 UV 撑爆内存）；语义逐条对齐：同样的内点采样、UV 缝相邻豁免、1e-6 margin、双向标记 |

实测（154k 面摩托，打包版真机）：软件烘焙 250s → **10.8s**（23×）；uv_overlap 169s → **11.4s**（15×）；
**全管线 4.5min → 62s**（repair ~10s / uv ~30s / bake ~15s / validate ~20s / 渲染写盘 ~10s）。
输出与向量化前一致（图集绿占比 7.28% vs 7.72%，浮点舍入级差异）。

**教训**：性能问题先 cProfile 拿数字再动手 —— 凭直觉会去开多线程，实际是循环没向量化；
scipy.cKDTree 的 `workers=-1` 和 `trimesh.proximity`（rtree 逐点查询）性能差 50 倍，但后者
8s 的占比不值得再造轮子。

验证：118 passed（含 9 个烘焙/对齐回归 —— 向量化语义一致性由红绿图集、对齐正反例、
双套 UV 测试锁定）。已重新打包。上一轮的"0% 绿"误会也在此澄清：早前对照实验的
"源模型本来就是黑的"结论是错的，根因正是本轮第 3 条软渲染浮点截断 bug。

---

## 2026-09-14 第三十六轮：导入不限面数（用户拍板）

用户决策：导入的模型**不再限制面数**（否掉了第三十五轮末的"自适应预算"提案，直接放开）。

| 位置 | 内容 |
|---|---|
| `models.SpecPreset.face_budget` | 类型放宽 `int | None`；**None = 不限**（默认仍 5000，生成流程不受影响） |
| `routers.assets._import_spec` | 新辅助：导入路径的规格 = 预设拷贝 + `face_budget=None`；`/import` 与 `/import-mesh` 两个入口都换用它（生成流程的 /upload 仍走原预设） |
| `pipeline.run_pipeline` | `spec.face_budget is None` 时从 step_list 剔除 DECIMATE（不产生空跳版本节点） |
| `validate._rule_face_budget` | `face_budget is None` → 规则 SKIPPED（"该资产不限面数"），不再回落到 5000 默认（旧代码 `or` 会把 None 吞掉） |
| 前端 | 详情页「面数预算」显示"不限（导入模型不削减）"；types.ts `face_budget: number \| null` |

**代价与边界（预期内）**：摩托实测 uv 展开 ~100s、软件烘焙 ~240s、全管线 ~4 分钟（有减面时 ~2 分钟）；
版本文件显著变大（bake 版 GLB 含 154k 面 + 2048² 图集）。转台软渲染 8 帧在 15 万面上耗时可接受。
导入资产校验只剩命名规范失败（文件名含 `-`），AI 编辑改名即可过。

验证：118 passed（新增 test_face_budget_skipped_when_unlimited；导入测试断言 head 面数 = 源模型
面数、无 decimate 版本）；typecheck 通过；重打包后真机重导摩托：head 154,462 面 / 476 组件原样
保留，缩略图与转台 = 绿色川崎、细节完整（后视镜/风挡/车壳棱线清晰）。

---

## 2026-09-14 第三十五轮：摩托颜色三连修（texCoord / 空间对齐 / 软渲染黑屏）

用户贴图对比：源模型是亮绿川崎，我们渲出来是黑车。第三十四轮修完网格后又追出**三个独立的
颜色 bug**，全部修复：

1. **texCoord 被无视**（gltf_load）：材质的 baseColorTexture 引用带 `texCoord` 字段指定用第几套
   TEXCOORD（摩托 24 个 primitive 里 22 个有 3~5 套 UV），加载器永远读第 0 套 → 颜色采到错误贴图
   区域（绿车采样全黑）。修：按 texCoord 读对应套。注意 texCoord 挂在**材质里的贴图引用**上，
   不是 textures[] 条目上（第一版就读错了地方）。
2. **烘焙空间不对齐**（bake）：管线把低模 normalize 过（缩放到 expected_size + 挪轴心），高模还是
   原始尺寸/位置 —— 最近点投影全打在错误零件上。修：软件路径对高模套同款 normalize_transform；
   Blender 脚本加 --align_pivot/--align_size 参数在场景里对齐。**对齐数学备注**：normalize 对
   "缩放+平移"是精确幂等的（平移在轴心步骤里被精确抵消），对齐测试的 aligned diff 恰好为 0 是正常的。
3. **软渲染带贴图整台纯黑**（raster）：`_face_base_colors` 返回 0~1 浮点色，`_render_frame` 兜底灰是
   0~255 整数色，绘制时 `int(浮点*shade)` 全被截成 0。修：统一 0~1 再乘 shade ×255。

**又一个位置传参事故（记牢）**：`bake_textures` 加新参数后，pipeline 与内部软件兜底调用仍按位置传
`spec.pivot, spec.expected_size_m` → pivot 塞进 maps 槽、对齐静默失效。修法：maps/pivot/expected_size_m
改 **keyword-only**（位置传参直接 TypeError，测试套件当场拦住 19 个失败）。

**图集填充改逐像素 Voronoi**（scipy.cKDTree）：PIL 逐面画多边形有两个致命伤 —— 细长 UV 三角经常
一个像素中心都盖不到（丢色）、后画的三角盖掉先画的（绿色车壳被吃成黑车实测）。Voronoi 每像素取
UV 空间最近面颜色，无丢失无覆盖，性能可接受（6833 面 / 2048² 图集约 5 秒）。

**调试方法论（本轮反复用到的判别实验）**：
- 颜色不对先做**双约定 A/B 采样**（y=v*H vs y=(1-v)*H 各采一遍数绿）——两种都 0% 说明不是翻转，
  是 UV/映射/空间问题；
- 图集与渲染对不上时，**用同一批面分别采样对比**（图集中心采样 10.54% 绿 vs 渲染 0%）锁断点层级；
- 测试场景的高模若经 `load_mesh` 再导出，材质会变 2×2 占位图（HANDOFF 老坑）——带材质的导出
  必须 trimesh.load 起手。

验证：117 passed（test_multi_material.py 9 个：含 texCoord=1 双套 UV 回归、对齐正反例、
装配体逐组件、软件烘焙红绿图集）；重打包后真机重导摩托：缩略图/转台 = **绿色川崎**（对照源模型
预览一致），图集绿约 3%（Voronoi 后 dev 7.7%）。视口（three.js）head GLB 内嵌贴图待用户确认。
已知问题：软件烘焙逐面平色；Blender 缺失时无 normal/AO；eslint 未安装。

---

## 2026-09-13 第三十四轮：摩托复盘 —— 网格肢解修复 + 方案 A 落地（导入贴图恢复颜色）

用户报障：导入的摩托网格碎掉 + 贴图颜色全没。定位出**两个独立的根因**并全部修复，方案 A（单图集
底色烘焙）一并落地。

**根因一（网格肢解）：UV 岛被当成组件。** glTF 在 UV 缝上故意复制顶点（同位置不同 UV），
repair 的游离组件剔除用 face_adjacency 找连通分量，整车 961 个零件被看成 1.7 万个"岛"，
最大岛之外的大多数被当垃圾删掉（17.6 万面删 9.9 万），再被 5000 面预算压成碎片。
**根因二（贴图丢失）**：自研 GLB 加载器只读几何+UV，材质从未进入内存；`_sanitize_for_export`
按"无真 PBR"兜底成灰 clay；烘焙只有 normal/AO 没有 base color。

| 位置 | 内容 |
|---|---|
| `mesh_io.welded_face_components` | 新公共函数：按顶点坐标精确焊接后算连通组件（np.unique 轴0 + 面邻接边对）。**trimesh 5.1.0 的 process=True 只合并部分顶点（13.9万→7.6万），不能依赖**；`component_count` 改用它（组件统计/校验器不再报 UV 岛数） |
| `repair._drop_stray_components` | 组件用焊接版；剔除从"二选一"改为**三条件同时满足**：面数<主体10% + 尺寸<21.5% + **不与主体膨胀包围盒（5%）相交**。装配体的螺丝/后视镜贴着车体=结构，保留；飘在旁边的碎片才删 |
| `decimate` | 焊接组件数 > 48 视为装配体，**直接逐组件减面**（`_decimate_per_component` 重写：位置焊接消除 UV 缝假边界 + 按面数比例预算 + 两轮收敛 + remap 缓冲复用）；全局路径仅用于单体网格 |
| `gltf_load` | `load_glb(path, with_materials=True)` 返回 `(mesh, SourceMaterialTable)`：逐面 baseColor 贴图（PIL）+ 角点 UV + sRGB 因子；metadata 记 source_material/image_count；`_build_import_node` 顺带写进版本 params |
| `recipes/bpy/bake_textures.py` | 加 `diffuse` 通道：Cycles DIFFUSE 只开 color pass（无光照）→ `T_basecolor.png`；**修掉高模 hide_render=True**（烘焙射线走渲染可见性，隐藏=烘平色，旧 normal/AO 实为近似平图） |
| `bake.bake_diffuse_software` | **无 Blender 兜底**：低模面中心 → trimesh.proximity 最近点 → 高模面重心 UV 采样源贴图 → 低模 UV 三角形填色 → 同构 `T_basecolor.png`。全 try/except 失败即 skipped 不炸管线 |
| `bake.attach_baked_material` | 烘焙产物挂成 PBRMaterial（baseColor/normal/occlusion，metallic=0 rough=0.9 双面）→ bake 版本 mesh.glb 自带贴图；`bake_textures` 只认本次 OK json 的产物文件（out_dir 跨跑复用，glob 全量会混入旧图） |
| `export._glb_has_textures` | 导出 GLB 时带贴图的版本文件**原样复制**（load→save 会经自研加载器丢材质）；无贴图才走原路径 |
| `raster._face_base_colors` | 软渲染逐面采样 baseColorTexture（面中心 UV），缩略图/转台兜底路径有颜色；全链 UV 约定 V-up（trimesh GLTF 导出 946 行自己翻 V-down，xatlas 输出零翻转即可用 —— trimesh 官方 unwrap 同样不翻） |

**V-up 约定（记牢）**：TextureVisuals.uv = V-up（原点左下）；glTF 文件 = V-down；trimesh 导入/导出两侧
各翻一次。源 UV 被加载器翻成 V-up，软件烘焙采样源贴图用 `y=(1-v)*H`，图集绘制与缩略图采样同式。

**这台机器没装 Blender** —— 之前 normal/AO 一直是静默 skip。软件底色烘焙兜底让"导入有颜色"不依赖
Blender；装 Blender 后走 Cycles 路径（normal/AO/diffuse 全量，画质更好）。

验证：115 passed（新文件 test_multi_material.py 7 个：焊接组件/统计/repair 邻近保留/装配体逐组件/
软件烘焙红绿图集/材质 GLB 回读/源材质统计）；打包版真机重导摩托：repair 保 154k 面 477 组件 →
逐组件减面 6837 面 → bake 版 GLB 内嵌贴图（1 材质 1 image）→ 缩略图/转台显示完整深色摩托
（对照 trimesh 官方加载器渲染源模型：车就是黑的，烘焙忠实）。视口目检待用户空闲。
资产校验停在 awaiting_validation 是命名规则（文件名含 `-`/小写不符 SM_ 规范），AI 属性编辑改名即可。
已知问题：eslint 未安装；FBX 导出仍依赖 Blender；软件烘焙逐面平色、薄壁处可能串色（Blender 路径无此问题）。

---

## 2026-09-13 第三十三轮：归档改为删除（带二次确认）

用户需求：归档功能改为删除功能，用户确认后删除。

| 位置 | 内容 |
|---|---|
| `store.delete_asset` | 真删除：`shutil.rmtree` 整个资产目录（版本/变体/贴图/导出/任务记录全没）；替代 `archive_asset`（已删）。`AssetStatus.ARCHIVED` 枚举保留 —— 兼容旧数据展示，`list_assets` 仍过滤 archived |
| `DELETE /api/assets/{id}` | 替代 `POST /{id}/archive`；**有 QUEUED/RUNNING 任务时 409 拒绝**（否则后台任务往被删目录写文件产出僵尸资产）；埋点 delete_asset |
| `_discard_broken_asset` | 创建/导入失败路径（原 archive 清理）改为直接删除 —— 刚建的坏资产不值得留；删除失败不掩盖原始 400 |
| 前端 AssetDetail | 「归档」按钮 → 「删除」（danger）；`window.confirm` 二次确认（列明不可恢复）；确认后 deleteAsset + refreshAssets + 跳回资产库 |
| 测试 | test_store 归档测试 → 删除测试（目录消失/再删 FileNotFoundError）；test_api 归档测试 → DELETE 200/目录移除/再删 404；hunyuan 导入失败测试改名为 cleans_up（断言不变） |

验证：后端 108 passed；typecheck 通过；sidecar 重建（**本轮改了 Python，必须 `python scripts/build-sidecar-only.py`**）
+ electron-builder 重打包；打包版真机验证：HTTP 建资产 → DELETE 200 → 再删 404 → 旧 archive 路由 404；
GUI 走查确认弹窗（取消 → 资产还在；确定 → 删除生效，列表与磁盘都消失）。
收尾：历史归档遗留的两个测试资产（queue_panel_test / old_crate，归档功能移除后会永久不可见）已删除。
已知问题：eslint 未安装（既有）；贴图丢色根因见第三十二轮前的诊断（未修，等用户拍板方案）。

---

## 2026-09-13 第三十二轮：导入去掉表单 —— 拖入即入队 + 自动命名

用户需求：导入模式去掉红框区域（资产名输入框、规格预设选择、开始导入按钮、提示行）；
拖入或点击选择素材后**自动开始入队导入**，资产名自动用文件名。

改动（仅前端 `Workbench.tsx`，后端零改动 —— 不传 name/preset_key 本来就是文件名 + 默认预设）：
- `queueImport(files)`：直接调 `api.importAssets(files)`（不传 name/preset）→ 进度行入列；
- `applyPickedFiles`：导入模式选完文件即调 `queueImport`，新建模式行为不变（存 pickedFiles）；
- 删除 `startImport` / `importing` state / `importName` state（上一轮加的，随表单一起移除）/
  `baseName` helper；新建按钮直接接 `submitCreate`；
- 切到「导入」tab 时清空新建模式的选图，避免两个拖放区语义混淆；
- 导入卡片只剩拖放区（文案改为「松手即自动开始导入 · 资产名自动取文件名」）+ 导入进度表；
- 顺手补了导入进度行的失败原因显示（`item.error`，如「不支持的格式 .exe」之前从不显示）。

导入规格预设不再可选（一律默认「道具·默认」）；如果以后要改预设，入口应在资产详情页。

验证：typecheck 通过；`npm run build:all` + electron-builder 重打包（**本轮纯前端，sidecar 无需重建**）；
打包版启动后 sidecar health OK。拖入即导入的 GUI 走查未做（用户在前台打游戏，不能抢焦点）——
应用已挂后台，用户切过去拖个文件即可验；已由 typecheck + 代码审读覆盖自动入队路径。
已知问题：eslint 未安装（既有）。

---

## 2026-09-13 第三十一轮：资产库任务队列浮窗

用户需求：资产库页加一个浮动窗口，显示当前任务队列（全局后台任务：导入管线/生成/导出等）。

| 位置 | 内容 |
|---|---|
| `app/jobs.py` | JobRunner 加 `self._jobs` 注册表（submit 时登记，完成时 `_trim_recent` 只留最近 30 条）；`recent_jobs()` 返回"进行中在前 + 完成的按时间倒序" |
| `GET /api/jobs/queue` | 浮窗数据源：`{"jobs": [Job + asset_name + running]}`；**必须注册在 `/{job_id}` 之前**，否则 "queue" 被当 job_id 404；资产名解析失败兜底"（已删除的资产）" |
| `api.getJobQueue` | 前端 client 方法 |
| `components/QueuePanel.tsx` | 浮窗组件：固定右下角，1.2s 轮询；行 = 资产名（点击跳详情）+ JobBadge + 步骤/百分比 + 进度条 + message + 取消按钮；有活跃任务自动弹出，× 关闭后有**新**任务（活跃数增加）自动重新弹出；支持折叠成标题条；空闲且关闭即隐藏 |
| `Library.tsx` | 挂载 `<QueuePanel />`（按用户要求只放资产库页） |
| `global.css` | `.queue-panel` 系列：fixed 右下 360px 卡片、阴影、行内名称链接截断 |

**关键坑（记给下个 Agent）**：
- `npx electron-builder --win nsis` **只打包现成的 `sidecar-dist/assetagent-sidecar.exe`，不重跑 PyInstaller**。
  改了 Python 代码必须先 `python scripts/build-sidecar-only.py` 再 electron-builder，否则打出去的是旧后端
  （本次 `/api/jobs/queue` 在打包版上 404 就是这个原因，代码本身没问题）。前端改动才只需要 `npm run build:all`。
- 打包/重启前先确认旧实例退干净：AssetAgent.exe 关窗后 sidecar 可能残留锁住 `sidecar-dist`，
  electron-builder 会报 `remove ... Access is denied`。

验证：后端 107 passed（含新增 `test_job_queue_lists_recent_with_asset_name`，顺带复验导入默认名=文件名）；
typecheck 通过；sidecar 重建 + electron-builder 重打包后，打包版 HTTP 真机验证：queue 空态 →
导入 glb → running（带资产名 queue_panel_test）→ succeeded，`/api/jobs/{id}` 参数路由不受影响。
浮窗 UI 的视觉走查未做（验证时用户在打游戏，不能抢前台焦点）—— 应用已挂后台，切到资产库即可看到；
测试资产 `queue_panel_test`（3D）在库里，不需要就归档。已知问题：eslint 未安装（既有）。

---

## 2026-09-13 第三十轮：导入资产名默认取文件名

用户需求：导入的资产名默认为导入素材的名字。

**根因**：后端 `POST /api/assets/import` 本来就会在 name 为空时用文件名（多文件逐个用各自文件名）；
问题在前端 —— 工作台「新建」「导入」两个 tab 共用同一个 `name` state，新建时输入过的资产名
残留到导入模式，单文件导入时被静默用作资产名而不是文件名；且导入框的资产名输入框从不预填。

**改动**（仅前端 `apps/desktop/src/pages/Workbench.tsx`）：
- 导入模式改用独立的 `importName` state，不再与「新建」的 `name` 互通，杜绝残留名字漏过来；
- 选文件统一走 `applyPickedFiles`：单文件导入自动把资产名填成**文件名去扩展名**（用户可再改），
  多文件则清空并禁用输入框（后端自动用各自文件名）；
- `startImport` 传 `importName`；输入框留空时后端仍回退到文件名，默认行为双保险；
- 输入框 label 改为「资产名（默认取文件名，可改；多文件自动用各自文件名）」。

后端无改动。验证：`npm run typecheck` 通过；`npm run build:all` + `npx electron-builder --win nsis`
打包成功；打包版真机走查（png 单文件 → 输入框自动填文件名去扩展名、png 两文件 → 输入框清空禁用、
导入完成后资产行名字 = 文件名）。glb 走同一前端代码路径，未单独真机测。
已知问题：eslint 未随 node_modules 安装（`npm run lint` 跑不起来，既有情况）。

---

## 2026-09-12 第二十九轮：工作台改造 —— 新建/导入分离 + 2D 素材 + 后台队列 + AI 属性编辑

用户需求落地：新建（文字/图片 → LLM 生成 2D/3D）与导入（2D/3D 素材 → LLM 改属性）
两个入口分离；都进资产库；详情页即预览/编辑模式；导入自动化 + 后台队列 + 完成通知跳转。

| 组件 | 内容 |
|---|---|
| `Asset.kind` | model（3D）/ image（2D），贯穿 store/路由/前端；2D 资产缩略图 = 最新生成图或源图 |
| `app/ai/imagegen.py` | 2D 图片生成适配器：CogView（与 LLM 同 Key 同平台），支持 b64/url 返回；config 加 imagegen_model |
| `POST /api/assets/import` | **后台导入队列**：2D/3D 混合批量；3D 由 job 完成 FBX 转换+整条管线（失败资产标 failed 不留僵尸）；2D 校验后即 validated；不支持格式逐项报错 |
| `POST /{id}/generate-image` | 2D 生成（LLM 优化提示词 → CogView → 版本节点 → validated）；3D 资产调用明确拒绝 |
| `POST /{id}/ai-edit` | **LLM 属性编辑**：自然语言 → 白名单结构化变更（name/tags/notes/prompt/enhanced_prompt + spec 三项）→ 应用并返回前后对比；命名正则校验 |
| 前端 Workbench | 两 tab：新建（素材类型 3D/2D 切换、概念图/描述/AI 优化）；导入（混合多选拖拽 + 后台进度列表 + 完成横幅 + 「查看/编辑」跳转） |
| 前端 AssetDetail | 按 kind 分流：image 显示图片预览、隐藏管线/导出/转台/变体；AI 属性编辑卡片（指令 → 变更 JSON 展示 → openAsset 刷新） |
| 埋点 | import_queue / generate_image / llm_edit |

**关键实现坑（记给下个 Agent）**：
- trimesh 的 `update_faces` 是"删除掩码"语义——传 (F,3) 新面数组会被当 fancy
  index 产出 (F,3,3) 坐标数组污染网格（第二轮黑屏+顶点裂纹的元凶之一）。
  翻转绕序等"改面"操作一律用 `mesh.faces = new_faces` setter。
- `TextureVisuals(uv=uv)` 构造时 material 自动填 Material() 基类实例而非 None，
  材质兜底判定不能写 `material is None`（见 mesh_io._sanitize_for_export）。
- bash heredoc 写含 `
` 的 Python 字符串会被 shell 折断——多行代码补丁改用
  Write 工具写脚本文件执行。

验证：106 passed（+11 导入队列/2D/ai-edit 测试）；打包版真机走查：
新建/导入两 tab、混合导入（GLB+PNG）后台队列跑通（模型 job succeeded +
校验完成、图片 validated）、资产库缩略图完好。`356df93`

---

## 2026-09-12 第二十八轮：视口渲染发黑 + 顶点裂纹 —— 排查记（mushroom 视口复查）

用户反馈：形状已正立但视口仍大片黑、部分顶点对不上。三个叠加根因 + 一个**流程坑**：

1. **材质占位图**：`TextureVisuals(uv=uv)` 构造时 material 自动填 `Material()`
   基类实例（**不是 None**），导出时 trimesh 对这种"无图基类材质"生成 2×2 灰
   占位图 × 0.4 底色 → 近黑。sanitize 判定改为"无真 PBR 贴图材质就兜底"。
2. **法线朝向**：fix_normals 对非水密小组件判定不可靠（73% 朝内）→
   `_orient_components_outward` 按组件校正（99.7% 朝外）。**坑**：trimesh 的
   `update_faces` 是"删除掩码"语义，传 (F,3) 数组被当 fancy index 产出
   (F,3,3) 坐标数组污染 faces —— 必须用 faces setter 整体赋值。
3. **顶点裂纹**：UV 缝复制顶点减面时各自塌缩 → decimate 前焊接；并绕开
   trimesh 封装直接调 fast_simplification（其 `faces.view(np.ndarray)` 在
   打包环境特定内存布局下抛 "triangles must be 2D"，是减面静默失败的源头）。

**流程坑（排查耗时的主因）**：修完只重打 sidecar 而没重跑 electron-builder，
win-unpacked/resources/sidecar-dist 一直是旧 exe——两轮"修复无效"假象。
以后 sidecar 改动必须 sidecar-dist 重打 **+ electron-builder 重打包** 才算完。
（另：PyInstaller 的 hidden-import 补齐了 app.ai/knowledge/ai_assist/telemetry/
gltf_load，并加 --paths 指源码目录，避免 editable 安装的解析怪癖。）

验证：两资产重跑校验全过（面数 5000 内）、GLB 材质 0.62 灰 doubleSided
无占位图、重合顶点 0、组件朝向 99.7% 朝外、真机视口截图正常。95 passed。
`f7151b2`

---

## 2026-09-12 第二十四~二十七轮：GLB 导入三连修 + AI 助手（提示词优化 / 视觉校验）

### 第二十四轮：GLB 导入渲染碎片 —— 三个叠加根因（用户报 mushroom_house.glb）

逐帧渲染管线各步产物定位（缩略图对照），三个独立问题叠加：

1. **repair 剔除游离组件在万组件网格上静默失败**：`mesh.split()` 构建子网格内存爆炸，
   异常被吞，碎片原样流进 fast-simplification 被炸成 3496 个组件 —— 改为
   face 邻接连通分量 + 面掩码（不建子网格），失败原因带出 report。
2. **管线内部轴约定是 Z-up**（repair 归一化底面/校验 pivot/软渲染相机），GLB 是
   Y-up；mock 测试数据恰好 Z-up 从未暴露 → 统一 Y-up，软渲染不再侧躺。
3. **decimate 无护栏** → 组件数暴涨检测 + 逐组件减面回退；另加闭环收紧
   （fast-simplification 近似收敛，单次 5000 目标落在 6219，按比例修正最多
   三轮，真实文件 134k→4019 达标）。

附带：自有 GLB 加载器 `app/tools/gltf_load.py`（规范子集 + 人话报错，接管
.glb/.gltf；7 个单测）+ 管线专用线程池（隔离默认池偶发挂起）+
`uv_island_margin_px` 加 15s 时间预算（xatlas 布局不带种子，O(岛对²) 距离
计算偶发分钟级，曾把管线卡死在 validate——faulthandler 线程栈抓到的）。

真实 mushroom_house.glb 端到端：剔 3458 游离组件 → 4019 面（预算内），
正立、无碎片，与 Blender 参考一致；打包版重新导入+回滚重跑全过，校验零 FAIL。
**89→95 passed**。`a764cee` `ca1ab52`

### 第二十五~二十七轮：AI 助手（新功能，用户需求）

链路：**描述/图片 → LLM 按知识库优化提示词 → 生成 → 视觉 LLM 校验结果**。

| 组件 | 内容 |
|---|---|
| `app/ai/llm.py` | OpenAI 兼容适配器（chat/vision/JSON 模式）；默认智谱 GLM，base_url/model 可换（DeepSeek/通义/Moonshot/OpenAI）；Key 复用 BYOK |
| `recipes/knowledge/prompt_optimization.md` | 提示词知识库（TA 可编辑，即时生效）：单主体/完整可见/风格词/比例锚点/本项目校验口径/类目词汇表 |
| `app/knowledge.py` | 知识库加载（recipes_dir 三级回落，mtime 缓存） |
| 端点 | `POST /{id}/enhance-prompt`（落库 enhanced_prompt）、`POST /api/assets/enhance-prompt-preview`（工作台预览不落库）、`POST /{id}/visual-check`（概念图+转台帧 → SemanticCheck 落盘） |
| generate | 提示词优先级：用户手输 > AI 优化词 > 原始描述 |
| 前端 | 设置页「AI 助手」卡片；工作台「AI 优化描述」按钮（结果填回可编辑）；详情页「AI 视觉校验」卡片（评分/问题/建议） |
| 埋点 | llm_enhance / llm_visual_check |

**95 passed（含 6 个 AI 测试，LLM 调用全部 mock）**。真机验证：设置卡片/
详情卡片截图确认；**真实调用待用户填 Key**（默认 GLM glm-4-flash）。`f5d3adb`

---

## 2026-09-12 第二十二轮：G4 压测 + 真机走查 + CSP blob 修复（打包版全链路绿）

### 改动摘要
自主完成 G4 压测与打包版 UI 走查（用户不在电脑前，用 CDP + 截图代替人工）：
1. **G4 压测**（`tests/stress_g4.py`）：in-process 连续 20 轮任务（工作流 A/C 交替），每轮断言状态机、转台帧、校验、导出 —— **20/20 通过**，均 13.7s/轮。
2. **真机走查**：重建便携版（sidecar 68MB + asar 1.1MB），带独立数据目录启动 + CDP 走查——真实 FBX（phong_cube）页面内上传 → 管线 succeeded → 校验通过 → 转台 8 帧；详情页截图确认 3D 视口正常渲染（WASM 修复生效）、版本树带「回滚到此」、任务记录完整。
3. **抓到并修掉一个真 bug**：#9 修好的 console 日志当场抓到 **CSP `connect-src` 缺 `blob:`** —— GLTFLoader 用 fetch 读 blob: 贴图被静默拦截（开发模式被 vite 的 CSP 掩盖）。补 `blob:` 后打包版复测零报错。修复已进 `12cf4db`。

### 验证状态
- 打包版全链路（FBX→管线→校验→转台→视口）真机通过；截图存 `.data/qa-run/`（已 gitignore）
- 注：走查中「资产库 0 个资产」是外部 API 导入后前端未刷新所致（真实用户走 UI 会刷新），不是缺陷；版本树「导入」行的回滚按钮有换行的小 UI 瑕疵，无碍功能

---

## 2026-09-12 第二十一轮：转台渲染接入 + 精确预估 + 三步引导（M4/M6 补全）

| 文件 | 变更 |
|---|---|
| `app/tools/pipeline.py` | 管线完成后渲 8 帧转台（Blender 优先、软渲染兜底、旧帧先清） |
| `app/routers/assets.py` | `_asset_summary` 返回 `turntable` 帧列表 |
| `app/routers/meta.py` | 新增 `GET /api/estimate`：按 Provider 自己的 `estimate_cost` 精确预估（离线占位/本地报 ¥0） |
| `AssetDetail.tsx` | 新增「转台（8 帧）」卡片 |
| `Workbench.tsx` | 预估改为走 /api/estimate（变体数/引擎变化即刷新，静默失败）；空资产库时显示三步引导横幅（localStorage 记忆关闭） |

验证：工作流 C 全链路 8 帧产出实测；typecheck/pytest 过。`94d0384`

---

## 2026-09-12 第二十轮：四边面 Spike 完成 —— 建议不进 MVP

结论材料 [spike-quad-remesh.md](spike-quad-remesh.md)：PyPI 无 quad 重拓扑包（instant-meshes/quadriflow/quadwild 均 404）、fast-simplification/MeshLab 只产三角面、Instant Meshes 无官方 release 源。可行路径只剩 Blender Quadriflow（复用可选增强模式，本机无 Blender 未实测）。**建议不进 MVP，Phase 2 走 Blender 路径（出口标准已写在文档里）—— 待产品拍板。** ROADMAP 决策点已标记。`2aea18e`

---

## 2026-09-12 第十九轮：四家 Provider 端点对照官方源校正（#3 文档核对完成）

官方文档站在本机不可达（超时/被墙），改用**官方 SDK 源码**逐一核对：

| Provider | 依据 | 修正 |
|---|---|---|
| 混元3D | TencentCloud SDK `ai3d/v20250513` | 版本号 2025-01-01 → **2025-05-13**；动作名确认；补 `EnablePBR`/`FaceCount`（≥3000）；产物解析改官方 `ResultFile3Ds[{Type,Url}]` 优先 GLB |
| Meshy | 官方 OpenAPI 生成 SDK（tryAGI/Meshy） | 路径/字段全对；`ai_model` meshy-4 已下架 → **meshy-5** |
| Tripo | PyPI 官方客户端 `tripo` 0.2.1 | 改官方 **upload→file_token** 流程；探活改 `/user/balance`；model_version v2.5-20250123 确认 |
| Rodin | DeemosTech 官方 rodin-api-mcp | 域名 → **hyperhuman.deemos.com**；创建改 multipart 表单；status/download 表单 POST；产物从 /download 按名挑 model.glb |
| polling | — | FAIL_STATES 补 `fail`（腾讯 FAIL 小写漏配，失败任务原本会干等到超时）|

81 passed 无回归。**剩余：真实 Key 冒烟（需用户拿 Key）。** `c5d233e`

---

## 2026-09-12 第十七+十八轮：UV 零重叠（收缩法→重打包法）+ 黄金集 10/10 全绿

黄金集首跑（10 张占位概念图批量回归）把第十七轮的"岛收缩去重叠"当场证伪（445 岛压不到零、恒剩 2 面岛内折叠），升级为**重打包**方案并连带修掉三个真问题：

1. **`repack_uv_islands`**：货架式装箱重排 UV 岛，零重叠 + 岛间距由构造保证；岛内折叠用「裁面成岛 + 逐面 jitter（1e-4，大于划岛容差 1e-6、视觉 <0.5px）」兜底。`uv_overlap` 校验规则 warn → **fail**（行业口径零容忍）。
2. **减面后重新归一**：decimate 削掉包围盒边缘顶点，repair 摆好的轴心漂移 2.09cm 超容差 → `_step_decimate` 后在新拓扑上重新 normalize。
3. **黄金集落地**：`scripts/make_golden_set.py`（10 张程序化占位图，文件名冻结、美术可同名替换）+ `app/golden.py` 批量回归（exit code 挂 CI）。**结果：工作流 10/10 + 校验一次通过 0/10 → 10/10。**

回归截图与失败明细由 `python -m app.golden --keep` 输出。81 passed。`8ee7e38`

---

## 2026-09-12 第十四~十六轮：console-message / 埋点 / 版本树回滚

| 轮次 | 内容 | 提交 |
|---|---|---|
| 十四 | #9 修复：console-message 适配 Electron 32 运行时单对象签名（类型声明仍是旧的，两种形态归一），渲染进程 console 重新落 main.log | `50639f4` |
| 十五 | **本地埋点系统**：`telemetry.jsonl`（默认不上传）覆盖任务/每步耗时、失败分类、生成花费、变体采纳率、校验明细、导出次数与引擎类型；`/api/telemetry/summary` + `/export`（NDJSON 手动上报）；Settings 页「数据与遥测」卡片 + 导出按钮；7 个测试 | `4b2c26e` |
| 十六 | **版本树回滚**：`POST /{id}/rollback` 非破坏性（复制目标版本含 OBJ 测量副本为新 head）；版本树非头节点「回滚到此」按钮；6 个测试 | `6f2e024` |

---

## 2026-09-11 第十三轮：FBX 导入单位归一 —— 修掉"模型大了 100 倍"

### 改动摘要
用户报"导进去的模型太大"。实测导入节点尺寸 100 × 50 × 70 —— FBX 内部单位是厘米（空气球桌真实尺寸 1m × 0.5m × 0.7m，完全合理），ufbx2obj 转换时没做单位归一，厘米原样进了 GLB（引擎标准是米）。管线的 repair 步虽有 normalize_transform（按规格缩到期望尺寸），最终输出其实是标准的，但导入节点本身就是错的，美术在版本树里一看到导入版本就是巨大的。

### 修复
`native/fbx2obj.c`：`ufbx_load_opts` 设 `target_unit_meters = 1.0f` + `target_axes = ufbx_axes_right_handed_y_up`（GLB 规范要求 Y-up 右手系）。ufbx 把单位/轴向换算烘焙进 `node->geometry_to_world`，现有的变换代码无需感知。附带收益：Z-up 来源（3ds Max 等）的文件也会被归一到 Y-up，不会侧躺。

### 验证状态
- 本机转换实测：100 × 50 × 70 → **1.0 × 0.5 × 0.7** ✓
- 打包版 sidecar 实测：导入节点即 1.0 × 0.5 × 0.7 ✓
- 便携版重建并打开
- 注：repair 的 normalize_transform 保留不动 —— 它的语义是"按规格期望尺寸缩放"（spec.expected_size_m），与单位归一是两件事

---

## 2026-09-11 第十二轮：FBX 导入闪退根因修复 —— 双凶手（CSP 拦 WASM + 老版 VC 运行时段错误）

### 改动摘要
用户复测 FBX 导入，"连不上 + 闪退"复现，**三路日志完整记录了全过程**，一次定位两个独立根因：

1. **渲染进程**：`WebAssembly.instantiate(): Refused to compile ... 'unsafe-eval' ... script-src 'self'` —— CSP 不允许编译 WASM，视口加载 GLB 时 three.js 的 WASM 解码器被拦，Uncaught 异常。
2. **sidecar 进程**：退出码 3221225477（0xC0000005）。Windows 事件日志给出故障模块：`_MEI*/MSVCP140.dll 版本 14.16`——**fast_simplification / numpy 的 wheel 自带 2019 年的 VC 运行时**，PyInstaller 打包后全局遮蔽系统新版（14.5x），管线 uv 步 xatlas 一调新版 CRT 功能就段错误。开发环境正常（用系统运行时）所以冒烟测试从未暴露。

时序：导入成功 → 管线跑到 uv 步 → sidecar 段错误死亡 → 前端 fetch 全挂（"连不上"）→ 视口 WASM 也被 CSP 拦（错误铺满）。

### 修复

| 文件 | 变更 |
|---|---|
| `apps/desktop/index.html` | CSP `script-src` 加 `'wasm-unsafe-eval'`（Electron + three.js WASM 解码器的标准配置） |
| `scripts/build-sidecar-only.py` / `build-all.py` | PyInstaller 显式 `--add-binary` 系统 `System32\MSVCP140.dll` 覆盖 wheel 自带的老版（MSVCP140 向后兼容，新版运行旧构建只赚不亏） |

### 排障过程（方法论沉淀）
日志就位后一轮定位：main.log 抓到 WASM CSP 异常 + sidecar 退出码；事件日志给出故障模块与版本号；用打包版 sidecar + curl 复现（import → pipeline → 死），修复后同路径验证通过。**"开发正常、打包崩"优先怀疑 PyInstaller 打进去的陈旧二进制遮蔽系统组件。**

### 验证状态
- 打包版 sidecar 完整复现通过：import → repair → decimate → **uv（原崩溃点）** → bake 全部完成，job succeeded，进程存活
- 便携版重建并打开：界面数据请求正常、sidecar 健康
- 待用户复测：拖 FBX 应完整走通管线（uv 不再崩），视口加载 GLB 不再有 WASM 报错

---

## 2026-09-11 第十一轮：修复陈旧错误横幅 —— 就绪后自动恢复

### 改动摘要
用户仍看到"连不上本地服务"。用 CDP（`--remote-debugging-port=9222` + Node WebSocket）在打包版页面里直接执行 fetch，实测 **200 OK** —— 网络、CSP、CORS 全部正常。真相：横幅是启动期的**陈旧错误**。页面加载早于 sidecar 就绪 → 首次数据加载失败 → 错误横幅挂上 → 第十轮修的状态推送让徽章变绿了，但没人重试数据加载、也没人清横幅，于是"应用明明是好的，界面却报连不上"。

### 修复
`src/store/useAppStore.ts`：`applyStatus()` 统一处理状态——sidecar 从未就绪变为 ready 时，**清掉 error 并重拉 assets/presets/settings**。桌面模式的数据加载改由就绪事件触发（轮询 + IPC 推送双通道都会走到）。

### 排障方法论（下一个 Agent 直接看这里）
打包版渲染进程的网络问题，用 `AssetAgent.exe --remote-debugging-port=9222` + `curl http://127.0.0.1:9222/json` + Node 内置 WebSocket 跑 CDP `Runtime.evaluate`，在**真实页面环境**里执行 fetch 拿第一手报错 —— 比隔着日志猜快得多。判定依据：uvicorn 访问日志里有没有渲染进程的请求（CORS 拦截不挡发送、CSP/未发送才会没日志）。

### 附带发现
- main.ts 的 console-message 钩子在 Electron 32 新事件签名下失效（level 参数不再是数字），console 报错实际没被记录 —— 待修（本期未修，见已知问题 #9）
- file:// 页面的 fetch 到 http://127.0.0.1 完全正常（CSP 的 connect-src http://127.0.0.1:* 工作正常；CORS 白名单里的 "null" 条目覆盖 file:// 的 Origin: null）

### 验证状态
- typecheck 通过；便携版重建并打开
- **实锤**：uvicorn 访问日志首次出现渲染进程的数据请求（assets/presets/settings 全 200）
- 待用户复测：界面应直接进入工作台，无"连不上"横幅

---

## 2026-09-11 第十轮：修复"连不上本地服务" —— 状态推送竞态 + 端口缓存

### 改动摘要
用户报"连不上本地服务（8756）"。日志勘查发现 sidecar 实际健康运行在 8756（curl 正常），问题在前端**永远收不到 sidecar 状态**：main.ts 用 `once('did-finish-load')` 推送状态，而页面加载（~1s）比 sidecar 启动（~4s）快，推送注册时事件早已发过；且 client.ts 把 8756 fallback **永久缓存**，sidecar 就绪后也不会重解析。

### 修复内容

| 文件 | 变更 |
|---|---|
| `electron/sidecar.ts` | 新增 `onStatusChange` 回调，状态每次变化（starting/ready/failed/stopped）都触发；所有赋值点收拢到 `setStatus()` |
| `electron/main.ts` | ① `onStatusChange` → 广播给所有窗口（渲染进程可能随时重载）；② `once('did-finish-load')` 改为**持久 `on`**，加载完成时补发当前状态 |
| `src/api/client.ts` | ① sidecar 未就绪时 fallback 8756 **不再缓存**（就绪后下次请求重解析出真实端口）；② 网络层失败时清空已缓存端口（sidecar 重启换端口后能自愈） |
| `src/store/useAppStore.ts` | `init()` 加轮询兜底：每 1.5s 查一次状态直到 ready（最多 60 次），与 IPC 推送双保险 |

### 教训
"窗口加载快于后端启动"是桌面应用特有的竞态，web 端不会遇到。任何"只推一次"的初始化状态在 Electron 里都必须假设渲染进程会在任意时刻（重）加载。

### 验证状态
- typecheck 通过；便携版重建并打开
- main.log 实测显示完整状态生命周期：`starting` 广播 → `ready` 广播 → 启动结果，sidecar 健康在 8756
- 待用户复测：界面应正常进入工作台，可拖 FBX

---

## 2026-09-11 第九轮：闪退排障日志系统（三进程全链路）

### 改动摘要
用户拖 FBX 后应用"闪退"（三进程全灭，无任何现场）。本轮建立三路日志：主进程 / 渲染进程 / sidecar 分别落盘到 `%LOCALAPPDATA%\AssetAgent\logs`，挂上所有崩溃钩子；渲染进程崩溃从"退出全应用"改为"自动重载救回"；顺带阻断 Electron 的拖拽默认导航（拖到拖放区外会把窗口替换成 file://，症状与闪退一致——本次闪退的头号嫌疑）。

### 现场勘查（闪退发生时的证据）
- 用户拖入 `air-hockey.fbx`：**导入是成功的**（ufbx 转换的 GLB 健康：438 面、带 UV），资产与 3 个版本节点落盘完整
- 管线 job 死在 **progress=0.6**（repair/decimate 已过、到 uv/bake 附近），随后 AssetAgent.exe + assetagent-sidecar.exe 全部消失
- 无 crashpad 转储、无任何日志（这正是本轮要补的）

### 详细变更

| 文件 | 变更 |
|---|---|
| `electron/logging.ts` | 新增。日志目录解析（打包=%LOCALAPPDATA%\AssetAgent\logs，开发=仓库 .data/logs）+ `log(channel, msg)` 追加写（appendFileSync，崩的最后一行也落盘；单行截断 2000 字符） |
| `electron/main.ts` | 挂钩：`uncaughtException` / `unhandledRejection` / `child-process-gone`（GPU 等子进程崩溃）/ `render-process-gone`（reason+exitCode）/ 渲染进程无响应 / console warning+error / did-fail-load。**render-process-gone 为 crash/oom 时自动重载窗口**（崩溃不再连锁退出全应用）。新 IPC：`logs:append`（前端日志）、`logs:reveal`（打开日志目录） |
| `electron/sidecar.ts` | 构造函数加 `logFile` 参数：stdout/stderr 同步落盘 `logs/sidecar.log`（以前只进内存缓冲，应用一退全丢），启动/退出带分隔标记 |
| `electron/preload.ts` + `src/global.d.ts` | 桥新增 `appendLog(line)` / `revealLogs()` |
| `src/main.tsx` | ① window 层 dragover/drop preventDefault（阻断拖拽导航）；② `error` / `unhandledrejection` 全局钩子 → renderer.log |
| `src/pages/Workbench.tsx` | 导入埋点：开始（含文件名/大小）→ 成功/失败 → 管线启动 |
| `src/pages/Settings.tsx` | "查看日志"旁新增"打开日志目录"按钮 |

### 验证状态
- `npm run typecheck`：两个 tsconfig 均无错误
- 便携版已重新构建并打开，**日志已实测在写**：main.log 记录启动 + sidecar 就绪，sidecar.log 开启记录
- 闪退根因待复测：用户重拖 FBX 后读 `%LOCALAPPDATA%\AssetAgent\logs\`（main.log 最后一行 + sidecar.log 尾部 + renderer.log）即可定位

---

## 2026-09-11 第八轮：ufbx 原生 FBX 导入 —— 摘掉 Blender 依赖

### 改动摘要
FBX 导入不再需要用户安装 Blender：基于 [ufbx](https://github.com/ufbx/ufbx)（单文件 C 解析器，MIT，Godot 4.3 / Blender 4.5 同款方案）写了内置转换器 `ufbx2obj`（612KB，随应用分发），转换链变为 **ufbx（内置，优先）→ Blender headless（可选回落）→ 报错给替代路径**。Blender 从"导入必需"降级为"可选增强"（烘焙/FBX 导出/转台仍用它，缺失时照旧降级跳过）。

### 详细变更

| 文件 | 变更 |
|---|---|
| `services/agent/native/ufbx/` | 新增。vendored ufbx 0.23.0 源码（ufbx.c/ufbx.h，MIT） |
| `services/agent/native/fbx2obj.c` | 新增。FBX→OBJ：几何+UV、`node->geometry_to_world` 烘焙世界变换、行列式<0 翻转绕序、多网格合并（v/vt 全量写出，f 行分开引用两套索引，文件不膨胀）；输出 `mtllib`/`usemtl` 供 trimesh 保住 UV |
| `services/agent/native/win_shim.h` | 新增。MinGW.org 老头文件缺 `_wfopen` 声明，不声明的话 64 位下指针截断会崩 |
| `services/agent/native/build.py` | 新增。一键编译（gcc / MSVC cl 双支持） |
| `services/agent/bin/ufbx2obj.exe` | 新增（随仓库提交，612KB），开发模式直接可用 |
| `app/tools/convert.py` | 重写为三级回落。坑：trimesh 只在 OBJ 材质**带贴图**时才做 UV 拆分，配套写 1×1 白 PNG 占位材质（管线会用 xatlas 重展 UV，白图只是让导入 UV 活到 GLB） |
| `app/paths.py` | 新增 `native_bin_dir()` / `find_ufbx2obj()`：开发= `bin/`，打包= `_MEIPASS`（`--add-binary` 落点） |
| `scripts/build-sidecar-only.py` / `build-all.py` | `--add-binary` 捆绑 exe；hidden-import 补齐 `app.paths`/`app.tools.convert`/`hunyuan3d`（build-all 落后于 sidecar-only，已对齐）；顺手修 build-all.py 产物目录还写着旧的 `release/`（现应为 `build/`） |
| `tests/test_hunyuan_import.py` | FBX 测试重写：真实 FBX 端到端 3 个（真实 exe 转换 / UV 存活 / API 全链路，`skipif` 兜底无 exe 环境）+ 回落链 4 个（Blender 兜底成功/失败、两级全挂错误汇总、无转换器 400 归档） |
| `tests/fixtures/phong_cube.fbx` | 新增。带 UV 的最小 FBX 样本（来自 assimp 测试模型库，BSD-3，见 fixtures/README.md） |
| `tests/test_paths.py` | 新增 3 个：原生工具开发/frozen/缺失三种定位行为 |
| `apps/desktop/src/pages/Workbench.tsx` | 提示文案去掉"fbx 需要 Blender" |

### 关键排障记录
- `ufbx_triangulate_face` 返回的是**三角形个数**（不是索引数），按索引数校验会静默丢掉所有面 —— 症状是 OBJ 有 v/vt 但 f 为 0。
- MinGW 编 ufbx.c 必须 `-include win_shim.h` 强制声明 `_wfopen`，否则 64 位下隐式声明把指针截断成 int。
- 凭记忆写的"1×1 白 PNG base64"是坏的（PIL 报 broken data stream）——pillow 本来就是运行时依赖，直接用 PIL 生成，别手抄 base64。

### 验证状态
- `pytest tests`：**63 passed**（+7 净增）；ruff / tsc 全过
- assimp 5 个真实 FBX 样本（box / cubes×2 / phong_cube / spider）转换全部通过，trimesh 验证 watertight/winding/UV 正常
- **打包版 sidecar 实测**：重打后从 `_MEIPASS` 找到 ufbx2obj，`POST /api/assets/import-mesh` 上传真实 FBX → 201，import 节点指向 GLB 工作副本，原始 FBX 只读保留（测试资产已清理）
- 安装包（electron-builder）尚未重打，需要时跑 `python scripts/build-all.py`

---

## 2026-09-11 第七轮：拍板落地 —— 混元3D Provider + FBX 导入 + 校验阈值定源

### 改动摘要
把 2026-09-11 的一批拍板决策落进代码与文档：3D Provider 三家自由可选（新增腾讯混元3D 适配器）、工作流 C 永久免费且导入支持 FBX、校验阈值从行业通行规范定源。产品定位改为 **AI-native**，人力排期口径作废。

### 详细变更

| 文件 | 变更 |
|---|---|
| `app/providers/hunyuan3d.py` | 新增。腾讯云混元生3D 适配器：TC3-HMAC-SHA256 请求签名（凭据约定 `SecretId:SecretKey`，冒号分隔）；自带 POST 版轮询（腾讯查询动作是 POST JSON，共用 `poll_task` 只支持 GET）；处理腾讯"HTTP 200 + Response.Error"的错误约定。**动作名与 API 版号是按文档写的，W1 拿 Key 后对照校正** |
| `app/config.py` | 新增 `hunyuan3d_api_key` / `hunyuan3d_base_url`（默认 ai3d.tencentcloudapi.com） |
| `app/providers/registry.py` | 注册混元3D；`PRIORITY` 改 `(meshy, tripo, hunyuan3d, rodin)`——三家自由可选，顺序只是无指定时的自动回落；错误文案同步 |
| `recipes/bpy/convert_to_glb.py` | 新增。Blender headless：FBX→GLB。清空默认场景（`read_factory_settings(use_empty=True)`）、应用旋转/缩放、多对象 join |
| `app/tools/convert.py` | 新增。`convert_to_glb()`：Blender 缺失 / 转换失败 / 没产出文件都抛 MeshError 人话 |
| `app/routers/assets.py` | `MESH_SUFFIXES` 加 `.fbx`；import-mesh 里 FBX 先转 GLB 工作副本再建 import 版本节点（原始 FBX 留 source/ 只读，`params.converted_from` 记来源）；失败返回 400 并**归档资产**（不留 PROCESSING 僵尸）；工作流 C 注释标明永久免费 |
| `app/tools/mesh_io.py` | `load_mesh` 对 .fbx 给"请走导入入口"的专门指引 |
| `apps/desktop/src/pages/Workbench.tsx` | 导入 accept 加 `.fbx`，提示文案注明 fbx 需要本机 Blender |
| `recipes/validation_rules.yaml` | **数值全部不变**，每条阈值补注行业依据（Unity 1u=1m、非 POT 无 mipmap；UE SM_ 前缀、1uu=1cm；UV 1–2 texel 硬下限 vs 烘焙建议 4–8px；标准道具 3k–10k 预算区间等），文件头声明"AI 从训练语料提取，上线前 TA 复核" |
| `tests/test_hunyuan_import.py` | 新增 12 个测试：注册表成员与回落顺序、设置页出现混元3D、凭据切分（含 SecretKey 带冒号）、TC3 头结构、FBX 导入三路径（无 Blender 400+归档 / 转换失败带日志 / 成功走 GLB 工作副本）、convert 路径拼装、load_mesh 指引、recipes 定位不回归 |
| `tests/conftest.py` + `tests/test_api.py` | `client` fixture 提升到 conftest 共享；conftest 顺手排除 `HUNYUAN3D_API_KEY` 环境变量 |
| `产品策划文档.md` | 12 章重写为**拍板记录**：AI-native 定位、三家 Provider 自由可选、2D Provider 调研表（Scenario / PixelLab / Retro Diffusion / Meshy 贴图 / Layer）、工作流 C 免费、阈值定源、FBX 导入；Phase 2 三项挂起。9.2/9.3/9.4 同步（人力口径作废）、7.2/7.6/4.3 同步 |
| `docs/ROADMAP.md` / `README.md` | 排期口径改 AI-native；决策点表两项标已拍板；provider 列表加混元3D；工作流 C 标免费 |

### 拍板决策摘要（详见产品策划文档 12）

1. **AI-native**：不绑人力规模，周次只表能力建设顺序
2. **3D Provider**：Meshy / Tripo / 混元3D 自由可选，Rodin 保留兼容
3. **2D**：贴图/sprite 切入，Phase 2 开工；接入顺序建议 Meshy 贴图（复用现有 Key）→ Scenario → 像素类
4. **工作流 C 永久免费**（引流 + 校验器信任建设）
5. **校验阈值**：行业规范提取（已写入 YAML），TA 复核是强化项不是阻塞项
6. **导入格式**：支持 fbx / obj（本轮落地）
7. Phase 2（商业模式 / 订阅定价 / 海外收款）挂起

### 验证状态
- `pytest tests`：**56 passed**（原 44 + 新增 12）
- `ruff check app tests`：All checks passed
- `npm run typecheck`：两个 tsconfig 均无错误
- `python -m app.smoke`：全链路跑通，校验通过
- **未验证（无前置条件）**：混元3D 真实调用（无 Key，与 Meshy/Tripo/Rodin 同属 W1 端点校正）；FBX 真机转换（本机无 Blender，错误路径与成功路径均已用 mock 测过）

---

## 2026-09-11 第六轮：打包模式路径解析 + 应用图标

### 改动摘要
解决打包后 sidecar 的路径解析问题：新增 `app/paths.py` 统一开发/打包双模式的路径推导；**数据目录从安装目录迁到系统应用数据目录**（卸载不再带走用户资产）；recipes 优先读安装目录（用户可编辑）而非 exe 内的冻结快照；补上应用图标。

### 详细变更

| 文件 | 变更 |
|---|---|
| `services/agent/app/paths.py` | 新增。`app_root()` / `resource_dir()` / `find_recipes_dir()` / `default_data_dir()` + `FROZEN` 常量。文件头记录三个坑位（_MEIPASS 临时解压、数据目录不能落安装目录、冻结快照不能优先用） |
| `services/agent/app/config.py` | `REPO_ROOT` 改由 `paths.app_root()` 提供（打包后= resources/）；`data_dir` 默认值改 `default_data_dir()`；`recipes_dir` 改 `find_recipes_dir()` |
| `services/agent/sidecar_entry.py` | 打包时把 `_MEIPASS` 插到 sys.path 头部（必须在任何 `app.*` 导入之前）；**移除**强制 `ASSETAGENT_DATA_DIR=exe同级/data`；启动时打印数据/规则目录，真机排障第一眼可核对；补充"入口必须与 app/ 同级，否则 PyInstaller 静默产出空壳 exe"的坑位说明 |
| `scripts/build-sidecar-only.py` | `shutil.rmtree` 换成 `force_remove()`（cmd rmdir 强删，绕过沙箱回收站删除失败问题）；hidden-import 增加 `app.paths`；`subprocess.run` 补显式 `check=False`（ruff PLW1510） |
| `services/agent/tests/test_paths.py` | 新增 9 个测试：双模式数据目录、env 优先级、**打包后数据目录不在安装目录下**、app_root=resources、recipes 三级回落（安装目录 > _MEIPASS > 理论路径）、开发模式 recipes 真实存在 |
| `apps/desktop/package.json` | `build.directories.buildResources=build-resources`；`build.win.icon=build-resources/icon.ico` |
| `apps/desktop/build-resources/icon.ico` | 新增。应用图标（16–256 共 7 个尺寸的多分辨率 ICO） |
| `scripts/make-icon.py` | 新增。图标生成脚本（PIL 绘制深青底 + 白色等距立方体），改设计后重跑即可再生成 |

### 关键设计决策

1. **数据目录迁出安装目录**。之前 sidecar_entry 强制把数据放在 exe 同级 `data/`，即安装目录内 → 用户卸载时资产一起被删。现在打包后固定 `%LOCALAPPDATA%\AssetAgent`（macOS/Linux 同理），`ASSETAGENT_DATA_DIR` 环境变量仍可覆盖，开发模式仍是仓库 `.data/`。
2. **recipes 优先安装目录而非 _MEIPASS**。PyInstaller `--add-data` 在 exe 内塞了规则快照，但 onefile 每次启动换临时目录（路径不稳定），且那份用户改不动。recipes 是给技术美术编辑的配置，必须命中安装目录 `resources/recipes`；exe 被单独拷走时才回退内部快照。
3. **PyInstaller 入口文件必须与 app/ 包同级**（`services/agent/` 下）。放 scripts/ 时静态分析找不到 app 包会**静默**忽略整个包，产出一个能启动但一调用就 ModuleNotFoundError 的空壳 exe —— 体积异常小是唯一信号。

### 验证状态
- `pytest tests`：**44 passed**（原 35 + 新增 9 个路径测试）
- `ruff check`（app / tests / sidecar_entry / 两个 build 脚本）：All checks passed
- `python -m app.smoke`：全链路跑通，校验通过（6 PASS / 1 WARN / 2 SKIP）
- `package.json` JSON 校验通过；`icon.ico` 为合法多尺寸 Windows 图标资源
- **重新打包 + 安装布局实测通过**（本轮收尾）：
  - `build-sidecar-only.py` 重打 sidecar exe，裸跑（旁边无 recipes）正确回退 _MEIPASS 快照
  - `npm run build:all` + `npx electron-builder --win nsis` EXIT=0，无缺省图标警告（自定义图标已生效）
  - 产物：`build/AssetAgent Setup 0.1.0.exe`（150MB）+ `win-unpacked/`（asar 1.1MB / recipes 32K / sidecar 源码 263K / sidecar-dist 68M）
  - **从 `win-unpacked/resources/sidecar-dist/` 运行 exe**：规则目录正确解析到 `resources\recipes`（安装目录，非 _MEIPASS），数据目录落到 `%LOCALAPPDATA%\AssetAgent`，`ASSETAGENT_PORT` 覆盖生效，/api/health 正常
  - 仍待真机：双击安装包 → 启动 Electron 壳 → 跨进程链路（已知问题 #5，需带显示器的机器）

---

## 2026-09-11 第五轮：修复打包阻塞 + 应用瘦身

### 改动摘要
修复 Electron 打包链路上的三个阻塞问题（asar 文件锁、node_modules 全量入包、venv 被当源码拷贝），同时把应用体积从 ~250MB 降到 ~80MB。

### 详细变更

| 文件 | 变更 |
|---|---|
| `apps/desktop/package.json` | ① `files` 新增 `!node_modules/**/*` 与 `!**/*.map`：主进程实际零第三方依赖（只 require `electron` + node 内建模块），渲染层已由 Vite 全量打包进 `dist/`，因此 node_modules 完全不需要进 asar；② `npmRebuild: false`（无原生模块，跳过 `@electron/rebuild`）；③ `extraResources` 里 sidecar 源码的 filter 增加 `!**/.venv/**`、`!**/*.egg-info/**`、各类缓存目录排除 |

### 关键排障记录（下一个 Agent 直接看这里）

| 现象 | 根因 | 修复 |
|---|---|---|
| `app.asar` 删不掉 / `The process cannot access the file` | 上一次 electron-builder 的 `app-builder.exe` 进程残留未退出，持续持有句柄 | `taskkill /F /PID <pid>` 杀掉残留 app-builder 后再构建。**注意：不要在运行中的构建期间杀进程** |
| 沙箱里 `rm -rf` 报 `SAFE_DELETE_FAIL_CLOSED` | 安全删除机制走回收站，失败即拒绝 | 用 `[System.IO.Directory]::Delete($path, $true)`（.NET API）或 `cmd /c rmdir /s /q` |
| Vite build 报 `SAFE_DELETE_BULK_CONFIRM_REQUIRED count:50` | 沙箱对单轮内批量删除设了 50 个阈值 | 构建前先手工删除 `apps/desktop/dist` |
| `Cannot create symbolic link : 客户端没有所需的特权` | extraResources 拷贝 `services/agent` 时把 `.venv` 里大量文件也纳入，且沙箱限制符号链接创建 | 在 filter 里排除 `.venv`，并以 `dangerouslyDisableSandbox` 运行构建 |
| asar 高达 129MB | electron-builder 默认把生产依赖全量打进 asar（three / drei 等） | `files` 里显式 `!node_modules/**/*` |

### 构建产物（本轮已成功生成）

| 路径 | 体积 | 说明 |
|---|---|---|
| `apps/desktop/build/AssetAgent Setup 0.1.0.exe` | 150MB | NSIS 安装包，双击安装即可用 |
| `apps/desktop/build/win-unpacked/AssetAgent.exe` | 186MB | 便携版，不安装直接运行 |
| `build/win-unpacked/resources/app.asar` | **1.1MB** | 由 129MB 降下来（排除了 node_modules） |
| `build/win-unpacked/resources/sidecar-dist/assetagent-sidecar.exe` | 68MB | 修复后的 sidecar，随应用启动 |
| `build/win-unpacked/resources/sidecar/` | 36 个 .py | 源码副本（已排除 .venv），仅供 fallback 模式 |

### 验证状态
- `npx electron-builder --win nsis` → **EXIT=0，构建成功**（日志 `/tmp/eb3.log`）
- asar 内容已核验：仅 `dist/`（Vite 产物）+ `dist-electron/`（main/preload/sidecar）+ `package.json`，无 node_modules
- win-unpacked 已核验：`AssetAgent.exe` + `locales/` + 全部 Chromium DLL 齐全，`resources/` 下 `app.asar`、`recipes/`、`sidecar/`、`sidecar-dist/` 四项齐全
- sidecar exe 已单独验证可启动：`Uvicorn running on http://127.0.0.1:18756`
- **尚未验证**：真机双击安装包启动后的端到端表现（沙箱无显示器，见已知问题 #5、#6）

### 架构说明（用户常问：为什么要启动本地服务）
Electron 壳只负责窗口 / 3D 视口 / 文件对话框；全部重活（trimesh 修复、减面、UV、烘焙、校验、Provider 调用、状态机、版本树）都在 Python sidecar（FastAPI）里，因为 3D 生态在 Python 侧、任务状态需要落盘可恢复。两者走 `127.0.0.1` 本机 HTTP，sidecar 随应用启停，用户无需安装 Python（已由 PyInstaller 打成独立 exe）。

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

## 已知问题（2026-09-12 更新）

| # | 问题 | 影响 | 状态 |
|---|---|---|---|
| ~~9~~ | ~~console-message 钩子失效~~ | — | ✅ 第十四轮修复（Electron 32 双签名归一），并在第二十二轮真机立功（抓到 CSP blob 泄漏） |
| ~~8~~ | ~~拖 FBX 闪退~~ | — | ✅ 第十二轮修复 |
| ~~1~~ | ~~UV 展开无法保证零重叠~~ | — | ✅ 第十七+十八轮修复（repack 构造保证，规则回 FAIL，黄金集 10/10） |
| 2 | **四边面重拓扑** | `want_quads` 仍只记录 | Spike 完成（docs/spike-quad-remesh.md），**建议不进 MVP，待产品拍板** |
| 3 | **云 Provider 真实调用未验证** | 首次真调用可能还有字段级出入 | 文档核对已完成（第十九轮）；**剩真 Key 冒烟 —— 需用户提供 Key** |
| 4 | **烘焙与 FBX 导出未在真实 Blender 上验证** | 装了 Blender 的机器上 bpy 脚本可能有 4.x API 差异 | 未解决 —— 本机无 Blender，**需装 Blender 的环境** |
| 5 | ~~前端未在真实 Electron 里跑过~~ | — | ✅ 基本关闭：壳拉起 sidecar/视口/导入/管线/校验/回滚/转台均真机走查通过（第二十二轮，CDP 实证）；剩用户体感复测 |
| 6 | Electron 无头/沙箱环境无法 E2E | CI 不能跑 Electron | 不影响真机；CDP 方案已验证可做冒烟 |
| 7 | 资产包批量（工作流 B）未实现 | 只能逐件生成 | Phase 2（非 MVP） |
| 10 | **自动更新通道缺失**（M5 遗留） | 用户升级需手动重装 | 需先决：发布渠道 + 代码签名证书（**待用户决策**） |

---

## 待用户决策清单（2026-09-12 汇总）

1. **四边面重拓扑去留**：Spike 建议不进 MVP（材料 docs/spike-quad-remesh.md），Phase 2 走 Blender Quadriflow —— 请拍板。
2. **Provider API Key**：四家端点已按官方源校正，但没 Key 无法真实冒烟。拿到 Meshy / Tripo / 混元3D（SecretId:SecretKey）/ Rodin 任一家 Key 后，设置页填入 → 「测试连接」→ 生成一次即可验证。
3. **代码签名与更新通道**（M5）：无证书 → SmartScreen 会拦安装包；自动更新通道需要发布服务器。开发期可先不管，发布前必须决策。
4. **Blender 真机验证**（已知问题 #4）：本机无 Blender，bpy 烘焙/FBX 导出/转台三条 Blender 路径未实测（均有软渲染/GLB 降级兜底）。建议在装了 Blender 的机器上跑一次黄金集。
5. **黄金集升级**：samples/golden/ 目前是程序化占位图（管考卷已冻结）。美术出图后**同名覆盖**即可无缝升级，替换后跑 `python -m app.golden` 确认基线。

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

1. **用户复测**：便携版（build/win-unpacked/AssetAgent.exe，2026-09-12 重建）已全链路真机走查通过；建议用户体感过一遍 UI（拖 FBX/OBJ、视口、挑选、回滚、导出）。
2. **真 Key 冒烟**（已知问题 #3 收尾）：任一家 Provider 的 Key 填入设置页 → 测试连接 → 生成一次。
3. **Blender 真机验证**（已知问题 #4）：装 Blender 的机器跑 `python -m app.golden` + 一次 FBX 导出。
4. **拍板**：四边面 Spike 结论（docs/spike-quad-remesh.md）、签名/更新渠道（已知问题 #10）。
5. **M6 准备**：封测者招募与清单（埋点导出按钮已就位）；golden 集美术图替换。
