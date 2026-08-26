# 离线安装与发布

第一阶段不提供在线商店。Measurement Module 和 System Instrument 都以一个完整目录为安装
单元，可以通过 U 盘或内部文件服务复制；两者仍保持各自的名称和目录。

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

重启后，Measurement Module 会出现在 Modules Manager 中并保持 Disabled。复制 System
Instrument 目录后还要运行 Instrument Scanner：为它添加物理实例，确认固定面板、角色、
顺序和限制，再检查完整保存预览。单纯复制目录不会访问真实仪表。

扫描器把未分配 VISA 写入 `configs/visa.resources.toml`，把 System 实例写入
`configs/instruments/<instrument-id>.toml`。每种型号可有多个 `[[instances]]`；一个实例的
多个面板共用同一进程和连接。专用网络仪表的 Host/Port 由自己的模板字段提供。生成配置
保存时按预览全量覆盖；`configs/pid/<instance-id>.toml` 第一次建立后不会被扫描器覆盖或删除。

一个完全干净的发布目录可以没有 System 面板并正常启动，三个内置仿真也默认关闭。

## 所有扩展共用核心依赖

主框架统一提供并锁定 PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions。
System Instrument 与 Measurement Module 不声明、不安装第二套依赖，也不建立自己的 Python
runtime。

新实现确实需要其他第三方包时：

1. 把精确版本加入核心 `pyproject.toml` 和 `requirements-lock.txt`；
2. 在全新环境运行核心、System Instrument 和 Measurement Module 测试；
3. 由 GitHub Actions 重新构建完整 Windows 发布包。

这样发布 ZIP 本身就是完整离线环境，现场电脑不需要 Python 或联网安装。

## 发布前清单

- [ ] 核心稳定版使用与项目版本一致的 `v<版本>` 标签，由 GitHub Actions 的 Windows
      Runner 重新测试、打包和上传。
- [ ] 发布 ZIP 根目录有 `OpenLabControl.exe`、`InstrumentScanner.exe` 和唯一的
      `_internal/`，没有发布用 `tools/` 目录。
- [ ] 目录名、清单 ID 和文档一致。
- [ ] 版本号已更新，变化写入 README/CHANGELOG。
- [ ] 没有仪表地址、令牌、私钥、真实 DAT 或本机状态。
- [ ] 最小生命周期、异常状态、Pause/Stop、重复关闭测试通过。
- [ ] 真实仪表代码明确标注真机验证状态。
- [ ] 从一个全新的 OpenLab Control 文件夹完成手动复制安装测试。

## 第三方 Measurement Module

第三方目录应说明：

- 仪表组合、接线与固件要求；
- 数字状态码表；
- 默认设置和安全边界；
- 已验证与未验证的硬件；
- 自动测试和真机测试摘要。

System Instrument 通常随实验系统固定并由系统维护者部署。当前两类内容都只支持手动复制，
不提供在线索引或自动安装。
