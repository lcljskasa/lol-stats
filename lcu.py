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
    # 缓存、设置、浏览器数据放在 %LOCALAPPDATA%\LOLStats，不在 exe 旁边乱生成文件
    HERE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LOLStats")
    os.makedirs(HERE, exist_ok=True)
else:
    RES = HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
PAGE = 20  # 每次请求拉多少场，客户端对单次范围有限制，20 最稳
CACHE_VERSION = 2  # 对局字段变化时加 1，旧缓存自动作废
RUNE_PAGE_PREFIX = "速览"  # 我们导入的符文页名字前缀，只会改这种页

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


def lane_of(p):
    """旧版战绩格式里的 lane/role 换成位置名。"""
    tl = p.get("timeline") or {}
    lane, role = (tl.get("lane") or "").upper(), (tl.get("role") or "").upper()
    if lane == "JUNGLE":
        return "jungle"
    if lane in ("MIDDLE", "MID"):
        return "middle"
    if lane == "TOP":
        return "top"
    if lane in ("BOTTOM", "BOT"):
        return "utility" if role == "DUO_SUPPORT" else "bottom"
    return ""


def player_line(p):
    st = p.get("stats") or {}
    return {
        "c": p.get("championId"), "win": bool(st.get("win")),
        "k": st.get("kills", 0), "d": st.get("deaths", 0), "a": st.get("assists", 0),
        "cs": (st.get("totalMinionsKilled") or 0) + (st.get("neutralMinionsKilled") or 0),
        "dmg": st.get("totalDamageDealtToChampions", 0), "gold": st.get("goldEarned", 0),
        "vis": st.get("visionScore", 0), "lvl": st.get("champLevel", 0),
        "items": [st.get(f"item{i}", 0) for i in range(7)],
        "spells": [p.get("spell1Id"), p.get("spell2Id")],
        "perk": st.get("perk0"), "sub": st.get("perkSubStyle"), "pos": lane_of(p),
    }


def trim_game(g):
    """只留界面要用的字段，缓存文件小一点。"""
    p = (g.get("participants") or [{}])[0]
    return {"id": g.get("gameId"), "t": g.get("gameCreation") or 0, "dur": g.get("gameDuration") or 0,
            "q": g.get("queueId"), **player_line(p)}


def score_game(players):
    """给一局里 10 个人打分（3~10）。思路参考 OP Score：KDA、参团率、伤害、经济、视野、补刀加权，
    再在本局内归一化。胜方最高分是 MVP，败方最高分是 ACE。"""
    team_kills, team_dmg = {}, {}
    for p in players:
        team_kills[p["team"]] = team_kills.get(p["team"], 0) + p["k"]
        team_dmg[p["team"]] = team_dmg.get(p["team"], 0) + p["dmg"]
    for p in players:
        p["kp"] = round((p["k"] + p["a"]) / max(team_kills[p["team"]], 1), 3)
        p["dmgShare"] = round(p["dmg"] / max(team_dmg[p["team"]], 1), 3)
    top = {k: max((p[k] for p in players), default=1) or 1 for k in ("dmg", "gold", "vis", "cs")}
    raw = []
    for p in players:
        kda = min((p["k"] + p["a"]) / max(p["d"], 1), 10) / 10
        raw.append(0.28 * kda + 0.22 * p["kp"] + 0.2 * p["dmg"] / top["dmg"] + 0.12 * p["gold"] / top["gold"]
                   + 0.1 * p["vis"] / top["vis"] + 0.08 * p["cs"] / top["cs"] + (0.05 if p["win"] else 0))
    lo, hi = min(raw, default=0), max(raw, default=1)
    for p, r in zip(players, raw):
        p["score"] = round(3 + 7 * (r - lo) / ((hi - lo) or 1), 1)
    for win in (True, False):
        side = [p for p in players if p["win"] == win]
        if side:
            max(side, key=lambda p: p["score"])["badge"] = "MVP" if win else "ACE"
    return players


