"""LOL 战绩速览：后台盯着客户端，界面用 Edge 独立窗口打开本地网页。运行：python app.py"""
import ctypes
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import re
import urllib.request

import live
import opgg
from assets import _CTX as ASSET_CTX
from tray import Tray
from assets import ASSETS
from lcu import HERE, LCU, RES, NotConnected, POSITIONS

VERSION = "1.3.1"
REPO = "lcljskasa/lol-stats"
TITLE = "LOL 战绩速览"
APP_TOKEN = secrets.token_urlsafe(16)  # 只有我们自己的页面知道，挡住其他网页调用本地接口
SETTINGS = os.path.join(HERE, "settings.json")
PHASES = {"None": "在大厅", "Lobby": "在房间里", "Matchmaking": "排队中", "ReadyCheck": "找到对局",
          "ChampSelect": "选英雄中", "InProgress": "游戏中", "GameStart": "游戏加载中",
          "WaitingForStats": "等待结算", "EndOfGame": "对局结束", "PreEndOfGame": "对局结束"}


GAMES = 1000  # 每人查多少场
INSTANCE = os.path.join(HERE, "instance.json")  # 正在运行的那个程序的端口和令牌


def migrate_old_settings():
    """老版本把设置放在 exe 旁边：搬过来一次，并删掉 exe 旁边留下的旧文件夹。"""
    if not getattr(sys, "frozen", False):
        return
    old_dir = os.path.dirname(sys.executable)
    old = os.path.join(old_dir, "settings.json")
    try:
        if os.path.exists(old) and not os.path.exists(SETTINGS):
            shutil.move(old, SETTINGS)
        for name in ("cache", ".window"):
            p = os.path.join(old_dir, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass


def single_instance():
    """只允许开一个。已经开着的话，让它把主窗口显示出来，然后自己退出。"""
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX = k32.CreateMutexW(None, False, "Local\\LOLStatsSingleInstance")  # 留着句柄，程序退出时系统自动释放
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return
    try:
        with open(INSTANCE, encoding="utf-8") as f:
            info = json.load(f)
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/show", data=b"{}", method="POST",
                                     headers={"X-App-Token": info["token"]})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass
    sys.exit(0)


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def check_update():
    """启动后在后台看一下 GitHub 上有没有新版本，有的话在界面上提示。失败就算了，不打扰用户。"""
    time.sleep(5)
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15, context=ASSET_CTX) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except Exception:
        return
    latest = rel.get("tag_name") or ""
    if parse_version(latest) > parse_version(VERSION):
        STATE.update(update_info={"version": latest.lstrip("v"), "url": rel.get("html_url") or f"https://github.com/{REPO}/releases/latest",
                             "notes": (rel.get("body") or "").split("\n")[0][:80]})


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    # 场次固定 1000（客户端存不了那么多时，有多少显示多少）
    return {"topmost": True, "overlay": True, "autoPop": True, **saved, "games": GAMES}


