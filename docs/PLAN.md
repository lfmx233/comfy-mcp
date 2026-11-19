# comfy-mcp 详细方案（v0.1）

> 本文是可执行设计文档：每个模块做什么、每个工具长什么样、怎么测、几天做完、做完怎么验收。
> 状态：D1、D2、D3 已完成。修改本文请同步 README 的路线图。

---

## 1. 背景与竞品

Comfy Org 官方 [comfy-mcp](https://github.com/Comfy-Org/comfy-mcp)：Python，stdio，41 个工具，基于 comfy-cli（每条工具 shell 调 `comfy --where local --json` 再解析 envelope），默认打 `127.0.0.1:8188`，`COMFYUI_URL` 可指远程，2026-07 创建、11 月仍活跃，AGPL-3.0-or-later OR Commercial。

社区另有若干实现（SSE / Node 版 / Comfy Cloud 托管端点）。

**结论**：功能广度上正面撞车没有意义。差异化必须落在「生产可用性」：可复现、可观测、安全边界、上下文预算、多实例。

## 2. 目标与非目标

**目标（v0.1）**

| 编号 | 目标 | 判定方式 |
|---|---|---|
| G1 | Agent 能发现并正确选择工作流 | 3 个真实工作流，模型的工具选择正确率 ≥ 9/10 |
| G2 | 每次运行可复现 | manifest 含 workflow_hash + 解析后 seed + 模型文件，用同 manifest 重跑得到同一图 |
| G3 | 长任务不黑盒 | 60s 任务全程 progress 单调递增，客户端能画出进度 |
| G4 | 失败可控 | 超时 / 节点缺失 / 模型缺失 / OOM 四类错误都有明确错误码与建议 |
| G5 | 上下文友好 | 5 图任务结构化返回 < 20KB，不触发客户端截断 |
| G6 | 可离线验证 | 无 GPU 机器 `pytest` 全绿，CI 覆盖 3.11 / 3.12 |

**非目标**：节点/模型管理、前端、模型下载、覆盖全部 API、Comfy Cloud 专有端点。

## 3. 使用场景

**S1 一句话出图（Agent 主导）**
用户：「用动漫风格画一张雨夜霓虹街道，16:9，1024 宽」→ Agent 调 `comfy_list_workflows` 选 `txt2img_anime` → 填 prompt/width/height → `comfy_run` → 拿到首图 + manifest。

**S2 批量重跑（可复现）**
上次某张图不错，Agent 读 manifest 拿 seed，改 prompt 后重跑对比；同一 `idempotency_key` 重复调用不会重复烧 GPU。

**S3 失败定位**
GPU 显存不足 → 返回 `CUDA_OOM` + 建议降分辨率 / 换小模型 + 当前实例已加载模型列表，Agent 能自己改参数重试而不是把错误原样抛给用户。

## 4. 架构

### 4.1 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `server.py` | 定义 6 个工具，参数校验，调下层，组装返回 | 不写业务逻辑 |
| `registry.py` | 扫 `workflows/`，加载 json+yaml，启动时全量校验，算 workflow_hash | 不转换图（交给 graph） |
| `graph.py` | UI 导出 → API 格式；处理 widgets_values / Reroute / Primitive / group | 不改语义，只做结构映射 |
| `jobs.py` | job 状态机（queued→running→succeeded/failed/canceled/timeout）、进度加权、幂等表 | 不碰 HTTP |
| `client.py` | HTTP（/prompt /history /view /queue /interrupt /object_info /system_stats）+ ws 订阅 | 不判断业务成功与否 |
| `manifest.py` | 生成并落盘 `runs/<job_id>.json`，供 `comfy_job` 与重启恢复 | — |
| `resources.py` | `comfy://output/<prompt_id>/<n>` 资源读取 | — |
| `errors.py` | 错误码枚举 + 从 ComfyUI 报错映射到错误码 | — |

### 4.2 一次 `comfy_run` 的时序

```text
Agent                server                     ComfyUI
  │ comfy_run(wf,params) │                          │
  │─────────────────────>│ 1 参数白名单校验          │
  │                      │ 2 用 registry 里的 API 图 │
  │                      │   注入参数、解析 seed      │
  │                      │ 3 幂等键命中？→ 直接返回   │
  │                      │ 4 ws 连接(?clientId=X)   │
  │                      │─────────────────────────>│
  │                      │ 5 POST /prompt client_id=X│
  │                      │<─────────────────────────│ prompt_id
  │  progress 10%        │ 6 ws: execution_start     │
  │<─────────────────────│    executing / progress   │
  │  progress 55%        │                           │
  │<─────────────────────│    executed               │
  │                      │ 7 GET /history/<pid>      │
  │                      │ 8 GET /view 取首图缩略     │
  │  result + manifest   │ 9 写 runs/<job_id>.json   │
  │<─────────────────────│                           │
```

## 5. 数据契约

### 5.1 WorkflowInfo

```json
{
  "name": "txt2img_anime",
  "description": "动漫风格文生图，适合 1024 以内的竖图/方图",
  "tags": ["image", "anime"],
  "graph_hash": "sha256:9f2c...",
  "params": {
    "prompt": {"type": "string", "required": true, "max_len": 800,
               "description": "正向提示词，英文逗号分隔的 tag 更稳定"},
    "seed":   {"type": "integer", "default": -1, "minimum": 0, "maximum": 4294967295,
               "description": "-1 表示随机，运行结果里会记录解析后的真实值"},
    "width":  {"type": "integer", "enum": [512, 768, 1024], "default": 1024}
  }
}
```

### 5.2 RunResult

```json
{
  "job_id": "job_01JC8Z...",
  "status": "succeeded",
  "elapsed_s": 42.7,
  "manifest": {
    "workflow": "txt2img_anime",
    "workflow_hash": "sha256:9f2c...",
    "params": {"prompt": "rainy neon street, anime", "seed": 1234567, "width": 1024},
    "models": [{"node": "4", "name": "sd_xl_base_1.0.safetensors", "sha256": "e3b0..."}],
    "instance": "local-3090",
    "started_at": "2026-11-16T22:10:03+08:00"
  },
  "outputs": [
    {"index": 0, "kind": "image", "width": 1024, "height": 1024,
     "preview": "<inline ImageContent>", "resource": "comfy://output/<prompt_id>/0"},
    {"index": 1, "kind": "image", "width": 1024, "height": 1024,
     "resource": "comfy://output/<prompt_id>/1"}
  ],
  "warnings": []
}
```

### 5.3 错误码

| 码 | 触发 | retryable | Agent 该怎么办 |
|---|---|---|---|
| `INVALID_PARAM` | 白名单外路径 / 类型 / 范围不合法 | 否 | 读 message 里的 schema 改参数 |
| `UNKNOWN_WORKFLOW` | 名字不在 registry | 否 | 先 `comfy_list_workflows` |
| `NODE_MISSING` | /object_info 里没有该节点 | 否 | 换工作流或提示用户装节点 |
| `MODEL_MISSING` | 模型文件不在实例上 | 否 | 换实例或换模型 |
| `QUEUE_FULL` | 队列超限 | 是 | 退避重试 |
| `TIMEOUT` | 超过 timeout_s | 是 | 加超时或降分辨率 |
| `CUDA_OOM` | 显存不足 | 是 | 降分辨率 / 换小模型 |
| `INSTANCE_DOWN` | 连不上 | 是 | 换实例 |
| `CANCELED` | 用户取消 | 否 | — |
| `EXECUTION_FAILED` | 执行期报错但既不是 OOM 也不是已知类别（D3 新增） | 否 | 读 message 里的节点与异常，改图或改参数重跑 |

统一返回形状：`{"code": "...", "message": "给人看的", "hint": "给模型看的下一步", "retryable": bool, "job_id": ...}`

## 6. 工具规格

### 6.1 `comfy_status`
入参：无。返回 `{instances: [{name, url, online, queue_len, vram_used_gb, models_loaded}], workflows: 3}`。
用途：Agent 开工前自检，避免对着离线实例瞎试。

### 6.2 `comfy_list_workflows`
入参：`tag: str | None`。返回 `list[WorkflowInfo]`（见 5.1）。
关键：`description` 与参数 `description` 是写给模型看的，必须说明「什么时候用」，而不只是「是什么」。

### 6.3 `comfy_validate`
入参：`workflow: str, params: dict`。返回 `{ok: bool, missing_nodes: [], missing_models: [], param_errors: []}`。
实现：转换后的图里收集 `class_type` 与模型文件名 → 比对 `/object_info` 与 `/models`。**不提交 /prompt**，零 GPU 成本。

### 6.4 `comfy_run`
入参：`workflow`、`params`、`timeout_s=300`、`idempotency_key=None`、`preview=True`。
返回 `RunResult`。
错误：见错误码表。超时路径：`/interrupt` → `/queue delete` → 返回 `TIMEOUT` 且带 `job_id`（可用 `comfy_job` 复查）。

### 6.5 `comfy_job`
入参：`job_id`。返回 `{job_id, status, progress, started_at, elapsed_s, outputs?}`。数据源：内存表优先，miss 则读 `runs/`。

### 6.6 `comfy_cancel`
入参：`job_id`。返回 `{canceled: bool, queue_removed: bool}`。已完成的 job 返回 `canceled: false` 并说明原因。

## 7. 关键实现细节

### 7.1 UI 格式 → API 格式

浏览器导出的 `nodes/links` 与 `/prompt` 需要的 `{id: {class_type, inputs}}` 不是一回事。转换步骤：

1. `nodes[]` 建索引 `{id: node}`；`links[]` 建 `(link_id → [src_node, src_slot])`。
2. 每个节点取 `class_type = node.type`，`inputs` 先放 `_meta: {title}`。
3. `inputs` 里的连接项（`[src_node, src_slot]`）→ `[String(src_node), src_slot]`。
4. `widgets_values` 按 `/object_info` 里该节点的 `input.required` 顺序回填到非连线输入。
5. Reroute / PrimitiveNode 穿透：递归取上游真实来源。
6. 丢弃仅 UI 用的键（`pos`、`size`、`flags`、`order`、`mode`）。

**只做一次**：加载时转换，结果 + `sha256` 写入 registry。后续运行都用这份，保证 `workflow_hash` 恒定。

### 7.2 参数注入

sidecar 的 `path` 形如 `"3.inputs.seed"`，注入时按路径写值；路径不存在 → 启动时报错（配错立刻崩）。`seed == -1` 时生成随机值并写回，manifest 记录真实 seed。

### 7.3 进度映射

- ws 连接必须带 `?clientId=<X>`，且与 `POST /prompt` 的 `client_id` 相同，否则收不到事件。
- 权重：总进度 = (已完成节点数 + 当前节点内 `value/max`) / 总节点数 × 100。
- 事件源有三代格式，0.3.x 实际会同时发：老的单节点 `progress`（data.value/max）、新的 `progress_state`（data.nodes 是**全量**节点状态字典）、以及 `execution_cached`（命中缓存，直接算完成）。D3 实测发现只处理 `progress` 时中间进度全是 0，补上 `progress_state` 后才有 50 → 62.5 → 65.6 → 68.8 → 71.9 → 75 → 87.5 → 99 → 100 这样的细粒度。
- `report_progress` 必须单调递增；取 `max(上次, 本次)`，且成功前封顶 99（避免 Agent 把 99.9 当完成）。
- 心跳：15s 没有事件 → 探一次 `GET /history/<pid>`；ws 断开 → 切 1s 轮询 `/history`，直到完成或超时。

### 7.4 超时与取消

```python
async with asyncio.timeout(timeout_s):
    await wait_for_completion(pid)
# on TimeoutError:
await client.interrupt()  # 打断当前执行
await client.queue_delete([pid])  # 清排队
return error("TIMEOUT", retryable=True, job_id=job_id)
```

**不要**在超时后直接抛异常：Agent 拿不到 job_id 就无法判断是否重试。

### 7.5 输出交付

- 第 0 张：`GET /view?filename=...&subfolder=...&type=output` → 缩到最长边 512 → `Image(data=..., format="png")`。
- 其余：`ResourceLink(uri="comfy://output/<pid>/<n>", name=..., mimeType="image/png")`，`resources.py` 里按需读原图。
- 阈值：单图 > 1MB 或总图数 > 3 时，第 0 张也退化为链接（可在 `preview` 参数里覆盖）。

### 7.6 幂等与恢复

`idempotency_key = sha256(workflow_hash + json.dumps(params, sort_keys=True))`（seed 为 -1 时先解析成具体值再算）。
10 分钟窗口内同 key → 直接返回已有 job。job 状态每次变更写 `runs/<job_id>.json`；启动时扫描目录重建内存表（只恢复最近 100 条）。

## 8. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `COMFY_MCP_INSTANCES` | `./instances.yaml` | 多实例配置 |
| `COMFY_MCP_WORKFLOWS` | `./workflows` | 注册表目录 |
| `COMFY_MCP_RUNS` | `./runs` | manifest 落盘目录 |
| `COMFY_MCP_DEFAULT_TIMEOUT_S` | `300` | 默认工具超时 |
| `COMFY_MCP_PREVIEW_MAX_EDGE` | `512` | 缩略图最长边 |
| `COMFY_MCP_TRANSPORT` | `stdio` | `stdio` 或 `http` |
| `COMFY_MCP_HTTP_PORT` | `8848` | http 传输端口 |
| `COMFY_MCP_MAX_UPLOAD_MB` | `20` | 输入图上限 |

## 9. 测试计划

### 9.1 分层

| 层 | 手段 | 是否需要 GPU |
|---|---|---|
| 单元 | graph 转换、参数校验、错误映射 | 否 |
| 契约 | 用 `mcp.Client` 连内存 server，断言 schema 与 structured_content | 否 |
| 集成 | fake ComfyUI（Starlette）+ 录制 ws 帧回放 | 否 |
| 端到端 | 真实 ComfyUI 实例 | 是（仅 D5 手动） |

### 9.2 用例清单（目标 40+）

- graph：3 个真实工作流转换正确、哈希稳定、Reroute 穿透、group 展开、缺 object_info 时报错
- registry：sidecar 路径不存在报错、未知参数被拒、enum 生效、重复 name 报错
- 参数：超范围、类型错、未声明路径、seed=-1 解析
- 进度：3 类事件加权、乱序事件、重复值被过滤、ws 断开降级轮询
- 超时：调用 interrupt 与 queue delete、返回带 job_id 的 TIMEOUT
- 幂等：同 key 两次只提交一次；不同 params 不命中
- 恢复：写盘后重建内存表、`comfy_job` 能读历史
- 输出：5 图任务的 structured_content 体积 < 20KB
- 契约：每个工具 inputSchema/outputSchema 字段名与文档一致

### 9.3 fake ComfyUI

`tests/fake_comfy/app.py`：Starlette 应用，实现 `/prompt`（返回固定 prompt_id 并按剧本推 ws 帧）、`/history`、`/view`（返回固定 PNG）、`/object_info`（真实导出精简快照）、`/queue`、`/interrupt`。
`tests/fixtures/ws_frames.jsonl`：真机录制的帧，测试里按 `t_ms` 回放。

## 10. 里程碑

### D1 骨架与地基 ✅（2026-11-19 完成）
产出：仓库、`pyproject.toml`、`config.py`、`client.py`（HTTP + ws）、`server.py`（`comfy_status`）、`tests/fake_comfy/app.py`。
验收：`pytest` 绿（22 passed）；fake 服务器能被 curl 通；`comfy_status` 能对 fake 返回结构化数据；同一份代码对真机 127.0.0.1:8188（ComfyUI 0.3.60 / RTX 4060）同样返回 structuredContent。
备注：`httpx` 未安装，SDK 依赖的是 `httpx2`（import 名 `httpx2`）；pytest 需 `pythonpath = ["src", "."]` 才能 import `tests.fake_comfy`。

### D2 图转换与注册表 ✅（2026-11-19 完成）
产出：`graph.py`（UI 导出 → API prompt、校验、稳定哈希）、`registry.py`（sidecar 参数白名单）、`workflows/{txt2img_anime,img2img,sdxl_txt2img}.{json,yaml}`、`tests/test_graph.py`（22）、`tests/test_registry.py`（32）、`scripts/verify_d2.py`。
验收：76 passed；`ruff check` / `ruff format --check` 全绿；3 个工作流对真机 1810 个节点类校验零 issue，且都被 `/prompt` 接受（`5fa6621d…`、`8138b41e…`、`1a835cd2…`，随即 `/queue delete` + `/interrupt`，队列回到 0+0）；哈希 = `sha256:` + 规范化 JSON 的 sha256，重载与节点顺序无关。
备注（D2 的两个真实教训）：
1. UI 的 `widgets_values` 比 API 的 widget 槽多出「隐藏前端控件」（seed 的 `control_after_generate`、`dynamicPrompts`、LoadImage 的上传按钮、CLIPTextEncodeSDXL 的两个 toggle 且都排在末尾）。按 flagged 输入贪心跳值会在 SDXL/自定义节点上错位，改为**按值类型对齐的 DP 搜索**（宁可显式失败也不静默错位）。
2. 模型缺失必须在 `validate_prompt` 报 `MODEL_MISSING`，不能在转换期报 `GRAPH_WIDGET_MISMATCH`——否则「合法的缺模型」和「自定义节点顺序不同」混成一个错。

### D3 工具与进度 ✅（2026-11-19 完成）
产出：`server.py` 6 个工具（`comfy_status`/`list_workflows`/`validate`/`run`/`job`/`cancel`）、`jobs.py`（状态机 + 进度加权 + 幂等表）、`errors.py`（10 码 + 异常映射）、`client.py` 的 `event_stream`、fake ComfyUI 的真实执行模拟（ws 帧 + OOM + 普通异常 + 可中断）、`tests/fixtures/ws_frames_real.json`（真机抓帧 25 帧）、`tests/fixtures/registry*`、`scripts/verify_d3.py`。
验收：103 passed（原 76 + 27）；fake 上 `run → 至少 3 次 progress → result`；六个错误码路径都有用例（"INVALID_PARAM"、"UNKNOWN_WORKFLOW"、"NODE_MISSING"、"MODEL_MISSING" 走预检，"QUEUE_FULL"、"TIMEOUT"、"CUDA_OOM"、"EXECUTION_FAILED"、"INSTANCE_DOWN"、"CANCELED" 走执行）；真机 4060 上 `verify_d3.py` 全绿：一次真实出图 10.8s、同参数第二次 `reused=True`、强制超时返回 `TIMEOUT` + job_id 且队列被 interrupt 清空、ws 进度 27 帧单调到 100。
备注（D3 的三个真实教训）：
1. 未捕获异常会被 MCP SDK 包成 `UnexpectedToolError: Error executing tool comfy_run`，栈信息全丢。每个工具必须过一层 `@_guarded`，把领域错误翻成 5 字段信封。
2. 只处理 `progress` 帧会得到「0, 0, 0, 99」——0.3.60 真正描述进度的是 `progress_state`。
3. `report_progress` 在没有 request context 时（例如直接 `call_tool` 测试）会抛 `ValueError: Context is not available outside of a request`，上报必须 try/except，不能让进度失败连累任务。

### D4 交付与恢复
产出：`resources.py`、`manifest.py`、幂等、重启恢复。
验收：杀进程重启后 `comfy_job` 仍可查；5 图任务上下文 < 20KB。

### D5 真机联调
产出：接一台真实 ComfyUI，跑 3 个工作流，录 demo。
验收：README 命令原样可复现；progress 与真实执行同步。

### D6 发布
产出：README 定稿（含对比表、Non-goals）、demo gif、PyPI 包、`server.json` 上架 MCP Registry。
验收：`uvx comfy-mcp` 一条命令能起；registry 页面可搜到。

## 11. 验收清单

- [ ] 6 个工具在 Claude Desktop / Cursor 可见，模型能正确选中
- [ ] 非法参数在提交前失败，错误含 `hint` 告诉模型怎么改
- [ ] 真实工作流跑通，progress 单调递增，ws 断开后仍出结果
- [ ] 超时后 `/queue` 无残留
- [ ] 同 idempotency_key 两次调用只跑一次
- [ ] manifest 含 workflow_hash / 解析后 seed / 模型文件 / 耗时
- [ ] 5 图任务上下文增量 < 20KB
- [ ] 无 GPU 环境 `pytest` 全绿，CI 3.11 / 3.12 通过
- [ ] README 一条命令可复现 demo

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 官方项目持续迭代 | 卖点被追平 | 守住「可复现 + 安全边界 + 上下文预算」，README 明写对比，别装看不见 |
| 没有真机 | D5 卡死 | 云 GPU 按小时（AutoDL / RunPod）；本机 6G 显存可跑 SD1.5 |
| 图转换边界多 | 工作流跑不起来 | 只支持 3 个已转换验证的工作流，其余明确报「不支持」 |
| 范围蔓延 | 6 天做不完 | 冻结工具数量为 6；新需求进 v0.2 |
| Windows 路径 / 编码 | 本地开发踩坑 | CI 跑 Linux，本地用 pathlib 统一处理 |

## 13. 面试讲法与预设问答

**30 秒版本**：官方 comfy-mcp 解决了「能不能调」，我解决「敢不敢在生产里调」——把任意 graph 执行收敛成注册表 + 参数白名单，每次运行留可复现 manifest，长任务用 ws 事件做进度和超时，输出图给链接不塞上下文。

**预设问题**

1. *为什么不直接用官方的？* 它 41 个工具覆盖面更广，但没有 manifest、没有参数白名单、输出直接进上下文；我的场景是「Agent 自己决定要不要重跑」，缺的是可复现与可控失败。
2. *为什么不用 comfy-cli？* 多一层 shell 与文本解析，且无法同时管多实例；直接 HTTP + ws 延迟更低、错误更结构化。
3. *怎么保证可复现？* 工作流转换后固定 hash、seed 解析成具体值、记录模型文件名与 sha256；同 manifest 重跑得到同一图（D5 会实测）。
4. *进度怎么算的？* 节点数加权 + 节点内 value/max，单调递增；ws 断开降级成 /history 轮询。
5. *超时后 GPU 真的停了吗？* interrupt 打断执行、queue delete 清排队，测试里断言 /queue 为空。
6. *为什么不做任意 graph？* 自定义节点等于任意代码执行，模型能构造 graph 就是 RCE 面；白名单把攻击面收敛到已知工作流。
7. *大图怎么处理？* 首图缩到 512 给 ImageContent，其余 ResourceLink，客户端按需取；5 图任务 < 20KB。
8. *没 GPU 怎么测？* fake ComfyUI 实现 6 个端点 + 录制 ws 帧回放，CI 全绿。

## 14. 开放问题

- [ ] 是否需要 `comfy_upload_image`（图生图必需），当前排在 v0.2
- [ ] 多实例路由策略：按 `models` 命中优先，还是按负载？倾向命中优先 + 失败降级
- [ ] 是否提供 `--stdio` 之外的 docker 镜像（v0.2 再定）
