"""游戏内实时数据：读 Riot 官方的 Live Client Data API（游戏运行时本机 2999 端口，只读），整理给悬浮窗用。"""
import json
import os
import ssl
import urllib.request

from assets import ASSETS

LIVE_BASE = os.environ.get("LIVE_BASE", "https://127.0.0.1:2999")  # 离线测试时可以指向模拟服务
_CTX = ssl._create_unverified_context()  # 游戏用的是自签名证书

# 重伤装备（克制治疗）
GRIEVOUS = {3123, 3033, 6609, 3165, 3916, 3075, 3076, 3011}
# 除了“生命偷取 / 全能吸血”分类以外，额外算作治疗来源的装备：香炉、月石、救赎、振奋、裂隙
HEAL_ITEMS = {3504, 6617, 3107, 3065, 4633}
# 技能自带大量回复的英雄：索拉卡、剑魔、吸血鬼、塞拉斯、悠米、琴女、娜美、乌鸦、蒙多、狼人、稻草人、
# 贝蕾亚、奥拉夫、俄洛伊、赛娜、米利欧、厄加特、扎克、沃利贝尔、妮蔻、斯卡纳
HEAL_CHAMPS = {16, 266, 8, 517, 350, 37, 267, 50, 36, 19, 9, 233, 2, 420, 235, 902, 6, 154, 106, 518, 72}
CONTROL_WARD = 2055
POSITIONS = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "middle", "BOTTOM": "bottom", "UTILITY": "utility"}


def fetch():
    """游戏没开时返回 None。"""
    try:
        with urllib.request.urlopen(f"{LIVE_BASE}/liveclientdata/allgamedata", context=_CTX, timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _items_index():
    return {i["id"]: i for i in ASSETS._list("items")}


def _alias_index():
    return {(c.get("alias") or "").lower(): c["id"] for c in ASSETS._list("champions")}


def summarize(data):
    if not data:
        return {"active": False}
    items, aliases = _items_index(), _alias_index()
    me_info = data.get("activePlayer") or {}
    me_key = me_info.get("riotId") or me_info.get("summonerName")
    players, me = [], None
    for p in data.get("allPlayers") or []:
        raw = (p.get("rawChampionName") or "").rsplit("_", 1)[-1].lower()
        champ = aliases.get(raw)
        inv = [i for i in p.get("items") or [] if i.get("itemID")]
        value = 0
        for i in inv:
            info = items.get(i["itemID"], {})
            if "Consumable" in (info.get("categories") or []) or i.get("consumable"):
                continue  # 药水、控制守卫这些消耗品不算装备价值
            value += info.get("priceTotal", 0)
        ids = {i["itemID"] for i in inv}
        heal_items = [i for i in ids if i in HEAL_ITEMS
                      or {"LifeSteal", "SpellVamp"} & set(items.get(i, {}).get("categories") or [])]
        sc = p.get("scores") or {}
        row = {
            "champ": champ, "name": p.get("riotIdGameName") or p.get("summonerName") or "",
            "team": p.get("team"), "pos": POSITIONS.get((p.get("position") or "").upper(), ""),
            "level": p.get("level", 0), "dead": bool(p.get("isDead")), "respawn": round(p.get("respawnTimer") or 0),
            "k": sc.get("kills", 0), "d": sc.get("deaths", 0), "a": sc.get("assists", 0),
            "cs": sc.get("creepScore", 0), "ward": round(sc.get("wardScore") or 0),
            "items": [i["itemID"] for i in sorted(inv, key=lambda i: i.get("slot", 9))],
            "value": value, "grievous": sorted(ids & GRIEVOUS), "heal": sorted(heal_items),
            "healChamp": champ in HEAL_CHAMPS,
            "controlWards": sum(i.get("count", 1) for i in inv if i["itemID"] == CONTROL_WARD),
        }
        players.append(row)
        if me_key and me_key in (p.get("riotId"), p.get("summonerName")):
            me = row
    if not me and players:
        me = players[0]
    my_team = me["team"] if me else "ORDER"
    mine = [p for p in players if p["team"] == my_team]
    enemy = [p for p in players if p["team"] != my_team]
    abilities = {k: (v or {}).get("abilityLevel", 0) for k, v in (me_info.get("abilities") or {}).items() if k in "QWER"}
    game = data.get("gameData") or {}
    return {
        "active": True,
        "time": round(game.get("gameTime") or 0),
        "mode": game.get("gameMode"),  # CLASSIC / ARAM / ...
        "me": me, "level": me_info.get("level", me["level"] if me else 0), "gold": round(me_info.get("currentGold") or 0),
        "abilities": abilities,
        "mine": mine, "enemy": enemy,
        "value": {"mine": sum(p["value"] for p in mine), "enemy": sum(p["value"] for p in enemy)},
        "kills": {"mine": sum(p["k"] for p in mine), "enemy": sum(p["k"] for p in enemy)},
        # 敌方的治疗来源 / 我方的重伤
        "enemyHeal": [{"champ": p["champ"], "items": p["heal"], "kit": p["healChamp"]} for p in enemy if p["heal"] or p["healChamp"]],
        "myGrievous": [{"champ": p["champ"], "items": p["grievous"]} for p in mine if p["grievous"]],
    }
