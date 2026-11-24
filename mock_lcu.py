"""模拟英雄联盟客户端接口，用来在没开游戏时测试。

运行：python mock_lcu.py   （默认 127.0.0.1:28999，密码 test）
然后另开终端：
    set LCU_PORT=28999& set LCU_TOKEN=test& set LCU_PROTOCOL=http& python app.py
"""
import base64
import json
import os
import random
import re
import struct
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, TOKEN = 28999, "test"
DELAY = 0.04  # 模拟每次请求的耗时
CHAMPS = [(145, "虚空之女"), (412, "魂锁典狱长"), (89, "曙光女神"), (64, "盲僧"), (103, "九尾妖狐"),
          (86, "德玛西亚之力"), (222, "暴走萝莉"), (53, "蒸汽机器人"), (51, "皮城女警"), (22, "寒冰射手"),
          (117, "仙灵女巫"), (111, "深海泰坦"), (238, "影流之主"), (157, "疾风剑豪"), (122, "诺克萨斯之手")]
PLAYERS = [  # summonerId, puuid, 名字, 位置, 胜率倾向, 常用英雄, 本局英雄
    (1, "p-me", "峡谷的风", "bottom", 0.53, [145, 222, 51, 22], 145),
    (2, "p-2", "钩子大师", "utility", 0.62, [412, 53, 111], 412),
    (3, "p-3", "野区霸主", "jungle", 0.41, [64, 238, 122], 64),
    (4, "p-4", "中路小将", "middle", 0.50, [103, 157, 238, 117], 117),
    (5, "p-5", "上单守门员", "top", 0.46, [86, 122, 157], 0),
]
ENEMY_PLAYERS = [  # 进入游戏后才看得到的敌方 5 人
    (6, "e-6", "寒冰老手", "bottom", 0.57, [22, 51, 222], 22),
    (7, "e-7", "风之剑", "middle", 0.49, [157, 238, 103], 157),
    (8, "e-8", "狐狸精", "jungle", 0.52, [103, 64, 122], 103),
    (9, "e-9", "深海巨人", "utility", 0.55, [111, 412, 53], 111),
    (10, "e-10", "剑魔本魔", "top", 0.61, [266, 86, 122], 266),
]
ALL_PLAYERS = PLAYERS + ENEMY_PLAYERS
HISTORY = 1000  # 每人模拟多少场历史
ENEMIES = [22, 157, 103, 111, 0]  # 对面已选：寒冰、亚索、阿狸、泰坦，还有一个没选
LANE = {"top": ("TOP", "SOLO"), "jungle": ("JUNGLE", "NONE"), "middle": ("MIDDLE", "SOLO"),
        "bottom": ("BOTTOM", "DUO_CARRY"), "utility": ("BOTTOM", "DUO_SUPPORT")}
ITEMS = {"bottom": [6672, 3124, 3302, 3115, 3006, 3031, 3094, 3087, 3036],
         "utility": [3190, 3109, 2065, 3222, 3107, 3117, 3050],
         "jungle": [6610, 3071, 3053, 3047, 6333, 3161, 3742],
         "middle": [3089, 3135, 4645, 3157, 3020, 6655, 3165],
         "top": [3078, 6333, 3053, 3047, 3071, 3065, 3075]}
KEYSTONE = {"bottom": (8008, 8000, 8300), "utility": (8439, 8400, 8300), "jungle": (8010, 8000, 8400),
            "middle": (8112, 8100, 8200), "top": (8437, 8400, 8000)}
SPELLS = {"bottom": [4, 7], "utility": [4, 14], "jungle": [4, 11], "middle": [4, 14], "top": [4, 12]}
START = time.time()
ALIAS = {145: "Kaisa", 412: "Thresh", 64: "LeeSin", 117: "Lulu", 86: "Garen",
         22: "Ashe", 157: "Yasuo", 103: "Ahri", 111: "Nautilus", 266: "Aatrox"}
# 游戏内实时数据：我方 卡莎/锤石/盲僧/璐璐/盖伦，敌方 寒冰/亚索/阿狸/泰坦/剑魔
LIVE_TEAMS = [
    ("ORDER", [("峡谷的风", 145, "BOTTOM", [6672, 3006, 3124, 1055, 2055], (4, 2, 3)),
               ("钩子大师", 412, "UTILITY", [3190, 3117, 2055, 2055], (0, 3, 6)),
               ("野区霸主", 64, "JUNGLE", [6610, 3047, 3071], (3, 4, 2)),
               ("中路小将", 117, "MIDDLE", [6616, 3020, 3108], (1, 2, 4)),
               ("上单守门员", 86, "TOP", [6333, 3047, 3053], (2, 3, 1))]),
    ("CHAOS", [("路人甲", 22, "BOTTOM", [3153, 3006, 3072], (3, 2, 4)),
               ("路人乙", 111, "UTILITY", [3190, 3109, 2055], (1, 2, 7)),
               ("路人丙", 157, "MIDDLE", [6673, 3006, 3031], (5, 3, 2)),
               ("路人丁", 103, "JUNGLE", [6655, 3020, 3157], (2, 2, 5)),
               ("路人戊", 266, "TOP", [6692, 3074, 3047, 3065], (4, 1, 2))]),
]


