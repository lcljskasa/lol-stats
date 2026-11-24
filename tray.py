"""系统托盘图标：显示主窗口 / 悬浮窗开关 / 退出。"""
import os

try:
    import pystray
    from PIL import Image
except ImportError:  # 没装 pystray 时不提供托盘，最小化按钮就普通最小化
    pystray = None


class Tray:
    def __init__(self, icon_path, on_show, on_toggle_overlay, overlay_on, on_quit):
        self.icon = None
        if pystray is None or not os.path.exists(icon_path):
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda: on_show(), default=True),  # 单击托盘图标也是这个
            pystray.MenuItem("游戏内悬浮窗", lambda: on_toggle_overlay(), checked=lambda _: overlay_on()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: on_quit()),
        )
        self.icon = pystray.Icon("LOLStats", Image.open(icon_path), "LOL 战绩速览", menu)
        self._told = False

    @property
    def ok(self):
        return self.icon is not None

    def start(self):
        if self.icon:
            self.icon.run_detached()

    def tell_once(self):
        """第一次缩到托盘时提示一下，免得以为程序关了。"""
        if self.icon and not self._told:
            self._told = True
            try:
                self.icon.notify("程序在右下角托盘里继续运行，进入选英雄时会自动弹出", "LOL 战绩速览")
            except Exception:
                pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
