# 发布（D6）

这份文档记录「把它发出去」的每一步，以及卡在哪。

## 现状

仓库里的东西已经可以发布，缺的只是凭据：

| 步骤 | 状态 |
| --- | --- |
| PyPI 元数据（license / urls / classifiers / extras） | ✅ 已补，`twine check` 通过 |
| 构建 sdist + wheel | ✅ `python -m build` 出两个文件 |
| 干净 venv 安装 wheel 并启动 CLI | ✅ `comfy-mcp --version` / `--check` 正常 |
| Registry 清单 `server.json` | ✅ 按 2025-12-11 schema 校验通过 |
| `twine upload` | ⛔ 缺 PyPI API token |
| `mcp-publisher publish` | ⛔ 缺 GitHub 登录态 |

所以 D6 交付的是「产物 + 脚本 + 文档」，最后两条命令得由仓库主人在自己机器上敲。上传这种事本来也不该交给一个 Agent。

## 1. 构建

```bash
pip install build twine
python -m build --outdir dist
python -m twine check dist/*
```

`--no-isolation` 可以跳过临时环境、直接用当前 venv 里的 hatchling，本机装了 hatchling 时更快。产物：

```text
dist/comfy_mcp-0.1.0.dev0-py3-none-any.whl   52570 bytes
dist/comfy_mcp-0.1.0.dev0.tar.gz            359849 bytes
Checking dist/comfy_mcp-0.1.0.dev0-py3-none-any.whl: PASSED
Checking dist/comfy_mcp-0.1.0.dev0.tar.gz: PASSED
```

wheel 里有 11 个模块，加上 `dist-info/licenses/LICENSE`（PEP 639 的 `license-files`）。sdist 比 wheel 大，是因为它带着 README 引用的 `demo/contact-sheet.jpg` 和迭代文档。

## 2. 先验一遍，再上传

构建能过不代表装得上。用一个全新 venv 装 wheel，是最省事的一道保险：

```bash
python -m venv /tmp/venv-d6
/tmp/venv-d6/bin/pip install dist/comfy_mcp-0.1.0.dev0-py3-none-any.whl
/tmp/venv-d6/bin/comfy-mcp --version    # comfy-mcp 0.1.0.dev0
/tmp/venv-d6/bin/comfy-mcp --check      # 会打印实例的版本 / 队列 / 显存
```

这个 venv 里没有 Pillow、没有 pytest、没有 starlette 显式声明，全是 `pip` 顺着 `mcp` 的依赖装下来的。能跑起来说明依赖声明没漏。

`--check` 那一步需要 ComfyUI 在线，没有的话拿假服务器顶上：

```bash
# 在仓库里，用 dev 环境起假 ComfyUI
.venv/bin/python -m uvicorn tests.fake_comfy.app:app --port 8899
# 另一边
COMFYUI_URL=http://127.0.0.1:8899 /tmp/venv-d6/bin/comfy-mcp --check
```

## 3. 发布到 PyPI

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-...        # 只放环境变量，别写进文件
twine upload dist/*
```

用户名固定写 `__token__`，密码是 API token（scope 选 project: comfy-mcp 就够，不用给全账户权限）。

版本号目前是 `0.1.0.dev0`。PEP 440 认定它是预发布，`pip install comfy-mcp` 默认不装它，要用 `pip install --pre comfy-mcp`。想在 PyPI 上直接 `pip install comfy-mcp`，先把三处版本号一起改成 `0.1.0`：

- `pyproject.toml` 的 `version`
- `src/comfy_mcp/__init__.py` 的 `__version__`
- `server.json` 的 `version` 与 `packages[0].version`

`tests/test_release.py` 会盯这三处是否一致，改漏一处测试就红。

## 4. 发布到 MCP Registry

Registry 只存元数据，包本体还在 PyPI，所以顺序不能反。

```bash
# 装官方 CLI（Go 二进制，不需要 Node）
# 见 https://github.com/modelcontextprotocol/registry
mcp-publisher login github
mcp-publisher publish          # 读仓库根目录的 server.json
```

`server.json` 的关键字段：

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.lfmx233/comfy-mcp",
  "version": "0.1.0.dev0",
  "packages": [{"registryType": "pypi", "identifier": "comfy-mcp", "version": "0.1.0.dev0", "transport": {"type": "stdio"}}]
}
```

几条硬约束，踩了会被拒：

- `name` 必须是 `命名空间/名字` 形式，GitHub 账号下发就是 `io.github.<user>/<repo>`，冒号斜杠都别改；
- `description` 上限 100 字符（第一次写 128 字符，被 schema 的 `maxLength` 挡下来了）；
- `packages[].version` 必须和 PyPI 上真实存在的版本逐字一致，写 `latest` 或 `^1.2.3` 都不行；
- `transport.type` 是 `stdio`——这个 server 默认走 stdio，和 Registry 里那些远端 HTTP server 不一样。

`environmentVariables` 里列的每一个变量，`tests/test_release.py` 都会去 `config.py` 里对一遍。广告一个代码根本不读的变量，等于骗用户。

## 5. 上线之后

这几处现在写的是「待发布」，发布完要回头改：

- `README.md` 安装段：`pipx install comfy-mcp`、`uvx comfy-mcp`、`pip install comfy-mcp` 三条正式写法；
- README 顶部的徽章：加 PyPI 版本和 Registry 条目；
- `docs/PLAN.md` 的 D7：勾掉；
- `iterations/D7-*.md`：记下发布时间和第一次 `uvx` 冷启动的耗时。

## 验收清单

- [x] `python -m build` 产出 sdist + wheel
- [x] `twine check` 两个产物都 PASSED
- [x] 干净 venv 装 wheel，`--version` 与 `--check` 正常
- [x] `server.json` 通过官方 schema 校验
- [x] CI 在 3.11 / 3.12 / 3.13 上跑 pytest + ruff + build
- [ ] `twine upload`（需要 token）
- [ ] `mcp-publisher publish`（需要 GitHub 登录）
