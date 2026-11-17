#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""sfx.py — 视频音效选材（最小可用版）。

只用 Python 3.9 标准库和 macOS 自带的 afconvert；没有常驻进程，用完即走。
子命令：doctor find take register export clean。规格见同目录 SKILL.md。
"""
import argparse
import array
import base64
import datetime
import hashlib
import html
import http.client
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import zipfile

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_API = "https://freesound.org/apiv2"
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "sfx-skill-freesound", "api_key"
AFCONVERT = "/usr/bin/afconvert"
USER_AGENT = "sfx-skill/%s (personal sound picker; python-urllib)" % VERSION

RUN_LIMIT = 30                 # 每次运行最多发多少次 API 请求
DAY_LIMIT = 300                # 每天最多多少次（官方上限 2000）
CACHE_TTL = 7 * 86400          # 查询缓存保留 7 天
CACHE_CAP = 20 * 1024 * 1024   # 缓存目录上限 20 MB
PER_GROUP = 3                  # 每类动作给几个候选
MIN_RESULTS = 5                # 带指标过滤的结果少于这个数就去掉指标条件重查
PAGE_SIZE = 30
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_EMBED_BYTES = 3 * 1024 * 1024
MAX_ZIP_ENTRIES, MAX_ZIP_FILE, MAX_ZIP_TOTAL = 5000, 50 * 1024 * 1024, 1024 * 1024 * 1024
AUDIO_EXT = (".wav", ".mp3", ".ogg", ".oga", ".m4a", ".aif", ".aiff", ".flac")
MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".oga": "audio/ogg",
        ".m4a": "audio/mp4", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".flac": "audio/flac"}
FIELDS = ("id,name,tags,description,license,username,duration,num_downloads,avg_rating,num_ratings,"
          "previews,duration_effective,start_time,single_event,amplitude_peak_ratio,"
          "temporal_centroid_ratio,loudness")
STRENGTHS = {"weak": "weak", "弱": "weak", "轻": "weak", "medium": "medium", "中": "medium",
             "strong": "strong", "强": "strong", "重": "strong"}
NO_KEY_MSG = ("没有 Freesound key。请在你自己的终端运行："
              "security add-generic-password -s %s -a %s -w  （按提示粘贴，不要把 key 发到聊天里）"
              % (KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT))


class SfxError(Exception):
    pass


# ---------------------------------------------------------------- 基础工具

_SECRETS = set()


def redact(text):
    """把任何可能出现的 key 换成 ***。所有输出和落盘文本都经过这里。"""
    text = str(text)
    for s in _SECRETS:
        if s and s in text:
            text = text.replace(s, "***")
    return text


def out(msg=""):
    print(redact(msg))


def log(msg):
    print(redact(msg), file=sys.stderr)


def _run(cmd, **kw):
    """子进程统一入口（测试可替换）。"""
    return subprocess.run(cmd, **kw)


def clean(s, n=200):
    """把不可信文本压成一行、去掉控制字符并截断。只是清洗，不是转义。"""
    s = "" if s is None else str(s)
    s = re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:n]


def safe_name(s, n=80):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("._")[:n] or "x"


def is_http_url(u):
    try:
        p = urllib.parse.urlparse(str(u))
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def home():
    return os.environ.get("SFX_HOME") or os.path.expanduser("~/Library/Application Support/sfx-skill")


def api_base():
    return (os.environ.get("SFX_API_BASE") or DEFAULT_API).rstrip("/")


def cache_dir():
    return os.path.join(home(), "cache")


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def today():
    return datetime.date.today().isoformat()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_text(path, text):
    """原子写入；写之前再做一次 key 脱敏。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(redact(text))
    os.replace(tmp, path)


