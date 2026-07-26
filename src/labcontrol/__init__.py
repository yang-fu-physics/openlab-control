"""OpenLab Control 核心包。

核心包只定义框架、运行时和内置模拟设备；真实仪表驱动与 Measurement Module 通过清单式
插件加载。版本号同时用于源码运行、DAT 文件头和打包产物，发布时必须与 ``pyproject.toml``
保持一致。
"""

__version__ = "0.11.1"
