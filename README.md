# comfy-mcp

> 把 ComfyUI 交给 Agent，但别让它乱跑。

![status](https://img.shields.io/badge/status-v0.1--dev-orange)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![tests](https://img.shields.io/badge/tests-144%20passed-brightgreen)

## 这是什么

一个 ComfyUI 的 MCP server。配置好之后，Claude Desktop、Cursor 或者自己写的 Agent 就能跑你注册过的工作流：参数只在你声明的范围内动，跑完留一份能复现的记录。

- 只跑 `workflows/` 里注册过的工作流，模型怎么劝都没用；
- 参数走白名单，yaml 里没声明的字段 Agent 改不动；
- 每次运行写 manifest：工作流哈希、解析后的 seed、模型文件名和 sha256、耗时、用的哪个实例；
- 长任务给进度、给超时、给取消。超时不是干等，是真发 `/interrupt` 再清队列；
- 出图只内联一张缩略图，其余给 `comfy://` 资源链接。base64 灌上下文这种事不做。

官方的 [comfy-mcp](https://github.com/Comfy-Org/comfy-mcp) 已经能把 ComfyUI 调起来了（41 个工具，还在更新）。这边想解决的是另一个问题：敢不敢让一个会自己编参数的模型，去生产环境里跑你的显卡。

## 进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| D1 | 配置 / HTTP+ws 客户端 / `comfy_status` / 假 ComfyUI / 22 个测试 | ✅ 2026-11-19 |
| D2 | UI→API 图转换 + 工作流注册表（3 个真机工作流） | ✅ 2026-11-19 |
| D3 | 6 个工具 + 进度映射 + 10 个错误码 + 假服务器真执行 | ✅ 2026-11-19 |
| D4 | 交付、manifest 落盘、重启恢复、模型 sha256、首图缩略与资源链接 | ✅ 2026-11-19 |
| D5 | 真机端到端 demo：3 个工作流 + 真实进度 + 冷启动复现（本机 4060，全程 57s） | ✅ 2026-11-19 |
| D6 | 可发布产物：PyPI 元数据、sdist + wheel、Registry 清单、CI | ✅ 2026-11-21 |
| D7 | 真发出去：PyPI 上传 + Registry 收录（要凭据，见 [docs/RELEASE.md](docs/RELEASE.md)） | ⏳ 待凭据 |

每个里程碑为什么这么做、踩了哪些坑、怎么验的，都在 [iterations/](iterations/)：
[D1 骨架](iterations/D1-骨架与地基.md) · [D2 图转换](iterations/D2-图转换与注册表.md) · [D3 工具与进度](iterations/D3-工具与进度.md) · [D4 交付与恢复](iterations/D4-交付与恢复.md) · [D5 真机 Demo](iterations/D5-真机Demo.md) · [D6 可发布产物](iterations/D6-发布.md)

## 和官方那个比，差在哪

Comfy Org 的官方实现更全，这点不装看不见：41 个工具、基于 comfy-cli、AGPL 与商业双许可、十一月还在提交。它解决的是「能不能调」。

我关心的是另一组问题，五个，全是踩出来的：

| 问题 | 官方做法 | 这里 |
|---|---|---|
| 结果不可复现 | 直接返回输出文件 | 每次运行写 manifest：工作流哈希 / 解析后 seed / 模型文件 / 耗时 / 实例 |
| 长任务黑盒 | wait / watch 轮询 | websocket 事件映射成 MCP progress；超时自动 interrupt 并清队列 |
| 任意 graph 可执行 | 接受任意工作流 JSON | 只跑 registry + 参数白名单，非法参数在 GPU 排队前就失败 |
| 大图灌上下文 | 返回输出文件 | 首图缩略 ImageContent，其余 ResourceLink |
| 多实例盲打 | 单个 COMFYUI_URL | instances.yaml，按模型驻留挑实例，失败降级 |

一句话概括：官方给的是遥控器，这边想做个装了护栏的遥控器。

## 特性

- 6 个工具，入参由 JSON Schema 校验，出参走 `outputSchema` + `structuredContent`（MCP 2026-07-28 规范）。
- 工作流注册表：`workflows/<name>.json` 配同名 `<name>.yaml`，参数白名单里写清路径、类型、范围、枚举。
- 进度是算出来的，不是编的：`execution_cached` / `progress_state` / `progress` / `executed` 四类 ws 事件加权成 0–100，单调不回退，断线降级成轮询。
- 超时先 `/interrupt`、再 `/queue delete`，返回带 `retryable` 的结构化错误，让模型知道该不该重试。
- `idempotency_key` 去重；job 状态落盘 `runs/<job_id>.json`，重启后照样能查，重放的请求也照样把图还回来。
- manifest 里的模型 sha256 按 `mtime + size` 缓存，不重复读盘。
- 五张图的任务，上下文增量控制在 20KB 以内（实测 4 图 2127 字节）。
- fake ComfyUI 覆盖全部 HTTP 端点，CI 不需要显卡。

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+ | 用了 `asyncio.timeout`、`X \| None`、`dataclass(slots=True)` |
| ComfyUI | 0.3.x | 只依赖 `/system_stats` `/prompt` `/history` `/view` `/object_info` `/queue` `/interrupt` `/ws`，都是稳定端点 |
| GPU | 非必需 | 单元测试全离线，只有真机联调和 demo 需要显卡 |

server 和 ComfyUI 各用各的 Python 解释器，中间走 HTTP。别把依赖装进整合包那个解释器里：里面压着一堆自定义节点的版本约束，动一个可能崩一片。

## 安装

还没发到 PyPI，目前只能从源码装，挑一种就行。

**想直接用、不碰源码的**，装进独立环境，不污染系统里那些跑着别的项目的 Python：

```bash
pipx install git+https://github.com/lfmx233/comfy-mcp
```

机器上有 uv 的话，连装都不用装，跑完就丢：

```bash
uvx --from git+https://github.com/lfmx233/comfy-mcp comfy-mcp --check
```

**要改代码的**，克隆下来建虚拟环境：

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

国内网络加个镜像，快很多：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ".[dev]"
```

**别用 `pip install -e`。** 仓库路径里有中文时（比如 `D:\工作\个人项目\comfy-mcp`），editable 安装写出来的 `.pth` 是 UTF-8 内容，CPython 的 `site` 模块按 GBK 去解，结果整个解释器起不来：

```text
Fatal Python error: init_import_site: Failed to import the site module
UnicodeDecodeError: 'gbk' codec can't decode byte 0xaa in position 12: illegal multibyte sequence
```

那时候连 `python -c "print(1)"` 都跑不了，`PYTHONUTF8=1` 也救不回来。删掉 `site-packages\_editable_impl_*.pth` 就好，然后改用上面的常规安装。英文路径下 `-e` 没事，但没必要赌。

装完验证一下：

```bash
comfy-mcp --version     # comfy-mcp 0.1.0.dev0
comfy-mcp --check       # 逐个实例打印版本 / 队列 / 显存，连不上返回码 1
```

`--check` 是给人看的便利命令，Agent 那边用 `comfy_status`，拿到的是同一份数据。

## 配置

配置全靠环境变量，没有第二套语法要学。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COMFY_MCP_INSTANCES` | `instances.yaml` | 实例清单；文件不存在就回落成单实例 |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | 只在 `instances.yaml` 不存在时生效 |
| `COMFY_MCP_WORKFLOWS` | `workflows` | 工作流注册表目录 |
| `COMFY_MCP_RUNS` | `runs` | 运行记录目录（`<job_id>.json` + `checksums.json`） |
| `COMFY_MCP_MODELS_DIR` | 空 | 指向 ComfyUI 的 `models/`，设了才会算模型 sha256 |
| `COMFY_MCP_KEEP_RUNS` | `100` | 保留多少条运行记录，启动时按 mtime 删超出的 |
| `COMFY_MCP_DEFAULT_TIMEOUT_S` | `300` | 单次运行的默认超时 |
| `COMFY_MCP_PREVIEW_MAX_EDGE` | `512` | 首图缩略的最长边 |
| `COMFY_MCP_TRANSPORT` | `stdio` | `stdio` 或 `http`（streamable-http） |
| `COMFY_MCP_HTTP_PORT` | `8848` | `http` 传输的监听端口 |
| `COMFY_MCP_MAX_UPLOAD_MB` | `20` | 上传素材上限 |
| `COMFY_MCP_MAX_QUEUE` | `20` | 队列深度上限，超了直接报 `QUEUE_FULL`，不让请求埋进实例自己的队列 |

`instances.yaml` 可选，单卡单机完全可以不写：

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

- `name` 不能重名，工具调用时用 `instance` 参数点它；不传就用第一条。
- `url` 支持 `http://` 和 `https://`，`ws://` / `wss://` 自动推导。
- `tags` / `models` 是留给「按需选实例」的，现在只作展示。

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

`.cursor/mcp.json`，结构一样，照抄 `mcpServers` 那段就行。

### 命令行直连（调试用）

```bash
# 让 server 以 stdio 跑起来，手工喂 JSON-RPC（或者接 MCP Inspector）
python -m comfy_mcp

# 端到端冒烟：起 server、列工具、调 comfy_status
python scripts/smoke_d1.py
```

## 六个工具

| 工具 | 用途 | 阻塞 | 状态 |
|---|---|---|---|
| `comfy_status` | 实例版本、GPU 显存、队列长度 | 否 | ✅ |
| `comfy_list_workflows` | 列出工作流和参数 schema，等于给 Agent 的说明书 | 否 | ✅ |
| `comfy_validate` | 校验节点和模型齐不齐，不花 GPU | 否 | ✅ |
| `comfy_run` | 跑一次工作流，带进度，可设超时 | 是 | ✅ |
| `comfy_job` | 查任务状态（重启后仍可用） | 否 | ✅ |
| `comfy_cancel` | 取消任务并清队列 | 否 | ✅ |

错误统一成一个信封：`{code, message, hint, retryable, job_id}`，十个码——`INVALID_PARAM` `UNKNOWN_WORKFLOW` `NODE_MISSING` `MODEL_MISSING` `QUEUE_FULL` `TIMEOUT` `CUDA_OOM` `INSTANCE_DOWN` `CANCELED` `EXECUTION_FAILED`。`hint` 是给模型看的，告诉它下一步往哪儿改，而不是甩一句 "invalid params" 完事。

前九个是设计阶段定的。`EXECUTION_FAILED` 是跑起来之后补的：ComfyUI 返回 `status_str=error`，但异常既不是 OOM 也不属于任何已知类别（比如 `RuntimeError: mat1 and mat2 shapes cannot be multiplied`），总得有个兜底，否则这类失败会掉进「未知异常」，把一坨栈信息糊给模型。它 `retryable: false`，同样的图同样的参数再跑一遍还是同样的错，重试只是白烧 GPU。

一次成功的调用长这样（`comfy_run` 返回值，节选）：

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

`width` / `height` 是 D4 才有值的：`/history` 不返回尺寸，所以取图做缩略时顺手从文件头把宽高解出来（PNG、JPEG、GIF、WebP、BMP 各一套头解析），不依赖 Pillow。

## 返回里都有什么（D4）

一次 `comfy_run` 返回三类 content 块，各管一摊：

| 块 | 内容 | 为什么 |
|---|---|---|
| `text` | 上面那段 JSON（job_id / manifest / outputs / error） | 完整事实，能存档，能 diff |
| `image` | 首图缩略，最多一张 | 让模型「看见」结果，不用再去读文件 |
| `resource_link` | 每个输出一个 `comfy://output/<prompt_id>/<index>` | 原图按需取，不撑上下文；模型也能只挑它要的那张 |

`comfy://output/...` 是 MCP 模板资源，取回来的是 ComfyUI 上的原始字节，不是缩略图。下游还要再加工的话，不用回头翻文件系统。

首图为什么要重新编码，两条规则，各治一种浪费：

1. 几何：长边超过 `COMFY_MCP_PREVIEW_MAX_EDGE`（默认 512）就等比缩；
2. 体积：文件超过 48KB 就重编码。512×512 的二次元 PNG 有 373KB，转成 JPEG 剩 32KB，小了 11 倍多，而模型根本看不出差别。

带 alpha 的图保持 PNG；重编码反而变大的话原样返回；没装 Pillow 就整条逻辑降级成原图，不影响出图。

运行记录落在 `runs/<job_id>.json`，原子写，临时文件加 `os.replace`，断电不会留下半个 JSON：

- 记什么：workflow / workflow_hash / instance / status / progress / prompt_id / idempotency_key / 解析后的 params / api 图 / models（含 sha256）/ outputs / error / warnings / 三个时间戳。
- 重启恢复：启动时按 mtime 读最近 `COMFY_MCP_KEEP_RUNS`（默认 100）条进内存，`comfy_job` 对老 job 一样能答；坏文件跳过并记 warning，不炸启动。
- 幂等也跨重启：idempotency key 一起落盘，重启后同名请求照样命中复用，图也照样返回。
- 失败的不进幂等表：OOM、TIMEOUT、CANCELED 都是可重试的，把它们缓存起来等于把错误也缓存了。

模型 sha256 不便宜，几 GB 一个文件，所以 `runs/checksums.json` 按 `size + mtime_ns` 记一份摘要缓存，文件没动就不再读盘。实测第二次运行不重算。

## Demo（D5）

一条命令，三个工作流，一分钟左右（RTX 4060 Laptop 8GB）：

```powershell
$env:COMFY_MCP_MODELS_DIR="D:\AIGC\ComfyUI-aki-v2\ComfyUI\models"
.\.venv\Scripts\python.exe scripts\demo.py
```

![demo](https://github.com/lfmx233/comfy-mcp/raw/main/demo/contact-sheet.jpg)

*左：SD1.5 文生图 512×512；中：图生图，输入是张简笔画，denoise 0.55、8 步，所以画风偏平；右：SDXL 底模加 refiner，768×768。*

```text
== 2. 3 个工作流各跑一次 ==
  [ok] txt2img_anime  7af39798e121   8.06s  512x512  420 KB  sha256:b209d0c1442a
  [ok] img2img        c9c95f29592a   4.76s  768x768  815 KB  sha256:b1e243cb684b
  [ok] sdxl_txt2img   8c8fbfadfc3e  33.75s  768x768  879 KB  sha256:54781cdf00c9
  [ok] txt2img_anime  73576f153038   1.4s  512x512  420 KB  sha256:b209d0c1442a  ← 同一份 manifest 重跑
  [ok] ws 51 frames / 18 progress updates / 7.9s: 12.5 → 62.5 → 100.0
总耗时 56.9s
DEMO: PASS
```

跑完留四样东西：`demo/outputs/*.png` 原图、`demo/contact-sheet.jpg` 上面那张拼图、`demo/report.json`（每个 job 的 id、耗时、字节 sha256、参数）、`demo/transcript.txt`（终端原文）。

复现才是这一步的重点：

```powershell
python scripts/demo.py --repro 7af39798e121
```

它去 `runs/<job_id>.json` 里把参数原样捞出来，换一个 idempotency key 重新提交，再比 sha256：

| 场景 | 耗时 | 结果 |
|---|---|---|
| 同一进程内重跑（命中 ComfyUI 节点缓存） | 0.0s | `REPRO: IDENTICAL` |
| 重启 ComfyUI 后重跑（缓存清空，真重算） | 5.25s | `REPRO: IDENTICAL` |

第二行才算数。ComfyUI 的缓存是内存态，重启就没了，5.25s 说明它真的重算了一遍，图还能一字节不差，靠的是 manifest 里参数完整（包括解析后的 seed），不是缓存走运。整套演示流程和三分钟口播稿在 [docs/DEMO.md](docs/DEMO.md)。

## 加一个工作流

两步：ComfyUI 里导出的工作流 json 扔进 `workflows/`，再写一个同名 yaml，声明哪些参数允许 Agent 动。

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

几个容易踩的点：

- `path` 和 `paths` 二选一，路径在启动时就校验，写错立刻报错，不会等到运行时才发现。
- `seed` 写 `default: -1` 表示每次随机，但它会先解析成一个具体整数，再用来算幂等键和 manifest——不然「同参数」根本无从谈起。
- 模型文件名（`ckpt_name` 之类）也是普通参数。指向本机没有的文件，校验阶段就报 `MODEL_MISSING`，顺手把本机可用的前几个列给你。
- 图可以是普通导出（带前端控件），也可以是 API 格式，两种都吃。前端控件值和 API 槽位怎么对齐是自动算的，不用手改 json。
- 参数不在 yaml 里声明，Agent 就是改不动。这个项目「敢在生产里用」的底气，基本就来自这一条。

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

到 D4 这十个模块就全齐了：`config.py` / `client.py` / `server.py` / `graph.py` / `registry.py` / `jobs.py` / `manifest.py` / `hashes.py` / `resources.py` / `errors.py`。

## 几个取舍

1. **不做通用 graph 执行器。** ComfyUI 的自定义节点等于任意代码执行，把「任意 graph 都能跑」的口子开给模型，是安全问题不是功能问题。注册表加白名单把它收成受限接口，代价是加工作流要写个 yaml，但这本来就是「生产可用」的意思。
2. **输出优先 ResourceLink。** 1024×1024 的 PNG 大约 1.5MB，base64 之后 2MB，五张图上下文直接见底。首图缩略让模型「看」，剩下的给链接按需读。
3. **不套 comfy-cli。** 少一层 shell 和文本解析，直接讲 HTTP 加 websocket，顺便就能同时服务本机和远端好几个实例。
4. **job 表放内存，manifest 落盘。** 不引 Redis。单用户开发场景，重启恢复靠扫 `runs/` 目录就够了。
5. **依赖 `httpx2` 而不是 `httpx`。** MCP Python SDK 2.x 自带 `httpx2`（import 名就叫 `httpx2`），直接复用，省一层版本冲突。
6. **尺寸自己解，不靠 Pillow。** `/history` 不给宽高，而 Pillow 只当可选加速器用：PNG/JPEG/GIF/WebP/BMP 的头字段手写解析，装不装 Pillow 都能报出尺寸，Pillow 只负责重编码。
7. **工具返回 `CallToolResult`，不返回 dict。** SDK 对标注成 `CallToolResult` 的函数原样透传（不校验、不套 `outputSchema`），这样一条返回里能同时塞文本、图片和资源链接，`structuredContent` 还是那份 JSON。

## 开发与测试

```bash
pytest                      # 144 个用例，全离线，不需要 GPU
ruff check . && ruff format --check .

# 跑假 ComfyUI（Starlette），再用 curl 或 smoke 脚本验证
python -m uvicorn tests.fake_comfy.app:app --port 8899
curl http://127.0.0.1:8899/system_stats
```

改完代码怎么跑（非 editable 安装不会自动同步源码，这点容易忘）：

```bash
# 1) 直接 pytest —— 它按 pyproject.toml 的 pythonpath 读 src/
pytest

# 2) 临时指向源码跑 CLI
# Windows
set PYTHONPATH=src && comfy-mcp --check
# Linux / macOS
PYTHONPATH=src comfy-mcp --check

# 3) 或者重装一遍
pip install --no-cache-dir --force-reinstall ".[dev]"
```

pip 报 `[WinError 5] 拒绝访问: '...\\pip\\cache\\wheels\\3a'` 就加 `--no-cache-dir`，是缓存目录被占着。

接真机：

```bash
# 先起 ComfyUI，例如秋叶整合包
# D:\AIGC\ComfyUI-aki-v2\python\python.exe main.py --listen 127.0.0.1 --port 8188
COMFY_MCP_INSTANCES=nope.yaml COMFYUI_URL=http://127.0.0.1:8188 python scripts/smoke_d1.py

# 校验 + 真实提交（提交后立刻取消，不会真烧 GPU）
python scripts/verify_d2.py --dry-run   # 只校验，不提交
python scripts/verify_d2.py             # 校验 + POST /prompt + 立刻 /interrupt

# D3：真机跑通一次 comfy_run（会真的出图，约 10s）
$env:PYTHONPATH="src"; python scripts/verify_d3.py
# 加 --timeout-check 再跑一个「必超时」的任务，验证 interrupt + 清队列
$env:PYTHONPATH="src"; python scripts/verify_d3.py --timeout-check

# D4：验证 manifest / 尺寸 / 资源 / 缩略 / 重启恢复 / 5 图预算
# 要 COMFY_MCP_MODELS_DIR 指向 ComfyUI 的 models/ 才会算 sha256
$env:PYTHONPATH="src"; python scripts/verify_d4.py

# D5：端到端 demo（3 个工作流 + ws 进度 + 同参数复现），产物写 demo/
python scripts/demo.py
python scripts/demo.py --only txt2img_anime      # 只跑一个，调试用
python scripts/demo.py --repro 7af39798e121      # 用 runs/ 里的 manifest 重跑并比 sha256
```

D3 真机输出（4060 Laptop，SD1.5 512×512 4 步）：

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

D4 真机输出（同一台 4060，SD1.5 512×512 6 步）：

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

D5 的输出在上面「Demo（D5）」那节，完整终端记录在 `demo/transcript.txt` 和 `demo/report.json`。

设计与验收标准见 [docs/PLAN.md](docs/PLAN.md)。

## 不打算做的事

- 节点和模型管理。该用 ComfyUI-Manager 就用它。
- 前端界面。这是给 Agent 用的 MCP server，不是给人点的平台。
- 内置模型下载。
- 覆盖全部 ComfyUI API。只保「跑工作流」这条主路径。
- 兼容 Comfy Cloud 的专有端点。

## 版本记录

- `0.1.0.dev0`：D1–D6。sdist 与 wheel 已构建、`twine check` 通过、干净 venv 装得上；PyPI 和 Registry 还没上传，卡在凭据上，步骤全写在 [docs/RELEASE.md](docs/RELEASE.md)。
- 迭代记录在 [iterations/](iterations/)，一个里程碑一份：目标 → 产出 → 验收 → 踩坑 → 遗留 → 下一步。
- 每份记录对应一个 commit，出问题能翻回是哪次改动带进来的。

## 许可

MIT。设计灵感来自 [MCP 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) 和 Comfy Org 的 [comfy-mcp](https://github.com/Comfy-Org/comfy-mcp)；本项目是独立实现，不含它的代码。
