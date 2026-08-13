import atexit
import shutil
import tkinter as tk
from pathlib import Path

from ui.ping_window import PingApp


def move_debug_log():
    """将工作目录下的 debug.log 移到 log/ 目录（输入法等第三方产生的日志）"""
    src = Path("debug.log")
    if not src.exists():
        return

    log_dir = Path("log")
    log_dir.mkdir(exist_ok=True)

    dst = log_dir / "debug.log"
    try:
        if dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))
    except Exception:
        pass


def main():

    move_debug_log()

    root = tk.Tk()

    app = PingApp(root)

    atexit.register(move_debug_log)

    root.mainloop()


if __name__ == "__main__":
    main()
