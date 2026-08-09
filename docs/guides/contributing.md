# 参与核心开发

扩展作者通常不需要修改核心。只有两个以上独立扩展都无法用现有公共接口安全实现时，才
考虑增加核心能力。

## 修改原则

- 保持 GUI、Runtime、设备和文件写入边界。
- 不在插件导入或构造阶段执行 I/O。
- 不从 GUI 直接访问 Device Plugin 或 Measurement Module 后端。
- 新增 SEQ 语法时同时更新模型、解析、格式化、执行、参数窗口和往返测试。
- 新增告警使用稳定的 source/code/context。
- 所有安全限制必须配置化并有边界测试。
- 优先缩小公共接口，不把单个仪表的特殊流程加入核心。

## 提交前

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe run.py --headless-demo --timeout 120
.\.venv\Scripts\mkdocs.exe build --strict
```

同时更新：

- `CHANGELOG.md`；
- 受影响网页和技术参考；
- 扩展版本与支持矩阵；
- 新增或改变的安全测试。

## 文档发布

- 当前只保留一个稳定站点，不显示开发版入口。
- 主分支中的文档变更通过检查后，直接更新稳定站点。
- 将来确实需要同时维护多个版本时，再增加版本选择功能。

本地网页构建产物 `site/` 不提交 Git；GitHub Pages 的生成文件由自动工作流维护在
`gh-pages` 分支。

## 真实设备改动

提交代码测试之外，还必须说明：

- 使用的完整仪表型号、选件和固件；
- 低风险验证覆盖的命令顺序和状态；
- 未验证路径；
- timeout、断线、Stop/Error 和硬件联锁结果。

公开 issue、日志和测试数据必须脱敏。
