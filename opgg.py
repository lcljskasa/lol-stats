"""OP.GG 英雄数据：出装、符文、技能加点、克制关系、梯队榜、对位推荐。

数据来自 OP.GG 网站背后的接口（非官方，League Akari 也在用），对方改接口时这里可能失效。
"""
import json
import threading
import time
import urllib.parse

from assets import fetch_url

BASE = "https://lol-api-champion.op.gg/api/global/champions"
# 我们的位置名 ↔ OP.GG 的位置名
TO_OPGG = {"top": "top", "jungle": "jungle", "middle": "mid", "bottom": "adc", "utility": "support"}
FROM_OPGG = {"TOP": "top", "JUNGLE": "jungle", "MID": "middle", "ADC": "bottom", "SUPPORT": "utility"}
TIERS = ("all", "platinum_plus", "emerald_plus", "diamond_plus", "master_plus")

_cache, _lock = {}, threading.Lock()


def _get(path, params=None, ttl=1800):
    url = f"{BASE}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    with _lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    data = json.loads(fetch_url(url, timeout=20)).get("data")
    with _lock:
        _cache[url] = (time.time(), data)
    return data


def _rate(x):
    return round(x["win"] / x["play"], 4) if x.get("play") else None


def _top(lst, n):
    return [{"ids": x.get("ids"), "play": x.get("play"), "win": _rate(x), "pick": x.get("pick_rate")} for x in (lst or [])[:n]]


def champion_list(mode="ranked", tier="emerald_plus"):
    params = {"tier": tier} if mode == "ranked" and tier in TIERS else None
    return _get(f"/{mode}", params) or []


def main_positions(mode="ranked"):
    """每个英雄最常走的位置（按 role_rate 从高到低）。"""
    out = {}
    for c in champion_list(mode):
        ps = sorted(c.get("positions") or [], key=lambda p: -(p.get("stats") or {}).get("role_rate", 0))
        out[c["id"]] = [FROM_OPGG.get(p["name"]) for p in ps if FROM_OPGG.get(p["name"])]
    return out


def tiers(mode="ranked", tier="emerald_plus"):
    """梯队榜：每个英雄每个位置一行。"""
    rows = []
    for c in champion_list(mode, tier):
        if mode == "aram":
            st = c.get("average_stats") or {}
            td = st.get("tier_data") or {}
            rows.append({"id": c["id"], "pos": "", "tier": td.get("tier") or st.get("tier"), "rank": td.get("rank") or st.get("rank"),
                         "prev": td.get("rank_prev"), "win": st.get("win_rate"), "pick": st.get("pick_rate"),
                         "ban": st.get("ban_rate"), "kda": st.get("kda")})
            continue
        for p in c.get("positions") or []:
            st = p.get("stats") or {}
            td = st.get("tier_data") or {}
            rows.append({"id": c["id"], "pos": FROM_OPGG.get(p["name"], ""), "tier": td.get("tier"), "rank": td.get("rank"),
                         "prev": td.get("rank_prev"), "win": st.get("win_rate"), "pick": st.get("pick_rate"),
                         "ban": st.get("ban_rate"), "kda": st.get("kda"), "role": st.get("role_rate")})
    rows.sort(key=lambda r: (r["pos"], r["rank"] or 999))
    return rows


def _raw_build(champ, pos, mode, tier, target=None):
    if mode == "aram":
        return _get(f"/aram/{champ}/none")
    params = {"tier": tier} if tier in TIERS else {}
    if target:  # 只统计对位是 target 的对局：针对这个对手的出装和符文
        params["target_champion"] = target
    return _get(f"/ranked/{champ}/{TO_OPGG.get(pos, 'none') if pos else 'none'}", params or None)


