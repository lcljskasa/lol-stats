"""连接英雄联盟客户端的本地接口（LCU API）：找端口密码、分页并发拉战绩、本地缓存。只用 Python 标准库。"""
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

if getattr(sys, "frozen", False):              # 打包成 exe 后
    RES = sys._MEIPASS                          # 界面文件在 exe 内部
    HERE = os.path.dirname(sys.executable)      # 缓存、设置放在 exe 旁边
else:
    RES = HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
PAGE = 20  # 每次请求拉多少场，客户端对单次范围有限制，20 最稳

POSITIONS = {"top": "上单", "jungle": "打野", "middle": "中单", "bottom": "ADC", "utility": "辅助"}
TIERS = {"IRON": "黑铁", "BRONZE": "青铜", "SILVER": "白银", "GOLD": "黄金", "PLATINUM": "铂金",
         "EMERALD": "翡翠", "DIAMOND": "钻石", "MASTER": "大师", "GRANDMASTER": "宗师", "CHALLENGER": "王者"}


class NotConnected(Exception):
    pass


def find_credentials():
    """返回 (port, token, protocol)。优先读客户端进程的启动参数，读不到再找 lockfile。"""
    if os.environ.get("LCU_PORT"):  # 离线测试用
        return os.environ["LCU_PORT"], os.environ.get("LCU_TOKEN", ""), os.environ.get("LCU_PROTOCOL", "https")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='LeagueClientUx.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
    except Exception:
        out = ""
    port = re.search(r"--app-port=(\d+)", out)
    token = re.search(r"--remoting-auth-token=([\w-]+)", out)
    if port and token:
        return port.group(1), token.group(1), "https"
    for path in _lockfile_candidates(out):
        try:
            _, _, port, token, protocol = open(path, encoding="utf-8").read().strip().split(":")
            return port, token, protocol
        except (OSError, ValueError):
            continue
    raise NotConnected("没找到英雄联盟客户端，请先登录游戏")


def _lockfile_candidates(cmdline):
    found = re.search(r'--install-directory=([^"]+?)(?:"|\s--)', cmdline)
    if found:
        yield os.path.join(found.group(1).strip(), "lockfile")
    for drive in "CDEFG":
        yield rf"{drive}:\Riot Games\League of Legends\lockfile"
        yield rf"{drive}:\WeGameApps\英雄联盟\LeagueClient\lockfile"
        yield rf"{drive}:\英雄联盟\LeagueClient\lockfile"


def trim_game(g):
    """只留界面要用的字段，缓存文件小一点。"""
    p = (g.get("participants") or [{}])[0]
    st = p.get("stats") or {}
    return {
        "id": g.get("gameId"), "t": g.get("gameCreation") or 0, "dur": g.get("gameDuration") or 0,
        "q": g.get("queueId"), "c": p.get("championId"), "win": bool(st.get("win")),
        "k": st.get("kills", 0), "d": st.get("deaths", 0), "a": st.get("assists", 0),
        "cs": (st.get("totalMinionsKilled") or 0) + (st.get("neutralMinionsKilled") or 0),
    }