class State:
    """界面看到的全部数据。后台线程改它，网页每秒来取一次。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.settings = load_settings()
        self.connected = False
        self.status = "正在连接客户端…"
        self.phase = ""
        self.view = "idle"           # idle / team / self
        self.players = []            # 每个元素：{key, pos, champ, me, loading, ...战绩}
        self.pick = {}               # 选英雄时：{champ, pos, enemies:[英雄id]}
        self.update_info = None
        self.version = 0

    def update(self, **kw):
        """只有内容真的变了才加版本号，界面看到版本号变了才会重画。"""
        with self.lock:
            changed = False
            for k, v in kw.items():
                if getattr(self, k) != v:
                    setattr(self, k, v)
                    changed = True
                elif isinstance(v, (list, dict)):
                    # 内容一样也换成新对象（load_players 靠对象身份判断结果是否有效），但不用通知界面重画
                    setattr(self, k, v)
            if changed:
                self.version += 1

    def snapshot(self):
        with self.lock:
            return {"connected": self.connected, "status": self.status, "phase": self.phase,
                    "view": self.view, "players": self.players, "settings": self.settings, "pick": self.pick,
                    "appVersion": VERSION, "update": self.update_info,
                    "version": self.version}


class Worker(threading.Thread):
    def __init__(self, state):
        super().__init__(daemon=True)
        self.s = state
        self.lcu = None
        self.lobby_key = None
        self.jobs = ThreadPoolExecutor(max_workers=5, thread_name_prefix="player")
        self.want_self = threading.Event()
        self.want_team = threading.Event()
        self.want_refresh = threading.Event()
        self.self_mode = False   # 选英雄时用户切到了“我的战绩”
        self.game_key = None     # 当前对局双方名单，避免每秒重复查
        self.raw_phase = ""      # 客户端原始的阶段名（InProgress 等）

    def run(self):
        while True:
            try:
                if self.lcu is None:
                    self.lcu = LCU()
                    self.lcu.champions()
                    ASSETS.set_lcu(self.lcu)
                    self.s.update(connected=True, status="已连接客户端")
                self.tick()
            except NotConnected as e:
                self.lcu, self.lobby_key = None, None
                ASSETS.set_lcu(None)
                self.s.update(connected=False, status=str(e), phase="")
                time.sleep(3)
            except Exception as e:  # 接口格式变了之类的意外，别让线程退出
                self.s.update(status=f"出错了：{e}")
            time.sleep(1)

    # 并发查询一批玩家；每查完一个就更新一次界面
    def load_players(self, entries, view):
        want = self.s.settings["games"]
        players = [{**e, "loading": True} for e in entries]
        self.s.update(view=view, players=players)
        started = time.time()

        def one(i, e):
            try:
                data = self.lcu.player(want, puuid=e.get("puuid") or None, summoner_id=e.get("summonerId") or None)
            except Exception as err:
                data = {"name": "（查询失败）", "error": str(err)}
            with self.s.lock:
                if self.s.players is players:
                    players[i] = {**players[i], **data, "loading": False}
                    self.s.version += 1

        futures = [self.jobs.submit(one, i, e) for i, e in enumerate(entries)]
        for f in futures:
            f.result()
        self.s.update(status=f"已加载 {len(entries)} 人 × {want} 场，用时 {time.time() - started:.1f} 秒")

    def load_game(self):
        """进入加载界面后，双方 10 人的身份是公开的：读当前对局名单，查出双方战绩。
        （选英雄阶段 Riot 会隐藏对面身份，我们不去绕过。）返回 True 表示这一轮已经处理。"""
        sess = self.lcu.get("/lol-gameflow/v1/session") or {}
        gd = sess.get("gameData") or {}
        one, two = gd.get("teamOne") or [], gd.get("teamTwo") or []
        if not one or not two:
            return False
        me = (self.lcu.me() or {}).get("puuid")
        mine, theirs = (one, two) if any(p.get("puuid") == me for p in one) else (two, one)
        key = tuple(str(p.get("puuid") or p.get("summonerId")) for p in mine + theirs)
        if key == self.game_key:
            return False  # 同一局：交给下面的通用逻辑（处理“重新查询”等）
        self.game_key = key
        entries = [{
            "key": str(p.get("puuid") or p.get("summonerId")), "puuid": p.get("puuid") or None,
            "summonerId": p.get("summonerId") or None,
            "pos": POSITIONS.get((p.get("selectedPosition") or "").lower(), ""),
            "champ": p.get("championId") or None, "me": p.get("puuid") == me, "enemy": side == "enemy",
        } for side, team in (("mine", mine), ("enemy", theirs)) for p in team]
        self.s.update(status="对局开始，正在查询双方 10 人…")
        self.load_players(entries, "team")
        return True

    def tick(self):
        phase = self.lcu.phase() or ""
        self.raw_phase = phase
        self.s.update(phase=PHASES.get(phase, phase))

        if self.want_team.is_set():
            self.want_team.clear()
            self.self_mode, self.lobby_key = False, None
        if self.want_self.is_set():
            self.want_self.clear()
            self.self_mode = True
            me = self.lcu.me() or {}
            entry = {"key": me.get("puuid"), "puuid": me.get("puuid"), "summonerId": me.get("summonerId"),
                     "pos": "", "champ": None, "me": True}
            self.load_players([entry], "self")
            return

        if phase in ("GameStart", "InProgress", "Reconnect") and not self.self_mode and self.load_game():
            return
        if phase == "ChampSelect":
            self.game_key = None

        if phase != "ChampSelect":
            if self.want_refresh.is_set():
                self.want_refresh.clear()
                with self.s.lock:
                    entries = [{k: p.get(k) for k in ("key", "puuid", "summonerId", "pos", "champ", "me", "enemy")}
                               for p in self.s.players]
                    view = self.s.view
                if entries:
                    self.load_players(entries, view)
            self.lobby_key = None
            self.s.update(pick={})
            return

        session = self.lcu.champ_select() or {}
        team = session.get("myTeam") or []
        if not team:  # 刚进选英雄时房间信息还没准备好，等下一轮
            return
        me_cell = session.get("localPlayerCellId")
        entries = [{
            "key": str(m.get("puuid") or m.get("summonerId") or m.get("cellId")),
            "puuid": m.get("puuid"), "summonerId": m.get("summonerId"),
            "pos": POSITIONS.get((m.get("assignedPosition") or "").lower(), ""),
            "pos_raw": (m.get("assignedPosition") or "").lower() or None,
            "champ": m.get("championId") or m.get("championPickIntent") or None,
            "me": m.get("cellId") == me_cell,
        } for m in team]
        # 对面身份本来就公开的人（部分非排位模式）也一起查；排位里被隐藏的对面玩家一律跳过，不去还原
        for m in session.get("theirTeam") or []:
            puuid = m.get("puuid") or ""
            if m.get("nameVisibilityType") == "HIDDEN" or not puuid.strip("0-"):
                continue
            entries.append({"key": puuid, "puuid": puuid, "summonerId": m.get("summonerId") or None,
                            "pos": POSITIONS.get((m.get("assignedPosition") or "").lower(), ""),
                            "pos_raw": (m.get("assignedPosition") or "").lower() or None,
                            "champ": m.get("championId") or m.get("championPickIntent") or None,
                            "me": False, "enemy": True})
        key = tuple(e["key"] for e in entries)
        me = next((e for e in entries if e["me"]), {})
        enemies = [m.get("championId") for m in session.get("theirTeam") or [] if m.get("championId")]
        if key != self.lobby_key:  # 新的一局：看一下是不是大乱斗（每局只查一次）
            sess = self.lcu.get("/lol-gameflow/v1/session") or {}
            self.aram = ((sess.get("gameData") or {}).get("queue") or {}).get("gameMode") == "ARAM"
        self.s.update(pick={"champ": me.get("champ"), "pos": me.get("pos_raw"), "enemies": enemies,
                            "aram": bool(getattr(self, "aram", False))})

        if key != self.lobby_key:
            self.self_mode = False
        elif self.self_mode:
            if self.want_refresh.is_set():   # 自己页面的刷新
                self.want_refresh.clear()
                self.want_self.set()
            return
        if key != self.lobby_key or self.want_refresh.is_set():
            self.want_refresh.clear()
            self.lobby_key = key
            bring_to_front()
            self.s.update(status=f"进入选英雄，正在查询 {len(entries)} 位队友…")
            self.load_players(entries, "team")
            return

        # 同一局里只更新正在选的英雄
        with self.s.lock:
            changed = False
            for p, e in zip(self.s.players, entries):
                if p.get("champ") != e["champ"]:
                    p["champ"], changed = e["champ"], True
            if changed:
                self.s.version += 1


# ---------- 窗口控制（Windows API） ----------
import ctypes.wintypes as _wt

_u32 = ctypes.WinDLL("user32")  # 独立的一份：下面的参数类型声明不会影响 pywebview 自己的调用
# 64 位系统上要写明参数类型，否则 HWND_TOPMOST(-1) 这类句柄会被截断，调用直接失败
_u32.SetWindowPos.argtypes = [_wt.HWND, _wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u32.SetWindowPos.restype = _wt.BOOL
_u32.ShowWindow.argtypes = [_wt.HWND, ctypes.c_int]
_u32.GetWindowLongW.argtypes = [_wt.HWND, ctypes.c_int]
_u32.SetWindowLongW.argtypes = [_wt.HWND, ctypes.c_int, ctypes.c_long]
_u32.GetWindowRect.argtypes = [_wt.HWND, ctypes.POINTER(_wt.RECT)]
_u32.SetForegroundWindow.argtypes = [_wt.HWND]
_u32.IsIconic.argtypes = [_wt.HWND]
_u32.GetDpiForWindow.argtypes = [_wt.HWND]
HWND_TOPMOST, HWND_NOTOPMOST = _wt.HWND(-1), _wt.HWND(-2)
def _find_window():
    user32 = _u32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == TITLE and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


WINDOW = None  # pywebview 窗口对象（用 Edge 时为 None）


def _set_z(hwnd, on):
    _u32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def set_topmost(on):
    if WINDOW is not None:
        WINDOW.on_top = on
        return
    hwnd = _find_window()
    if hwnd:
        _set_z(hwnd, on)


def bring_to_front():
    """进入选英雄时把窗口弹到最前面。Windows 不让后台程序直接抢焦点，所以先临时置顶再恢复。"""
    if MAIN.hidden:
        MAIN.show()
        time.sleep(0.4)
    hwnd = _find_window()
    if hwnd:
        if _u32.IsIconic(hwnd):  # 只在最小化时还原，最大化的窗口保持原样
            _u32.ShowWindow(hwnd, 9)
        _set_z(hwnd, True)
        _u32.SetForegroundWindow(hwnd)
        if not STATE.settings.get("topmost", True):
            _set_z(hwnd, False)


# ---------- 主窗口：缩到托盘 / 从托盘显示 ----------
class MainWindow:
    """缩到托盘时把页面换成空白页，网页内核占的内存就能释放掉；显示时再把界面加载回来。"""

    def __init__(self):
        self.hidden = False
        self.url = None
        self.tray = None

    def to_tray(self):
        if WINDOW is None:
            return
        if not (self.tray and self.tray.ok):  # 没有托盘就普通最小化
            WINDOW.minimize()
            return
        WINDOW.hide()
        self.hidden = True
        WINDOW.load_html("<body style='background:#1b1b20'></body>")
        self.tray.tell_once()

    def show(self):
        if WINDOW is None:
            return
        if self.hidden:
            self.hidden = False
            WINDOW.load_url(self.url)
        WINDOW.show()
        WINDOW.restore()
        hwnd = _find_window()
        if hwnd:
            _set_z(hwnd, True)
            _u32.SetForegroundWindow(hwnd)
            if not STATE.settings.get("topmost", True):
                _set_z(hwnd, False)

    def quit(self):
        if self.tray:
            self.tray.stop()
        os._exit(0)


MAIN = MainWindow()


def save_settings():
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(STATE.settings, f)


def toggle_auto_pop():
    with STATE.lock:
        STATE.settings["autoPop"] = not STATE.settings.get("autoPop", True)
        STATE.version += 1
    save_settings()


def toggle_overlay():
    with STATE.lock:
        STATE.settings["overlay"] = not STATE.settings.get("overlay", True)
        LIVE.dismissed = False
        STATE.version += 1
    save_settings()


def ensure_desktop_shortcut():
    """桌面快捷方式：第一次运行时创建；之后每次启动检查，exe 换了位置就把快捷方式改指过去。
    用户自己删掉了快捷方式，就不再自动加回来。"""
    if not getattr(sys, "frozen", False):
        return
    exe = sys.executable.replace("'", "''")
    folder = os.path.dirname(sys.executable).replace("'", "''")
    allow_create = "$false" if STATE.settings.get("shortcutCreated") else "$true"
    ps = ("$p=Join-Path ([Environment]::GetFolderPath('Desktop')) 'LOL战绩速览.lnk'; $sh=New-Object -ComObject WScript.Shell; "
          f"$exe='{exe}'; "
          "if (Test-Path $p) { $s=$sh.CreateShortcut($p); if ($s.TargetPath -ne $exe) { $s.TargetPath=$exe; "
          f"$s.WorkingDirectory='{folder}'; $s.IconLocation=$exe+',0'; $s.Save(); 'updated' }} else {{ 'ok' }} }} "
          f"elseif ({allow_create}) {{ $s=$sh.CreateShortcut($p); $s.TargetPath=$exe; $s.WorkingDirectory='{folder}'; "
          "$s.IconLocation=$exe+',0'; $s.Description='LOL 战绩速览'; $s.Save(); 'created' } else { 'skip' }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not STATE.settings.get("shortcutCreated"):
            STATE.settings["shortcutCreated"] = True
            save_settings()
    except Exception as e:
        print("创建桌面快捷方式失败：", e, file=sys.stderr)


# ---------- 游戏内悬浮窗 ----------
OVERLAY_TITLE = "战绩速览 · 悬浮窗"
OVERLAY = None  # pywebview 窗口对象
OVERLAY_SIZE = (340, 660)


def _find_any(title):
    """按标题找窗口（包括隐藏的）。"""
    user32, found = _u32, []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == title:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


OFFSCREEN = -30000


class Overlay:
    """悬浮窗的显示 / 隐藏都用 Windows API 做，并且不激活窗口，避免把游戏切到后台。

    “隐藏”其实是把窗口挪到屏幕外：如果真的隐藏窗口，网页内核会以为窗口不可见而停止绘制，再显示时就是一片空白。"""

    def __init__(self):
        self.visible = False
        self.ready = False       # 启动时先藏好，之后才允许显示
        self.collapsed = False
        self.pos = None          # 上次的位置（物理像素）
        self.styled = False

    def hwnd(self):
        return _find_any(OVERLAY_TITLE)

    def _scale(self, hwnd):
        try:
            return _u32.GetDpiForWindow(hwnd) / 96
        except Exception:
            return 1

    def _style(self, hwnd):
        if self.styled:
            return
        u = _u32
        ex = u.GetWindowLongW(hwnd, -20)
        # WS_EX_TOOLWINDOW：不出现在任务栏和 Alt+Tab；WS_EX_NOACTIVATE：点它也不会抢走游戏的焦点
        u.SetWindowLongW(hwnd, -20, ex | 0x80 | 0x08000000)
        self.styled = True

    def show(self):
        h = self.hwnd()
        if not h or not self.ready:
            return
        self._style(h)
        u = _u32
        sc = self._scale(h)
        w, hh = int(OVERLAY_SIZE[0] * sc), int((52 if self.collapsed else OVERLAY_SIZE[1]) * sc)
        if not self.pos:
            self.pos = (u.GetSystemMetrics(0) - w - int(24 * sc), int(110 * sc))
        # 置顶，带位置和大小，SWP_NOACTIVATE | SWP_SHOWWINDOW
        if not u.SetWindowPos(h, HWND_TOPMOST, self.pos[0], self.pos[1], w, hh, 0x0010 | 0x0040):
            raise OSError(f"SetWindowPos 失败：{ctypes.GetLastError()}")
        self.visible = True

    def hide(self):
        h = self.hwnd()
        if not h or not self.visible:
            return
        import ctypes.wintypes as wt
        r = wt.RECT()
        _u32.GetWindowRect(h, ctypes.byref(r))
        if r.left > OFFSCREEN // 2:
            self.pos = (r.left, r.top)
        # 挪到屏幕外：SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
        _u32.SetWindowPos(h, None, OFFSCREEN, OFFSCREEN, 0, 0, 0x0001 | 0x0004 | 0x0010)
        self.visible = False

    def set_collapsed(self, on):
        self.collapsed = on
        if self.visible:
            h = self.hwnd()
            import ctypes.wintypes as wt
            r = wt.RECT()
            _u32.GetWindowRect(h, ctypes.byref(r))
            self.pos = (r.left, r.top)
            self.show()


OV = Overlay()


BLANK = "<body style='margin:0;background:#1b1b20'></body>"


class LiveWatcher(threading.Thread):
    """每秒读一次游戏内实时数据；游戏开着就显示悬浮窗，关了就隐藏。
    不在游戏里时悬浮窗换成空白页（省内存、不再每秒刷新），进游戏才加载真正的页面。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.data = {"active": False}
        self.dismissed = False   # 这一局里用户手动关掉了悬浮窗
        self.url = None          # 悬浮窗页面地址
        self.page = False        # 悬浮窗现在是不是真正的页面（不是空白页）
        self.popped = False      # 这一局的加载界面，主窗口是不是我们自动弹出来的
        self.tucked = False      # 游戏开始后是不是已经自动收回托盘

    def run(self):
        while True:
            try:
                raw = live.fetch()
                self.data = live.summarize(raw) if raw else {"active": False}
            except Exception as e:
                self.data = {"active": False, "error": str(e)}
            if not self.data.get("active"):
                self.dismissed = False
            self.auto_pop()
            want = self.data.get("active") and STATE.settings.get("overlay", True) and not self.dismissed
            if OVERLAY is not None and OV.ready:
                try:
                    if want and not self.page:      # 进游戏：先加载页面，等它画好再挪进屏幕
                        OVERLAY.load_url(self.url)
                        self.page = True
                        time.sleep(1.2)
                    if want and not OV.visible:
                        OV.show()
                    if not want and OV.visible:
                        OV.hide()
                    if not self.data.get("active") and self.page and not OV.visible:  # 对局结束：卸载页面
                        OVERLAY.load_html(BLANK)
                        self.page = False
                except Exception as e:
                    print("悬浮窗出错：", e, file=sys.stderr)
            time.sleep(1)


    def auto_pop(self):
        """加载界面：客户端已经是“游戏中”，但还读不到游戏内实时数据（游戏正式开始后才有）。
        这时把主窗口弹到最前面看双方 10 人；游戏一开始，如果窗口是我们弹出来的，就收回托盘交给悬浮窗。"""
        in_game = WORKER.raw_phase in ("GameStart", "InProgress", "Reconnect")
        active = bool(self.data.get("active"))
        if not in_game:
            self.popped = self.tucked = False
            return
        if WINDOW is None or not STATE.settings.get("autoPop", True):
            return
        try:
            if not active and not self.popped:          # 进入加载界面
                self.popped = True
                bring_to_front()
            elif active and self.popped and not self.tucked:  # 游戏正式开始
                self.tucked = True
                MAIN.to_tray()
            elif active and not self.popped:            # 中途重连 / 程序在游戏中才打开：什么都不做
                self.popped = self.tucked = True
        except Exception as e:
            print("自动弹出出错：", e, file=sys.stderr)


