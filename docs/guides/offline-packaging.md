# 离线安装与发布扩展

第一阶段不提供在线插件商店。一个扩展目录就是最小安装单元，可以通过 U 盘或内部文件服务
完全离线复制。

## 手动安装

Measurement Module：

```text
repository/modules/<module-id>/
    ↓
OpenLabControl/modules/<module-id>/
```

Device Plugin：

```text
repository/plugins/<plugin-id>/
    ↓
OpenLabControl/device_plugins/<plugin-id>/
```

重启后，框架显示类型、ID、版本、绝对路径和内容指纹。用户必须显式确认首次信任；任何
源码或 wheel 变化都会使旧信任失效。

## 不要重复声明框架依赖

主框架统一提供并锁定：

- PySide6
- QtAwesome
- packaging
- PyVISA
- typing_extensions

所有模块使用相同版本。不要在扩展中再次安装这些包，也不要携带自己的 PySide6 或
PyVISA 副本。

## 只有额外依赖才需要 wheels

如果扩展确实需要框架没有的第三方库，才在清单声明，并携带：

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
进入按扩展内容指纹隔离的 `plugin_runtime/`，不会覆盖主框架包。

## 发布前清单

- [ ] 目录名和稳定扩展 ID 一致。
- [ ] 版本号已更新，变更内容写入 README/CHANGELOG。
- [ ] 没有仪表地址、令牌、私钥、真实 DAT 或本机状态。
- [ ] 所有额外 wheel 与目标 Windows/Python ABI 匹配。
- [ ] requirements.lock 能在断网环境重建 runtime。
- [ ] 最小生命周期、异常状态、Pause/Stop、重复 close 测试通过。
- [ ] 真实仪表模块仍明确标注验证状态。
- [ ] 从一个全新的 OpenLab Control 文件夹完成复制安装测试。

## 第三方发布约定

未来允许第三方自行发布和安装 Measurement Module。现阶段建议每个扩展目录包含：

- 清晰的仪表组合与固件要求；
- 状态码表；
- 默认设置和安全边界；
- 已验证/未验证的硬件列表；
- 完整离线依赖；
- 自动测试和真机测试记录摘要。

插件索引和在线安装暂时只保留设计入口；手动复制仍是唯一正式安装路径。
