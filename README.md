# comfy-mcp

> 让 Agent 安全、可复现地驱动 ComfyUI。

![status](https://img.shields.io/badge/status-v0.1%20WIP-orange)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

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
| D2 | 图转换与工作流注册表 | ⏳ |
| D3 | 6 个工具 + 进度映射 | ⏳ |
| D4 | 交付、manifest、幂等与恢复 | ⏳ |
| D5 | 真机联调（本机 4060 已连通） | ⏳ |
| D6 | 发布（PyPI + MCP Registry） | ⏳ |

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
- **无 GPU 可测**：fake ComfyUI + 录制的 ws 帧回放，CI 不需要显卡

## 快速开始

> v0.1 尚未发布到 PyPI，以下为本地开发方式。

```bash
git clone https://github.com/<you>/comfy-mcp.git
cd comfy-mcp
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -e ".[dev]"
comfy-mcp --help
```

接入 Claude Desktop / Cursor：

```json
{
  "mcpServers": {
    "comfy": {
      "command": "comfy-mcp",
      "env": {
        "COMFY_MCP_INSTANCES": "C:/ai/instances.yaml",
        "COMFY_MCP_WORKFLOWS": "C:/ai/workflows",
        "COMFY_MCP_RUNS": "C:/ai/runs"
      }
    }
  }
}
```

`instances.yaml`：

```yaml
instances:
  - name: local-3090
    url: http://127.0.0.1:8188
    tags: [gpu24g]
    models: [sd_xl_base_1.0.safetensors]
```

## 工具一览

| 工具 | 用途 | 阻塞 |
|---|---|---|
| `comfy_status` | 实例、队列长度、已加载模型、已注册工作流数 | 否 |
| `comfy_list_workflows` | 列出工作流及其参数 schema（给 Agent 看的说明书） | 否 |
| `comfy_validate` | 校验节点 / 模型是否齐备，不花 GPU | 否 |
| `comfy_run` | 跑一次工作流，带进度，可设超时 | 是 |
| `comfy_job` | 查任务状态（重启后仍可用） | 否 |
| `comfy_cancel` | 取消任务并清队列 | 否 |

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

## 设计取舍

1. **不做通用 graph 执行器。** ComfyUI 自定义节点等于任意代码执行，把「任意 graph」接口暴露给模型是安全问题。注册表 + 白名单把它收敛成受限接口，代价是加工作流要写一个 yaml —— 这正是「生产可用」的含义。
2. **输出优先 ResourceLink。** 一张 1024×1024 PNG 约 1.5MB，base64 后 2MB。五张图直接把上下文吃光。首图缩略给模型「看」，其余给链接按需读。
3. **不基于 comfy-cli。** 少一层 shell 与文本解析，直接说 HTTP + websocket；也因此能同时服务本机与远端多个实例。
4. **job 表放内存 + manifest 落盘。** 不引 Redis：单用户开发场景，重启恢复靠扫 `runs/` 目录即可。

## 路线图

| 里程碑 | 内容 | 状态 |
|---|---|---|
| D1 | 骨架、配置、HTTP/ws 客户端、fake ComfyUI | 进行中 |
| D2 | UI→API 图转换、工作流注册表与 sidecar 校验 | 计划 |
| D3 | 6 个工具、结构化输出、进度上报、超时与取消 | 计划 |
| D4 | ResourceLink、manifest、幂等、重启恢复 | 计划 |
| D5 | 接真实 ComfyUI，跑通 3 个工作流，产出 demo | 计划 |
| D6 | README / 录屏 / PyPI / MCP Registry 上架 | 计划 |

详细设计与验收标准见 [docs/PLAN.md](docs/PLAN.md)。

## Non-goals

- 不做节点 / 模型管理（装节点请用 ComfyUI-Manager）
- 不做前端界面
- 不内置模型下载
- 不追求覆盖全部 ComfyUI API，只覆盖「跑工作流」这条主路径
- 不兼容 Comfy Cloud 的专有端点

## 开发

```bash
pytest            # 全部离线用例，不需要 GPU
ruff check .
```

## 许可与致谢

MIT。设计灵感来自 [MCP 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) 与 Comfy Org 的 [comfy-mcp](https://github.com/Comfy-Org/comfy-mcp)；
本项目为独立实现，不包含其代码。