class LCU:
    pages = ThreadPoolExecutor(max_workers=16, thread_name_prefix="page")

    def __init__(self):
        self.port, token, self.protocol = find_credentials()
        self.base = f"{self.protocol}://127.0.0.1:{self.port}"
        self.auth = "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()
        self.ctx = ssl._create_unverified_context()  # 客户端用的是自签名证书
        self._champions = None
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
        qm = data.get("queueMap") or {}

        def one(q, empty):
            x = qm.get(q) or {}
            tier = x.get("tier") or ""
            if not tier or tier in ("NONE", "UNRANKED"):
                return {"tier": "", "text": empty, "wins": x.get("wins", 0), "losses": x.get("losses", 0)}
            div = x.get("division") or ""
            div = "" if div in ("NA", "") or tier in ("MASTER", "GRANDMASTER", "CHALLENGER") else " " + div
            return {"tier": tier, "text": f"{TIERS.get(tier, tier)}{div}", "lp": x.get("leaguePoints", 0),
                    "wins": x.get("wins", 0), "losses": x.get("losses", 0)}

        # 顶层是单双排（保持原来的格式），flex 是灵活组排
        return {**one("RANKED_SOLO_5x5", "单双排无段位"), "flex": one("RANKED_FLEX_SR", "灵活组排无段位")}

    # ---- 战绩：分页并发 + 本地缓存 ----
    def _page(self, puuid, beg, end):
        data = self.get(f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex={beg}&endIndex={end - 1}")
        return [trim_game(g) for g in ((data or {}).get("games") or {}).get("games") or []]

    def _pages(self, puuid, beg, end, wave=10):
        """分批并发拉取：每批 10 页，某一页不满说明历史到头了，就不再继续。"""
        out = []
        for start in range(beg, end, PAGE * wave):
            stop = min(start + PAGE * wave, end)
            futures = [self.pages.submit(self._page, puuid, b, min(b + PAGE, stop)) for b in range(start, stop, PAGE)]
            done = False
            for b, f in zip(range(start, stop, PAGE), futures):
                got = f.result()
                out += got
                if len(got) < min(PAGE, stop - b):
                    done = True
            if done:
                break
        return out

    def matches(self, puuid, want):
        """返回最近 want 场（新到旧）。缓存里已有的老对局不再重复拉。"""
        path = os.path.join(CACHE_DIR, re.sub(r"[^\w-]", "_", puuid) + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {"depth": 0, "games": []}
        if cache.get("v") != CACHE_VERSION:
            cache = {"depth": 0, "games": []}
        known = {g["id"] for g in cache["games"]}

        first = self._page(puuid, 0, min(PAGE, want))
        if not first:                               # 接口这次没返回数据：用缓存兜底，别把缓存清空
            return cache["games"][:want]
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
                json.dump({"v": CACHE_VERSION, "depth": depth, "games": games[:1000]}, f, ensure_ascii=False)
        return games[:want]

    # ---- 单局详情 ----
    def game(self, game_id):
        """一局的 10 人数据和评分。对局结束后不会再变，所以缓存到磁盘。"""
        path = os.path.join(CACHE_DIR, "games", f"{int(game_id)}.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        g = self.get(f"/lol-match-history/v1/games/{int(game_id)}")
        if not g:
            return None
        ids = {i.get("participantId"): i.get("player") or {} for i in g.get("participantIdentities") or []}
        players = []
        for p in g.get("participants") or []:
            who = ids.get(p.get("participantId"), {})
            players.append({**player_line(p), "team": p.get("teamId"), "puuid": who.get("puuid"),
                            "name": who.get("gameName") or who.get("summonerName") or "",
                            "tag": who.get("tagLine") or "", "icon": who.get("profileIcon")})
        teams = []
        for t in g.get("teams") or []:
            mine = [p for p in players if p["team"] == t.get("teamId")]
            teams.append({"id": t.get("teamId"), "win": t.get("win") in ("Win", True),
                          "kills": sum(p["k"] for p in mine), "gold": sum(p["gold"] for p in mine),
                          "towers": t.get("towerKills", 0), "dragons": t.get("dragonKills", 0),
                          "barons": t.get("baronKills", 0)})
        out = {"id": g.get("gameId"), "t": g.get("gameCreation"), "dur": g.get("gameDuration"),
               "q": g.get("queueId"), "teams": teams, "players": score_game(players)}
        with self._cache_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        return out

    def duos(self, puuid, count=20):
        """最近 count 场里经常和这个人同队的玩家（至少 2 场）。"""
        games = self.matches(puuid, count)
        details = [f.result() for f in [self.pages.submit(self.game, g["id"]) for g in games]]
        together = {}
        for d in filter(None, details):
            me = next((p for p in d["players"] if p["puuid"] == puuid), None)
            if not me:
                continue
            for p in d["players"]:
                if p["team"] == me["team"] and p["puuid"] and p["puuid"] != puuid:
                    t = together.setdefault(p["puuid"], {"name": p["name"], "tag": p["tag"], "icon": p["icon"], "n": 0, "w": 0})
                    t["n"] += 1
                    t["w"] += me["win"]
        return sorted([t for t in together.values() if t["n"] >= 2], key=lambda t: -t["n"])[:8]

    # ---- 导入符文页（只有用户点按钮才会调用） ----
    def set_runes(self, name, primary, secondary, perks):
        body = {"name": f"{RUNE_PAGE_PREFIX} {name}"[:25], "primaryStyleId": primary, "subStyleId": secondary,
                "selectedPerkIds": perks, "current": True}
        pages = self.get("/lol-perks/v1/pages") or []
        ours = [p for p in pages if (p.get("name") or "").startswith(RUNE_PAGE_PREFIX) and p.get("isDeletable", True)]
        if ours:  # 已经有我们建的页：覆盖它，不动用户自己的页
            self.send("PUT", f"/lol-perks/v1/pages/{ours[0]['id']}", {**body, "id": ours[0]["id"]})
            try:
                self.send("PUT", "/lol-perks/v1/currentpage", ours[0]["id"])  # 设为当前使用的符文页
            except Exception:
                pass
            return "已更新符文页"
        try:
            self.send("POST", "/lol-perks/v1/pages", body)
            return "已新建符文页"
        except urllib.error.HTTPError:
            raise RuntimeError("符文页已满，请先在客户端删掉一页再导入")

    # ---- 召唤师技能（只能在选英雄阶段设置，用户点按钮或开了自动导入才会调用） ----
    def set_spells(self, a, b):
        """设置召唤师技能。闪现放在用户原来放闪现的那个键上（D 或 F），不打乱习惯。"""
        session = self.champ_select() or {}
        me = next((m for m in session.get("myTeam") or [] if m.get("cellId") == session.get("localPlayerCellId")), None)
        if me is None:
            raise RuntimeError("现在不在选英雄阶段，召唤师技能要在选英雄时设置")
        FLASH = 4
        if FLASH in (a, b):
            other = b if a == FLASH else a
            if me.get("spell2Id") == FLASH:      # 原来闪现在 F
                a, b = other, FLASH
            else:                                # 原来闪现在 D，或者原来没带闪现：放 D
                a, b = FLASH, other
        self.send("PATCH", "/lol-champ-select/v1/session/my-selection", {"spell1Id": a, "spell2Id": b})
        return "已设置召唤师技能"

    def send(self, method, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method=method,
                                     headers={"Authorization": self.auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=self.ctx, timeout=10) as resp:
            return resp.read()

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
