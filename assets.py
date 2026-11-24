"""游戏素材：英雄/装备/符文/召唤师技能的名称和图标。

优先从客户端本地接口读（和客户端语言一致）；没开客户端或客户端没有时，改从 CommunityDragon
（公开的英雄联盟素材镜像）下载并缓存到本地。
"""
import hashlib
import json
import os
import ssl
import threading
import time
import urllib.request

from lcu import CACHE_DIR, NotConnected

CDRAGON = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global"
ASSET_DIR = os.path.join(CACHE_DIR, "assets")
LISTS = {"champions": "champion-summary", "items": "items", "perks": "perks",
         "styles": "perkstyles", "spells": "summoner-spells"}

try:  # 用系统证书库校验 HTTPS（公司网络/代理环境下更稳）
    import truststore
    _CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    _CTX = ssl.create_default_context()


def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 lol-stats"})
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as resp:
        return resp.read()


def fetch_cached(url, max_age=86400):
    """下载并缓存到本地文件；缓存没过期就直接读。下载失败时用旧缓存。"""
    path = os.path.join(ASSET_DIR, hashlib.sha1(url.encode()).hexdigest())
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
        with open(path, "rb") as f:
            return f.read()
    try:
        body = fetch_url(url)
    except Exception:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        return None
    os.makedirs(ASSET_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)
    return body


def cdragon_url(lcu_path):
    """把客户端素材路径换成 CommunityDragon 地址：/lol-game-data/assets/X → .../global/default/x（小写）。"""
    rest = lcu_path.split("/lol-game-data/assets/", 1)[-1].lstrip("/")
    return f"{CDRAGON}/default/{rest.lower()}"


class Assets:
    def __init__(self):
        self.lcu = None
        self._lists = {}
        self._bytes = {}
        self._lock = threading.Lock()

    def set_lcu(self, lcu):
        self.lcu = lcu
        if lcu is not None:  # 连上客户端后改用客户端的名称（语言跟客户端一致）
            with self._lock:
                self._lists.clear()

    # ---- JSON 列表 ----
    def _client_json(self, path):
        if not self.lcu:
            return None
        try:
            return self.lcu.get(path)
        except NotConnected:
            return None
        except Exception:
            return None

    def _list(self, kind):
        with self._lock:
            if kind in self._lists:
                return self._lists[kind]
        name = LISTS[kind]
        data = self._client_json(f"/lol-game-data/assets/v1/{name}.json")
        if not data:
            body = fetch_cached(f"{CDRAGON}/zh_cn/v1/{name}.json")
            data = json.loads(body) if body else []
        if isinstance(data, dict):  # perkstyles 外面还包了一层
            data = data.get("styles") or []
        data = [d for d in data if isinstance(d, dict) and d.get("id", -1) > 0]
        with self._lock:
            self._lists[kind] = data
        return data

    def names(self):
        out = {kind: {d["id"]: d.get("name", "") for d in self._list(kind)} for kind in LISTS}
        # 每个符文系的符文树：{系id: [[基石...], [第1行...], [第2行...], [第3行...]]}，以及属性碎片的 3 行
        out["prices"] = {d["id"]: d.get("priceTotal", 0) for d in self._list("items")}
        out["slots"], out["shards"] = {}, []
        for st in self._list("styles"):
            rows = [s.get("perks") or [] for s in st.get("slots") or []]
            types = [s.get("type") for s in st.get("slots") or []]
            out["slots"][st["id"]] = [r for r, t in zip(rows, types) if t != "kStatMod"]
            if not out["shards"]:
                out["shards"] = [r for r, t in zip(rows, types) if t == "kStatMod"]
        return out

    def ability(self, champ, key):
        """英雄技能图标，key 是 p / q / w / e / r。"""
        info = self._client_json(f"/lol-game-data/assets/v1/champions/{champ}.json") or {}
        if not info.get("spells"):
            body = fetch_cached(f"{CDRAGON}/zh_cn/v1/champions/{champ}.json", max_age=7 * 86400)
            info = json.loads(body) if body else {}
        if key == "p":
            path = (info.get("passive") or {}).get("abilityIconPath")
        else:
            path = next((s.get("abilityIconPath") for s in info.get("spells") or [] if (s.get("spellKey") or "").lower() == key), None)
        body = self._get_bytes(path) if path else None
        return (body, "image/png" if body[:4] == bytes([0x89, 0x50, 0x4E, 0x47]) else "image/jpeg") if body else (None, "")

    # ---- 图片 ----
    def _get_bytes(self, path):
        """先找客户端，没有再去 CommunityDragon。"""
        if path in self._bytes:
            return self._bytes[path]
        body = None
        if self.lcu:
            try:
                body = self.lcu.raw(path)
            except Exception:
                body = None
        if body is None:
            body = fetch_cached(cdragon_url(path), max_age=30 * 86400)
        if body:
            self._bytes[path] = body
        return body

    def _champion(self, cid):
        info = self._client_json(f"/lol-game-data/assets/v1/champions/{cid}.json")
        if not info:
            body = fetch_cached(f"{CDRAGON}/zh_cn/v1/champions/{cid}.json", max_age=7 * 86400)
            info = json.loads(body) if body else {}
        return info

    def art(self, kind, ident):
        """kind: square / load / splash / profile / item / perk / style / spell。返回 (bytes, content-type)。"""
        path = None
        if kind == "profile":
            path = f"/lol-game-data/assets/v1/profile-icons/{ident}.jpg"
        elif kind in ("square", "load", "splash"):
            if kind == "square":
                path = f"/lol-game-data/assets/v1/champion-icons/{ident}.png"
                info = self._champion(ident) if not self.lcu else self._client_json(
                    f"/lol-game-data/assets/v1/champions/{ident}.json") or {}
                path = info.get("squarePortraitPath") or path
            else:
                info = self._champion(ident)
                skin = (info.get("skins") or [{}])[0]
                path = (skin.get("loadScreenPath") or skin.get("splashPath")) if kind == "load" \
                    else (skin.get("uncenteredSplashPath") or skin.get("splashPath"))
        elif kind in ("item", "perk", "style", "spell"):
            lst = self._list({"item": "items", "perk": "perks", "style": "styles", "spell": "spells"}[kind])
            path = next((d.get("iconPath") for d in lst if d["id"] == ident), None)
        if not path:
            return None, ""
        body = self._get_bytes(path)
        if not body:
            return None, ""
        if body[:4] == b"\x89PNG":
            ctype = "image/png"
        elif body[:4] == b"RIFF":
            ctype = "image/webp"
        else:
            ctype = "image/jpeg"
        return body, ctype


ASSETS = Assets()
