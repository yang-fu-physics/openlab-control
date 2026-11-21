# 参与核心开发

日常接入新仪表或新测量方法，编写独立的 **Measurement Module** 或 **System Instrument** 即可，不需要改核心源码。只有当两个以上独立驱动都无法通过现有公共接口安全完成需求时，才考虑扩展核心框架。

## 核心架构守则

- **严格分层**：保持 GUI、Runtime 线程、独立子进程驱动与 `DatRunLogger` 单一写手之间的边界；不要从 GUI 直接操作仪表或模块后端。
- **零隐式 I/O**：扫描发现、代码导入和对象构造阶段绝不连接硬件。
- **语法完整闭环**：新增 SEQ 指令时，同步更新数据模型、解析器、格式化器、执行器、参数弹窗和往返测试。
- **稳定事件键**：新增报警统一使用稳定的 `source / code / context`，安全限制必须可配置并附带边界测试。
- **保持核心克制**：不要把某一台特定型号的专有流程写死进核心。

## 提交代码前必跑检查

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe run.py --headless-demo --timeout 120
.\.venv\Scripts\mkdocs.exe build --strict
```

如有行为变化，请同步更新：

- `CHANGELOG.md`；
- 受影响的使用文档与技术参考；
- 对应驱动/模块的兼容版本与测试记录。

## 文档站点发布规则

- 当前只维护一个稳定版在线文档站点，不对外暴露开发版入口；
- 合并到 `main` 的文档修改在通过严格构建检查后会自动发布到 `gh-pages`；
- 本地构建生成的 `site/` 目录不要提交到 Git。

## 涉及真实硬件的提交说明

除自动化单元测试外，请在 PR 或提交说明里写清：

1. 测试所用的完整仪表型号、选件与固件版本；
2. 覆盖了哪些低风险命令顺序与状态；
3. 哪些路径尚未在真机上验证；
4. 超时、断线、Stop/Error 及硬件联锁的实际表现（公开日志请先脱敏）。
