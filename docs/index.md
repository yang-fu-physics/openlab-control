---
hide:
  - navigation
  - toc
---

<section class="olc-hero">
  <div>
    <div class="olc-eyebrow">OpenLab Control · Developer Guide</div>
    <h1>让控制核心保持小，让测量方案自由生长。</h1>
    <p class="olc-hero-copy">
      面向低温、磁场与电输运实验的中文开发教程。从一个可运行的四通道模块开始，逐步加入
      设置窗口、自定义 SEQ 指令、仪表驱动、离线安装和真实硬件安全测试。
    </p>
    <div class="olc-actions">
      <a class="md-button md-button--primary" href="getting-started/">开始使用</a>
      <a class="md-button" href="development/first-module/">开发第一个模块</a>
      <a class="md-button" href="reference/public-api/">查看公共 API</a>
    </div>
  </div>
  <div class="olc-terminal" aria-label="Measurement Module 最小接口示例">
    <pre><code><span class="olc-keyword">class</span> Module:
    columns = {<span class="olc-string">"Resistance"</span>: <span class="olc-string">"Ohm"</span>}

    <span class="olc-keyword">def</span> <span class="olc-method">open</span>(self, api):
        self.instrument = open_instrument()

    <span class="olc-keyword">def</span> <span class="olc-method">measure</span>(self, slot, api):
        api.checkpoint()
        <span class="olc-keyword">return</span> {<span class="olc-string">"Resistance"</span>: self.read()}

    <span class="olc-keyword">def</span> <span class="olc-method">close</span>(self, api):
        self.instrument.close()</code></pre>
  </div>
</section>

<div class="olc-statbar">
  <div class="olc-stat"><strong>3 个</strong><span>必需后端方法</span></div>
  <div class="olc-stat"><strong>1 个</strong><span>扩展一个独立进程</span></div>
  <div class="olc-stat"><strong>0 次</strong><span>Frontend 直接仪表 I/O</span></div>
  <div class="olc-stat"><strong>3 层</strong><span>核心、插件、硬件安全边界</span></div>
</div>

## 从能运行的例子开始

<p class="olc-section-lead">
教程按一次真实开发的顺序组织。每一章都有明确产物、可复制代码和验证方法，不要求作者先理解
整个核心实现。
</p>

<div class="olc-card-grid">
  <a class="olc-card" href="development/first-module/">
    <span class="olc-card-number">01 / MODULE</span>
    <h3>写出第一个模块</h3>
    <p>从 module.toml 和 backend.py 开始，用 open、measure、close 完成最小生命周期。</p>
  </a>
  <a class="olc-card" href="development/results-and-slots/">
    <span class="olc-card-number">02 / DATA</span>
    <h3>正确返回四通道数据</h3>
    <p>理解 slot、稀疏行、数字状态码、Warning、Error、rawdata 与动态 DAT 列。</p>
  </a>
  <a class="olc-card" href="development/sequence-commands/">
    <span class="olc-card-number">03 / SEQUENCE</span>
    <h3>给模块增加 SEQ 指令</h3>
    <p>Enable 后动态注册普通动作和可任意嵌套的扫描，无需修改核心解析器。</p>
  </a>
  <a class="olc-card" href="development/instrument-drivers/">
    <span class="olc-card-number">04 / DRIVER</span>
    <h3>隔离底层仪表命令</h3>
    <p>每台仪表一个文件，backend.py 只编排连接、配置、测量和安全收尾。</p>
  </a>
  <a class="olc-card" href="development/device-plugin/">
    <span class="olc-card-number">05 / DEVICE</span>
    <h3>接入温度和磁场设备</h3>
    <p>实现统一的异步 Device Plugin，让核心继续负责上下限、角色、恢复和 Hold。</p>
  </a>
  <a class="olc-card" href="guides/safety-checklist/">
    <span class="olc-card-number">06 / SAFETY</span>
    <h3>通过真实仪表安全门</h3>
    <p>检查身份、量程、超时、写入歧义、联锁、Stop/Error 和人工急停。</p>
  </a>
</div>

## 核心只负责共同问题

OpenLab Control 不控制 PPMS 本体，也不假设所有实验仪表共享一套界面或命令。核心统一
处理 SEQ、进程隔离、数据写入、错误语义和温场安全限制；扩展作者决定自己的协议、测量
时序、状态码和设置窗口。

<div class="olc-screenshot">
  <img src="main-window-preview.png" alt="OpenLab Control 主窗口与 Sequence Command Bar" loading="lazy">
</div>

!!! warning "真实仪表还需要现场验证"

    示例和自动测试不能替代仪表本机限流、限压、限温、磁体保护、硬件联锁和人工急停。
    未完成低风险真机验证的扩展必须保持 Beta，并禁止无人值守运行。

<div class="olc-next">
  <a href="getting-started/">下一步：五分钟运行仿真 →</a>
  <a href="development/">已经熟悉程序：直接理解扩展边界 →</a>
</div>
