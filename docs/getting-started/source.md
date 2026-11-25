# 搭建源码开发环境

如果你准备编写新模块、调试驱动或参与框架本身开发，建议使用源码环境。需要 **Python 3.11 或更高版本**。

## 1. 拉取代码并一键初始化

在 PowerShell 中运行：

```powershell
git clone --branch v0.20.6 https://github.com/yang-fu-physics/openlab-control.git
cd openlab-control
.\setup.bat
.\run.bat
```

- `setup.bat` 会创建 `.venv` 并严格按 `requirements-lock.txt` 安装经过发布验证的依赖版本，避免包版本漂移。
- `run.bat` 始终运行当前源码，不会误启动 `dist/` 里的旧打包程序。
- 上述命令检出与本手册一致的稳定版；若准备给核心仓库提 PR，再切换到 `main` 分支。

## 2. 配置仿真或真实仪表

程序启动默认读取 `configs/general.toml`（首次启动若不存在，会自动从仓库随附的 `configs/general.default.toml` 复制一份，并已被 `.gitignore` 忽略），没有任何系统仪表面板也能正常打开。要启用内置仿真或接入真实仪表，运行扫描向导：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

扫描器会按职责生成三类本地配置文件（均已被 `.gitignore` 忽略）：

- `configs/visa.resources.toml`：留给测量模块的 VISA 地址清单；
- `configs/instruments/<仪表ID>.toml`：系统仪表实例、面板顺序、角色与安全限值；
- `configs/pid/<实例ID>.toml`：仅在首次保存带 PID 字段的温控仪时从模板复制，此后不会覆盖或删除。

详细用法见 [扫描与配置仪表](../guides/instrument-scanner.md)。

## 3. 运行自动化测试

检查语法并运行全量核心测试：

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

如需无界面试跑一条包含温场指令的示例序列，请先在扫描器中勾选对应内置仿真，再执行：

```powershell
.\.venv\Scripts\python.exe run.py --headless-demo --sequence examples\nested_scan.seq --timeout 120
```

## 4. 本地预览和校验文档网站

文档工具单独列在 `requirements-docs.txt` 中，不会打进软件发布包：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-docs.txt
.\.venv\Scripts\mkdocs.exe serve
```

浏览器打开终端提示的本地地址即可实时预览。修改文档提交前，请务必运行严格构建确保没有断链或语法警告：

```powershell
.\.venv\Scripts\mkdocs.exe build --strict
```

## 5. 推荐开发习惯

1. 先写一个不依赖真机的测试（用假连接验证命令顺序与异常处理）；
2. 做最小范围的代码改动；
3. 跑通受影响的单元测试，再跑完整测试套件；
4. 涉及真实仪表协议改动时，最后再进行有人值守的低风险真机验证。

!!! danger "不要提交本机配置和实验隐私"

    真实仪表地址、`configs/general.toml`、`configs/instruments/`、`configs/visa.resources.toml`、`configs/module_instances.toml`、`configs/pid/`、
    报警 Token、实验 `.dat` 以及 `module_data/` 都不要提交到 Git。
    此外，每次运行自动保存的 `runs/<运行目录>/configuration/` 包含当次使用的配置快照，对外分享前请先检查并脱敏。