def live_data():
    t = time.time() - START + 14 * 60  # 从第 14 分钟开始
    players = []
    for team, members in LIVE_TEAMS:
        for name, cid, pos, items, (k, d, a) in members:
            players.append({
                "riotId": f"{name}#1", "riotIdGameName": name, "summonerName": name,
                "rawChampionName": f"game_character_displayname_{ALIAS[cid]}", "team": team, "position": pos,
                "level": 11, "isDead": name == "路人丙", "respawnTimer": 18.4 if name == "路人丙" else 0,
                "scores": {"kills": k, "deaths": d, "assists": a, "creepScore": 120, "wardScore": 14.2},
                "items": [{"itemID": i, "slot": n, "count": 1, "consumable": i == 2055} for n, i in enumerate(items)],
            })
    return {
        "activePlayer": {"riotId": "峡谷的风#1", "level": 11, "currentGold": 812.5,
                         "abilities": {"Q": {"abilityLevel": 5}, "W": {"abilityLevel": 1}, "E": {"abilityLevel": 3},
                                       "R": {"abilityLevel": 2}}},
        "allPlayers": players,
        "gameData": {"gameMode": os.environ.get("MOCK_MODE", "CLASSIC"), "gameTime": t},
        "events": {"Events": []},
    }


RUNE_PAGES = [{"id": 1, "name": "我的符文页", "isDeletable": True, "current": True}]
MY_SPELLS = {"spell1Id": 14, "spell2Id": 4}  # 模拟用户习惯把闪现放在 F


def line(rnd, pos, champ, win):
    """一个人一局的数据（旧版战绩格式）。"""
    keystone, primary, sub = KEYSTONE[pos]
    items = rnd.sample(ITEMS[pos], 6) + [3340 if pos != "utility" else 3364]
    dmg = rnd.randint(6000, 32000) if pos != "utility" else rnd.randint(3000, 12000)
    st = {"win": win, "kills": rnd.randint(0, 13), "deaths": rnd.randint(1, 9), "assists": rnd.randint(1, 16),
          "totalMinionsKilled": rnd.randint(20, 40) if pos == "utility" else rnd.randint(140, 280),
          "neutralMinionsKilled": rnd.randint(100, 180) if pos == "jungle" else rnd.randint(0, 12),
          "totalDamageDealtToChampions": dmg, "goldEarned": rnd.randint(7000, 16000), "visionScore": rnd.randint(10, 80 if pos == "utility" else 35),
          "champLevel": rnd.randint(12, 18), "perk0": keystone, "perkPrimaryStyle": primary, "perkSubStyle": sub}
    st.update({f"item{i}": it for i, it in enumerate(items)})
    lane, role = LANE[pos]
    return {"championId": champ, "spell1Id": SPELLS[pos][0], "spell2Id": SPELLS[pos][1], "stats": st,
            "timeline": {"lane": lane, "role": role}}
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_assets")  # 本地测试用的英雄图（不提交）
PROFILE_ICONS = {1: 29, 2: 4568, 3: 5367, 4: 6, 5: 3500}


_GAMES = {}


def games_for(sid):
    if sid not in _GAMES:
        _GAMES[sid] = _make_games(sid)
    return _GAMES[sid]


def _make_games(sid):
    s = next(p for p in ALL_PLAYERS if p[0] == sid)
    rnd = random.Random(s[1])
    now = int(time.time() * 1000)
    t, games = now - 40 * 60000, []
    for i in range(HISTORY):
        main = rnd.random() < 0.75
        q = rnd.choice([420, 420, 420, 430, 450, 440])
        win = rnd.random() < s[4] + (0.08 if main else -0.05) + (0.15 if i < 4 and sid == 2 else 0) - (0.4 if i < 4 and sid == 3 else 0)
        games.append({
            "gameId": sid * 100000 + HISTORY - i, "gameCreation": t,
            "gameDuration": 150 if rnd.random() < 0.03 else rnd.randint(1100, 2400), "queueId": q,
            "participants": [line(rnd, s[3], rnd.choice(s[5]) if main else rnd.choice(CHAMPS)[0], win)],
        })
        t -= rnd.randint(30, 600) * 60000
    return games