def build(champ, pos=None, mode="ranked", tier="emerald_plus", target=None):
    """一个英雄（某位置）的推荐出装、符文、技能、克制关系、版本趋势。给了 target 就是针对这个对手的版本。"""
    if mode != "aram" and not pos:
        pos = (main_positions().get(champ) or [None])[0]
    target = target if mode != "aram" else None
    d = _raw_build(champ, pos, mode, tier, target) or {}
    sample = sum(r.get("play", 0) for r in d.get("core_items") or [])
    summary = d.get("summary") or {}
    positions = [{"pos": FROM_OPGG.get(p["name"]), "role": (p.get("stats") or {}).get("role_rate"),
                  "win": (p.get("stats") or {}).get("win_rate")}
                 for p in summary.get("positions") or [] if FROM_OPGG.get(p["name"])]
    cur = next((p for p in summary.get("positions") or [] if FROM_OPGG.get(p["name"]) == pos), None)
    st = (cur or {}).get("stats") or summary.get("average_stats") or {}
    td = st.get("tier_data") or {}

    runes = []
    for r in (d.get("runes") or [])[:4]:
        runes.append({"primary": r.get("primary_page_id"), "primaryRunes": r.get("primary_rune_ids"),
                      "secondary": r.get("secondary_page_id"), "secondaryRunes": r.get("secondary_rune_ids"),
                      "shards": r.get("stat_mod_ids"), "play": r.get("play"), "win": _rate(r), "pick": r.get("pick_rate")})
    skills = None
    if d.get("skill_masteries"):
        m = d["skill_masteries"][0]
        order = ((m.get("builds") or [{}])[0]).get("order") or ((d.get("skills") or [{}])[0]).get("order")
        skills = {"priority": m.get("ids"), "order": order, "win": _rate(m), "pick": m.get("pick_rate")}

    counters = [{"id": c["champion_id"], "play": c["play"], "win": _rate(c)} for c in d.get("counters") or [] if c.get("play")]
    counters.sort(key=lambda c: -c["play"])
    common = counters[:20]  # 只看常见对手，样本太少的胜率不准
    trend = [{"v": t.get("version"), "win": t.get("rate"), "rank": t.get("rank")}
             for t in ((d.get("trends") or {}).get("win") or [])][:8][::-1]
    return {
        "champ": champ, "pos": pos, "mode": mode, "tier": tier, "positions": positions, "target": target,
        "sample": sample,  # 针对对手时样本可能很少，界面上提示
        "stats": {"win": st.get("win_rate"), "pick": st.get("pick_rate"), "ban": st.get("ban_rate"), "kda": st.get("kda"),
                  "play": st.get("play"), "tier": td.get("tier") or st.get("tier"), "rank": td.get("rank") or st.get("rank")},
        "spells": _top(d.get("summoner_spells"), 2),
        "starter": _top(d.get("starter_items"), 2),
        "boots": _top(d.get("boots"), 3),
        "core": _top(d.get("core_items"), 3),
        "last": _top(d.get("last_items"), 6),
        "runes": runes,
        "skills": skills,
        "hard": sorted(common, key=lambda c: c["win"])[:5],      # 我方胜率最低：难打
        "easy": sorted(common, key=lambda c: -c["win"])[:5],     # 我方胜率最高：好打
        "trend": trend,
    }


def matchup(my_champ, enemy, pos, tier="emerald_plus"):
    """对位推荐：在这个位置，哪些英雄打 enemy 胜率高；我当前英雄打它的胜率。

    用的是对面英雄自己的克制表：表里记录的是“对面英雄”打各英雄的胜率，越低说明那个英雄越克制它。
    """
    d = _raw_build(enemy, pos, "ranked", tier) or {}
    rows = [{"id": c["champion_id"], "play": c["play"], "win": round(1 - _rate(c), 4)}
            for c in d.get("counters") or [] if c.get("play")]
    total = sum(r["play"] for r in rows) or 1
    picks = [r for r in rows if r["play"] / total >= 0.005]  # 去掉太冷门的对局
    picks.sort(key=lambda r: -r["win"])
    mine = next((r for r in rows if r["id"] == my_champ), None) if my_champ else None
    return {"enemy": enemy, "pos": pos, "picks": picks[:8], "mine": mine}
