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

import opgg
from assets import ASSETS
from lcu import HERE, LCU, RES, NotConnected, POSITIONS

TITLE = "LOL 战绩速览"
APP_TOKEN = secrets.token_urlsafe(16)  # 只有我们自己的页面知道，挡住其他网页调用本地接口
SETTINGS = os.path.join(HERE, "settings.json")
PHASES = {"None": "在大厅", "Lobby": "在房间里", "Matchmaking": "排队中", "ReadyCheck": "找到对局",
          "ChampSelect": "选英雄中", "InProgress": "游戏中", "GameStart": "游戏加载中",
          "WaitingForStats": "等待结算", "EndOfGame": "对局结束", "PreEndOfGame": "对局结束"}


def load_settings():
    try:
        return {"games": 100, "topmost": True, **json.load(open(SETTINGS, encoding="utf-8"))}
    except (OSError, ValueError):
        return {"games": 100, "topmost": True}


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

    def tick(self):
        phase = self.lcu.phase() or ""
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

        if phase != "ChampSelect":
            if self.want_refresh.is_set():
                self.want_refresh.clear()
                with self.s.lock:
                    entries = [{k: p.get(k) for k in ("key", "puuid", "summonerId", "pos", "champ", "me")}
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
def _find_window():
    user32 = ctypes.windll.user32
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
    ctypes.windll.user32.SetWindowPos(hwnd, -1 if on else -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def set_topmost(on):
    if WINDOW is not None:
        WINDOW.on_top = on
        return
    hwnd = _find_window()
    if hwnd:
        _set_z(hwnd, on)


def bring_to_front():
    """进入选英雄时把窗口弹到最前面。Windows 不让后台程序直接抢焦点，所以先临时置顶再恢复。"""
    hwnd = _find_window()
    if hwnd:
        if ctypes.windll.user32.IsIconic(hwnd):  # 只在最小化时还原，最大化的窗口保持原样
            ctypes.windll.user32.ShowWindow(hwnd, 9)
        _set_z(hwnd, True)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        if not STATE.settings.get("topmost", True):
            _set_z(hwnd, False)


# ---------- 本地网页服务 ----------
STATE = State()
WORKER = Worker(STATE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json; charset=utf-8", code=200, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        """只接受发给 127.0.0.1 的请求（防 DNS 重绑定），接口还要带上启动时生成的令牌（防其他网页调用）。"""
        host_ok = self.headers.get("Host") == f"127.0.0.1:{self.server.server_address[1]}"
        return host_ok and (not self.path.startswith("/api/") or self.headers.get("X-App-Token") == APP_TOKEN)

    def do_GET(self):
        if not self.trusted():
            return self.send(b"{}", code=403)
        if self.path in ("/", "/index.html"):
            with open(os.path.join(RES, "web", "index.html"), "rb") as f:
                page = f.read().replace(b"__APP_TOKEN__", APP_TOKEN.encode())
            return self.send(page, "text/html; charset=utf-8")
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        route = url.path
        if route == "/api/state":
            data = STATE.snapshot()
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
        elif self.path == "/api/refresh":
            WORKER.want_refresh.set()
        elif self.path == "/api/window" and WINDOW is not None:
            action = body.get("action")
            if action == "minimize":
                WINDOW.minimize()
            elif action == "maximize":
                WINDOW.restore() if getattr(WINDOW, "_maxed", False) else WINDOW.maximize()
                WINDOW._maxed = not getattr(WINDOW, "_maxed", False)
            elif action == "close":
                WINDOW.destroy()
        elif self.path == "/api/settings":
            with STATE.lock:
                if body.get("games") in (20, 50, 100, 200):
                    STATE.settings["games"] = body["games"]
                if "topmost" in body:
                    STATE.settings["topmost"] = bool(body["topmost"])
                    set_topmost(STATE.settings["topmost"])
                with open(SETTINGS, "w", encoding="utf-8") as f:
                    json.dump(STATE.settings, f)
                STATE.version += 1
            if "games" in body:
                WORKER.want_refresh.set()
        self.send(b"{}")


def find_edge():
    for p in (shutil.which("msedge"),
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if p and os.path.exists(p):
            return p
    return None


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    WORKER.start()

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
                                       easy_drag=False, background_color="#07090D",
                                       on_top=STATE.settings.get("topmost", True))
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
