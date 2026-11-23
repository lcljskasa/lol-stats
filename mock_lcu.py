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
            "participants": [{"championId": rnd.choice(s[5]) if main else rnd.choice(CHAMPS)[0], "stats": {
                "win": win, "kills": rnd.randint(0, 13), "deaths": rnd.randint(1, 9), "assists": rnd.randint(1, 16),
                "totalMinionsKilled": rnd.randint(20, 260), "neutralMinionsKilled": rnd.randint(0, 20)}}],
        })
        t -= rnd.randint(30, 600) * 60000
    return games


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
                 "championPickIntent": 0} for i, s in enumerate(PLAYERS)]})
        if p == "/lol-game-data/assets/v1/champion-summary.json":
            return self.reply([{"id": -1, "name": "无"}] + [{"id": c, "name": n} for c, n in CHAMPS])
        if m := re.fullmatch(r"/lol-game-data/assets/v1/champions/(\d+)\.json", p):
            cid = m.group(1)
            return self.reply({"id": int(cid), "squarePortraitPath": f"/mock/{cid}_square.png",
                               "skins": [{"loadScreenPath": f"/mock/{cid}_load.jpg",
                                          "uncenteredSplashPath": f"/mock/{cid}_splash.jpg"}]})
        if m := re.fullmatch(r"/mock/([\w.]+)", p):
            f = os.path.join(ASSETS, m.group(1))
            if os.path.exists(f):
                return self.reply(open(f, "rb").read(), ctype="image/png" if f.endswith(".png") else "image/jpeg")
            if m.group(1).endswith("_square.png"):
                return self.reply(icon_png(int(m.group(1).split("_")[0])), ctype="image/png")
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


if __name__ == "__main__":
    print(f"模拟客户端已启动：http://127.0.0.1:{PORT}  密码 {TOKEN}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
