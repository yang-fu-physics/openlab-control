# 建立源码开发环境

源码开发需要 Python 3.11 或更高版本。Windows 项目已经提供批处理脚本，普通使用者不需要
手工组合依赖。

## 安装运行依赖

```powershell
git clone --branch v0.17.0 https://github.com/yang-fu-physics/openlab-control.git
cd openlab-control
.\setup.bat
.\run.bat
```

`setup.bat` 创建 `.venv`，并安装 `requirements-lock.txt` 中经过验证的应用依赖。
`run.bat` 始终启动当前源码，不会误用 `dist/` 里的旧程序。

上面固定到与本网站一致的稳定版。准备参与核心开发时，才改为克隆或切换到 `main`。

## 使用本机配置

默认配置只用于仿真。接入真实仪表时先创建不会进入 Git 的本机副本：

```powershell
Copy-Item .\configs\default.toml .\configs\site.local.toml
.\run.bat --config configs\site.local.toml
```

只在 `site.local.toml` 中填写仪表地址和现场安全范围。需要给团队一个起点时，提交删去真实
地址和秘密的 `site.example.toml`，不要提交本机文件。

## 运行完整测试

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe run.py --headless-demo --sequence examples\nested_scan.seq
```

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

    仪表地址、令牌、私钥、真实实验 DAT、`module_data/`、`trust_state/` 和本机 runtime
    都不应进入 Git。每次 Run 保存的 `runs/**/configuration.toml` 是本机配置的完整快照，
    也可能包含真实地址；分享或提交运行目录前必须检查并脱敏。
