# D5 · 真机 Demo（2026-11-19）

## 目标

来自 `docs/PLAN.md` 的 D5 原文：

> 产出：接一台真实 ComfyUI，跑 3 个工作流，录 demo。
> 验收：README 命令原样可复现；progress 与真实执行同步。

拆开来是四件事：三个真实工作流在真机上跑通、进度条跟着真实节点走、同一份 manifest 重跑得到同一张图、把过程固化成别人能照抄的 demo 脚本。

## 交付物

| 文件 | 行数 | 作用 |
|---|---|---|
| `scripts/demo.py` | 420 | 五段式 demo：实例 → 跑活 → 进度 → 复现 → 产物；`--only` 选工作流、`--repro JOB_ID [--expect SHA256]` 按 manifest 重跑 |
| `docs/DEMO.md` | 100 | 前置、一条命令、五段说明、产物清单、复现、三分钟口播稿、已知限制 |
| `demo/contact-sheet.jpg` | — | 三张图拼一张（1584×550，221623 字节），README 引用 |
| `demo/report.json` | — | 每个 job 的 id / 耗时 / 字节数 / sha256 / 参数 / 进度采样点 |
| `demo/transcript.txt` | — | 复现那一段的终端原文 |
| `src/comfy_mcp/manifest.py` | 180（+29） | `CHECKSUMS = "checksums.json"`；`__len__` / `load` / `prune` 三处 glob 跳过它 |
| `src/comfy_mcp/server.py` | 701 | 摘要缓存路径改用 `settings.runs_dir / CHECKSUMS` |
| `tests/test_manifest.py` | 144（+47） | 新增「摘要缓存不能被当成 manifest」用例 |
| `README.md` | 377 | 加 `## Demo（D5）` 一节（拼图 + 输出摘要 + 复现表格）、进度表 D5 ✅、verify 段补三条 demo 命令 |

## 验收结果

真机（RTX 4060 Laptop 8GB，ComfyUI 0.3.60，python 3.12.10，torch 2.8.0+cu129，启动后 `vram_free` 3.76GB）：

```text
== 1. 实例 ==
  [ok] comfy_status: ComfyUI 0.3.60  cuda:0 RTX 4060 Laptop  free 3.76/8.0GB  queue 0+0
== 2. 3 个工作流各跑一次 ==
  [ok] txt2img_anime: 7af39798e121  8.06s  512x512  420 KB  sha256:b209d0c1442a
  [ok] img2img:        c9c95f29592a  4.76s  768x768  815 KB  sha256:b1e243cb684b
  [ok] sdxl_txt2img:   8c8fbfadfc3e 33.75s  768x768  879 KB  sha256:54781cdf00c9
== 3. 进度 ==
  [ok] ws 51 frames, 18 progress updates in 7.9s
       12.5 → 25.0 → 37.5 → 50.0 → 62.5 → 63.7 → … → 99.0 → 100.0
== 4. 复现 ==
  [ok] 73576f153038  1.37s  sha256:b209d0c1442a  同 7af39798e121 → 一致
== 5. 产物 ==
  [ok] demo/contact-sheet.jpg 1584x550 221623 bytes
总耗时 56.9s
DEMO: PASS
```

**冷启动复现（D5 真正的验收）**：kill ComfyUI → 重新起（`vram_free` 回到 6.94GB）→ `demo.py --repro 7af39798e121`：

```text
== 用 runs/7af39798e121.json 的参数重跑 ==
  期望 b209d0c1442a7b50…  实际 b209d0c1442a7b50…  → 一致
  耗时 5.25s，输出 430446 字节
REPRO: IDENTICAL
```

同进程重跑是 0.0s（命中 ComfyUI 内存节点缓存），冷启动是 5.25s —— 后者才是真重算，也才是「可复现」的证据。

离线：`pytest -q` → 144 passed in 10.18s；`ruff check .` → All checks passed!；`ruff format --check .` → 38 files already formatted。

## 关键决定

1. **demo 前必须重启 ComfyUI，并且把这条写进 README 和 DEMO.md。**
   不是「小心一点」，是物理现实：8GB 卡上 SD1.5 与 SDXL 来回换，显存碎片化之后 SDXL 会从 33.75s 掉到 >420s。与其在文档里假装稳定，不如把「demo 前重启」当成流程的一部分，同时把数字（33.75s vs 超时）都留档。

2. **`refiner_seed` 钉成常数 7，不用默认的 -1。**
   SDXL 工作流的 refiner 段默认 seed 是 -1（每次随机）。它不影响「图能不能出」，但会让「同参数重跑」变成一句空话——主 sampler 的 seed 一样，refiner 的噪声却换了。代价是这个参数失去随机性，但 demo 要的就是可复现。

