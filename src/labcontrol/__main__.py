"""支持 ``python -m labcontrol`` 的最小入口。

实际启动、参数解析和退出码管理统一放在 :mod:`labcontrol.app`，避免控制台入口与模块入口
产生两套行为。
"""

from .app import main


if __name__ == "__main__":
    raise SystemExit(main())
