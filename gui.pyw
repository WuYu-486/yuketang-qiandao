# -*- coding: utf-8 -*-
"""双击启动图形界面（不会触发 Windows 对批处理文件的安全警告）。

和 gui.bat 等价：找到项目里的 .venv，用它的 pythonw.exe 启动 gui.py。
双击 gui.bat 时 Windows 可能提示「无法验证发布者」，双击本文件不会。

用法:
    双击 gui.pyw
    .venv\\Scripts\\pythonw.exe gui.pyw
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PYW = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
GUI = os.path.join(ROOT, "gui.py")


def _fatal(text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text), "雨课堂助手", 0x10)
    except Exception:
        pass


def main():
    if not os.path.exists(PYW):
        _fatal("没找到 .venv\\Scripts\\pythonw.exe。\n\n"
               "请先按 README 的「安装」一节创建虚拟环境并安装依赖，\n"
               "或改用 gui.bat（会给出同样的提示）。")
        return 1
    try:
        subprocess.Popen([PYW, GUI], cwd=ROOT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        _fatal(f"启动失败：{type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