3. **`--repro` 从 `runs/<job_id>.json` 读参数，而不是从 `demo/report.json` 读。**
   report.json 是给人看的摘要（sha256、耗时），manifest 才是执行事实（完整参数 + 工作流哈希）。让复现依赖摘要文件，等于让「证据」依赖「结论」。

4. **contact sheet 用 Pillow 拼，不当成服务能力。**
   它在 `scripts/` 里，不在 `src/` 里：拼图是演示需求，不是 MCP server 的职责。server 端仍然只做「首图缩略 + 其余 ResourceLink」。

5. **进度用直连 `/ws` 旁路采一份，和 server 的进度映射分开统计。**
   demo 的 section 3 是「证明进度真实」的实验装置，不该和被测对象共用状态；它拿到 51 帧原始事件，server 那份映射只取自己需要的四类。

## 踩坑

1. **同一个进程跑多次 demo，SDXL 连续两次超时（>420s，progress 恒 0.0）。**
```text
manifest: status=timeout  job=654c5f9742cc / 7bf24d4f0e1a
          prompt cb428e99-73de-4c09-98d0-333c86aca74f
/history: execution_interrupted @ node 15 KSamplerAdvanced
          execution_start → interrupted 相隔 534 秒
```
   根因不是队列（当时 queue 0+0），是采样在换页：进程里先后驻留过 SD1.5 与 SDXL 两套模型，启动时 `vram_free` 只剩 0.88–2.62GB。解法：demo 前重启 ComfyUI；新进程里 `vram_free` 6.94–7.0GB，SDXL 稳定 33.75s。这条已写进 `docs/DEMO.md` 的「已知限制」和 README 的 demo 小节。

2. **`runs/checksums.json` 被 `ManifestStore` 当成 manifest 读。**
```text
skipping unreadable manifest: runs\checksums.json: unsupported schema None (this build reads 1)
```
   每次启动刷一行，不致命但很脏。根因：`load()` / `prune()` / `__len__()` 都按 `runs/*.json` glob，而摘要缓存恰好也住在 `runs/` 里。解法：`manifest.py` 里加常量 `CHECKSUMS = "checksums.json"`，三处 glob 都排除它，`server.py` 用同一个常量拼路径（避免两边字符串各写一遍再次漂移）。补了回归用例 `test_the_digest_cache_is_not_mistaken_for_a_manifest`。

3. **拼图的标签说的是缩放后的尺寸，768 的图自称 512。**
   三块面板统一缩放到 512 高，`draw.text` 却用了 `tile.width x tile.height`（缩放后），于是 sdxl 与 img2img 明明 768×768 也被标成 512×512。这种错不会让任何测试变红，只会让看的人以为「三个工作流都出 512」。解法：循环里顺手记 `sizes.append((image.width, image.height))`，标签用原始尺寸。**标签要陈述事实，不是陈述你刚做完的变换。**

4. **`--repro` 的耗时一度打出 1794463913.6s。**
   section 4 误用了 section 2 的 `started` 时间戳（那是另一个作业的起点），要单独 `t0`。教训：计时变量必须和自己的作业同生命周期，别图省事复用。

5. **demo.py 的 `passed` 判定一开始漏了 `--repro`。**
   只跑 `--only txt2img_anime` 时没有 section 4，`session.repro` 是空 dict，`repro.get("identical")` 直接抛 KeyError 的写法被改成 `.get("identical", True)`；同时把 section 4 抽成 `check_repro()`，只有真跑过 txt2img_anime 才执行，否则打印 `[skip]`。

6. **用 run_code 的 JS 模板字符串写 README 里的 Markdown 代码块，整批 edit 全废。**
```text
Error: code run failed (exception): Expected ',', got 'numeric literal'
```
   反引号 + `${…}` 在模板字符串里是语法，不是文本。改为「单引号字符串数组 + join」或用占位符替换，别把 Markdown 直接塞进模板字符串。

## 遗留

1. demo 依赖 ComfyUI 的 `input/example.png` 已存在；上传接口不在六个工具里，脚本不做兜底（缺文件时报 ComfyUI 自己的错）。
2. contact sheet 固定三列，不按工作流数量自适应。
3. `--repro` 只验证 sha256，不验证「换分辨率/换机器仍一致」（本机只有一张卡，跨机验证要等有第二台机器或云 GPU）。
4. `demo/outputs/*.png` 约 2MB 不入库（`.gitignore` 已排除），只保留拼图与 report.json；想看原图要自己跑一次。
5. 没有录屏/gif——README 用静态拼图 + 数字，gif 留到 D6 再说。

## 下一步

D6：发布。README 定稿（对比表、Non-goals 已在）、`server.json` 上架 MCP Registry、PyPI 打包，让 `uvx comfy-mcp` 一条命令能起。