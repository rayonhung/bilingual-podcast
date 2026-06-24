#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
雙語 Podcast 小幫手
------------------
這支小程式在你電腦本機跑一個迷你伺服器，幫網頁介面做那些瀏覽器自己被擋住的事：
讀取 RSS、抓節目附的字幕、下載音檔、呼叫 Groq 做轉錄與翻譯。

用法：
  1. 把這個檔案和 index.html 放在「同一個資料夾」
  2. 在那個資料夾打開終端機，執行：  python3 serve.py
  3. 它會自動打開瀏覽器；若沒有，手動開終端機印出的網址
只需要 Python 3，不必安裝任何套件。
"""

import json, os, re, sys, time, base64, hashlib, threading, webbrowser, urllib.request, urllib.error, urllib.parse, subprocess, tempfile, shutil, html, mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
HERE = os.path.dirname(os.path.abspath(__file__))
GROQ = "https://api.groq.com/openai/v1"
SERVER_GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
APP_COOKIE = "bpp_auth"
PODNS = "podcastindex.org/namespace"          # 用 localname 比對，不綁完整命名空間
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MAX_AUDIO = 25 * 1024 * 1024                   # Groq 免費轉錄單檔上限約 25MB
CHUNK_BYTES = 18 * 1024 * 1024                 # 切段大小上限（留餘裕）
CHUNK_SECONDS = 180                            # 每段最長約 3 分鐘：第一段更快出來，也比較不會長時間卡住
FIRST_CHUNK_SECONDS = 20                       # 第一段先短一點，讓第一批雙語字幕快點出來
FALLBACK_CHUNK_SECONDS = 60                    # 失敗段重試時改切成較穩的短段，不用極短片段
CHUNK_OVERLAP_SECONDS = 4                      # 每段互相重疊幾秒，避免 Whisper 在切段邊界漏字

# 下載＋切好的音檔暫存在記憶體，讓前端能一段一段地要結果（逐段交付）
JOBS = {}
JOBS_LOCK = threading.Lock()

def delete_job(job):
    path = (job or {}).get("audioPath")
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def curl_bytes(url, timeout=120, headers=None):
    args = ["curl", "-L", "--fail", "--silent", "--show-error",
            "--max-time", str(timeout), "-A", UA]
    for k, v in (headers or {}).items():
        if v:
            args.extend(["-H", "%s: %s" % (k, v)])
    args.append(url)
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or
                           "curl 下載失敗（exit %s）" % p.returncode)
    return p.stdout

def has_cmd(name):
    return shutil.which(name) is not None

def auth_token():
    if not APP_PASSWORD:
        return ""
    return hashlib.sha256(("bpp-auth|" + APP_PASSWORD).encode("utf-8")).hexdigest()

def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out

def http_get(url, timeout=120, accept=None):
    headers = {
        "User-Agent": UA,
        "Accept": accept or "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers={
        **headers,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 406, 429):
            return curl_bytes(url, timeout=timeout, headers=headers)
        raise

def localname(tag):
    return tag.split("}")[-1] if "}" in tag else tag

def ts_to_sec(ts):
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + s

# ----------------------------------------------------------------------------
# RSS 解析：取出每集的標題、日期、音檔網址、（若有）字幕網址
# ----------------------------------------------------------------------------
import xml.etree.ElementTree as ET

def normalize_url(url, base=None):
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if base:
        return urllib.parse.urljoin(base, url)
    return url

def parse_feed(xml_bytes, base_url=None):
    root = ET.fromstring(xml_bytes)
    channel = next((el for el in root.iter() if localname(el.tag) == "channel"), root)
    show = ""
    show_author = ""
    show_description = ""
    show_image = ""
    for el in channel:
        ln = localname(el.tag)
        if ln == "title" and not show:
            show = (el.text or "").strip()
        elif ln in ("author", "owner") and not show_author:
            if ln == "owner":
                show_author = next(((c.text or "").strip() for c in el
                                    if localname(c.tag) in ("name", "email") and (c.text or "").strip()), "")
            else:
                show_author = (el.text or "").strip()
        elif ln in ("description", "summary") and not show_description:
            show_description = re.sub(r"<[^>]+>", " ", html.unescape(el.text or ""))
            show_description = re.sub(r"\s+", " ", show_description).strip()
        elif ln == "image" and not show_image:
            show_image = normalize_url(el.get("href") or "", base_url)
            if not show_image:
                show_image = normalize_url(next(((c.text or "").strip() for c in el
                                                 if localname(c.tag) == "url"), ""), base_url)
    episodes = []
    for it in root.iter():
        if localname(it.tag) != "item":
            continue
        title, pub, audio = "", "", None
        description, duration, episode_image = "", "", ""
        best = None  # (url, type, score)
        for ch in it:
            ln = localname(ch.tag)
            if ln == "title":
                title = (ch.text or "").strip()
            elif ln == "pubDate":
                pub = (ch.text or "").strip()
            elif ln == "enclosure":
                u = ch.get("url")
                ty = (ch.get("type") or "")
                if u and ("audio" in ty or u.lower().split("?")[0].endswith((".mp3", ".m4a", ".aac", ".ogg", ".wav"))):
                    audio = normalize_url(u, base_url)
            elif ln in ("description", "summary", "encoded") and not description:
                description = re.sub(r"<[^>]+>", " ", html.unescape(ch.text or ""))
                description = re.sub(r"\s+", " ", description).strip()
            elif ln == "duration":
                duration = (ch.text or "").strip()
            elif ln == "image":
                episode_image = normalize_url(ch.get("href") or (ch.text or ""), base_url)
            elif ln == "transcript" and "podcastindex.org/namespace" in ch.tag:
                u = ch.get("url")
                ty = (ch.get("type") or "").lower()
                if not u:
                    continue
                score = {"application/json": 3, "application/x-subrip": 2,
                         "application/srt": 2, "text/srt": 2, "text/vtt": 1}.get(ty, 0)
                if best is None or score > best[2]:
                    best = (normalize_url(u, base_url), ty, score)
        if audio:
            episodes.append({
                "title": title or "(未命名)",
                "date": pub,
                "audio": audio,
                "description": description,
                "duration": duration,
                "image": episode_image or show_image,
                "transcript": best[0] if best else None,
                "transcriptType": best[1] if best else None,
            })
    return {
        "show": show,
        "author": show_author,
        "description": show_description,
        "image": show_image,
        "feedUrl": base_url or "",
        "episodes": episodes,
    }


def search_podcasts(term, country="us", limit=24):
    term = (term or "").strip()
    if not term:
        return []
    query = urllib.parse.urlencode({
        "term": term,
        "media": "podcast",
        "entity": "podcast",
        "country": (country or "us")[:2],
        "limit": max(1, min(int(limit or 24), 40)),
    })
    raw = http_get("https://itunes.apple.com/search?" + query, timeout=30,
                   accept="application/json")
    payload = json.loads(raw.decode("utf-8", "replace"))
    results = []
    for item in payload.get("results", []):
        feed_url = normalize_url(item.get("feedUrl"))
        if not feed_url:
            continue
        artwork = item.get("artworkUrl600") or item.get("artworkUrl100") or item.get("artworkUrl60") or ""
        artwork = re.sub(r"/\d+x\d+bb", "/600x600bb", artwork)
        results.append({
            "id": str(item.get("collectionId") or feed_url),
            "title": item.get("collectionName") or item.get("trackName") or "Podcast",
            "author": item.get("artistName") or "",
            "image": artwork,
            "feedUrl": feed_url,
            "genre": item.get("primaryGenreName") or "",
            "episodeCount": item.get("trackCount") or 0,
        })
    return results

# ----------------------------------------------------------------------------
# 字幕檔解析：SRT / VTT / podcast JSON  ->  [{start,end,text}]
# ----------------------------------------------------------------------------
def parse_srt_vtt(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    segs = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        tci = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if tci is None:
            continue
        m = re.search(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", lines[tci])
        if not m:
            continue
        body = " ".join(lines[tci + 1:]).strip()
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            segs.append({"start": ts_to_sec(m.group(1)),
                         "end": ts_to_sec(m.group(2)), "text": body})
    return segs

def merge_words(segs, max_chars=90):
    """有些 JSON 字幕是一個字一段，合併成順口的句子。"""
    out, buf, st, en = [], [], None, None
    for s in segs:
        if st is None:
            st = s["start"]
        en = s["end"]
        buf.append(s["text"])
        joined = " ".join(buf).strip()
        if re.search(r"[.!?。！？]$", s["text"]) or len(joined) >= max_chars:
            out.append({"start": st, "end": en, "text": joined})
            buf, st, en = [], None, None
    if buf:
        out.append({"start": st or 0, "end": en or 0, "text": " ".join(buf).strip()})
    return out

def parse_json_transcript(text):
    data = json.loads(text)
    raw = []
    for s in data.get("segments", []):
        st = s.get("startTime")
        if st is None:
            continue
        en = s.get("endTime", st)
        body = (s.get("body") or "").strip()
        if body:
            raw.append({"start": float(st), "end": float(en), "text": body})
    avg = sum(len(s["text"]) for s in raw) / max(1, len(raw))
    return merge_words(raw) if avg < 25 else raw   # 字太短代表是逐字級，合併

def fetch_transcript(url, ttype):
    data = http_get(url, accept="application/json,text/vtt,text/plain,*/*")
    text = data.decode("utf-8", "replace")
    ty = (ttype or "").lower()
    if "json" in ty or text.lstrip().startswith("{"):
        try:
            return parse_json_transcript(text)
        except Exception:
            pass
    return parse_srt_vtt(text)

# ----------------------------------------------------------------------------
# 長音檔自動切段：照 MP3 frame 邊界切，純 Python、不需 ffmpeg
# ----------------------------------------------------------------------------
# Layer III 位元率表（kbps），index 1..14 有效
_BR_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_BR_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_SR = {3: [44100, 48000, 32000, 0],   # MPEG1
       2: [22050, 24000, 16000, 0],   # MPEG2
       0: [11025, 12000, 8000, 0]}    # MPEG2.5

def _skip_id3(data):
    """跳過開頭的 ID3v2 標籤（如果有），回傳第一個音訊 frame 的位移。"""
    if data[:3] == b"ID3" and len(data) >= 10:
        size = ((data[6] & 0x7f) << 21) | ((data[7] & 0x7f) << 14) | \
               ((data[8] & 0x7f) << 7) | (data[9] & 0x7f)
        return 10 + size
    return 0

def _frame_info(data, i):
    """若 data[i:] 是合法的 MP3 Layer III frame header，回傳 (frame_len, duration_sec)；否則 None。"""
    if i + 4 > len(data):
        return None
    b1, b2 = data[i + 1], data[i + 2]
    if data[i] != 0xFF or (b1 & 0xE0) != 0xE0:      # frame sync（11 個 1）
        return None
    ver = (b1 >> 3) & 0x03                          # 3=MPEG1 2=MPEG2 0=MPEG2.5 1=保留
    layer = (b1 >> 1) & 0x03                        # 1=Layer III
    if ver == 1 or layer != 1:
        return None
    br_i = (b2 >> 4) & 0x0F
    sr_i = (b2 >> 2) & 0x03
    pad = (b2 >> 1) & 0x01
    if br_i in (0, 15) or sr_i == 3:
        return None
    bitrate = (_BR_V1_L3 if ver == 3 else _BR_V2_L3)[br_i] * 1000
    sr = _SR[ver][sr_i]
    if ver == 3:                                    # MPEG1 Layer III
        flen, spf = 144 * bitrate // sr + pad, 1152
    else:                                           # MPEG2 / 2.5 Layer III
        flen, spf = 72 * bitrate // sr + pad, 576
    if flen <= 0:
        return None
    return flen, spf / sr

def _find_sync(data, i):
    """
    從 i 開始往後找下一個合法 frame 的起點。
    用「下一個 frame 也接得上」做二次確認，避免把音訊資料裡剛好出現的 0xFF 誤判成 frame。
    找不到回 -1。（用來跳過 ID3 標籤後的 padding、或串流中的雜訊。）
    """
    n = len(data)
    while i + 4 <= n:
        fi = _frame_info(data, i)
        if fi:
            nxt = i + fi[0]
            if nxt + 4 > n or _frame_info(data, nxt):
                return i
        i += 1
    return -1

def mp3_chunks(data, max_bytes, max_seconds=None, first_max_seconds=None, overlap_seconds=0):
    """
    把 MP3 位元組依 frame 邊界切成多段，每段不超過 max_bytes（也不超過 max_seconds 秒）。
    overlap_seconds 會讓相鄰段落重疊一小段，降低語音辨識在切段邊界漏字的機率。
    回傳 [(chunk_bytes, start_time_sec), ...]；不是可解析的 MP3 就回 None。
    start_time_sec 是該段在整集裡的起始秒數，用來把各段的字幕時間軸接回去。
    """
    i = _find_sync(data, _skip_id3(data))           # 跳過 ID3 標籤與其後的 padding，定位第一個 frame
    if i < 0:
        return None
    n = len(data)
    chunks = []
    cur_start = i
    cur_dur = 0.0    # 目前這段累積的長度（秒）
    elapsed = 0.0    # 目前這段開頭的絕對時間（秒）
    frames = []       # 目前這段裡的 frame 起點與其段內時間，用來做 overlap
    while i + 4 <= n:
        fi = _frame_info(data, i)
        if not fi:                                  # 撞到雜訊或結尾標籤：嘗試 resync，找不到就收工
            j = _find_sync(data, i + 1)
            if j < 0:
                break
            i = j
            continue
        flen, dur = fi
        if i + flen > n:
            break
        # 如果再加這個 frame 會超過大小或長度上限，就先把目前這段收尾
        too_big = (i + flen) - cur_start > max_bytes
        limit_seconds = first_max_seconds if not chunks and first_max_seconds is not None else max_seconds
        too_long = limit_seconds is not None and cur_dur + dur > limit_seconds
        if (too_big or too_long) and i > cur_start:
            chunks.append((data[cur_start:i], elapsed))
            overlap_local = cur_dur
            new_start = i
            new_dur = 0.0
            if overlap_seconds and cur_dur > overlap_seconds * 2:
                target = cur_dur - overlap_seconds
                for pos, local_t, _ in frames:
                    if local_t >= target:
                        overlap_local = local_t
                        new_start = pos
                        new_dur = cur_dur - local_t
                        break
            elapsed += overlap_local
            cur_start = new_start
            cur_dur = new_dur
            frames = [(pos, local_t - overlap_local, fdur)
                      for pos, local_t, fdur in frames if pos >= cur_start]
        frames.append((i, cur_dur, dur))
        cur_dur += dur
        i += flen
    if i > cur_start:
        chunks.append((data[cur_start:i], elapsed))
    return chunks or None

def ffmpeg_duration(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or "ffprobe failed")
    return float(p.stdout.decode("utf-8", "replace").strip())

def normalize_audio_for_playback(audio_bytes, filename):
    """把來源音訊標準化成 Safari 穩定支援的 MP3，並讓播放與轉錄共用同一份檔案。"""
    if not has_cmd("ffmpeg"):
        return audio_bytes, filename
    suffix = os.path.splitext(filename.split("?")[0])[1] or ".audio"
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "source" + suffix)
            out = os.path.join(td, "playback.mp3")
            with open(src, "wb") as f:
                f.write(audio_bytes)
            p = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", src, "-vn", "-ac", "2", "-ar", "44100", "-b:a", "96k", out,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode:
                raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or "ffmpeg failed")
            with open(out, "rb") as f:
                normalized = f.read()
            if normalized:
                base = os.path.splitext(os.path.basename(filename.split("?")[0]))[0] or "podcast"
                return normalized, base + ".mp3"
    except Exception as e:
        print("[job] 音訊標準化失敗，沿用原始格式：%s" % e, flush=True)
    return audio_bytes, filename

def ffmpeg_chunks(audio_bytes, filename, max_seconds=None, first_max_seconds=None, overlap_seconds=0):
    """
    用 ffmpeg 依真正時間切段並重編碼，避開 podcast MP3 metadata / VBR / frame parser 造成的開頭偏移。
    回傳 [(chunk_bytes, start_time_sec), ...]；沒有 ffmpeg/ffprobe 或切段失敗就回 None。
    """
    if not (has_cmd("ffmpeg") and has_cmd("ffprobe")):
        return None
    suffix = os.path.splitext(filename.split("?")[0])[1] or ".mp3"
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "source" + suffix)
            with open(src, "wb") as f:
                f.write(audio_bytes)
            total = ffmpeg_duration(src)
            if not total or total <= 0:
                return None
            chunks = []
            start = 0.0
            idx = 0
            while start < total - 0.1:
                limit = first_max_seconds if idx == 0 and first_max_seconds is not None else max_seconds
                dur = min(limit or total, total - start)
                out = os.path.join(td, "chunk_%03d.mp3" % idx)
                args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", "%.3f" % start, "-t", "%.3f" % dur, "-i", src,
                        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", out]
                p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if p.returncode:
                    raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or "ffmpeg failed")
                with open(out, "rb") as f:
                    chunks.append((f.read(), start))
                if start + dur >= total:
                    break
                start = max(0.0, start + dur - overlap_seconds)
                idx += 1
            return chunks or None
    except Exception as e:
        print("[job] ffmpeg 切段失敗，改用 MP3 frame fallback：%s" % e, flush=True)
        return None

# ----------------------------------------------------------------------------
# Groq：轉錄 + 翻譯
# ----------------------------------------------------------------------------
TRANSIENT = (429, 500, 502, 503, 504)              # Groq 暫時性錯誤（過載/服務不穩），可重試

def _urlopen_retry(req, timeout, attempts=5):
    """對 Groq 送出請求；遇到暫時性 5xx/429 就退避重試，回傳 response bytes。"""
    for n in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in TRANSIENT and n < attempts - 1:
                time.sleep(min(2 ** n, 15))         # 1,2,4,8…秒退避
                continue
            raise
        except urllib.error.URLError:
            if n < attempts - 1:
                time.sleep(min(2 ** n, 15))
                continue
            raise

def split_audio(audio_bytes, filename="audio.mp3"):
    """把音檔切成 [(bytes, start_sec), ...]。

    雲端部署時優先用 ffmpeg 依真正時間切段，避免 podcast MP3 metadata / VBR 造成開頭偏移。
    沒有 ffmpeg 時才退回純 Python MP3 frame 切段。
    """
    chunks = ffmpeg_chunks(audio_bytes, filename, CHUNK_SECONDS, FIRST_CHUNK_SECONDS, CHUNK_OVERLAP_SECONDS)
    if chunks:
        return chunks
    chunks = mp3_chunks(audio_bytes, CHUNK_BYTES, CHUNK_SECONDS, FIRST_CHUNK_SECONDS, CHUNK_OVERLAP_SECONDS)
    if chunks:
        return chunks
    if len(audio_bytes) <= MAX_AUDIO:
        return [(audio_bytes, 0.0)]
    return None

def groq_transcribe(audio_bytes, filename, model, key):
    # 沒超過上限：直接一次轉完
    if len(audio_bytes) <= MAX_AUDIO:
        return _transcribe_one(audio_bytes, filename, model, key)
    # 超過上限：照 MP3 frame 邊界自動切段，逐段轉錄，再把時間軸接回去
    chunks = split_audio(audio_bytes, filename)
    if not chunks:
        raise ValueError("這集音檔超過 25MB，又不是能自動切段的 MP3 格式。"
                         "請改用 MP3，或先剪成短一點的片段。")
    base = os.path.splitext(os.path.basename(filename.split("?")[0]))[0] or "audio"
    out = []
    for n, (chunk, t0) in enumerate(chunks):
        for s in _transcribe_one(chunk, "%s_%02d.mp3" % (base, n), model, key):
            out.append({"start": s["start"] + t0, "end": s["end"] + t0, "text": s["text"]})
    return out

def _transcribe_one(audio_bytes, filename, model, key, attempts=3, max_time=75):
    data = groq_transcribe_with_curl(audio_bytes, filename, model, key, attempts=attempts, max_time=max_time)
    return [{"start": s["start"], "end": s["end"], "text": (s.get("text") or "").strip()}
            for s in data.get("segments", []) if (s.get("text") or "").strip()]

def _transcribe_one_with_split_fallback(audio_bytes, filename, model, key, attempts=1):
    try:
        return _transcribe_one(audio_bytes, filename, model, key, attempts=attempts)
    except Exception as original_error:
        parts = mp3_chunks(audio_bytes, CHUNK_BYTES, FALLBACK_CHUNK_SECONDS, overlap_seconds=2)
        if not parts or len(parts) <= 1:
            raise
        print("[job] 原段失敗，改切成 %d 個小段補救…" % len(parts), flush=True)
        out = []
        for n, (part, offset) in enumerate(parts):
            try:
                segs = _transcribe_one(part, "%s_retry_%02d.mp3" % (filename, n), model, key,
                                       attempts=1, max_time=45)
            except Exception as sub_error:
                raise RuntimeError("Groq 轉錄失敗；原段和切小段都失敗：%s" % sub_error) from original_error
            for s in segs:
                out.append({"start": s["start"] + offset, "end": s["end"] + offset, "text": s["text"]})
        return out

def _transcribe_one_with_urllib(audio_bytes, filename, model, key):
    boundary = "----bpb" + os.urandom(8).hex()
    body = b""
    for name, val in (("model", model), ("response_format", "verbose_json")):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, name, val)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode()
    body += audio_bytes + b"\r\n" + ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(GROQ + "/audio/transcriptions", data=body, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "User-Agent": UA,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        data = json.loads(_urlopen_retry(req, timeout=300, attempts=3))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            data = groq_transcribe_with_curl(audio_bytes, filename, model, key)
        else:
            raise
    return [{"start": s["start"], "end": s["end"], "text": (s.get("text") or "").strip()}
            for s in data.get("segments", []) if (s.get("text") or "").strip()]

def groq_transcribe_with_curl(audio_bytes, filename, model, key, attempts=3, max_time=75):
    suffix = os.path.splitext(filename.split("?")[0])[1] or ".mp3"
    last = ""
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(audio_bytes)
        f.flush()
        args = ["curl", "--fail", "--silent", "--show-error", "--connect-timeout", "12", "--max-time", str(max_time),
                "-H", "Authorization: Bearer " + key,
                "-F", "model=" + model,
                "-F", "response_format=verbose_json",
                "-F", "file=@%s;filename=%s" % (f.name, os.path.basename(filename) or "audio.mp3"),
                GROQ + "/audio/transcriptions"]
        for n in range(attempts):
            p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if not p.returncode:
                return json.loads(p.stdout)
            last = p.stderr.decode("utf-8", "replace").strip()
            if not any(x in last for x in ("429", "500", "502", "503", "504", "timed out")):
                break
            time.sleep(1.5 * (n + 1))
    raise RuntimeError("Groq 轉錄失敗：" + last)

def groq_translate(texts, target, key):
    sysp = ("You are a subtitle translator. Translate each line into natural, fluent " + target +
            ". Keep meaning faithful and concise; do not merge or split lines. "
            'Return ONLY a JSON object {"t":[...]} where t is an array of strings, '
            "exactly the same length and order as the input array.")
    last_err = None
    for model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        for strict_json in (True, False):
            payload = {"model": model, "temperature": 0.2,
                       "messages": [{"role": "system", "content": sysp},
                                    {"role": "user", "content": json.dumps(texts, ensure_ascii=False)}]}
            if strict_json:
                payload["response_format"] = {"type": "json_object"}
            try:
                return _groq_translate_payload(payload, key, len(texts))
            except Exception as e:
                last_err = e
                continue
    raise RuntimeError("Groq 翻譯失敗：" + str(last_err))

def _parse_translation_content(content, expected):
    try:
        parsed = json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise ValueError("翻譯回應不是 JSON")
        parsed = json.loads(m.group(0))
    arr = parsed.get("t") or parsed.get("translations") or []
    if len(arr) < expected:
        raise ValueError("翻譯回應數量不足（%d/%d）" % (len(arr), expected))
    return [arr[i] if i < len(arr) else "" for i in range(expected)]

def _groq_translate_payload(payload, key, expected):
    j = groq_translate_with_curl(payload, key)
    return _parse_translation_content(j["choices"][0]["message"]["content"], expected)

def groq_translate_with_curl(payload, key):
    args = ["curl", "--fail", "--silent", "--show-error", "--connect-timeout", "20", "--max-time", "75",
            "-H", "Authorization: Bearer " + key,
            "-H", "Content-Type: application/json",
            "--data", json.dumps(payload, ensure_ascii=False),
            GROQ + "/chat/completions"]
    last = ""
    for n in range(3):
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not p.returncode:
            return json.loads(p.stdout)
        last = p.stderr.decode("utf-8", "replace").strip()
        if not any(x in last for x in ("429", "500", "502", "503", "504", "timed out")):
            break
        time.sleep(1.5 * (n + 1))
    raise RuntimeError("Groq 翻譯失敗：" + last)

def explain_http_error(where, e):
    detail = ""
    try:
        detail = e.read().decode("utf-8", "replace")[:300]
    except Exception:
        pass
    msg = "%s 回報 HTTP %s" % (where, e.code)
    if "error code: 1010" in detail.lower():
        msg += "（Cloudflare 1010，來源網站或 API 擋下這次請求）"
    if detail:
        msg += "：" + re.sub(r"\s+", " ", detail).strip()
    return msg

# ----------------------------------------------------------------------------
# 逐段交付：先下載＋切段（job_start），再讓前端一段一段要轉錄結果（job_chunk）
# ----------------------------------------------------------------------------
def job_start(data):
    """下載（或收下上傳的）音檔、切段、暫存在記憶體，回傳每段的起始時間。"""
    if data.get("audioB64"):
        audio = base64.b64decode(data["audioB64"])
        name = data.get("filename", "audio.mp3")
    else:
        audio = http_get(data["audioUrl"], timeout=600, accept="audio/*,application/octet-stream,*/*")
        name = data["audioUrl"].split("?")[0].split("/")[-1] or "audio.mp3"
    audio, name = normalize_audio_for_playback(audio, name)
    print("[job] 已下載 %.1f MB，開始切段…" % (len(audio) / 1024 / 1024), flush=True)
    chunks = split_audio(audio, name)
    if not chunks:
        raise ValueError("這集音檔超過 25MB，又不是能自動切段的 MP3 格式。請改用 MP3，或先剪成短一點的片段。")
    print("[job] 切成 %d 段，準備逐段轉錄" % len(chunks), flush=True)
    job_id = hashlib.sha1(("%s|%s|%d|%f" % (data.get("audioUrl", ""), name, len(audio), time.time())).encode()).hexdigest()[:16]
    suffix = os.path.splitext(name.split("?")[0])[1] or ".mp3"
    fd, audio_path = tempfile.mkstemp(prefix="bpp_%s_" % job_id, suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        delete_job({"audioPath": audio_path})
        raise
    now = time.time()
    with JOBS_LOCK:
        # 清掉太舊的，再限制最多保留幾個，避免記憶體一直長大
        for jid in [k for k, v in JOBS.items() if now - v["ts"] > 1800]:
            delete_job(JOBS.pop(jid))
        while len(JOBS) >= 4:
            oldest = min(JOBS, key=lambda k: JOBS[k]["ts"])
            delete_job(JOBS.pop(oldest))
        JOBS[job_id] = {
            "chunks": chunks,
            "name": name,
            "ts": now,
            "audioPath": audio_path,
            "audioSize": len(audio),
            "audioType": mimetypes.guess_type(name)[0] or "audio/mpeg",
        }
    return {"jobId": job_id, "count": len(chunks),
            "starts": [round(t, 3) for _, t in chunks],
            "audioUrl": "/api/job_audio?jobId=" + job_id}

def job_chunk(data):
    """轉錄某一段，回傳該段的字幕（時間軸已接回整集）。翻譯由前端分批處理。"""
    with JOBS_LOCK:
        job = JOBS.get(data["jobId"])
        if job:
            job["ts"] = time.time()            # 續命，避免處理中被清掉
    if not job:
        raise ValueError("這次的工作階段已過期，請重新點選這一集。")
    idx = int(data["index"])
    if idx < 0 or idx >= len(job["chunks"]):
        raise ValueError("段落索引超出範圍，請重新點選這一集。")
    chunk, t0 = job["chunks"][idx]
    base = os.path.splitext(os.path.basename(job["name"].split("?")[0]))[0] or "audio"
    attempts = max(1, min(3, int(data.get("attempts", 3))))
    print("[job] 轉錄第 %d/%d 段…" % (idx + 1, len(job["chunks"])), flush=True)
    filename = "%s_%02d.mp3" % (base, idx)
    if data.get("splitFallback"):
        segs = _transcribe_one_with_split_fallback(chunk, filename, data["model"], data["key"], attempts=attempts)
    else:
        segs = _transcribe_one(chunk, filename, data["model"], data["key"], attempts=attempts)
    print("[job] 第 %d 段完成，%d 句" % (idx + 1, len(segs)), flush=True)
    return {"index": idx,
            "segments": [{"start": s["start"] + t0, "end": s["end"] + t0, "text": s["text"]} for s in segs]}

# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安靜一點
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")  # 永遠拿最新的網頁，別用瀏覽器快取
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_auth_cookie(self):
        body = json.dumps({"ok": True}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Set-Cookie", "%s=%s; Path=/; Max-Age=2592000; SameSite=Lax; HttpOnly" %
                         (APP_COOKIE, auth_token()))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, data=None):
        if not APP_PASSWORD:
            return True
        cookies = parse_cookies(self.headers.get("Cookie"))
        if cookies.get(APP_COOKIE) == auth_token():
            return True
        return bool(data and data.get("appPassword") == APP_PASSWORD)

    def _require_auth(self, data=None):
        if self._authed(data):
            return
        raise PermissionError("請先輸入 App 密碼。")

    def _groq_key(self, data):
        if SERVER_GROQ_KEY:
            return SERVER_GROQ_KEY
        key = (data.get("key") or "").strip()
        if not key:
            raise ValueError("先貼上 Groq 金鑰。")
        return key

    def _proxy_audio(self, url):
        headers = {
            "User-Agent": UA,
            "Accept": "audio/*,application/octet-stream,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        req = urllib.request.Request(url, headers=headers)
        try:
            upstream = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code not in (206, 416):
                raise
            upstream = e
        status = upstream.getcode() or (206 if rng else 200)
        self.send_response(status)
        ctype = upstream.headers.get("Content-Type") or "audio/mpeg"
        self.send_header("Content-Type", ctype)
        for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
            v = upstream.headers.get(h)
            if v:
                self.send_header(h, v)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(256 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            upstream.close()

    def _serve_job_audio(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["ts"] = time.time()
        if not job or not os.path.isfile(job.get("audioPath", "")):
            return self._send(404, {"error": "這份同步音訊已過期，請重新點選這一集。"})

        size = int(job["audioSize"])
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)", rng)
        if match:
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = min(int(match.group(2)), size - 1)
            if not match.group(1) and match.group(2):
                length = min(int(match.group(2)), size)
                start = size - length
                end = size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", job["audioType"])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "private, max-age=1800")
        self.end_headers()
        try:
            with open(job["audioPath"], "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)
        except FileNotFoundError:
            return

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "找不到 index.html，請確認它和 serve.py 在同一個資料夾。", "text/plain; charset=utf-8")
        elif path == "/api/config":
            self._send(200, {"serverKey": bool(SERVER_GROQ_KEY),
                             "passwordRequired": bool(APP_PASSWORD),
                             "authed": self._authed()})
        elif path == "/api/audio":
            if APP_PASSWORD and not self._authed():
                return self._send(401, {"error": "請先輸入 App 密碼。"})
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            url = (qs.get("url") or [""])[0]
            if not url.startswith(("http://", "https://")):
                return self._send(400, {"error": "missing audio url"})
            try:
                self._proxy_audio(url)
            except urllib.error.HTTPError as e:
                self._send(502, {"error": explain_http_error("音檔", e)})
            except urllib.error.URLError as e:
                self._send(502, {"error": "連不上音檔來源：%s" % e.reason})
        elif path == "/api/job_audio":
            if APP_PASSWORD and not self._authed():
                return self._send(401, {"error": "請先輸入 App 密碼。"})
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._serve_job_audio((qs.get("jobId") or [""])[0])
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            return self._send(400, {"error": "壞掉的請求內容"})
        try:
            route = self.path.split("?")[0]
            if route == "/api/auth":
                if not APP_PASSWORD:
                    return self._send(200, {"ok": True})
                if data.get("appPassword") != APP_PASSWORD:
                    return self._send(401, {"error": "App 密碼不正確。"})
                return self._send_auth_cookie()
            if route == "/api/search":
                self._require_auth(data)
                return self._send(200, {"results": search_podcasts(
                    data.get("query", ""),
                    data.get("country", "us"),
                    data.get("limit", 24),
                )})
            if route == "/api/feed":
                self._require_auth(data)
                self._send(200, parse_feed(http_get(data["feedUrl"], accept="application/rss+xml,application/xml,text/xml,*/*"),
                                           data.get("feedUrl")))
            elif route == "/api/transcript":
                self._require_auth(data)
                self._send(200, {"segments": fetch_transcript(data["url"], data.get("type"))})
            elif route == "/api/transcribe":
                self._require_auth(data)
                audio = http_get(data["audioUrl"], timeout=600, accept="audio/*,application/octet-stream,*/*")
                name = data["audioUrl"].split("?")[0].split("/")[-1] or "audio.mp3"
                self._send(200, {"segments": groq_transcribe(audio, name, data["model"], self._groq_key(data))})
            elif route == "/api/transcribe_upload":
                self._require_auth(data)
                audio = base64.b64decode(data["audioB64"])
                self._send(200, {"segments": groq_transcribe(audio, data.get("filename", "audio.mp3"),
                                                             data["model"], self._groq_key(data))})
            elif route == "/api/job_start":
                self._require_auth(data)
                self._send(200, job_start(data))
            elif route == "/api/job_chunk":
                self._require_auth(data)
                data["key"] = self._groq_key(data)
                self._send(200, job_chunk(data))
            elif route == "/api/translate":
                self._require_auth(data)
                self._send(200, {"t": groq_translate(data["texts"], data["target"], self._groq_key(data))})
            else:
                self._send(404, {"error": "unknown endpoint"})
        except PermissionError as e:
            self._send(401, {"error": str(e)})
        except urllib.error.HTTPError as e:
            where = "上游服務"
            if self.path.startswith("/api/feed"):
                where = "RSS feed"
            elif self.path.startswith("/api/search"):
                where = "Podcast 目錄"
            elif self.path.startswith("/api/transcript"):
                where = "字幕檔"
            elif self.path.startswith("/api/transcribe") or self.path.startswith("/api/job"):
                where = "音檔或 Groq API"
            elif self.path.startswith("/api/translate"):
                where = "Groq 翻譯 API"
            self._send(502, {"error": explain_http_error(where, e)})
        except urllib.error.URLError as e:
            self._send(502, {"error": "連不上來源：%s" % e.reason})
        except Exception as e:
            self._send(500, {"error": str(e)})


def kill_stale_servers(port):
    """
    關掉還占著這個 port 的舊 serve.py。
    只砍指令列裡含 "serve.py" 的「自己人」，避免誤殺剛好用同一個 port 的其他程式。
    需要 lsof（macOS / Linux 內建）；沒有就安靜跳過。
    """
    me = os.getpid()
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    except (FileNotFoundError, OSError):
        return
    for tok in out.decode("utf-8", "replace").split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid == me:
            continue
        cmd = ""
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
        except Exception:
            pass
        if "serve.py" not in cmd:
            continue                                # 不是我們的伺服器，留著
        try:
            os.kill(pid, 15)                        # 先客氣地請它收工
            time.sleep(0.4)
            os.kill(pid, 9)                         # 還沒走就強制
        except (ProcessLookupError, PermissionError, OSError):
            pass
        print("已關掉一個還開著的舊伺服器 (PID %d)" % pid)


def main():
    os.chdir(HERE)
    if not os.path.exists(os.path.join(HERE, "index.html")):
        print("⚠️  找不到 index.html，請確認它和 serve.py 在同一個資料夾再執行。")

    # 雲端平台（Render / Railway / Fly 等）會設 PORT 環境變數：綁 0.0.0.0、不要開瀏覽器
    env_port = os.environ.get("PORT")
    if env_port:
        httpd = ThreadingHTTPServer(("0.0.0.0", int(env_port)), Handler)
        print("雙語 Podcast 小幫手已啟動，listening on 0.0.0.0:%s" % env_port)
        httpd.serve_forever()
        return

    # 本機模式：先關掉還開著的舊伺服器，確保只剩這一個、而且就在 8000
    kill_stale_servers(PORT)
    httpd = None
    port = PORT
    for candidate in range(PORT, PORT + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise OSError("找不到可用的本機 port（已試過 %s-%s）" % (PORT, PORT + 19))

    url = "http://localhost:%d" % port
    print("雙語 Podcast 小幫手已啟動 →  " + url)
    print("（要關掉就回到這個視窗按 Ctrl+C）")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    httpd.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已關閉。掰掰 👋")
