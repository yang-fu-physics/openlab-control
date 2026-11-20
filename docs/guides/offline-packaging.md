# 离线安装与发布

OpenLab Control 面向常见处于内网或离线环境的实验室电脑，因此**不设在线插件商店**，所有扩展都以“复制整个文件夹”的方式离线安装。

## 1. 离线安装扩展（复制目录即可）

**安装测量模块（Measurement Module）**：

```text
openlab-control-Module/modules/<模块ID>/
        ↓ 整个目录复制到
OpenLabControl/modules/<模块ID>/
```

重启软件后，新模块会出现在 **Modules** 列表中（默认 Disabled）。

**安装系统仪表（System Instrument）**：

```text
openlab-control-Device/instruments/<仪表ID>/
        ↓ 整个目录复制到
OpenLabControl/system_instruments/<仪表ID>/
```

复制完成后，运行 **Instrument Scanner** 为该型号添加物理实例、配置连接与安全上下限并保存，再重启主程序。单纯复制目录不会接触任何真实硬件。

> 扫描器保存的结果写入 `configs/visa.resources.toml` 与 `configs/instruments/<仪表ID>.toml`；已存在的 `configs/pid/<实例ID>.toml` 不会被扫描器覆盖或删除。

## 2. 依赖统一打包，现场无需装 Python

框架共享并锁定以下五个核心包：

- `PySide6`
- `QtAwesome`
- `packaging`
- `PyVISA`
- `typing_extensions`

所有测量模块和系统仪表直接复用主框架环境，不各自建虚拟环境，也不在界面里临时装包。如果新驱动确实需要额外的第三方 Python 包，正确做法是：

1. 将精确版本号加入核心 `pyproject.toml` 和 `requirements-lock.txt`；
2. 在全新环境跑通核心、系统仪表和测量模块的全部测试；
3. 推送版本标签，由 GitHub Actions 重新打出完整离线的 Windows ZIP 发布包。

## 3. 正式版本发布自检清单

- [ ] `pyproject.toml` 版本号与 Git 标签 `v<版本>` 一致，由 GitHub Actions Windows Runner 自动测试、打包并发布。
- [ ] 发布 ZIP 根目录包含 `OpenLabControl.exe`、`InstrumentScanner.exe` 和唯一的 `_internal/`，不含 `tools/`。
- [ ] 目录名、清单 ID 与文档完全一致。
- [ ] 版本变更已写入 `CHANGELOG.md` 与 `README.md`。
- [ ] 包内没有任何本机仪表地址、Token、私钥、真实 DAT 或 `module_data/`。
- [ ] 生命周期、异常状态、Pause/Stop 与重复关闭测试全部通过。
- [ ] 驱动文档明确写清了实机验证状态（已验证范围 / Beta 未验证）。
- [ ] 在一个全新解压的发布文件夹中实测过“手动复制安装 → 启动运行”。

## 4. 分享第三方模块建议附带的内容

给同事或社区分享一个测量模块目录时，建议在目录 `README.md` 里写清：

1. 适用仪表型号、接线方式与固件版本要求；
2. `StatusCode` 数字状态码对照表；
3. 默认参数与硬件安全限制；
4. 哪些组合已经实机测过、哪些仍为 Beta；
5. 自动化测试与真机测试记录摘要。
