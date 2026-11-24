"""检查更新（Gitee + GitHub）和一键更新。

一键更新的流程：
1. 旧程序把新版 exe 下载到 %LOCALAPPDATA%\\LOLStats\\update\\；
2. 用 `新exe --apply-update <旧exe路径> <旧程序PID>` 启动新版，旧程序退出；
3. 新版等旧程序退出后，把自己复制到旧 exe 的位置，再启动它，然后自己退出。
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from assets import _CTX

GITEE = "lclplm/lol-stats"
GITHUB = "lcljskasa/lol-stats"
EXE_NAME = "LOLStats.exe"
UA = {"User-Agent": "Mozilla/5.0"}


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def _get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _from_gitee():
    d = _get_json(f"https://gitee.com/api/v5/repos/{GITEE}/releases/latest")
    tag = d.get("tag_name") or ""
    exe = next((a.get("browser_download_url") for a in d.get("assets") or [] if a.get("name") == EXE_NAME), None)
    return {"tag": tag, "notes": d.get("body") or "", "exe": exe, "page": f"https://gitee.com/{GITEE}/releases/tag/{tag}"}


def _from_github():
    # releases/latest 会跳转到最新版本页，从跳转地址读版本号（不受 API 限流影响）
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_CTX))
    tag = None
    try:
        opener.open(urllib.request.Request(f"https://github.com/{GITHUB}/releases/latest", method="HEAD", headers=UA), timeout=10)
    except urllib.error.HTTPError as e:
        m = re.search(r"/releases/tag/([^/?#]+)", e.headers.get("Location") or "")
        tag = m.group(1) if m else None
    if not tag:
        return None
    notes = ""
    try:  # 说明文字要走 API，被限流就不显示，不影响更新
        notes = _get_json(f"https://api.github.com/repos/{GITHUB}/releases/tags/{tag}").get("body") or ""
    except Exception:
        pass
    return {"tag": tag, "notes": notes, "exe": f"https://github.com/{GITHUB}/releases/download/{tag}/{EXE_NAME}",
            "page": f"https://github.com/{GITHUB}/releases/tag/{tag}"}


def check(current):
    """返回新版本信息；没有新版或都连不上时返回 None。Gitee（国内快）优先，GitHub 兜底。"""
    found = []
    for fn in (_from_gitee, _from_github):
        try:
            r = fn()
            if r and r.get("tag"):
                found.append(r)
        except Exception:
            continue
    if not found:
        return None
    best = max(found, key=lambda r: parse_version(r["tag"]))
    if parse_version(best["tag"]) <= parse_version(current):
        return None
    same = [r for r in found if r["tag"] == best["tag"]]  # 两边都有这个版本：都能当下载源
    return {
        "version": best["tag"].lstrip("v"),
        "notes": next((r["notes"] for r in same if r["notes"]), ""),
        "page": same[0]["page"],
        "urls": [r["exe"] for r in same if r.get("exe")],
    }


def download(urls, dest, progress):
    """依次尝试各个下载地址，progress(已下载, 总大小)。返回下载好的文件路径。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last = None
    for url in urls:
        part = dest + ".part"
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30, context=_CTX) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    progress(done, total)
            with open(part, "rb") as f:
                head = f.read(2)
            size = os.path.getsize(part)
            if head != b"MZ" or size < 5 * 1024 * 1024 or (total and size != total):
                raise RuntimeError("下载的文件不完整")
            os.replace(part, dest)
            return dest
        except Exception as e:
            last = e
            try:
                os.remove(part)
            except OSError:
                pass
    raise RuntimeError(f"下载失败：{last}")


def _wait_pid(pid, timeout=40):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return  # 进程已经不在了
    k32.WaitForSingleObject(h, int(timeout * 1000))
    k32.CloseHandle(h)


def apply_update(target, pid, launch=True):
    """新版程序执行：等旧程序退出，把自己复制过去，再启动。"""
    import sys
    _wait_pid(pid)
    src = sys.executable
    for _ in range(30):  # 杀毒软件可能短暂锁住文件，重试一会儿
        try:
            shutil.copy2(src, target)
            break
        except OSError:
            time.sleep(1)
    else:
        target = src  # 实在替换不了：直接运行新版本身，至少用上新版
    if launch:
        subprocess.Popen([target], cwd=os.path.dirname(target), creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
    return target