LIVE = LiveWatcher()


# ---------- 本地网页服务 ----------
STATE = State()
WORKER = Worker(STATE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if os.environ.get("LOLSTATS_DEBUG"):  # 调试时打印每个请求
            print(self.headers.get("Host"), fmt % a, file=sys.stderr, flush=True)

    def send(self, body, ctype="application/json; charset=utf-8", code=200, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # 页面被关掉 / 卸载时浏览器中断了请求，正常情况

    def trusted(self):
        """只接受发给 127.0.0.1 的请求（防 DNS 重绑定），接口还要带上启动时生成的令牌（防其他网页调用）。"""
        host_ok = self.headers.get("Host") == f"127.0.0.1:{self.server.server_address[1]}"
        return host_ok and (not self.path.startswith("/api/") or self.headers.get("X-App-Token") == APP_TOKEN)

    def do_GET(self):
        if not self.trusted():
            return self.send(b"{}", code=403)
        if self.path in ("/", "/index.html", "/overlay"):
            name = "overlay.html" if self.path == "/overlay" else "index.html"
            with open(os.path.join(RES, "web", name), "rb") as f:
                page = f.read().replace(b"__APP_TOKEN__", APP_TOKEN.encode())
            return self.send(page, "text/html; charset=utf-8")
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        route = url.path
        if route == "/api/state":
            data = STATE.snapshot()
            if q.get("v") == str(data["version"]):
                data.pop("players")
            data["native"] = WINDOW is not None
            return self.json(data)
        if route.startswith("/art/"):  # /art/<种类>/<id>
            parts = route.split("/")
            body, ctype = None, ""
            try:
                if len(parts) == 4 and parts[3].isdigit():
                    body, ctype = ASSETS.art(parts[2], int(parts[3]))
                elif len(parts) == 5 and parts[2] == "ability" and parts[3].isdigit() and parts[4] in ("p", "q", "w", "e", "r"):
                    body, ctype = ASSETS.ability(int(parts[3]), parts[4])
            except Exception:
                body = None
            return self.send(body, ctype, cache=True) if body else self.send(b"", code=404)
        try:
            if route == "/api/assets":
                return self.json(ASSETS.names())
            if route == "/api/live":
                return self.json({**LIVE.data, "overlay": STATE.settings.get("overlay", True),
                                  "collapsed": OV.collapsed})
            if route == "/api/opgg/build":
                return self.json(opgg.build(int(q["champ"]), q.get("pos") or None, q.get("mode", "ranked"),
                                            q.get("tier", "emerald_plus"), int(q["target"]) if q.get("target") else None))
            if route == "/api/opgg/tiers":
                return self.json(opgg.tiers(q.get("mode", "ranked"), q.get("tier", "emerald_plus")))
            if route == "/api/opgg/matchup":
                return self.json(self.matchup(q))
            if route.startswith("/api/game/") and WORKER.lcu:
                return self.json(WORKER.lcu.game(int(route.rsplit("/", 1)[1])))
            if route == "/api/duos" and WORKER.lcu:
                return self.json(WORKER.lcu.duos(q["puuid"]))
        except Exception as e:  # OP.GG 连不上、客户端断开等
            return self.json({"error": str(e) or type(e).__name__}, code=502)
        self.send(b"{}", code=404)

    def matchup(self, q):
        """找出对位的敌人（对面英雄里最常走我这个位置的那个），再算推荐英雄。"""
        pos = q.get("pos") or None
        enemies = [int(x) for x in (q.get("enemies") or "").split(",") if x.strip().isdigit()]
        enemy = int(q["enemy"]) if q.get("enemy") else None
        guessed = False
        if not enemy and enemies and pos:
            mains = opgg.main_positions()
            ranked = sorted(enemies, key=lambda e: (mains.get(e) or [None]).index(pos) if pos in (mains.get(e) or []) else 99)
            if pos in (mains.get(ranked[0]) or []):
                enemy, guessed = ranked[0], True
        if not enemy or not pos:
            return {"enemy": None, "pos": pos}
        my = int(q["champ"]) if q.get("champ") else None
        return {**opgg.matchup(my, enemy, pos, q.get("tier", "emerald_plus")), "guessed": guessed}

    def json(self, data, code=200):
        return self.send(json.dumps(data, ensure_ascii=False).encode(), code=code)

    def do_POST(self):
        if not self.trusted():
            return self.send(b"{}", code=403)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/self":
            WORKER.want_self.set()
        elif self.path == "/api/runes":
            try:
                if not WORKER.lcu:
                    raise RuntimeError("没有连接客户端")
                msg = WORKER.lcu.set_runes(body.get("name", ""), int(body["primary"]), int(body["secondary"]),
                                           [int(x) for x in body["perks"]])
                return self.json({"ok": True, "message": msg})
            except Exception as e:
                return self.json({"ok": False, "message": str(e)}, code=400)
        elif self.path == "/api/team":
            WORKER.want_team.set()
        elif self.path == "/api/show":
            threading.Thread(target=MAIN.show, daemon=True).start()
        elif self.path == "/api/open-update":
            if STATE.update_info:
                webbrowser.open(STATE.update_info["url"])
        elif self.path == "/api/overlay":
            action = body.get("action")
            if action == "close":          # 这一局先不显示
                LIVE.dismissed = True
                OV.hide()
            elif action in ("collapse", "expand"):
                OV.set_collapsed(action == "collapse")
            elif action == "test":        # 主窗口的「预览悬浮窗」
                OV.show()
        elif self.path == "/api/refresh":
            WORKER.want_refresh.set()
        elif self.path == "/api/window" and WINDOW is not None:
            action = body.get("action")
            if action == "minimize":
                MAIN.to_tray()
            elif action == "maximize":
                WINDOW.restore() if getattr(WINDOW, "_maxed", False) else WINDOW.maximize()
                WINDOW._maxed = not getattr(WINDOW, "_maxed", False)
            elif action == "close":
                MAIN.quit()
        elif self.path == "/api/settings":
            with STATE.lock:
                if "topmost" in body:
                    STATE.settings["topmost"] = bool(body["topmost"])
                    set_topmost(STATE.settings["topmost"])
                if "overlay" in body:
                    STATE.settings["overlay"] = bool(body["overlay"])
                    LIVE.dismissed = False
                save_settings()
                STATE.version += 1
        self.send(b"{}")


def find_edge():
    for p in (shutil.which("msedge"),
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if p and os.path.exists(p):
            return p
    return None


def main():
    if "--no-window" not in sys.argv:
        single_instance()
        migrate_old_settings()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        with open(INSTANCE, "w", encoding="utf-8") as f:
            json.dump({"port": server.server_address[1], "token": APP_TOKEN}, f)
    except OSError:
        pass
    threading.Thread(target=check_update, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    WORKER.start()
    LIVE.start()

    if "--no-window" in sys.argv:
        print(url)
        threading.Event().wait()

    try:
        import webview  # 原生窗口（WebView2），打包成 exe 时用这个
    except ImportError:
        webview = None
    if webview:
        global WINDOW
        WINDOW = webview.create_window(TITLE, url, width=1360, height=840, min_size=(900, 600), frameless=True,
                                       easy_drag=False, background_color="#1B1B20",
                                       on_top=STATE.settings.get("topmost", True))
        global OVERLAY
        # 悬浮窗：建在屏幕外，进游戏时再挪进屏幕（不激活，不抢游戏焦点）
        LIVE.url = url + "overlay"
        OVERLAY = webview.create_window(OVERLAY_TITLE, html=BLANK, width=OVERLAY_SIZE[0], height=OVERLAY_SIZE[1],
                                        x=OFFSCREEN, y=OFFSCREEN, min_size=(200, 40), frameless=True, easy_drag=False, on_top=True,
                                        background_color="#1B1B20", focus=False)

        def hide_overlay_at_start():
            time.sleep(0.5)
            h = OV.hwnd()
            if h:
                OV._style(h)
            OV.visible, OV.ready = False, True

        OVERLAY.events.shown += lambda: threading.Thread(target=hide_overlay_at_start, daemon=True).start()
        # 关掉主窗口就整个退出（否则屏幕外的悬浮窗会让程序一直在后台运行）
        WINDOW.events.closed += lambda: MAIN.quit()
        MAIN.url = url
        MAIN.tray = Tray(os.path.join(RES, "icon.ico"), on_show=MAIN.show, on_toggle_overlay=toggle_overlay,
                         overlay_on=lambda: STATE.settings.get("overlay", True), on_quit=MAIN.quit,
                         on_toggle_pop=toggle_auto_pop, pop_on=lambda: STATE.settings.get("autoPop", True))
        MAIN.tray.start()
        threading.Thread(target=ensure_desktop_shortcut, daemon=True).start()
        webview.start(private_mode=False, storage_path=os.path.join(HERE, ".window"))
        return

    edge = find_edge()  # 没装 pywebview：用 Edge 的应用窗口模式
    if not edge:
        webbrowser.open(url)
        threading.Event().wait()

    proc = subprocess.Popen([
        edge, f"--app={url}", "--window-size=1320,820",
        f"--user-data-dir={os.path.join(HERE, '.window')}",
        "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
    ])
    for _ in range(100):  # 等窗口出来（标题加载完）再设置置顶
        time.sleep(0.2)
        if _find_window():
            time.sleep(0.5)
            set_topmost(STATE.settings.get("topmost", True))
            break
    proc.wait()  # 关掉窗口就退出


if __name__ == "__main__":
    main()
