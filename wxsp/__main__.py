"""Nuitka 编译入口。等价于 `python -m wxsp`。

打包后 sys.modules['__main__'] 就是这个模块,Nuitka 会注入 __compiled__ = True,
config.is_packaged() 据此判断走 platformdirs 路径。
"""

from __future__ import annotations

from wxsp.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
