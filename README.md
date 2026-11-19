# comfy-mcp

> 让 Agent 安全、可复现地驱动 ComfyUI。

![status](https://img.shields.io/badge/status-v0.1--dev-orange)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![tests](https://img.shields.io/badge/tests-143%20passed-brightgreen)

## 这是什么

一个 [MCP](https://modelcontextprotocol.io) server，把 ComfyUI 的工作流变成 Agent 可以**放心调用**的工具：

- 只跑**注册过**的工作流，只改**声明过**的参数；
- 每次运行留下**可复现**的记录（工作流哈希、解析后的 seed、模型文件、耗时）；
- 长任务有**进度、超时、取消**，不会让 Agent 干等；
- 大图给**链接**而不是 base64，不把上下文撑爆。

一句话：官方把 ComfyUI 暴露给了 Agent，本项目让 Agent **敢**用它。

## 进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| D1 | 配置 / HTTP+ws 客户端 / `comfy_status` / 假 ComfyUI / 22 个测试 | ✅ 2026-11-19 |
| D2 | UI→API 图转换 + 工作流注册表（3 个真机工作流） | ✅ 2026-11-19 |
| D3 | 6 个工具 + 进度映射 + 10 个错误码 + 假服务器真执行 | ✅ 2026-11-19 |
| D4 | 交付、manifest 落盘、重启恢复、模型 sha256、首图缩略与资源链接 | ✅ 2026-11-19 |
| D5 | 真机联调与端到端 demo（本机 4060 已连通） | ⏳ 下一步 |
| D6 | 发布（PyPI + MCP Registry） | ⏳ |

每个里程碑的「做了什么 / 踩了什么坑 / 怎么验证」都在 [iterations/](iterations/) 里：D1 见 [iterations/D1-骨架与地基.md](iterations/D1-骨架与地基.md)，D2 见 [iterations/D2-图转换与注册表.md](iterations/D2-图转换与注册表.md)，D3 见 [iterations/D3-工具与进度.md](iterations/D3-工具与进度.md)，D4 见 [iterations/D4-交付与恢复.md](iterations/D4-交付与恢复.md)。

## 为什么不是「又一个 ComfyUI MCP」

Comfy Org 官方已有 [comfy-mcp](https://github.com/Comfy-Org/comfy-mcp)（41 个工具，基于 comfy-cli，仍在活跃更新）。
它解决了「能不能调」，没有解决「敢不敢在生产里调」。五个坑，就是本项目的全部卖点：

| 坑 | 官方做法 | 本项目 |
|---|---|---|
| 结果不可复现 | 直接返回输出文件 | 每次运行写 manifest：工作流哈希 / 解析后 seed / 模型文件 / 耗时 / 实例 |
| 长任务黑盒 | wait / watch 轮询 | ComfyUI websocket 事件映射 MCP progress；超时自动 interrupt 并清队列 |
| 任意 graph 可执行 | 接受任意工作流 JSON | 只跑 registry + 参数白名单，非法参数在 GPU 排队前失败 |
| 大图灌上下文 | 返回输出文件 | 首图缩略 ImageContent，其余 ResourceLink |
| 多实例盲打 | 单个 COMFYUI_URL | instances.yaml，按模型驻留选实例，失败降级 |

## 特性

- **6 个工具**，全部类型化：入参由 JSON Schema 校验，出参走 `outputSchema` + `structuredContent`（MCP 2026-07-28 规范）
- **工作流注册表**：`workflows/<name>.json` + `<name>.yaml`，参数白名单声明路径、类型、范围、枚举
- **进度**：`execution_cached` / `progress_state` / `progress` / `executed` 四类 ws 事件加权成 0–100（单调不回退），断线自动降级轮询
- **可控执行**：工具级 deadline，超时先 `/interrupt` 再 `/queue delete`，返回可重试的结构化错误
- **幂等与恢复**：`idempotency_key` 去重；job 状态落盘 `runs/<job_id>.json`，重启后仍可查，重放请求照样返回图片
- **可复现**：manifest 记下工作流哈希、解析后的参数、模型文件名与 **sha256**（按 mtime+size 缓存，不重复算）
- **上下文友好**：5 张图的任务，上下文增量 < 20KB；首图缩略按「几何」和「体积」两条规则重编码
- **无 GPU 可测**：fake ComfyUI 覆盖全部 HTTP 端点，CI 不需要显卡

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+ | 用到 `asyncio.timeout`、`X \| None`、`dataclass(slots=True)` |
| ComfyUI | 0.3.x | 只依赖 `/system_stats` `/prompt` `/history` `/view` `/object_info` `/queue` `/interrupt` `/ws`，这些都是稳定端点 |
| GPU | 非必需 | 仅真机联调与 demo 需要；单元测试全离线 |

MCP server 与 ComfyUI **进程隔离**：用独立的 venv，通过 HTTP 连 ComfyUI，绝不往整合包的 Python 里装东西。

## 安装

```bash
git clone https://github.com/lfmx233/comfy-mcp.git
cd comfy-mcp
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install ".[dev]"
```

国内网络加镜像会快很多：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ".[dev]"
```

> **不建议用 `pip install -e`。** 仓库路径含中文时（例如 `D:\工作\个人项目\comfy-mcp`），editable 安装写出的 `.pth` 是 UTF-8 内容，CPython 的 `site` 模块却按 GBK 解码，结果是**整个解释器起不来**：
>
> ```text
> Fatal Python error: init_import_site: Failed to import the site module
> UnicodeDecodeError: 'gbk' codec can't decode byte 0xaa in position 12: illegal multibyte sequence
> ```
>
> 此时连 `python -c "print(1)"` 都失败，`PYTHONUTF8=1` 也没用。修法是删掉 `site-packages\_editable_impl_*.pth`，改用上面的非 editable 安装。纯英文路径下 `-e` 正常，但没必要为它冒险。

验证：

```bash
comfy-mcp --version     # comfy-mcp 0.1.0.dev0
comfy-mcp --check       # 逐个实例打印版本 / 队列 / 显存，连不上返回码 1
```

> `--check` 只是给人用的便利命令，Agent 用 `comfy_status` 拿同一份数据。

## 配置

全部通过环境变量，没有配置文件语法要学。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COMFY_MCP_INSTANCES` | `instances.yaml` | 实例清单；文件不存在时回落单实例 |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | 仅在 `instances.yaml` 不存在时生效 |
| `COMFY_MCP_WORKFLOWS` | `workflows` | 工作流注册表目录 |
| `COMFY_MCP_RUNS` | `runs` | 运行记录落盘目录（`<job_id>.json` + `checksums.json`） |
| `COMFY_MCP_MODELS_DIR` | 空 | ComfyUI 的 `models/` 目录；设置后 `comfy_run` 会算模型 sha256 |
| `COMFY_MCP_KEEP_RUNS` | `100` | 保留多少条运行记录，启动时按 mtime 删超出的旧记录 |
| `COMFY_MCP_DEFAULT_TIMEOUT_S` | `300` | 单次运行的默认超时 |
| `COMFY_MCP_PREVIEW_MAX_EDGE` | `512` | 首图缩略的最长边（超过就等比缩小） |
| `COMFY_MCP_TRANSPORT` | `stdio` | `stdio` 或 `http`（streamable-http） |
| `COMFY_MCP_HTTP_PORT` | `8848` | `http` 传输的监听端口 |
| `COMFY_MCP_MAX_UPLOAD_MB` | `20` | 上传素材上限 |
| `COMFY_MCP_MAX_QUEUE` | `20` | 队列深度上限，超过直接报 `QUEUE_FULL`，不让请求埋进实例子自己的队列 |

`instances.yaml`（可选，多卡 / 多机时用）：

```yaml
instances:
  - name: local-4060
    url: http://127.0.0.1:8188
    tags: [gpu8g, sd15]
    models: [anything-v5-PrtRE.safetensors]
  - name: remote-3090
    url: https://gpu.example.com
    tags: [gpu24g, sdxl]
    models: [sd_xl_base_1.0.safetensors]
```

- `name` 必须唯一，工具调用时用 `instance` 参数指定；不传则用第一条。
- `url` 支持 `http://` 与 `https://`（自动推导 `ws://` / `wss://`）。
- `tags` / `models` 供 D2 起的「按需选实例」使用，当前只做展示。

## 使用

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "comfy": {
      "command": "C:/工作/个人项目/comfy-mcp/.venv/Scripts/comfy-mcp.exe",
      "env": {
        "COMFY_MCP_INSTANCES": "C:/ai/instances.yaml",
        "COMFY_MCP_WORKFLOWS": "C:/ai/workflows",
        "COMFY_MCP_RUNS": "C:/ai/runs"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json` 用同样的 `mcpServers` 结构即可。

### 命令行直连（调试用）

```bash
# 让 server 以 stdio 跑起来，手工喂 JSON-RPC（或用 MCP Inspector）
python -m comfy_mcp

# 端到端冒烟：起 server、列工具、调 comfy_status
python scripts/smoke_d1.py
```

## 工具一览

| 工具 | 用途 | 阻塞 | 状态 |
|---|---|---|---|
| `comfy_status` | 实例版本、GPU 显存、队列长度 | 否 | ✅ v0.1-dev |
| `comfy_list_workflows` | 列出工作流及其参数 schema（给 Agent 看的说明书） | 否 | ✅ v0.1-dev |
| `comfy_validate` | 校验节点 / 模型是否齐备，不花 GPU | 否 | ✅ v0.1-dev |
| `comfy_run` | 跑一次工作流，带进度，可设超时 | 是 | ✅ v0.1-dev |
| `comfy_job` | 查任务状态（重启后仍可用） | 否 | ✅ v0.1-dev |
| `comfy_cancel` | 取消任务并清队列 | 否 | ✅ v0.1-dev |

统一错误结构（10 个码）：`INVALID_PARAM` `UNKNOWN_WORKFLOW` `NODE_MISSING` `MODEL_MISSING` `QUEUE_FULL` `TIMEOUT` `CUDA_OOM` `INSTANCE_DOWN` `CANCELED` `EXECUTION_FAILED`，返回
`{code, message, hint, retryable, job_id}`，`hint` 用来告诉模型下一步怎么改。

前 9 个是设计阶段定的，`EXECUTION_FAILED` 是 D3 之后加的：ComfyUI 报 `status_str=error` 但异常信息里既不是 OOM 也不是任何已知类别时（例如 `RuntimeError: mat1 and mat2 shapes cannot be multiplied`），必须有个兜底码，否则这类失败会掉进「未知异常」把栈信息糊给模型。它 `retryable: false`——同样的图同样的参数重跑一次还是同样的错，重试只是浪费 GPU。

一次成功调用的样子（`comfy_run` 的返回，节选）：

```json
{
  "job_id": "5dbf44be3d74",
  "prompt_id": "614c2fa6-757f-4781-a022-d0ff1ed4254c",
  "status": "succeeded",
  "elapsed_s": 2.66,
  "manifest": {
    "workflow": "txt2img_anime",
    "workflow_hash": "sha256:6a6f36af52f7…",
    "params": {"prompt": "a cat sitting on a windowsill", "seed": 12345, "steps": 4},
    "models": [{"node": "1", "name": "anything-v5-PrtRE.safetensors",
                "sha256": "7f96a1a9ca9b…"}],
    "instance": "default",
    "started_at": 1795089395.02
  },
  "outputs": [
    {"index": 0, "node": "9", "kind": "image", "filename": "ComfyUI_00001_.png",
     "width": 512, "height": 512, "resource": "comfy://output/614c2fa6-…/0"}
  ],
  "warnings": []
}
```

`width` / `height` 在 D4 之后有值了：`/history` 本身不返回尺寸，所以取图做缩略时顺手从 PNG/JPEG/GIF/WebP/BMP 的文件头解析出来（不依赖 Pillow）。

## 输出、资源与复现（D4）

一次 `comfy_run` 的返回由三类 content 块组成，各管一件事：

| 块 | 内容 | 为什么 |
|---|---|---|
| `text` | 上面的 JSON（job_id / manifest / outputs / error） | 完整事实，可存档、可 diff |
| `image` | 首图缩略，最多一个 | 让模型「看见」结果，不必再去读文件 |
| `resource_link` | 每个输出一个 `comfy://output/<prompt_id>/<index>` | 原图按需读，不撑上下文；模型也可以只挑它要的那张 |

`comfy://output/...` 是 MCP 模板资源，取回来的是 ComfyUI 上的**原始字节**（不是缩略图），所以下游要再加工时不必回头找文件。

**首图为什么要重编码**，两条规则分别解决两种浪费：

1. 几何：长边超过 `COMFY_MCP_PREVIEW_MAX_EDGE`（默认 512）就等比缩小；
2. 体积：文件本身超过 48KB 就重编码 —— 512×512 的二次元 PNG 约 **373KB**，转 JPEG 后 **32KB**，小了 11 倍，而模型看不出差别。

带透明通道的图保持 PNG；重编码反而更大时原样返回；没装 Pillow 时整条逻辑降级为原图，不影响出图。

**运行记录**落在 `runs/<job_id>.json`（原子写：临时文件 + `os.replace`，断电不会留半个 JSON）：

- 记什么：workflow / workflow_hash / instance / status / progress / prompt_id / idempotency_key / 解析后的 params / api 图 / models（含 sha256）/ outputs / error / warnings / 三个时间戳
- 重启恢复：启动时按 mtime 读最近 `COMFY_MCP_KEEP_RUNS`（默认 100）条进内存，`comfy_job` 对旧 job 依然能答；坏文件跳过并记 warning，不炸启动
- 幂等跨重启：idempotency key 一起落盘，重启后同名请求仍然命中复用（并且**依然返回首图**）
- 失败结果不进幂等表：OOM / TIMEOUT / CANCELED 是可重试的，缓存它们等于把错误也缓存了

模型 sha256 不便宜（几个 GB），所以 `runs/checksums.json` 按 `size + mtime_ns` 缓存摘要：文件没动就不再读盘。实测第二次运行不再重算。

## 加一个工作流

两步：把 ComfyUI 里导出的工作流 json 放进 `workflows/`，再写一个**同名** yaml 声明「允许 Agent 改哪些参数」。

```yaml
# workflows/txt2img_anime.yaml —— 文件名必须和 txt2img_anime.json 一致
name: txt2img_anime
description: 二次元文生图，SD1.5 + 风格 LoRA
tags: [image, anime, txt2img]
params:
  prompt:
    type: string
    path: 5.inputs.text          # 节点 id . inputs . 字段名
    required: true
    max_len: 800
  steps:
    type: integer
    path: 6.inputs.steps
    default: 30
    minimum: 1
    maximum: 100
  width:
    type: integer
    paths: [7.inputs.width, 7.inputs.height]   # 一个参数写多个位置
    default: 512
    enum: [512, 640, 768, 896, 1024]
```

要点：

- `path` / `paths` 二选一。路径在**启动时**校验，写错直接报错，不会等到运行时才发现。
- `seed` 的 `default: -1` 表示每次随机：先解析成具体整数，再拿它算幂等键与 manifest。
- 模型文件名（`ckpt_name` 等）也是普通参数；指向本机没有的文件会在校验时报 `MODEL_MISSING`，并提示本机可用的前几个。
- 图可以是 ComfyUI 的普通导出（带前端控件），也可以是 API 格式；两种都吃，前端控件与 API 槽位的对齐是自动的。
- 参数不在 yaml 里声明，Agent 就改不动 —— 这是这个项目「敢在生产里用」的全部来源。

## 架构

```text
MCP host (Claude Desktop / Cursor / 自研 Agent)
        │ stdio 或 streamable-http
        ▼
 comfy_mcp.server  ── 6 个工具（薄层：校验 + 编排）
        │
        ├── registry.py   加载 workflows/*.json + *.yaml，启动时校验
        ├── graph.py      UI 导出格式 → API 格式（一次性转换 + 哈希）
        ├── jobs.py       job 状态机 + 幂等表 + deadline
        ├── client.py     async HTTP + websocket（提交 / 进度 / 取图）
        ├── manifest.py   运行记录落盘 runs/<job_id>.json（原子写、启动恢复、prune）
        ├── hashes.py     模型 sha256 + size/mtime 缓存（checksums.json）
        ├── resources.py  图片魔数嗅探、文件头读尺寸、缩略重编码、ResourceLink
        └── errors.py     10 个错误码 + 异常 → 结构化错误
        ▼
 ComfyUI（本机 8188 / 远端 / 多实例）
```

到 D4 为止 `config.py` / `client.py` / `server.py` / `graph.py` / `registry.py` / `jobs.py` / `manifest.py` / `hashes.py` / `resources.py` / `errors.py` 全部就位。

## 设计取舍

1. **不做通用 graph 执行器。** ComfyUI 自定义节点等于任意代码执行，把「任意 graph」接口暴露给模型是安全问题。注册表 + 白名单把它收敛成受限接口，代价是加工作流要写一个 yaml —— 这正是「生产可用」的含义。
2. **输出优先 ResourceLink。** 一张 1024×1024 PNG 约 1.5MB，base64 后 2MB。五张图直接把上下文吃光。首图缩略给模型「看」，其余给链接按需读。
3. **不基于 comfy-cli。** 少一层 shell 与文本解析，直接说 HTTP + websocket；也因此能同时服务本机与远端多个实例。
4. **job 表放内存 + manifest 落盘。** 不引 Redis：单用户开发场景，重启恢复靠扫 `runs/` 目录即可。
5. **依赖 `httpx2` 而不是 `httpx`。** MCP Python SDK 2.x 自带 `httpx2`（import 名就是 `httpx2`），复用它省一层版本冲突。
6. **尺寸自己解析，不靠 Pillow。** `/history` 不给宽高，而 Pillow 只是「可选加速器」：PNG/JPEG/GIF/WebP/BMP 的头字段手写解析，装不装 Pillow 都能报出尺寸；Pillow 只用来重编码。
7. **工具返回 `CallToolResult` 而不是 dict。** SDK 对注解为 `CallToolResult` 的工具原样透传（不校验、不套 `outputSchema`），所以一条返回里能同时塞文本、图片和资源链接，而 `structuredContent` 仍然是那个 JSON。

## 开发与测试

```bash
pytest                      # 143 个用例，全离线，不需要 GPU
ruff check . && ruff format --check .

# 跑假 ComfyUI（Starlette），再用 curl 或 smoke 脚本验证
python -m uvicorn tests.fake_comfy.app:app --port 8899
curl http://127.0.0.1:8899/system_stats
```

改代码后的两种跑法（非 editable 安装不会自动同步源码）：

```bash
# 1) 直接跑 pytest —— 它按 pyproject.toml 的 pythonpath 读 src/
pytest

# 2) 临时指向源码跑 CLI
# Windows
set PYTHONPATH=src && comfy-mcp --check
# Linux / macOS
PYTHONPATH=src comfy-mcp --check

# 3) 或者重新装一遍
pip install --no-cache-dir --force-reinstall ".[dev]"
```

> 若 pip 报 `[WinError 5] 拒绝访问: '...\\pip\\cache\\wheels\\3a'`，加 `--no-cache-dir`。

接真机：

```bash
# 先起 ComfyUI，例如秋叶整合包
# D:\AIGC\ComfyUI-aki-v2\python\python.exe main.py --listen 127.0.0.1 --port 8188
COMFY_MCP_INSTANCES=nope.yaml COMFYUI_URL=http://127.0.0.1:8188 python scripts/smoke_d1.py

# 校验 + 真实提交（提交后立即取消，不会真的烧 GPU）
python scripts/verify_d2.py --dry-run   # 只校验，不提交
python scripts/verify_d2.py             # 校验 + POST /prompt + 立刻 /interrupt

# D3：真机跑通一次 comfy_run（会真的出图，约 10s）
$env:PYTHONPATH="src"; python scripts/verify_d3.py
# 加 --timeout-check 会再跑一个「必超时」的任务，验证 interrupt + 清队列
$env:PYTHONPATH="src"; python scripts/verify_d3.py --timeout-check

# D4：真机验证 manifest / 尺寸 / 资源 / 缩略 / 重启恢复 / 5 图预算
# 需要 COMFY_MCP_MODELS_DIR 指向 ComfyUI 的 models/ 才会算 sha256
$env:PYTHONPATH="src"; python scripts/verify_d4.py
```

D3 的真机输出（4060 Laptop，SD1.5 512×512 4 步）：

```text
[ok] comfy_status: 0.3.60 queue {'running': 0, 'pending': 0} workflows 3
[ok] comfy_list_workflows: img2img, sdxl_txt2img, txt2img_anime
[ok] comfy_validate: ok=True issues=[]
[ok] comfy_run: 5dbf44be3d74 in 10.8s, 1 output(s)
       first output: comfy://output/2328674e-…/0 size pending D4 kind=image
[ok] comfy_job: status=succeeded progress=100.0
[ok] idempotency: reused=True same_job=True
       frame kinds: {'status': 3, 'execution_start': 1, 'execution_cached': 1,
                     'progress_state': 12, 'executing': 4, 'progress': 4,
                     'executed': 1, 'execution_success': 1}
[ok] ws progress: 27 frames, samples 50.0 → 62.5 → 65.6 → 68.8 → 71.9 → 75.0 → 87.5 → 99.0 → 100.0
VERIFY_D3: PASS
```

D4 的真机输出（同一台 4060，SD1.5 512×512 6 步）：

```text
[ok] comfy_status reaches the instance — http://127.0.0.1:8188 0.3.60
[ok] comfy_run succeeded — 1.91s
[ok] one text block, one preview, one link per output
[ok] manifest carries the workflow hash
[ok] manifest lists the checkpoint — ['anything-v5-PrtRE.safetensors', 'BArtstyleV7-EXP.safetensors']
[ok] model digests were computed — ['7f96a1a9ca9b', '3186527b2b9a']
[ok] output size was read from the image bytes — 512x512
[ok] comfy://output resource returns the original PNG — 373196 bytes, (512, 512)
[ok] the inline preview is smaller than the original — 32416 < 373196 bytes
[ok] a fresh process still answers comfy_job from runs/ — job=a93c70843dd1 status=succeeded
[ok] four outputs fit in the context budget (2127 < 20000 bytes) — 4 outputs, 2127 bytes of JSON
VERIFY_D4: PASS
```

详细设计与验收标准见 [docs/PLAN.md](docs/PLAN.md)。

## Non-goals

- 不做节点 / 模型管理（装节点请用 ComfyUI-Manager）
- 不做前端界面
- 不内置模型下载
- 不追求覆盖全部 ComfyUI API，只覆盖「跑工作流」这条主路径
- 不兼容 Comfy Cloud 的专有端点

## 版本记录

- 迭代记录目录：[iterations/](iterations/)
- 每个里程碑一份：目标 → 产出 → 验收 → 踩坑 → 遗留 → 下一步

## 许可与致谢

MIT。设计灵感来自 [MCP 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) 与 Comfy Org 的 [comfy-mcp](https://github.com/Comfy-Org/comfy-mcp)；
本项目为独立实现，不包含其代码。
