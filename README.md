# comfy-mcp

> 让 Agent 安全、可复现地驱动 ComfyUI。

![status](https://img.shields.io/badge/status-v0.1--dev-orange)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![tests](https://img.shields.io/badge/tests-76%20passed-brightgreen)

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
| D3 | 6 个工具 + 进度映射 | ⏳ 下一步 |
| D4 | 交付、manifest、幂等与恢复 | ⏳ |
| D5 | 真机联调（本机 4060 已连通） | ⏳ |
| D6 | 发布（PyPI + MCP Registry） | ⏳ |

每个里程碑的「做了什么 / 踩了什么坑 / 怎么验证」都在 [iterations/](iterations/) 里：D1 见 [iterations/D1-骨架与地基.md](iterations/D1-骨架与地基.md)，D2 见 [iterations/D2-图转换与注册表.md](iterations/D2-图转换与注册表.md)。

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
- **进度**：`executing` / `progress` / `executed` 三类 ws 事件加权成 0–100，断线自动降级轮询
- **可控执行**：工具级 deadline，超时先 `/interrupt` 再 `/queue delete`，返回可重试的结构化错误
- **幂等与恢复**：`idempotency_key` 去重；job 状态落盘，重启后仍可查
- **上下文友好**：5 张图的任务，上下文增量 < 20KB
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
| `COMFY_MCP_RUNS` | `runs` | manifest 与输出落盘目录 |
| `COMFY_MCP_DEFAULT_TIMEOUT_S` | `300` | 单次运行的默认超时 |
| `COMFY_MCP_PREVIEW_MAX_EDGE` | `512` | 首图缩略的最长边 |
| `COMFY_MCP_TRANSPORT` | `stdio` | `stdio` 或 `http`（streamable-http） |
| `COMFY_MCP_HTTP_PORT` | `8848` | `http` 传输的监听端口 |
| `COMFY_MCP_MAX_UPLOAD_MB` | `20` | 上传素材上限 |

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
| `comfy_list_workflows` | 列出工作流及其参数 schema（给 Agent 看的说明书） | 否 | D3（注册表已就绪） |
| `comfy_validate` | 校验节点 / 模型是否齐备，不花 GPU | 否 | D3（校验逻辑已就绪） |
| `comfy_run` | 跑一次工作流，带进度，可设超时 | 是 | D3 |
| `comfy_job` | 查任务状态（重启后仍可用） | 否 | D3 |
| `comfy_cancel` | 取消任务并清队列 | 否 | D3 |

统一错误结构（9 个码）：`INVALID_PARAM` `UNKNOWN_WORKFLOW` `NODE_MISSING` `MODEL_MISSING` `QUEUE_FULL` `TIMEOUT` `CUDA_OOM` `INSTANCE_DOWN` `CANCELED`，返回
`{code, message, hint, retryable, job_id}`，`hint` 用来告诉模型下一步怎么改。

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
        └── manifest.py   运行记录落盘 runs/<job_id>.json
        ▼
 ComfyUI（本机 8188 / 远端 / 多实例）
```

当前仓库已有 `config.py` / `client.py` / `server.py` / `graph.py` / `registry.py`，`jobs.py` / `manifest.py` 随 D3、D4 补上。

## 设计取舍

1. **不做通用 graph 执行器。** ComfyUI 自定义节点等于任意代码执行，把「任意 graph」接口暴露给模型是安全问题。注册表 + 白名单把它收敛成受限接口，代价是加工作流要写一个 yaml —— 这正是「生产可用」的含义。
2. **输出优先 ResourceLink。** 一张 1024×1024 PNG 约 1.5MB，base64 后 2MB。五张图直接把上下文吃光。首图缩略给模型「看」，其余给链接按需读。
3. **不基于 comfy-cli。** 少一层 shell 与文本解析，直接说 HTTP + websocket；也因此能同时服务本机与远端多个实例。
4. **job 表放内存 + manifest 落盘。** 不引 Redis：单用户开发场景，重启恢复靠扫 `runs/` 目录即可。
5. **依赖 `httpx2` 而不是 `httpx`。** MCP Python SDK 2.x 自带 `httpx2`（import 名就是 `httpx2`），复用它省一层版本冲突。

## 开发与测试

```bash
pytest                      # 76 个用例，全离线，不需要 GPU
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
