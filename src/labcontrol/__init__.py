"""OpenLab Control 核心包。

核心包只定义框架、运行时和内置模拟仪表；System Instrument 与 Measurement Module 分别
通过自己的清单加载。版本号同时用于源码运行、DAT 文件头和打包产物，发布时必须与 ``pyproject.toml``
保持一致。
"""

__version__ = "0.15.1"