def game_detail(game_id):
    """一局完整的 10 人数据：查询者本人 + 4 个队友 + 5 个对手。"""
    sid = game_id // 100000
    me_game = next((g for g in games_for(sid) if g["gameId"] == game_id), None)
    if not me_game:
        return None
    rnd = random.Random(game_id)
    me = ALL_PLAYERS[sid - 1]
    win = me_game["participants"][0]["stats"]["win"]
    order = ["top", "jungle", "middle", "bottom", "utility"]
    parts, idents = [], []
    for team, team_win in ((100, win), (200, not win)):
        for pos in order:
            pid = len(parts) + 1
            if team == 100 and pos == me[3]:
                p = dict(me_game["participants"][0])
                who = {"gameName": me[2], "tagLine": str(1000 + me[0] * 37), "puuid": me[1], "profileIcon": PROFILE_ICONS.get(me[0], 29)}
            else:
                # 队友里经常出现同一个开黑好友
                friend = team == 100 and pos == "utility" and me[3] != "utility" and rnd.random() < 0.4
                p = line(rnd, pos, rnd.choice(CHAMPS)[0], team_win)
                name = "钩子大师" if friend else f"路人{rnd.randint(100, 999)}"
                who = {"gameName": name, "tagLine": "1074" if friend else str(rnd.randint(1000, 9999)),
                       "puuid": "p-2" if friend else f"x-{game_id}-{pid}", "profileIcon": 29}
            parts.append({**p, "participantId": pid, "teamId": team})
            idents.append({"participantId": pid, "player": who})
    teams = [{"teamId": t, "win": "Win" if w else "Fail", "towerKills": rnd.randint(6, 11) if w else rnd.randint(0, 5),
              "dragonKills": rnd.randint(1, 4), "baronKills": int(w and rnd.random() < .6)} for t, w in ((100, win), (200, not win))]
    return {**me_game, "participants": parts, "participantIdentities": idents, "teams": teams}


