# 建立源码开发环境

源码开发需要 Python 3.11 或更高版本。Windows 项目已经提供批处理脚本，普通使用者不需要
手工组合依赖。

## 安装运行依赖

```powershell
git clone --branch v0.19.0 https://github.com/yang-fu-physics/openlab-control.git
cd openlab-control
.\setup.bat
.\run.bat
```

`requirements.txt` 与 `pyproject.toml` 声明核心直接依赖；`requirements-lock.txt` 锁定源码
运行和发布验证使用的完整精确版本。`setup.bat` 创建 `.venv` 并安装这份锁定文件。
`run.bat` 始终启动当前源码，不会误用 `dist/` 里的旧程序。

上面固定到与本网站一致的稳定版。准备参与核心开发时，才改为克隆或切换到 `main`。

## 配置仪表

`configs/general.toml` 是唯一通用配置，安装后可直接运行：

```powershell
.\run.bat
```

全新目录没有 System Instrument 面板也能启动，三个内置仿真默认关闭。需要面板或真实仪表
时，再运行：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

扫描器分别写入：

- `configs/visa.resources.toml`：未分配的 VISA，供 Measurement Module 选择；
- `configs/instruments/<instrument-id>.toml`：System 实例、面板角色、顺序与限制；
- `configs/pid/<instance-id>.toml`：只在第一次需要时从示例或选定文件复制。

这些现场文件已被 Git 忽略。扫描器保存会按最后一页的完整预览覆盖生成配置；现有 PID 文件
不会被覆盖或删除。详细步骤见[扫描并配置仪表](../guides/instrument-scanner.md)。

## 运行完整测试

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

要无界面运行包含温场命令的示例，先在 Instrument Scanner 中明确启用所需仿真，再运行
`run.py --headless-demo ...`。三个仿真不会随源码环境自动开启。

## 本地预览开发者网站

网站依赖与应用运行依赖分开保存，不会进入 Windows 发布包：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-docs.txt
.\.venv\Scripts\mkdocs.exe serve
```

打开终端给出的本地地址即可预览。提交前必须运行严格构建：

```powershell
.\.venv\Scripts\mkdocs.exe build --strict
```

## 推荐开发循环

1. 先为预期行为写一个不连接硬件的测试。
2. 修改最小范围代码。
3. 单独运行受影响测试。
4. 运行完整核心或模块测试。
5. 只有涉及真实仪表协议时，才进行低风险现场测试。

!!! danger "不要提交实验室秘密"

    仪表地址、生成的现场仪表配置、令牌、私钥、真实实验 DAT 和 `module_data/`
    都不应进入 Git。每次 Run 保存的 `runs/**/configuration/` 是本机通用配置、仪表实例、
    未分配 VISA 和 PID 的快照，也可能包含真实地址；分享或提交运行目录前必须检查并脱敏。