class LCU:
    pages = ThreadPoolExecutor(max_workers=16, thread_name_prefix="page")

    def __init__(self):
        self.port, token, self.protocol = find_credentials()
        self.base = f"{self.protocol}://127.0.0.1:{self.port}"
        self.auth = "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()
        self.ctx = ssl._create_unverified_context()  # 客户端用的是自签名证书
        self._champions = None
        self._icons = {}
        self._cache_lock = threading.Lock()

    def raw(self, path):
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=10) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise NotConnected(f"客户端连接断开：{e}") from e

    def get(self, path):
        body = self.raw(path)
        return None if body is None else json.loads(body.decode("utf-8"))

    # ---- 基础信息 ----
    def phase(self):
        return self.get("/lol-gameflow/v1/gameflow-phase")

    def me(self):
        return self.get("/lol-summoner/v1/current-summoner")

    def champ_select(self):
        return self.get("/lol-champ-select/v1/session")

    def champions(self):
        if self._champions is None:
            data = self.get("/lol-game-data/assets/v1/champion-summary.json") or []
            self._champions = {c["id"]: c["name"] for c in data if c.get("id", -1) > 0}
        return self._champions

    def art(self, kind, ident):
        """英雄头像 square / 竖版原画 load / 横版原画 splash / 召唤师头像 profile。返回 (bytes, content-type)。"""
        key = (kind, ident)
        if key not in self._icons:
            if kind == "profile":
                path = f"/lol-game-data/assets/v1/profile-icons/{ident}.jpg"
            else:
                info = self.get(f"/lol-game-data/assets/v1/champions/{ident}.json") or {}
                skin = (info.get("skins") or [{}])[0]
                path = {"square": info.get("squarePortraitPath"),
                        "load": skin.get("loadScreenPath") or skin.get("splashPath"),
                        "splash": skin.get("uncenteredSplashPath") or skin.get("splashPath")}.get(kind)
                if not path and kind == "square":
                    path = f"/lol-game-data/assets/v1/champion-icons/{ident}.png"
            body = self.raw(path) if path else None
            ctype = "image/png" if body and body[:4] == b"\x89PNG" else "image/jpeg"
            self._icons[key] = (body, ctype)
        return self._icons[key]

    def summoner(self, summoner_id=None, puuid=None):
        if summoner_id:
            s = self.get(f"/lol-summoner/v1/summoners/{summoner_id}")
            if s:
                return s
        if puuid:
            return self.get(f"/lol-summoner/v2/summoners/puuid/{puuid}")
        return None

    def rank(self, puuid):
        try:
            data = self.get(f"/lol-ranked/v1/ranked-stats/{puuid}") or {}
        except NotConnected:
            raise
        except Exception:
            return None
        solo = (data.get("queueMap") or {}).get("RANKED_SOLO_5x5") or {}
        tier = solo.get("tier") or ""
        if not tier or tier in ("NONE", "UNRANKED"):
            return {"tier": "", "text": "单双排无段位"}
        div = solo.get("division") or ""
        div = "" if div in ("NA", "") or tier in ("MASTER", "GRANDMASTER", "CHALLENGER") else " " + div
        return {"tier": tier, "text": f"{TIERS.get(tier, tier)}{div}", "lp": solo.get("leaguePoints", 0),
                "wins": solo.get("wins", 0), "losses": solo.get("losses", 0)}

    # ---- 战绩：分页并发 + 本地缓存 ----
    def _page(self, puuid, beg, end):
        data = self.get(f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex={beg}&endIndex={end - 1}")
        return [trim_game(g) for g in ((data or {}).get("games") or {}).get("games") or []]

    def _pages(self, puuid, beg, end):
        futures = [self.pages.submit(self._page, puuid, b, min(b + PAGE, end)) for b in range(beg, end, PAGE)]
        out = []
        for f in futures:
            out += f.result()
        return out

    def matches(self, puuid, want):
        """返回最近 want 场（新到旧）。缓存里已有的老对局不再重复拉。"""
        path = os.path.join(CACHE_DIR, re.sub(r"[^\w-]", "_", puuid) + ".json")
        try:
            cache = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            cache = {"depth": 0, "games": []}
        known = {g["id"] for g in cache["games"]}

        first = self._page(puuid, 0, min(PAGE, want))
        fresh = [g for g in first if g["id"] not in known]
        if known and len(fresh) < len(first):      # 和缓存接上了：缓存仍然连续可用
            games = fresh + cache["games"]
            depth = cache["depth"] + len(fresh)
        else:                                       # 没缓存或断档太多：重新拉
            games, depth = first, len(first)
        if depth < want and len(first) == min(PAGE, want):
            more = self._pages(puuid, depth, want)
            seen = {g["id"] for g in games}
            games += [g for g in more if g["id"] not in seen]
            depth = max(depth, len(games))

        games.sort(key=lambda g: g["t"], reverse=True)
        with self._cache_lock:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"depth": depth, "games": games[:1000]}, f, ensure_ascii=False)
        return games[:want]

    def player(self, want, puuid=None, summoner_id=None):
        s = self.summoner(summoner_id, puuid) or {}
        puuid = puuid or s.get("puuid")
        name = s.get("gameName") or s.get("displayName") or s.get("internalName") or "（隐藏）"
        info = {"name": name, "tag": s.get("tagLine") or "", "level": s.get("summonerLevel"),
                "icon": s.get("profileIconId"), "puuid": puuid}
        if not puuid:
            return {**info, "error": "拿不到这个玩家的数据"}
        rank = self.pages.submit(self.rank, puuid)
        games = self.matches(puuid, want)
        return {**info, "rank": rank.result(), "games": games}
