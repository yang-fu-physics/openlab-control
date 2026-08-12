# 离线安装与发布

第一阶段不提供在线商店。Measurement Module 和 System Instrument 都以一个完整目录为安装
单元，可以通过 U 盘或内部文件服务完全离线复制；两者仍保持各自的名称和目录。

## 手动安装

Measurement Module：

```text
repository/modules/<module-id>/
    ↓
OpenLabControl/modules/<module-id>/
```

System Instrument：

```text
repository/instruments/<instrument-id>/
    ↓
OpenLabControl/system_instruments/<instrument-id>/
```

重启后，框架显示类型、ID、版本、绝对路径和内容指纹。用户必须显式确认首次信任；任何
源码或 wheel 变化都会使原确认失效。

System Instrument 还必须由现场配置的 `[[instruments]].backend` 选中才会启动。单纯复制
目录不会连接或改变真实仪表。Measurement Module 则在 Modules Manager 中由用户 Enable。

## 不要重复声明框架依赖

主框架统一提供并锁定：

- PySide6
- QtAwesome
- packaging
- PyVISA
- typing_extensions

所有 System Instrument 和 Measurement Module 使用相同版本。不要携带自己的 PySide6 或
PyVISA 副本。

## 只有额外依赖才需要 wheels

确实需要框架没有的第三方库时，才在对应清单声明，并携带：

```text
my_module/
├─ module.toml
├─ backend.py
├─ requirements.lock
└─ wheels/
   └─ extra_package-1.2.3-py3-none-any.whl
```

`requirements.lock` 必须使用精确 `==` 版本和 SHA-256：

```text
extra-package==1.2.3 \
    --hash=sha256:<完整哈希>
```

安装固定使用 `--no-index --only-binary=:all: --require-hashes`，不存在联网回退。额外依赖
进入按“类型 + ID + 内容指纹”隔离的 `runtime_packages/`，不会覆盖框架包。

## 发布前清单

- [ ] 核心稳定版使用与项目版本一致的 `v<版本>` 标签，由 GitHub Actions 的 Windows
      Runner 重新测试、打包和上传；不把开发电脑生成的 ZIP 手工上传为正式资产。
- [ ] 发布 ZIP 根目录有 `OpenLabControl.exe`、`InstrumentScanner.exe` 和唯一的
      `_internal/`，没有发布用 `tools/` 目录。
- [ ] 目录名、清单 ID 和文档一致。
- [ ] 版本号已更新，变化写入 README/CHANGELOG。
- [ ] 没有仪表地址、令牌、私钥、真实 DAT 或本机状态。
- [ ] 所有额外 wheel 与目标 Windows/Python ABI 匹配。
- [ ] `requirements.lock` 能在断网环境重建 runtime。
- [ ] 最小生命周期、异常状态、Pause/Stop、重复关闭测试通过。
- [ ] 真实仪表代码明确标注真机验证状态。
- [ ] 从一个全新的 OpenLab Control 文件夹完成手动复制安装测试。

## 第三方 Measurement Module

未来可以让第三方自行发布 Measurement Module。现阶段建议每个模块目录说明：

- 仪表组合、接线与固件要求；
- 数字状态码表；
- 默认设置和安全边界；
- 已验证与未验证的硬件；
- 完整离线依赖；
- 自动测试和真机测试摘要。

System Instrument 通常随实验系统固定并由系统维护者部署。当前两类内容都只支持手动复制，
不提供在线索引或自动安装。