def icon_png(cid):
    """生成一张纯色小图当英雄头像。"""
    rnd = random.Random(cid)
    rgb = bytes([rnd.randint(40, 200), rnd.randint(40, 200), rnd.randint(60, 220)])
    raw = b"".join(b"\x00" + rgb * 32 for _ in range(32))
    chunk = lambda tag, data: struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class H(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        pass

    def reply(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/liveclientdata/allgamedata":  # 游戏内实时数据（真实接口不需要密码）
            ok = os.environ.get("MOCK_PHASE") == "InProgress" and not os.environ.get("MOCK_LOADING")
            return self.reply(live_data()) if ok else self.reply({}, 404)
        want = "Basic " + base64.b64encode(f"riot:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != want:
            return self.reply({"message": "unauthorized"}, 401)
        time.sleep(DELAY)
        p = self.path
        by_id = {str(s[0]): s for s in ALL_PLAYERS}
        by_puuid = {s[1]: s for s in ALL_PLAYERS}
        if p == "/lol-gameflow/v1/gameflow-phase":  # MOCK_PHASE=InProgress 模拟游戏中
            return self.reply(os.environ.get("MOCK_PHASE", "ChampSelect"))
        if p == "/lol-gameflow/v1/session":  # 设置环境变量 MOCK_ARAM=1 模拟大乱斗
            mode = "ARAM" if os.environ.get("MOCK_ARAM") else "CLASSIC"
            data = {"gameData": {"queue": {"gameMode": mode, "id": 450 if mode == "ARAM" else 420}}}
            if os.environ.get("MOCK_PHASE") in ("InProgress", "GameStart"):  # 游戏中才有双方名单
                team = lambda ps: [{"puuid": s[1], "summonerId": s[0], "championId": s[6] or s[5][0],
                                    "selectedPosition": s[3].upper()} for s in ps]
                data["gameData"].update({"teamOne": team(PLAYERS), "teamTwo": team(ENEMY_PLAYERS)})
            return self.reply(data)
        if p == "/lol-summoner/v1/current-summoner":
            return self.reply({"summonerId": 1, "puuid": "p-me", "gameName": "峡谷的风", "tagLine": "12345",
                               "profileIconId": PROFILE_ICONS[1], "summonerLevel": 141})
        if p == "/lol-champ-select/v1/session":
            return self.reply({"localPlayerCellId": 0, "myTeam": [
                {"cellId": i, "summonerId": s[0], "puuid": s[1], "assignedPosition": s[3], "championId": s[6],
                 "championPickIntent": 0, **(MY_SPELLS if i == 0 else {})} for i, s in enumerate(PLAYERS)],
                # 排位：对面身份隐藏（puuid 为空）；设 MOCK_VISIBLE=1 模拟对面身份公开的模式
                "theirTeam": [{"cellId": 5 + i, "championId": c,
                               **({"puuid": ENEMY_PLAYERS[i][1], "summonerId": ENEMY_PLAYERS[i][0], "nameVisibilityType": "VISIBLE",
                                   "assignedPosition": ENEMY_PLAYERS[i][3]} if os.environ.get("MOCK_VISIBLE") else
                                  {"puuid": "", "nameVisibilityType": "HIDDEN", "obfuscatedPuuid": "xxxx"})}
                              for i, c in enumerate(ENEMIES)]})
        if m := re.fullmatch(r"/lol-match-history/v1/games/(\d+)", p):
            g = game_detail(int(m.group(1)))
            return self.reply(g) if g else self.reply({}, 404)
        if p == "/lol-perks/v1/pages":
            return self.reply(RUNE_PAGES)
        # 英雄列表不提供：让程序去 CommunityDragon 拿完整列表（顺便测试这条兜底路径）
        if m := re.fullmatch(r"/lol-game-data/assets/v1/champions/(\d+)\.json", p):
            cid = m.group(1)
            if not os.path.exists(os.path.join(ASSETS, f"{cid}_load.jpg")):
                return self.reply({}, 404)  # 没有本地图的英雄：让程序去 CommunityDragon 拿
            return self.reply({"id": int(cid), "squarePortraitPath": f"/mock/{cid}_square.png",
                               "skins": [{"loadScreenPath": f"/mock/{cid}_load.jpg",
                                          "uncenteredSplashPath": f"/mock/{cid}_splash.jpg"}]})
        if m := re.fullmatch(r"/mock/([\w.]+)", p):
            f = os.path.join(ASSETS, m.group(1))
            if os.path.exists(f):
                return self.reply(open(f, "rb").read(), ctype="image/png" if f.endswith(".png") else "image/jpeg")
            return self.reply({}, 404)
        if m := re.fullmatch(r"/lol-game-data/assets/v1/profile-icons/(\d+)\.jpg", p):
            f = os.path.join(ASSETS, f"profile_{m.group(1)}.png")
            return self.reply(open(f, "rb").read(), ctype="image/png") if os.path.exists(f) else self.reply({}, 404)
        if m := re.fullmatch(r"/lol-summoner/v1/summoners/(\d+)", p):
            s = by_id.get(m.group(1))
            return self.reply({"summonerId": s[0], "puuid": s[1], "gameName": s[2], "tagLine": str(1000 + s[0] * 37),
                               "summonerLevel": 100 + s[0] * 41, "profileIconId": PROFILE_ICONS.get(s[0], 29)}) if s else self.reply({}, 404)
        if m := re.fullmatch(r"/lol-ranked/v1/ranked-stats/(.+)", p):
            tier, div = {"p-2": ("DIAMOND", "III"), "p-3": ("SILVER", "I"), "p-4": ("EMERALD", "IV"),
                         "p-5": ("", "")}.get(m.group(1), ("PLATINUM", "II"))
            return self.reply({"queueMap": {"RANKED_SOLO_5x5": {"tier": tier, "division": div, "leaguePoints": 47,
                                                                "wins": 88, "losses": 79},
                                            "RANKED_FLEX_SR": {"tier": "GOLD", "division": "I", "leaguePoints": 12,
                                                               "wins": 21, "losses": 17}}})
        if m := re.match(r"/lol-match-history/v1/products/lol/([^/?]+)/matches\?begIndex=(\d+)&endIndex=(\d+)", p):
            s = by_puuid.get(m.group(1))
            beg, end = int(m.group(2)), int(m.group(3))
            games = games_for(s[0])[beg:end + 1] if s else []
            return self.reply({"games": {"games": games}})
        return self.reply({"message": "not found"}, 404)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_POST(self):
        if self.path == "/lol-perks/v1/pages":
            if len(RUNE_PAGES) >= 3:
                return self.reply({"message": "Max pages reached"}, 400)
            RUNE_PAGES.append({**self._body(), "id": len(RUNE_PAGES) + 1, "isDeletable": True})
            print("新建符文页", RUNE_PAGES[-1])
            return self.reply(RUNE_PAGES[-1])
        return self.reply({}, 404)

    def do_PATCH(self):
        if self.path == "/lol-champ-select/v1/session/my-selection":
            MY_SPELLS.update(self._body())
            print("设置召唤师技能", MY_SPELLS, flush=True)
            return self.reply({})
        return self.reply({}, 404)

    def do_PUT(self):
        if self.path == "/lol-perks/v1/currentpage":
            pid = self._body()
            for x in RUNE_PAGES:
                x["current"] = x["id"] == pid
            print("当前符文页", pid, flush=True)
            return self.reply({})
        if m := re.fullmatch(r"/lol-perks/v1/pages/(\d+)", self.path):
            page = next((x for x in RUNE_PAGES if x["id"] == int(m.group(1))), None)
            if page:
                page.update(self._body())
                print("更新符文页", page)
                return self.reply(page)
        return self.reply({}, 404)


if __name__ == "__main__":
    print(f"模拟客户端已启动：http://127.0.0.1:{PORT}  密码 {TOKEN}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
