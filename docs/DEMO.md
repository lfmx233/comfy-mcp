# Demo：在一台 8GB 笔记本上跑三个工作流

这篇文档是给两种人看的：想验证这个仓库的人（照抄命令就行），和面试里要现场演示的人（三分钟口播稿在最后）。

一台 RTX 4060 Laptop（8GB 显存）+ ComfyUI 0.3.60，跑 SD1.5 的两条链路（文生图、图生图）和 SDXL 的一条链路（底模 + refiner）。三个工作流都是真实导出的 UI 图，不是手写 JSON。

## 0. 前置

- ComfyUI 在 `127.0.0.1:8188` 跑着，`system_stats` 能看到 `comfyui_version`
- 仓库依赖装好：`.venv\Scripts\python.exe -m pip install --no-cache-dir ".[dev]"`
- `workflows/` 里三个 sidecar 指向的模型都在 ComfyUI 的 `models/` 里
- 图生图的输入图 `example.png` 在 ComfyUI 的 `input/` 里

## 1. 一条命令

PowerShell（复制即用，路径按自己的改）：

```powershell
cd D:\工作\个人项目\comfy-mcp
$env:PYTHONIOENCODING = "utf-8"
$env:COMFYUI_URL          = "http://127.0.0.1:8188"
$env:COMFY_MCP_WORKFLOWS  = "workflows"
$env:COMFY_MCP_RUNS       = "runs"
$env:COMFY_MCP_MODELS_DIR = "D:\AIGC\ComfyUI-aki-v2\ComfyUI\models"

.\.venv\Scripts\python.exe scripts\demo.py
```

只要其中一两个工作流（调试时省时间）：

```powershell
.\.venv\Scripts\python.exe scripts\demo.py --only txt2img_anime --only img2img
```

## 2. 它到底做了什么

| 段落 | 动作 | 想证明什么 |
| --- | --- | --- |
| 1. 实例 | `comfy_status` | server 真的连上了 ComfyUI，版本、显存、队列都是读回来的 |
| 2. 跑活 | `comfy_validate` → `comfy_run` → `read_resource` | 三个工作流都过参数白名单、都被接受、都能取回图 |
| 3. 进度 | 直连 `/ws` 喂 `Progress` | 进度条不是装饰：每个采样点都对应一次真实节点事件 |
| 4. 复现 | 换 idempotency key 再跑一次 | 同一份 manifest 再来一遍，图的 sha256 不变 |
| 5. 产物 | `demo/` 下四个文件 | 有东西可以给别人看 |

## 3. 产物

```
demo/
  outputs/txt2img_anime.png   512x512   SD1.5 文生图
  outputs/img2img.png         768x768   SD1.5 图生图（denoise 0.55）
  outputs/sdxl_txt2img.png    768x768   SDXL 底模 + refiner
  contact-sheet.jpg           三张拼一张，README 用
  report.json                 每个 job 的 id / 耗时 / 字节 sha256 / 参数
  transcript.txt              终端输出原文
```

`report.json` 里一条记录长这样（真实数字，参数做了省略）：

```json
{
  "workflow": "txt2img_anime",
  "job_id": "7af39798e121",
  "prompt_id": "9fb48196-39f8-466b-8fb5-fd9eff3c5fe2",
  "elapsed_s": 8.06,
  "wall_s": 8.13,
  "width": 512,
  "height": 512,
  "bytes": 430446,
  "sha256": "b209d0c1442a7b503c08d8bf002bf9842a9bfcbc4ead64bbbff46ba6683e1fcb",
  "workflow_hash": "sha256:6a6f36af52f74f268c20078954e053ce6bb9ba3374824f627f52e990b6aeb91e",
  "params": {"prompt": "1girl, …", "seed": 424242, "steps": 8, "width": 512, "height": 512}
}
```

## 4. 复现：换一台机器（或重启一次）还能画出同一张图

```powershell
# 冷启动验证：先关掉 ComfyUI 再起（节点缓存是内存态，重启即清）
.\.venv\Scripts\python.exe scripts\demo.py --repro 7af39798e121
```

它从 `runs/7af39798e121.json` 里把参数原样读出来，换一个 idempotency key 重新提交，再和 `demo/report.json` 里记的 sha256 比：

```
== 用 runs/7af39798e121.json 的参数重跑 ==
  workflow=txt2img_anime  params={"prompt": "1girl, solo, silver hair, …", "seed": 424242, "steps": 8, …}
  期望 b209d0c1442a7b50…  实际 b209d0c1442a7b50…  → 一致
  耗时 5.25s，输出 430446 字节
REPRO: IDENTICAL
```

两次实测摆在一起，差别就在「缓存有没有被清掉」：

| 场景 | 耗时 | sha256 |
| --- | --- | --- |
| 同一进程重跑（ComfyUI 节点缓存命中） | 0.0s | `b209d0c1442a…` |
| 杀掉 ComfyUI 再起，然后 `--repro` | 5.25s（真重算） | `b209d0c1442a…` |

第二次才算数：5.25s 说明真的重算了，不是读缓存。

为什么这件事值得写：ComfyUI 自己只保证「同一台机器、同一个进程里重跑命中节点缓存」，进程一重启缓存就没了。manifest 里存的是**参数**，不是缓存指针——所以重启之后还能复现，靠的是参数完整、模型可查（`models[].sha256`），不是靠运气。

## 5. 现场演示口播（约 3 分钟）

- **0:00–0:20 定位**：市面上已经有一个官方 comfy-mcp，41 个工具，把 ComfyUI 的 API 全暴露出来。它解决「能不能调」。我做的是「敢不敢在生产里调」——参数白名单、可复现 manifest、真进度、超时会真的打断。
- **0:20–0:50 跑一条**：`comfy_run` 一个 `txt2img_anime`，指出返回里三样东西：`job_id`、`manifest`（参数 + 工作流哈希 + 模型 sha256）、`outputs`（首图内联缩略、其余是 `comfy://` 链接）。强调图不是 base64 塞满上下文。
- **0:50–1:40 出错会怎样**：故意传一个不存在的 checkpoint → `MODEL_MISSING`，hint 里写着「工作流声明的模型在实例上不存在」，而且这个错在**校验期**就报了，没有真提交。再演示一个 `timeout_s=2`：返回 `TIMEOUT`，并且真的发了 `/interrupt`，队列回到 0+0。
- **1:40–2:30 可复现**：指着 `runs/<job_id>.json`——参数、工作流哈希、模型 sha256 都在里面。`--repro` 重跑一次，sha256 一致。
- **2:30–3:00 边界**：8GB 卡上 SDXL 双模型会换页，所以 demo 用 768 不用 1024；这不是「跑不动」，是「知道自己在什么硬件上」。真正的取舍是：宁可慢一点、可解释，也不要一个「有时候能跑」的 demo。

## 6. 已知限制（写在明处）

- 只在 Windows + ComfyUI 0.3.60 上验证过；协议层没用私有扩展，但没在 Linux/macOS 上跑过
- 8GB 显存下 SDXL 底模 + refiner 会来回换页，同一实例连续换模型后单次耗时可能从几十秒涨到几分钟（见 `iterations/D5-*.md` 的实测）；演示前重启一次 ComfyUI 最稳
- 图生图需要输入图已经在 ComfyUI 的 `input/` 里；上传接口不在这六个工具里
- 没有 Web UI，也没有多用户鉴权——这是「给 agent 用的 MCP server」，不是「给人用的平台」