def write_json(path, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_key():
    """环境变量优先，其次钥匙串。返回值只在内存里用，永不输出。"""
    k = os.environ.get("SFX_FREESOUND_KEY", "").strip()
    if not k and not os.environ.get("SFX_NO_KEYCHAIN"):
        try:
            p = _run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                      "-a", KEYCHAIN_ACCOUNT, "-w"], capture_output=True, text=True, timeout=15)
            if p.returncode == 0:
                k = (p.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            k = ""
    if len(k) >= 8:
        _SECRETS.add(k)
    return k


# ---------------------------------------------------------------- 许可闸门

def load_licenses():
    return load_json(os.path.join(HERE, "licenses.json"))


def classify_license(raw):
    """把许可字段归类。同时接受名称和网址两种写法；认不出的一律 unknown。"""
    s = str(raw or "").strip().lower()
    if not s:
        return "unknown"
    if "publicdomain/zero" in s or re.fullmatch(r"(cc0|creative commons 0|creative commons zero)( 1\.0)?", s):
        return "cc0"
    if "sampling" in s:
        return "sampling+"
    if re.search(r"by-nc|non[- ]?commercial", s):
        return "cc-by-nc"
    if re.search(r"by-sa|share[- ]?alike|by-nd|no[- ]?deriv", s):
        return "unknown"
    if "/licenses/by/" in s or re.fullmatch(r"(attribution|cc[- ]?by)( \d\.\d)?", s):
        return "cc-by"
    return "unknown"


def license_label(cls, raw, lic):
    base = lic["classes"].get(cls, {}).get("label", cls)
    if cls == "cc-by":
        m = re.search(r"(\d\.\d)", str(raw or ""))
        return "CC BY " + m.group(1) if m else "CC BY"
    return base


def allowed(cls, lic):
    return bool(lic["classes"].get(cls, {}).get("allowed"))


def credit_line(entry, lic):
    """需要署名时返回署名文字，否则 None。格式取自 Freesound FAQ。"""
    c = lic["classes"].get(entry.get("license"), {})
    if not c.get("attribution"):
        return None
    tpl = c.get("template") or '"{title}" by {author} ( {url} ) licensed under {license}'
    return tpl.format(title=clean(entry.get("title")), author=clean(entry.get("author")),
                      url=entry.get("url", ""),
                      license=license_label(entry.get("license"), entry.get("license_raw"), lic))


# ---------------------------------------------------------------- 台账

def ledger_path():
    return os.path.join(home(), "ledger.jsonl")


def ledger_load():
    """读台账。只追加：同一个 id 后写的行覆盖先写的字段。"""
    entries = {}
    try:
        with open(ledger_path(), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    entries[e["id"]] = dict(entries.get(e["id"], {}), **e)
                except (ValueError, KeyError, TypeError):
                    log("提示：台账第 %d 行读不了，已跳过" % n)
    except FileNotFoundError:
        pass
    return entries


def ledger_append(entry):
    os.makedirs(home(), exist_ok=True)
    with open(ledger_path(), "a", encoding="utf-8") as f:
        f.write(redact(json.dumps(entry, ensure_ascii=False)) + "\n")


# ---------------------------------------------------------------- 缓存

def cache_prune():
    """删过期缓存，再把总量压到上限以内（先删最旧的）。usage.json 不动。"""
    files, removed, freed = [], 0, 0
    for root, _dirs, names in os.walk(cache_dir()):
        for n in names:
            if n == "usage.json":
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    limit = time.time() - CACHE_TTL
    keep = []
    for mt, size, p in sorted(files):
        if mt < limit:
            os.remove(p)
            removed, freed = removed + 1, freed + size
        else:
            keep.append((mt, size, p))
    total = sum(s for _m, s, _p in keep)
    for mt, size, p in keep:
        if total <= CACHE_CAP:
            break
        os.remove(p)
        total, removed, freed = total - size, removed + 1, freed + size
    return removed, freed, total


# ---------------------------------------------------------------- 联网

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 带 key 的请求不跟随重定向，避免把 key 送到别的主机


class Net:
    """Freesound 官方 API 客户端：串行、限速、限额、缓存、有限重试。"""

    def __init__(self, key):
        self.key = key
        self.base = api_base()
        self.run_count = 0        # 本次运行发出的 API 请求数（含重试）
        self.downloads = 0
        self.last = 0.0
        self.disabled = None      # 一旦填了原因，本次运行不再访问 Freesound
        self.timeout = env_float("SFX_TIMEOUT", 30)
        self.interval = env_float("SFX_MIN_INTERVAL", 1.0)
        try:
            self.delays = [float(x) for x in os.environ.get("SFX_RETRY_DELAYS", "2,5").split(",")][:2]
        except ValueError:
            self.delays = [2.0, 5.0]

    # ---- 用量计数
    def _usage_path(self):
        return os.path.join(cache_dir(), "usage.json")

    def usage(self):
        try:
            u = load_json(self._usage_path())
            if u.get("date") == today():
                return u
        except (OSError, ValueError):
            pass
        return {"date": today(), "count": 0}

    def _save_usage(self, u):
        write_json(self._usage_path(), u)

    # ---- 底层
    def _opener(self, url, with_key):
        handlers = []
        host = urllib.parse.urlparse(url).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            handlers.append(urllib.request.ProxyHandler({}))  # 本机地址不走系统代理
        if with_key:
            handlers.append(_NoRedirect())
        return urllib.request.build_opener(*handlers)  # 其余情况自动跟随系统代理

    def _throttle(self):
        wait = self.interval - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()

    def _same_host(self, url):
        return urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(self.base).netloc

    def _fetch(self, url, with_key, limit, count):
        """发一次或多次请求。只对超时、连接错误、5xx 重试，最多 2 次。"""
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if with_key and self.key:
            headers["Authorization"] = "Token " + self.key
        last = ""
        for attempt in range(3):
            if count:
                u = self.usage()
                if u.get("blocked"):
                    self.disabled = "今天已触发 Freesound 日限额，明天再用"
                elif u["count"] >= DAY_LIMIT:
                    self.disabled = "今天已用满本地日限额 %d 次" % DAY_LIMIT
                elif self.run_count >= RUN_LIMIT:
                    self.disabled = "本次运行已用满 %d 次请求" % RUN_LIMIT
                if self.disabled:
                    raise SfxError(self.disabled)
            self._throttle()
            if count:
                self.run_count += 1
                u["count"] += 1
                self._save_usage(u)
            try:
                req = urllib.request.Request(url, headers=headers)
                with self._opener(url, with_key).open(req, timeout=self.timeout) as r:
                    body = r.read(limit + 1)
                if len(body) > limit:
                    raise SfxError("返回内容超过 %d 字节，已放弃" % limit)
                return body
            except urllib.error.HTTPError as e:
                detail = self._detail(e)
                if e.code == 429:
                    raise self._throttled(detail)
                if 500 <= e.code < 600:
                    last = "服务器错误 %d" % e.code
                else:
                    hint = {401: "key 无效或没有提供", 403: "被拒绝访问", 404: "地址不存在"}.get(e.code, "请求未成功")
                    if e.code in (401, 403) and count:
                        self.disabled = "Freesound 返回 %d（%s）" % (e.code, hint)
                    raise SfxError("Freesound 返回 %d（%s）%s" % (e.code, hint, "：" + detail if detail else ""))
            except (urllib.error.URLError, socket.timeout, http.client.HTTPException, OSError) as e:
                reason = getattr(e, "reason", e)
                last = "超时" if "timed out" in str(reason) or isinstance(reason, socket.timeout) else "连接失败（%s）" % clean(reason, 80)
            if attempt < 2:
                time.sleep(self.delays[attempt] if attempt < len(self.delays) else 0)
        raise SfxError("%s，已重试 2 次仍失败" % last)

    @staticmethod
    def _detail(e):
        try:
            raw = e.read(4096).decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""
        try:
            d = json.loads(raw)
            raw = d.get("detail", "") if isinstance(d, dict) else ""
        except ValueError:
            raw = ""
        return clean(raw, 160)

    def _throttled(self, detail):
        low = detail.lower()
        daily = any(w in low for w in ("day", "daily", "sustained", "2000", "24 hour"))
        if daily:
            u = self.usage()
            u["blocked"] = "daily-429"
            self._save_usage(u)
            self.disabled = "Freesound 日限额已满（429），今天停用，明天再试"
        else:
            self.disabled = "Freesound 分钟限额已满（429），请一分钟后再运行"
        return SfxError(self.disabled + ("；服务器说明：" + detail if detail else ""))

    # ---- 对外
    def search_url(self, params):
        return self.base + "/search/?" + urllib.parse.urlencode(sorted(params.items()))

    def search(self, params):
        """返回 (结果字典, 是否命中缓存)。缓存命中不耗配额。"""
        url = self.search_url(params)
        cpath = os.path.join(cache_dir(), "q-%s.json" % hashlib.sha256(url.encode()).hexdigest()[:20])
        try:
            if time.time() - os.stat(cpath).st_mtime < CACHE_TTL:
                return load_json(cpath)["data"], True
        except (OSError, ValueError, KeyError):
            pass
        if self.disabled:
            raise SfxError(self.disabled)
        if not self.key:
            self.disabled = NO_KEY_MSG
            raise SfxError(NO_KEY_MSG)
        body = self._fetch(url, True, 4 * 1024 * 1024, True)
        try:
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise SfxError("Freesound 返回的不是预期的 JSON")
        write_json(cpath, {"url": url, "data": data})
        return data, False

    def download(self, url, dest_stem):
        """下载一个预览文件，按文件头判断真实格式。只接受 https，或与 API 同主机的地址。"""
        if not is_http_url(url) or not (url.lower().startswith("https://") or self._same_host(url)):
            raise SfxError("预览地址不被接受")
        body = self._fetch(url, self._same_host(url), MAX_PREVIEW_BYTES, False)
        ext = sniff_ext(body)
        if not ext:
            raise SfxError("下载到的不是可识别的音频文件")
        self.downloads += 1
        path = dest_stem + ext
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        return path


# ---------------------------------------------------------------- 音频

def sniff_ext(b):
    """看文件头判断真实格式，不相信地址里的扩展名。"""
    if b[:4] == b"OggS":
        return ".ogg"
    if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return ".wav"
    if b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return ".mp3"
    if b[:4] == b"fLaC":
        return ".flac"
    if b[:4] == b"FORM" and b[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    if b[4:8] == b"ftyp":
        return ".m4a"
    return ""


def ogg_supported():
    """afconvert -hf 把格式表打印到 stderr，所以两个输出都要看。"""
    try:
        p = _run([AFCONVERT, "-hf"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return "Oggf" in ((p.stderr or "") + (p.stdout or ""))


def read_wav(path):
    """手写 RIFF 解析。afconvert 可能写扩展格式头（0xFFFE），Python 3.9 的 wave 模块读不了。"""
    with open(path, "rb") as f:
        b = f.read()
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        raise SfxError("不是 WAV 文件")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(b):
        cid, size = b[pos:pos + 4], struct.unpack("<I", b[pos + 4:pos + 8])[0]
        body = b[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            tag, ch, sr, _rate, _align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, sr, bits)
        elif cid == b"data":
            data = body
            break
        pos += 8 + size + (size & 1)
    if not fmt or data is None:
        raise SfxError("WAV 缺少 fmt 或 data 块")
    tag, ch, sr, bits = fmt
    if tag != 1 or bits != 16 or ch < 1 or sr < 1:
        raise SfxError("只支持 16 位 PCM 的 WAV")
    a = array.array("h")
    a.frombytes(data[:len(data) // (2 * ch) * (2 * ch)])
    if sys.byteorder == "big":
        a.byteswap()
    return sr, ch, a


def measure_samples(sr, ch, a):
    """量时长、起音、峰值时刻、尾音终点、峰值电平。单位秒和 dBFS。"""
    env = a if ch == 1 else [max(abs(a[i + c]) for c in range(ch)) for i in range(0, len(a) - ch + 1, ch)]
    n = len(env)
    if n == 0:
        return {"dur": 0.0}
    peak, pk = 0, 0
    for i, x in enumerate(env):
        v = x if x >= 0 else -x
        if v > peak:
            peak, pk = v, i
    if peak == 0:
        return {"dur": round(n / sr, 3), "silent": True}
    thr_s, thr_e = peak * 10 ** (-40 / 20.0), peak * 10 ** (-50 / 20.0)
    start = next(i for i, x in enumerate(env) if abs(x) >= thr_s)
    end = next(i for i in range(n - 1, -1, -1) if abs(env[i]) >= thr_e)
    return {"dur": round(n / sr, 3), "start": round(start / sr, 3), "peak": round(pk / sr, 3),
            "tail": round((end + 1) / sr, 3), "peak_db": round(20 * math.log10(peak / 32768.0), 1)}


def make_wav(src, dst):
    """解码成 48 kHz、16 位、标准头的 WAV，并返回量测结果。"""
    tmp = dst + ".dec.wav"
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)   # afconvert 不会自己建目录
    try:
        p = _run([AFCONVERT, "-f", "WAVE", "-d", "LEI16@48000", src, tmp],
                 capture_output=True, text=True, timeout=180)
        if p.returncode != 0 or not os.path.isfile(tmp):
            raise SfxError("afconvert 解码失败：" + clean(p.stderr or p.stdout, 120))
        sr, ch, a = read_wav(tmp)
    except (OSError, subprocess.SubprocessError) as e:
        raise SfxError("afconvert 无法运行：" + clean(e, 120))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    out_a = a
    if sys.byteorder == "big":
        out_a = array.array("h", a)
        out_a.byteswap()
    w = wave.open(dst, "wb")
    w.setnchannels(ch)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(out_a.tobytes())
    w.close()
    m = measure_samples(sr, ch, a)
    return m, (sr, ch, a)


def make_m4a(src, dst):
    try:
        p = _run([AFCONVERT, "-f", "m4af", "-d", "aac", "-b", "96000", src, dst],
                 capture_output=True, text=True, timeout=180)
        return p.returncode == 0 and os.path.isfile(dst)
    except (OSError, subprocess.SubprocessError):
        return False


def waveform_svg(sr, ch, a, cols=300):
    """可选的内嵌波形图：每列画最小值到最大值的一条竖线。"""
    mono = a if ch == 1 else a[::ch]
    step = max(1, len(mono) // cols)
    bars = []
    for i, c in enumerate(range(0, len(mono), step)):
        seg = mono[c:c + step]
        bars.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (i, i, 30 - max(seg) / 32768.0 * 29, 30 - min(seg) / 32768.0 * 29))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d 60" preserveAspectRatio="none" '
            'style="width:100%%;height:48px" stroke="currentColor" stroke-width="1">%s</svg>' % (max(1, len(bars)), "".join(bars)))


# ---------------------------------------------------------------- 事件清单与动作映射

def load_map():
    return load_json(os.path.join(HERE, "sfx_map.json"))


def resolve_action(name, smap):
    key = str(name or "").strip().lower()
    for k, spec in smap["actions"].items():
        if key == k or key in [str(x).lower() for x in spec.get("aliases", [])]:
            return k
    raise SfxError("不认识的动作类型：%s（可用：%s）" % (clean(name, 40), "、".join(sorted(smap["actions"]))))


def load_events(path, smap):
    try:
        data = load_json(path)
    except (OSError, ValueError) as e:
        raise SfxError("事件清单读不了：%s" % clean(e, 120))
    events = data if isinstance(data, list) else data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list) or not events:
        raise SfxError("事件清单里没有 events")
    project = clean(data.get("project"), 80) if isinstance(data, dict) else ""
    res, seen = [], set()
    for i, e in enumerate(events, 1):
        if not isinstance(e, dict):
            raise SfxError("第 %d 个事件不是对象" % i)
        try:
            t = float(e["t"])
            dur = float(e["dur"]) if e.get("dur") is not None else None
        except (KeyError, TypeError, ValueError):
            raise SfxError("第 %d 个事件缺少 t（秒）或 dur 不是数字" % i)
        if t < 0 or (dur is not None and dur < 0):
            raise SfxError("第 %d 个事件的 t 或 dur 是负数" % i)
        strength = STRENGTHS.get(str(e.get("strength", "medium")).strip().lower())
        if not strength:
            raise SfxError("第 %d 个事件的 strength 只能是 weak、medium、strong" % i)
        eid = safe_name(e.get("id") or "e%02d" % i, 24)
        if eid in seen:
            raise SfxError("事件 id 重复：%s" % eid)
        seen.add(eid)
        res.append({"id": eid, "t": t, "action": resolve_action(e.get("action"), smap), "dur": dur,
                    "strength": strength, "material": clean(e.get("material"), 40),
                    "style": clean(e.get("style"), 40), "note": clean(e.get("note"), 120)})
    return res, project or os.path.basename(os.path.dirname(os.path.abspath(path)))


def group_events(events, smap):
    """按“动作类型 + 强弱（+ 材质、风格）”去重：同一组全片用同一条音效。"""
    groups = {}
    for e in events:
        parts = [e["action"], e["strength"]] + [re.sub(r"[^\w]+", "_", x.lower()).strip("_") for x in (e["material"], e["style"]) if x]
        gid = "-".join(p for p in parts if p)
        g = groups.get(gid)
        if not g:
            spec = smap["actions"][e["action"]]
            g = groups[gid] = {
                "id": gid, "action": e["action"], "label": spec.get("label", e["action"]),
                "strength": e["strength"], "material": e["material"], "style": e["style"],
                "events": [], "queries": list(spec["queries"]), "negative": list(spec.get("negative", [])),
                "filter": dict(spec.get("filter", {})), "descriptor_filter": dict(spec.get("descriptor_filter", {})),
                "target": dict(spec.get("target", {})), "align": spec.get("align", "start"),
                "peak_target_db": spec.get("peak_target_db", -9) + smap.get("strength_gain_db", {}).get(e["strength"], 0),
                "local_tags": [str(x).lower() for x in spec.get("local_tags", [])]}
        g["events"].append(e["id"])
    for g in groups.values():
        durs = sorted(e["dur"] for e in events if e["id"] in g["events"] and e["dur"])
        g["dur"] = durs[len(durs) // 2] if durs else None
    return list(groups.values())


def query_text(g, query):
    words = [query] + [x for x in (g["material"], g["style"]) if x] + ["-" + n for n in g["negative"]]
    return " ".join(words)


def filter_text(lic, spec):
    names = [c["freesound_filter_name"] for c in lic["classes"].values() if c.get("allowed") and c.get("freesound_filter_name")]
    parts = ["license:(%s)" % " OR ".join('"%s"' % n for n in names)]
    for k in sorted(spec):
        v = spec[k]
        if isinstance(v, bool):
            parts.append("%s:%s" % (k, "true" if v else "false"))
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            parts.append("%s:[%s TO %s]" % (k, v[0], v[1]))
    return " ".join(parts)


def plan_steps(g):
    """这一组最多会发的请求（按顺序）：带指标 → 去掉指标 → 换第二组关键词。"""
    full = dict(g["filter"], **g["descriptor_filter"])
    plan = [(g["queries"][0], full, False, None)]
    if g["descriptor_filter"]:
        plan.append((g["queries"][0], g["filter"], True, MIN_RESULTS))   # 少于 5 条才发
    if len(g["queries"]) > 1:
        plan.append((g["queries"][1], g["filter"], True, PER_GROUP))     # 仍不足 3 条才发
    return plan


def planned_requests(g, lic):
    return [({"query": query_text(g, q), "filter": filter_text(lic, f), "fields": FIELDS,
              "page_size": str(PAGE_SIZE), "sort": "score"}, unf, need) for q, f, unf, need in plan_steps(g)]


def print_dry_run(events, groups, lic, key):
    """试运行的输出：每组第 1 步给出地址，后面的步骤只写与上一步的差别。"""
    fields = urllib.parse.urlencode({"fields": FIELDS})
    total = sum(len(plan_steps(g)) for g in groups)
    out("试运行：不联网、不写文件。%d 个事件，%d 组；最多发 %d 次请求（每次运行上限 %d），结果够用就提前停。"
        % (len(events), len(groups), total, RUN_LIMIT))
    out("请求头 Authorization: Token ***    key：%s" % ("有" if key else "无，实际运行时不会发请求"))
    out("每个请求都带 %s ，下面写作 fields=…" % fields)
    for g in groups:
        out("\n[%s] %s，%d 处" % (g["id"], g["label"], len(g["events"])))
        prev = None
        for i, ((params, _unf, need), (q, spec, _u, _n)) in enumerate(zip(planned_requests(g, lic), plan_steps(g)), 1):
            if prev is None:
                url = urllib.parse.urlencode(sorted(params.items())).replace(fields, "fields=…")
                out("  1. GET %s/search/?%s" % (api_base(), url))
            else:
                gone = sorted(set(prev[1]) - set(spec))
                diff = (["filter 去掉 " + "、".join(gone)] if gone else []) + (["query 换成 " + query_text(g, q)] if q != prev[0] else [])
                out("  %d. 不足 %d 条才发：%s" % (i, need, "；".join(diff) or "与上一步相同"))
            prev = (q, spec)


# ---------------------------------------------------------------- 打分与候选

def _num(v, default=None):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def score_result(r, g):
    """软打分：关键词、负面词、评分（按人数收缩）、下载量（取对数）、指标贴近度。"""
    name = clean(r.get("name"), 300).lower()
    tags = " ".join(clean(t, 40).lower() for t in (r.get("tags") or []) if isinstance(t, str))
    desc = clean(r.get("description"), 600).lower()
    words = set(w for q in g["queries"] for w in q.lower().split()) | set(x.lower() for x in (g["material"], g["style"]) if x)
    s = 0.0
    for w in words:
        s += 2.0 * (w in name) + 1.5 * (w in tags) + 0.5 * (w in desc)
    for n in g["negative"]:
        s -= 2.0 * (n.lower() in name or n.lower() in tags)
    n_r, avg = _num(r.get("num_ratings"), 0), _num(r.get("avg_rating"), 0)
    s += ((avg * n_r + 3.5 * 10) / (n_r + 10) - 3.5) / 1.5
    s += math.log10(1 + max(0, _num(r.get("num_downloads"), 0))) / 5.0
    for k, t in g["target"].items():
        v = _num(r.get(k))
        if v is not None and _num(t) is not None:
            s -= 2.0 * min(1.0, abs(v - t))
    want, d = g.get("dur"), _num(r.get("duration"))
    if want and d:
        s -= min(1.0, abs(d - want) / want)
    return round(s, 3)


def freesound_candidates(net, g, lic, notes):
    """查 Freesound，返回通过许可闸门并打好分的候选（已排序）。"""
    best = {}
    for params, unfiltered, need in planned_requests(g, lic):
        if need is not None and len(best) >= need:
            continue
        data, cached = net.search(params)
        notes.append("Freesound 请求：%s%s，返回 %d 条" % ("未带指标过滤" if unfiltered else "带指标过滤",
                                                 "（缓存）" if cached else "", len(data["results"])))
        got, blocked = [], []
        for r in data["results"]:
            if not isinstance(r, dict):
                continue
            try:
                sid = int(r["id"])
            except (KeyError, TypeError, ValueError):
                continue
            raw = r.get("license")
            cls = classify_license(raw)
            if not allowed(cls, lic):   # 二次校验：不信服务器端过滤
                blocked.append((sid, raw))
                continue
            pv = r.get("previews") if isinstance(r.get("previews"), dict) else {}
            previews = {k: pv.get("preview-hq-" + k) for k in ("ogg", "mp3") if is_http_url(pv.get("preview-hq-" + k))}
            if not previews:
                continue
            lo_hi = g["filter"].get("duration")
            d = _num(r.get("duration"))
            if lo_hi and d is not None and not (lo_hi[0] * 0.5 <= d <= lo_hi[1] * 1.5):
                continue
            got.append({"id": "freesound:%d" % sid, "source": "freesound", "url": "https://freesound.org/s/%d/" % sid,
                        "title": clean(r.get("name"), 120), "author": clean(r.get("username"), 80),
                        "license": cls, "license_raw": clean(raw, 120), "license_label": license_label(cls, raw, lic),
                        "tags": [clean(t, 30) for t in (r.get("tags") or []) if isinstance(t, str)][:20],
                        "score": score_result(r, g), "unfiltered": unfiltered, "previews": previews,
                        "server": {k: r.get(k) for k in ("duration", "start_time", "single_event", "amplitude_peak_ratio",
                                                         "temporal_centroid_ratio", "loudness") if r.get(k) is not None}})
        if blocked:   # 每次请求只汇总一行，附一个例子
            more = len(blocked) > 1
            notes.append("已排除 freesound:%d%s，许可不在白名单（%s原始值：%s）" % (
                blocked[0][0], " 等 %d 条" % len(blocked) if more else "", "例子的" if more else "", clean(blocked[0][1], 60)))
        for c in got:
            best.setdefault(c["id"], c)   # 先到的（带指标过滤的）保留原标记
    res = sorted(best.values(), key=lambda c: (-c["score"], c["id"]))
    return res, any(c["unfiltered"] for c in res)


def local_candidates(g, ledger, lic):
    """本地台账里适合这一组的条目；人耳确认过的排最前。"""
    res = []
    for e in ledger.values():
        if not allowed(e.get("license"), lic) or not e.get("file"):
            continue
        if not os.path.isfile(os.path.join(home(), e["file"])):
            continue
        tags = set(str(t).lower() for t in e.get("tags", []))
        words = tags | set(re.findall(r"[a-z]{2,}", str(e.get("title", "")).lower()))
        s = 5.0 * (("action:" + g["action"]) in tags) + len(set(g["local_tags"]) & words)
        if s <= 0:
            continue
        s += 2.0 * sum(1 for x in (g["material"], g["style"]) if x and x.lower() in words)
        m, rng = e.get("m") or {}, g["filter"].get("duration")
        dur = (m["tail"] - m["start"]) if "tail" in m and "start" in m else m.get("dur")   # 有量测就按有声部分的长度比
        if dur and rng and not (rng[0] * 0.5 <= dur <= rng[1] * 1.5):
            continue
        s += 10.0 * bool(e.get("ok"))
        res.append({"id": e["id"], "source": e.get("source", ""), "url": e.get("url", ""), "title": clean(e.get("title"), 120),
                    "author": clean(e.get("author"), 80), "license": e["license"], "license_raw": e.get("license_raw", ""),
                    "license_label": license_label(e["license"], e.get("license_raw"), lic), "tags": e.get("tags", [])[:20],
                    "score": round(s, 3), "local": True, "ok": bool(e.get("ok")), "file": e["file"], "m": e.get("m") or {}})
    res.sort(key=lambda c: (-c["score"], c["id"]))
    return res


def fetch_preview(net, cand, use_ogg, tmp):
    """下载预览（Ogg 优先，不行换 MP3），解码量测。成功返回 (缓存路径, wav 路径, 量测, 样本)。"""
    order = ["ogg", "mp3"] if use_ogg else ["mp3", "ogg"]
    stem = os.path.join(cache_dir(), "previews", safe_name(cand["id"].replace(":", "-")))
    errors = []
    for kind in order:
        url = cand.get("previews", {}).get(kind)
        if not url:
            continue
        if kind == "ogg" and not use_ogg:
            continue  # 本机解不了 Ogg，就不下载它
        try:
            cached = [p for p in (stem + "-" + kind + e for e in AUDIO_EXT) if os.path.isfile(p)]
            path = cached[0] if cached else net.download(url, stem + "-" + kind)
            wav = os.path.join(tmp, safe_name(cand["id"]) + ".wav")
            m, pcm = make_wav(path, wav)
            cand["via"] = "api-preview-" + kind
            return path, wav, m, pcm
        except SfxError as e:
            errors.append("%s 预览：%s" % (kind, e))
    raise SfxError("；".join(errors) or "没有可用的预览地址")


def manual_links(g):
    q = g["queries"][0]
    return [{"site": "Freesound 网页搜索", "url": "https://freesound.org/search/?q=" + urllib.parse.quote_plus(q)},
            {"site": "Mixkit 音效", "url": "https://mixkit.co/free-sound-effects/", "hint": "在站内搜：" + q},
            {"site": "Kenney", "url": "https://kenney.nl/", "hint": "在 Audio 分类里找"}]


# ---------------------------------------------------------------- 试听页

def render_audition(state, blobs, proj_dir):
    """生成单文件试听页。页面里所有来自外部的文字都先转义；不含任何脚本。"""
    e = html.escape
    parts = ['<!doctype html><html lang="zh"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; media-src data:; style-src 'unsafe-inline'; img-src data:\">",
             "<title>音效试听 · %s</title>" % e(state["project"]),
             "<style>body{font:15px/1.5 -apple-system,sans-serif;margin:12px;max-width:760px}"
             "h2{margin:22px 0 6px;font-size:17px}.c{border:1px solid #8884;border-radius:8px;padding:8px 10px;margin:8px 0}"
             "audio{width:100%;margin-top:6px}.m{opacity:.75;font-size:13px}code{font-size:12px;word-break:break-all}"
             ".w{color:#b45309}</style></head><body>",
             "<h1 style=\"font-size:20px\">音效试听 · %s</h1><p class=\"m\">生成于 %s。听完后把选中的编号告诉助手，或运行每条下面的命令。</p>"
             % (e(state["project"]), e(state["generated_at"]))]
    for g in state["groups"]:
        parts.append("<h2>%s（%s）· %d 处</h2>" % (e(g["label"]), e(g["id"]), len(g["events"])))
        for lv in g["levels"]:
            if lv["status"] != "ok":
                parts.append('<p class="m w">%s：%s</p>' % (e(lv["level"]), e(lv.get("reason", lv["status"]))))
        if g.get("links"):
            parts.append('<p class="w">候选不足，可以手动下载后登记：</p><ul>')
            for ln in g["links"]:
                parts.append('<li><a href="%s">%s</a> %s</li>' % (e(ln["url"], quote=True), e(ln["site"]), e(ln.get("hint", ""))))
            parts.append("</ul>")
        for i, c in enumerate(g["candidates"], 1):
            m = c.get("m") or {}
            flags = ["已确认过" if c.get("ok") else "", "本地库" if c.get("local") else "",
                     "未经指标过滤" if c.get("unfiltered") else ""]
            link = '<a href="%s">来源页</a>' % e(c["url"], quote=True) if is_http_url(c.get("url")) else "无来源链接"
            parts.append('<div class="c"><b>%d. %s</b> <span class="m">%s</span><br>' % (i, e(c["title"]), e(" · ".join(f for f in flags if f))))
            parts.append('<span class="m">作者 %s · 许可 %s · %s · 编号 %s</span><br>' % (e(c["author"] or "—"), e(c["license_label"]), link, e(c["id"])))
            if m.get("dur") is not None and "peak" in m:
                parts.append('<span class="m">时长 %.3f 秒 · 起音 %.3f · 峰值 %.3f · 尾音到 %.3f · 峰值电平 %.1f dB</span>'
                             % (m["dur"], m["start"], m["peak"], m["tail"], m["peak_db"]))
            else:
                parts.append('<span class="m w">本机没能量测这条音效</span>')
            blob = blobs.get(c["id"])
            if blob and blob.get("svg"):
                parts.append(blob["svg"])
            if blob and blob.get("data"):
                parts.append('<audio controls preload="none" src="data:%s;base64,%s"></audio>' % (e(blob["mime"], quote=True), blob["data"]))
            else:
                parts.append('<p class="m w">这条没有内嵌音频（文件过大或转码失败）。</p>')
            parts.append("<code>python3 sfx.py take --project %s %s=%s</code></div>" % (e(json.dumps(proj_dir, ensure_ascii=False)), e(g["id"]), e(c["id"])))
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------- 子命令

def state_path(proj_dir):
    return os.path.join(proj_dir, "sfx", "candidates.json")


def cmd_find(args):
    smap, lic = load_map(), load_licenses()
    events, project = load_events(args.events, smap)
    proj_dir = os.path.abspath(args.project or os.path.dirname(os.path.abspath(args.events)))
    groups = group_events(events, smap)
    key = get_key()
    if args.dry_run:
        print_dry_run(events, groups, lic, key)
        return 0
    net = Net(key)
    if os.path.isdir(cache_dir()):
        cache_prune()
    ledger = ledger_load()
    use_ogg = ogg_supported()
    old = {}
    try:
        old = load_json(state_path(proj_dir)).get("chosen", {})
    except (OSError, ValueError, AttributeError):
        pass
    tmp = tempfile.mkdtemp(prefix="sfx-find-")
    blobs, need_key = {}, False
    try:
        for g in groups:
            g["levels"], g["candidates"], notes = [], [], []
            local = local_candidates(g, ledger, lic)
            g["levels"].append({"level": "本地台账", "status": "ok" if local else "none", "reason": "" if local else "没有匹配的已登记音效"})
            online = not args.offline and (args.online or not (any(c["ok"] for c in local) or len(local) >= PER_GROUP))
            remote = []
            if online:
                try:
                    remote, _unf = freesound_candidates(net, g, lic, notes)
                    g["levels"].append({"level": "Freesound", "status": "ok" if remote else "none",
                                        "reason": "" if remote else "没有通过过滤和许可闸门的结果"})
                except SfxError as e:
                    reason = str(e)
                    if reason == NO_KEY_MSG:   # 长提示只在末尾打印一次
                        reason, need_key = "没有 Freesound key", True
                    g["levels"].append({"level": "Freesound", "status": "failed", "reason": reason})
            elif args.offline:
                g["levels"].append({"level": "Freesound", "status": "skipped", "reason": "已指定离线"})
            have = set(c["id"] for c in local)
            remote = [c for c in remote if c["id"] not in have]   # 同一条音效本地已有就用本地那条，不重复列出
            fails = 0
            for c in local + remote:
                if len(g["candidates"]) >= PER_GROUP or fails >= PER_GROUP:   # 连续坏 3 条就不再往下试
                    break
                try:
                    if c.get("local"):
                        src = os.path.join(home(), c["file"])
                        wav = os.path.join(tmp, safe_name(c["id"]) + ".wav")
                        try:
                            m, pcm = make_wav(src, wav)
                            c["m"] = m
                        except SfxError:
                            wav, pcm = None, None
                    else:
                        src, wav, m, pcm = fetch_preview(net, c, use_ogg, tmp)
                        c["m"], c["cache_file"] = m, os.path.relpath(src, home())
                except SfxError as e:
                    notes.append("跳过 %s：%s" % (c["id"], e))
                    fails += 1
                    continue
                fails = 0
                blob = {}
                m4a = os.path.join(tmp, safe_name(c["id"]) + ".m4a")
                pick = (m4a, "audio/mp4") if wav and make_m4a(wav, m4a) else (wav, "audio/wav") if wav else (src, MIME.get(os.path.splitext(src)[1].lower(), ""))
                if pick[1] and os.path.getsize(pick[0]) <= MAX_EMBED_BYTES:
                    with open(pick[0], "rb") as f:
                        blob = {"mime": pick[1], "data": base64.b64encode(f.read()).decode("ascii")}
                if args.waveform and pcm:
                    blob["svg"] = waveform_svg(*pcm)
                blobs[c["id"]] = blob
                g["candidates"].append(c)
            if len(g["candidates"]) < PER_GROUP:
                g["links"] = manual_links(g)
                g["levels"].append({"level": "手动补充", "status": "links", "reason": "候选不足 %d 个，见搜索链接；下载后用 register 登记" % PER_GROUP})
            g["notes"] = notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if os.path.isdir(cache_dir()):
        cache_prune()
    state = {"version": 1, "project": project, "generated_at": now_iso(), "events": events, "groups": groups,
             "chosen": {k: v for k, v in old.items() if k in [g["id"] for g in groups]}}
    write_json(state_path(proj_dir), state)
    write_text(os.path.join(proj_dir, "sfx", "audition.html"), render_audition(state, blobs, proj_dir))
    for g in groups:
        out("[%s] %s，%d 处：%d 个候选" % (g["id"], g["label"], len(g["events"]), len(g["candidates"])))
        for c in g["candidates"]:
            m = c.get("m") or {}
            out("   %s  %s  许可 %s%s%s  时长 %s 峰值 %s" % (c["id"], c["title"][:40], c["license_label"], "  [已确认]" if c.get("ok") else "",
                                                     "  [未经指标过滤]" if c.get("unfiltered") else "", m.get("dur", "?"), m.get("peak", "?")))
        for lv in g["levels"]:
            if lv["status"] not in ("ok",):
                out("   · %s：%s" % (lv["level"], lv["reason"]))
        for n in g["notes"]:
            out("   · " + n)
    out("本次 API 请求 %d 次，下载预览 %d 个；今日累计 %d/%d。" % (net.run_count, net.downloads, net.usage()["count"] if os.path.isdir(cache_dir()) else 0, DAY_LIMIT))
    out("试听页：%s" % os.path.join(proj_dir, "sfx", "audition.html"))
    if need_key:
        out(NO_KEY_MSG)
    return 0


def resolve_pick(pair, groups):
    """把“组名=编号”解析成 (组, 候选)。编号可以省略来源前缀，但只做精确匹配，且必须在该组内唯一。"""
    gid, _, given = pair.partition("=")
    g = groups.get(gid)
    if g is None:
        raise SfxError("没有“%s”这个组。有效的组名：%s" % (clean(gid, 60), "、".join(sorted(groups)) or "（候选清单是空的）"))
    cands = {}
    for c in g.get("candidates", []):
        cands.setdefault(c["id"], c)
    hits = [c for cid, c in cands.items() if cid == given] or [c for cid, c in cands.items() if given and cid.partition(":")[2] == given]
    if len(hits) == 1:
        return g, hits[0]
    why = "对应多个候选，请写带来源前缀的完整编号" if hits else "不在这一组的候选里" if given else "后面没有写编号"
    valid = "、".join("%s=%s" % (gid, cid) for cid in cands) or "（这一组没有候选）"
    raise SfxError("[%s] 的编号“%s”%s。有效写法：%s；来源前缀在不重复时可以省略。" % (gid, clean(given, 60), why, valid))


def cmd_take(args):
    lic = load_licenses()
    proj_dir = os.path.abspath(args.project)
    try:
        state = load_json(state_path(proj_dir))
    except (OSError, ValueError):
        raise SfxError("这个工程还没有候选清单，请先运行 find")
    groups = {g["id"]: g for g in state["groups"]}
    ledger = ledger_load()
    net = None
    for g, cand in [resolve_pick(pair, groups) for pair in args.picks]:   # 先全部解析，有一个写错就什么都不做
        gid, cid = g["id"], cand["id"]
        if not allowed(cand["license"], lic):
            raise SfxError("%s 的许可不在白名单里" % cid)
        stem = safe_name(cid.replace(":", "-"))
        wav = os.path.join(proj_dir, "sfx", "audio", stem + ".wav")
        if cand.get("local"):
            entry = ledger.get(cid)
            if not entry:
                raise SfxError("台账里找不到 %s" % cid)
            m, _pcm = make_wav(os.path.join(home(), entry["file"]), wav)
            upd = {"id": cid, "tags": sorted(set(entry.get("tags", [])) | {"action:" + g["action"]}), "m": m}
            if args.ok:
                upd["ok"] = True
            if any(entry.get(k) != v for k, v in upd.items()):
                ledger_append(upd)
        else:
            src = os.path.realpath(os.path.join(home(), str(cand.get("cache_file", ""))))
            inside = src.startswith(os.path.realpath(cache_dir()) + os.sep)   # 候选清单里的路径不能指到缓存目录外
            if not cand.get("cache_file") or not inside or not os.path.isfile(src):
                net = net or Net(get_key())
                tmp = tempfile.mkdtemp(prefix="sfx-take-")
                try:
                    src = fetch_preview(net, cand, ogg_supported(), tmp)[0]
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            rel = os.path.join("files", stem + os.path.splitext(src)[1].lower())
            dst = os.path.join(home(), rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            m, _pcm = make_wav(dst, wav)
            ledger_append({"id": cid, "source": "freesound", "url": cand["url"], "title": cand["title"], "author": cand["author"],
                           "license": cand["license"], "license_raw": cand.get("license_raw", ""), "file": rel,
                           "sha256": sha256_file(dst), "via": cand.get("via", "api-preview"), "got_at": now_iso(),
                           "tags": sorted(set(cand.get("tags", [])) | {"action:" + g["action"]}), "m": m, "ok": bool(args.ok)})
        state["chosen"][gid] = cid
        out("已选定 [%s] %s  →  %s%s" % (gid, cid, os.path.relpath(wav, proj_dir), "（已标记为人耳确认）" if args.ok else ""))
    write_json(state_path(proj_dir), state)
    return 0


def place_at(t, peak, trim_in):
    """放置表偏移公式：at = 事件时刻 −（峰值时刻 − trim_in）。"""
    return round(t - (peak - trim_in), 3)


def build_placement(state, ledger, lic):
    items, missing, used = [], [], {}
    ev = {e["id"]: e for e in state["events"]}
    for g in state["groups"]:
        cid = state.get("chosen", {}).get(g["id"])
        entry = ledger.get(cid) if cid else None
        if not entry or not allowed(entry.get("license"), lic):
            missing += [{"event": eid, "group": g["id"], "reason": "还没有选定音效" if not cid else "台账里没有或许可不放行"} for eid in g["events"]]
            continue
        used[cid] = entry
        m = entry.get("m") or {}
        measured = "peak" in m
        trim0 = m.get("start", 0.0) if measured else 0.0
        tail = m.get("tail", m.get("dur")) if m else None
        times = sorted(ev[eid]["t"] for eid in g["events"])
        lead = (m["peak"] - trim0) if measured and g["align"] == "peak" else 0.0   # 片段比事件提前多少开始
        stem = safe_name(cid.replace(":", "-"))
        for eid in sorted(g["events"], key=lambda x: ev[x]["t"]):
            t, trim = ev[eid]["t"], trim0
            at = place_at(t, m["peak"], trim) if measured and g["align"] == "peak" else round(t, 3)
            if at < 0:          # 时间线开头放不下，就多裁掉一点
                trim, at = round(trim - at, 3), 0.0
            dur = round(tail - trim, 3) if tail is not None else ev[eid]["dur"]
            fade = 0.05
            nxt = next((x for x in times if x > t), None)
            if dur is not None and nxt is not None:
                next_start = nxt - lead   # 同类的下一条从这里开始播
                if at + dur > next_start:
                    dur, fade = round(max(0.02, next_start - at), 3), 0.1
            if dur is not None:
                dur = max(0.0, dur)
                fade = round(min(fade, dur / 2), 3)
            gain = None
            if "peak_db" in m:
                gain = int(max(-24, min(6, round(g["peak_target_db"] - m["peak_db"]))))
            item = {"event": "%s %s" % (eid, g["label"]), "file": "audio/%s.wav" % stem, "at": at, "trim_in": round(trim, 3),
                    "dur": dur, "gain_db": gain, "fade_out": fade, "source": entry.get("url", ""),
                    "license": entry["license"], "credit": credit_line(entry, lic)}
            if not measured:
                item["note"] = "本机没能量测这条音效，at 直接等于事件时刻"
            items.append(item)
    items.sort(key=lambda x: x["at"])
    return items, missing, used


def md_escape(s):
    return clean(s, 300).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_credits(project, used, lic):
    need = [e for e in used.values() if credit_line(e, lic)]
    free = [e for e in used.values() if not credit_line(e, lic)]
    lines = ["# 音效署名清单 · %s" % md_escape(project), "", "生成于 %s。只列出本工程实际用到的音效。" % now_iso(), "", "## 必须署名", ""]
    lines += ["- %s" % md_escape(credit_line(e, lic)) for e in sorted(need, key=lambda x: x["id"])] or ["（无）"]
    lines += ["", "## 无需署名（存档备查）", ""]
    for e in sorted(free, key=lambda x: x["id"]):
        c = lic["classes"].get(e["license"], {})
        lines.append("- %s｜作者 %s｜%s｜%s｜取得于 %s%s" % (md_escape(e.get("title")), md_escape(e.get("author") or "—"),
                                                     md_escape(license_label(e["license"], e.get("license_raw"), lic)),
                                                     md_escape(e.get("url")), md_escape(e.get("got_at")),
                                                     "｜限制：" + md_escape(c["restrictions"]) if c.get("restrictions") else ""))
    if not free:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


def cmd_export(args):
    lic = load_licenses()
    proj_dir = os.path.abspath(args.project)
    try:
        state = load_json(state_path(proj_dir))
    except (OSError, ValueError):
        raise SfxError("这个工程还没有候选清单，请先运行 find 和 take")
    items, missing, used = build_placement(state, ledger_load(), lic)
    for cid in used:
        if not os.path.isfile(os.path.join(proj_dir, "sfx", "audio", safe_name(cid.replace(":", "-")) + ".wav")):
            log("提示：工程里缺少 %s 的 WAV，请对它重新运行 take" % cid)
    placement = {"version": 1, "project": state["project"], "items": items}
    if missing:
        placement["missing"] = missing
    write_json(os.path.join(proj_dir, "sfx", "placement.json"), placement)
    write_text(os.path.join(proj_dir, "sfx", "CREDITS.md"), render_credits(state["project"], used, lic))
    out("放置表 %d 条%s；署名清单：必须署名 %d 条，无需署名 %d 条。" % (
        len(items), "，另有 %d 处还没有音效" % len(missing) if missing else "",
        sum(1 for e in used.values() if credit_line(e, lic)), sum(1 for e in used.values() if not credit_line(e, lic))))
    out("输出：%s" % os.path.join(proj_dir, "sfx"))
    return 0


def name_tags(*names):
    words = []
    for n in names:
        n = re.sub(r"([a-z])([A-Z])", r"\1 \2", os.path.splitext(os.path.basename(str(n)))[0])
        words += [w.lower() for w in re.findall(r"[A-Za-z]{2,}", n)]
    return sorted(set(words))


def measure_file(path, tmp):
    try:
        return make_wav(path, os.path.join(tmp, "m.wav"))[0]
    except SfxError:
        return {}


def zip_audit(zf):
    """Kenney 压缩包检查：拒绝路径穿越、绝对路径、符号链接和异常体积。返回 (音频条目, 许可文本)。"""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise SfxError("压缩包条目过多，拒绝处理")
    audio, lic_text, total = [], "", 0
    for zi in infos:
        name = zi.filename.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
            raise SfxError("压缩包里有越界路径（%s），拒绝处理" % clean(name, 60))
        if (zi.external_attr >> 16) & 0o170000 == 0o120000:
            raise SfxError("压缩包里有符号链接（%s），拒绝处理" % clean(name, 60))
        total += zi.file_size
        if zi.file_size > MAX_ZIP_FILE or total > MAX_ZIP_TOTAL:
            raise SfxError("压缩包解开后体积异常，拒绝处理")
        base = os.path.basename(name)
        if zi.is_dir() or not base or base.startswith("."):
            continue
        if re.search(r"licen[sc]e", base, re.I) and zi.file_size <= 65536:
            lic_text += zf.read(zi).decode("utf-8", "replace")
        elif base.lower().endswith(AUDIO_EXT):
            audio.append(zi)
    return audio, lic_text


def cmd_register(args):
    lic = load_licenses()
    if not args.url or not is_http_url(args.url):
        raise SfxError("登记必须提供来源页面链接（--url https://…），否则拒绝登记")
    src = os.path.abspath(args.path)
    if not os.path.isfile(src):
        raise SfxError("找不到文件：%s" % clean(args.path, 120))
    ledger = ledger_load()
    extra = [clean(t, 30).lower() for t in (args.tags or "").split(",") if t.strip()]
    added, skipped = 0, 0
    tmp = tempfile.mkdtemp(prefix="sfx-reg-")
    try:
        if zipfile.is_zipfile(src):
            if args.source != "kenney":
                raise SfxError("压缩包只支持 --source kenney")
            pack = safe_name(os.path.splitext(os.path.basename(src))[0], 40)
            with zipfile.ZipFile(src) as zf:
                audio, lic_text = zip_audit(zf)
                if not lic_text:
                    raise SfxError("压缩包里没有许可文件，无法确认是 CC0，拒绝登记")
                if not re.search(r"CC0|Creative Commons Zero|publicdomain/zero", lic_text, re.I):
                    raise SfxError("压缩包里的许可文件没有写明 CC0，拒绝登记")
                if not audio:
                    raise SfxError("压缩包里没有音频文件")
                seen = set()
                for zi in audio:
                    base = os.path.basename(zi.filename.replace("\\", "/"))
                    ext = os.path.splitext(base)[1].lower()
                    stem = safe_name("kenney-%s-%s" % (pack, os.path.splitext(base)[0]))
                    if stem in seen:   # 不同子目录里的同名文件：加上路径的短哈希区分
                        stem = safe_name(stem[:70] + "-" + hashlib.sha256(zi.filename.encode("utf-8", "replace")).hexdigest()[:6])
                    seen.add(stem)
                    aid, rel = "kenney:%s" % stem[len("kenney-"):], os.path.join("files", stem + ext)
                    dst = os.path.join(home(), rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with zf.open(zi) as fsrc, open(dst + ".part", "wb") as fdst:   # 文件名完全由我们生成，不使用包内路径
                        shutil.copyfileobj(fsrc, fdst)
                    os.replace(dst + ".part", dst)
                    digest = sha256_file(dst)
                    if ledger.get(aid, {}).get("sha256") == digest:
                        skipped += 1
                        continue
                    ledger_append({"id": aid, "source": "kenney", "url": args.url, "title": clean(base, 120),
                                   "author": clean(args.author or "Kenney", 80), "license": "cc0", "license_raw": "CC0（压缩包内许可文件）",
                                   "file": rel, "sha256": digest, "via": "manual", "got_at": now_iso(),
                                   "tags": sorted(set(name_tags(pack, base) + extra)), "m": measure_file(dst, tmp), "ok": bool(args.ok)})
                    added += 1
        else:
            ext = os.path.splitext(src)[1].lower()
            if ext not in AUDIO_EXT:
                raise SfxError("不是支持的音频格式：%s" % ext)
            cls = lic.get("sources", {}).get(args.source)
            if not cls:
                raise SfxError("不支持的来源：%s" % args.source)
            if args.source == "kenney":
                raise SfxError("Kenney 请直接登记下载的压缩包，以便读取包内的许可文件")
            digest = sha256_file(src)
            stem = safe_name("%s-%s-%s" % (args.source, os.path.splitext(os.path.basename(src))[0], digest[:8]))
            aid, rel = "%s:%s" % (args.source, stem[len(args.source) + 1:]), os.path.join("files", stem + ext)
            if ledger.get(aid, {}).get("sha256") == digest:
                skipped += 1
            else:
                dst = os.path.join(home(), rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                ledger_append({"id": aid, "source": args.source, "url": args.url, "title": clean(args.title or os.path.basename(src), 120),
                               "author": clean(args.author, 80), "license": cls, "license_raw": "", "file": rel, "sha256": digest,
                               "via": "manual", "got_at": now_iso(), "tags": sorted(set(name_tags(src) + extra)),
                               "m": measure_file(dst, tmp), "ok": bool(args.ok)})
                added += 1
                if not allowed(cls, lic):
                    out("注意：%s 的许可类别是“%s”，目前不放行，不会出现在候选和放置表里。" % (aid, cls))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out("已登记 %d 条，跳过重复 %d 条。台账：%s" % (added, skipped, ledger_path()))
    return 0


def cmd_clean(_args):
    if not os.path.isdir(cache_dir()):
        out("没有缓存目录，无需清理。")
        return 0
    removed, freed, total = cache_prune()
    out("删除 %d 个缓存文件，释放 %.1f KB；现在缓存占用 %.1f KB（上限 %d MB）。" % (removed, freed / 1024.0, total / 1024.0, CACHE_CAP // (1024 * 1024)))
    return 0


def cmd_doctor(args):
    ok = True
    v = sys.version_info
    good = v >= (3, 9)
    ok = ok and good
    out("[%s] Python %d.%d.%d（需要 3.9 或更新）" % ("OK" if good else "!!", v[0], v[1], v[2]))
    has_af = os.path.isfile(AFCONVERT)
    ok = ok and has_af
    out("[%s] afconvert %s" % ("OK" if has_af else "!!", "可用" if has_af else "找不到 " + AFCONVERT))
    if has_af:
        ogg = ogg_supported()
        out("[%s] 解码 Ogg：%s" % ("OK" if ogg else "--", "支持" if ogg else "不支持，将自动改用 MP3 预览"))
    key = get_key()
    out("[%s] Freesound key：%s" % ("OK" if key else "--", "有" if key else "无"))
    if not key:
        out("     " + NO_KEY_MSG)
    exists = os.path.isdir(home())
    out("[%s] 数据目录：%s（%s）" % ("OK" if exists else "--", home(), "已存在" if exists else "还没有，首次写入时创建"))
    if os.path.isdir(cache_dir()):
        size = sum(os.path.getsize(os.path.join(r, n)) for r, _d, ns in os.walk(cache_dir()) for n in ns)
        u = Net("").usage()
        out("[OK] 缓存 %.1f KB；今日本地计数 %d/%d%s" % (size / 1024.0, u["count"], DAY_LIMIT, "；今天已触发日限额" if u.get("blocked") else ""))
    proxies = {k: re.sub(r"//[^@/]*@", "//", str(p)) for k, p in urllib.request.getproxies().items()}
    out("[OK] 系统代理：%s" % (", ".join("%s=%s" % kv for kv in sorted(proxies.items())) or "未设置"))
    if args.offline:
        out("[--] 已跳过联网检查")
        return 0 if ok else 1
    net = Net(key)
    net.interval = 0
    url = net.base + "/search/?" + urllib.parse.urlencode({"query": "test", "page_size": "1", "fields": "id"})
    try:
        net._fetch(url, True, 1 << 20, False)
        out("[OK] Freesound 可达，key 有效")
        try:
            u = json.loads(net._fetch(net.base + "/usage/", True, 1 << 20, False).decode("utf-8"))["current"]
            def _n(w):   # 真实接口字段是 num，limit 是 "60/minute" 这样的字符串；兼容旧写法 num_requests
                return w["num"] if "num" in w else w["num_requests"]
            out("[OK] 官方用量：最近一分钟 %s（上限 %s），最近 24 小时 %s（上限 %s）" % (
                _n(u["burst"]), u["burst"]["limit"], _n(u["sustained"]), u["sustained"]["limit"]))
        except (SfxError, ValueError, KeyError, TypeError) as e:
            out("[--] 官方用量读不到：%s" % clean(e, 120))
    except SfxError as e:
        msg = str(e)
        if "返回 401" in msg:
            out("[%s] Freesound 可达（返回 401）：%s" % ("--" if not key else "!!", "还没有配置 key，属于正常情况" if not key else "key 无效，请重新申请或重新存入钥匙串"))
        else:
            out("[!!] Freesound 连通检查失败：%s" % msg)
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口

def build_parser():
    p = argparse.ArgumentParser(prog="sfx.py", description="视频音效选材（最小可用版）")
    sub = p.add_subparsers(dest="cmd")
    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--offline", action="store_true", help="跳过联网检查")
    f = sub.add_parser("find", help="读事件清单，给每类动作找候选并生成试听页")
    f.add_argument("events", help="事件清单 JSON")
    f.add_argument("--project", help="视频工程目录（默认是事件清单所在目录）")
    f.add_argument("--dry-run", action="store_true", help="只打印将发出的请求，不联网")
    f.add_argument("--offline", action="store_true", help="只用本地台账")
    f.add_argument("--online", action="store_true", help="即使本地已有确认过的音效也查 Freesound")
    f.add_argument("--waveform", action="store_true", help="试听页里加波形图")
    t = sub.add_parser("take", help="选定候选：存档、转 WAV、量测、写台账")
    t.add_argument("--project", required=True)
    t.add_argument("--ok", action="store_true", help="标记为人耳确认过")
    t.add_argument("picks", nargs="+", metavar="组=编号")
    r = sub.add_parser("register", help="登记手动下载的文件或 Kenney 压缩包")
    r.add_argument("path")
    r.add_argument("--source", required=True, choices=["kenney", "mixkit", "pixabay"])
    r.add_argument("--url", help="来源页面链接（必填）")
    r.add_argument("--author")
    r.add_argument("--title")
    r.add_argument("--tags", help="逗号分隔的标签")
    r.add_argument("--ok", action="store_true", help="标记为人耳确认过")
    e = sub.add_parser("export", help="生成放置表和署名清单")
    e.add_argument("--project", required=True)
    sub.add_parser("clean", help="删过期缓存")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):   # 终端不是 UTF-8 时也不要因为中文报错
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    try:
        return {"doctor": cmd_doctor, "find": cmd_find, "take": cmd_take, "register": cmd_register,
                "export": cmd_export, "clean": cmd_clean}[args.cmd](args)
    except SfxError as e:
        log("错误：%s" % e)
        return 2
    except KeyboardInterrupt:
        log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
