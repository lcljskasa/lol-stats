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
HISTORY = 320  # 每人模拟多少场历史
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
RUNE_PAGES = [{"id": 1, "name": "我的符文页", "isDeletable": True, "current": True}]


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


def games_for(sid):
    s = next(p for p in PLAYERS if p[0] == sid)
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
    me = PLAYERS[sid - 1]
    win = me_game["participants"][0]["stats"]["win"]
    order = ["top", "jungle", "middle", "bottom", "utility"]
    parts, idents = [], []
    for team, team_win in ((100, win), (200, not win)):
        for pos in order:
            pid = len(parts) + 1
            if team == 100 and pos == me[3]:
                p = dict(me_game["participants"][0])
                who = {"gameName": me[2], "tagLine": str(1000 + me[0] * 37), "puuid": me[1], "profileIcon": PROFILE_ICONS[me[0]]}
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
        want = "Basic " + base64.b64encode(f"riot:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != want:
            return self.reply({"message": "unauthorized"}, 401)
        time.sleep(DELAY)
        p = self.path
        by_id = {str(s[0]): s for s in PLAYERS}
        by_puuid = {s[1]: s for s in PLAYERS}
        if p == "/lol-gameflow/v1/gameflow-phase":
            return self.reply("ChampSelect")
        if p == "/lol-summoner/v1/current-summoner":
            return self.reply({"summonerId": 1, "puuid": "p-me", "gameName": "峡谷的风", "tagLine": "12345",
                               "profileIconId": PROFILE_ICONS[1], "summonerLevel": 141})
        if p == "/lol-champ-select/v1/session":
            return self.reply({"localPlayerCellId": 0, "myTeam": [
                {"cellId": i, "summonerId": s[0], "puuid": s[1], "assignedPosition": s[3], "championId": s[6],
                 "championPickIntent": 0} for i, s in enumerate(PLAYERS)],
                "theirTeam": [{"cellId": 5 + i, "championId": c} for i, c in enumerate(ENEMIES)]})
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
                               "summonerLevel": 100 + s[0] * 41, "profileIconId": PROFILE_ICONS[s[0]]}) if s else self.reply({}, 404)
        if m := re.fullmatch(r"/lol-ranked/v1/ranked-stats/(.+)", p):
            tier, div = {"p-2": ("DIAMOND", "III"), "p-3": ("SILVER", "I"), "p-4": ("EMERALD", "IV"),
                         "p-5": ("", "")}.get(m.group(1), ("PLATINUM", "II"))
            return self.reply({"queueMap": {"RANKED_SOLO_5x5": {"tier": tier, "division": div, "leaguePoints": 47,
                                                                "wins": 88, "losses": 79}}})
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

    def do_PUT(self):
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
