#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RedGuo (红果短剧) episode downloader.

Resolves episodes of a 红果短剧 series via the official Fanqie/Hongguo playback
API (`api5-normal-sinfonlineb.fqnovel.com`), downloads the (CENC-encrypted)
media, derives the content key and decrypts it with ffmpeg.

This needs no 红果 account/login: the app playback API is anonymous. It is the
workaround for episodes that a public share (e.g. Quark) blocks.

Input:  a hongguoduanju.com detail/player URL, or a raw series_id.
Usage:
    python redguo_downloader.py "https://hongguoduanju.com/detail?series_id=7684943340281793560" \
        --output "D:\\project\\downloader\\downloads" --workers 3 --quality 1080

Requires: requests, pycryptodome, imageio-ffmpeg
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

API_BASE = "https://api5-normal-sinfonlineb.fqnovel.com"
API_PATH = "/novel/player/video_model/v1/"
HG_UA = ("com.phoenix.read/73532 (Linux; U; Android 16; zh_CN; 25053RT47C; "
         "Build/BP2A.250605.031.A3; Cronet/TTNetVersion:04657795 2026-01-23 "
         "QuicVersion:c67e9834 2025-09-08)")
MEDIA_REFERER = "https://novel.snssdk.com/"
SIGN_KEY = bytes([0x44, 0xB9, 0xB9, 0xD9, 0xA4, 0xAE, 0xF9, 0xFC, 0xA4, 0x93, 0xAA, 0x75,
                  0x7C, 0xA3, 0xC2, 0xC4, 0xA4, 0x96, 0x93, 0x8F])

log_lock = threading.Lock()
url_lock = threading.Lock()
url_cache: dict[str, tuple[float, str, str, bool]] = {}  # vid -> (expire, url, keyhex, encrypted)


def log(*a):
    with log_lock:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- #
# signing
# --------------------------------------------------------------------------- #
def _rotl8(x, n):
    x &= 0xFF
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _rev8(x):
    r = 0
    for i in range(8):
        r = (r << 1) | ((x >> i) & 1)
    return r & 0xFF


def _gen_id():
    return str(1_000_000_000_000_000_000 + random.randint(0, 8_000_000_000_000_000_000))


def _sign(prepared, body, now):
    ts = int(now) & 0xFFFFFFFF
    rawq = prepared.url.split("?", 1)[1].encode() if "?" in prepared.url else b""
    qh = hashlib.md5(rawq).digest()
    payload = bytearray(20)
    payload[0:4] = qh[:4]
    if body is not None:
        bh = hashlib.md5(body).digest()
        payload[4:8] = bh[:4]
        prepared.headers["X-SS-STUB"] = bh.hex().upper()
    payload[12:16] = bytes([0, 6, 11, 28])
    payload[16:20] = ts.to_bytes(4, "big")
    for i in range(20):
        payload[i] ^= SIGN_KEY[i]
    for i in range(20):
        mixed = _rotl8(payload[i], 4) ^ payload[(i + 1) % 20]
        payload[i] = _rev8(mixed) ^ 0xFF ^ 20
    sig = bytes([0x84, 0x04, 0x40, 0x1C, 0, 0]) + bytes(payload)
    prepared.headers["X-Khronos"] = str(ts)
    prepared.headers["X-Gorgon"] = sig.hex()
    prepared.headers["X-SS-Req-Ticket"] = str(int(now * 1000))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def fetch_video_model(video_id: str, retries: int = 4) -> dict:
    from requests import Request, Session

    last = None
    for attempt in range(retries):
        try:
            now = time.time()
            params = {
                "aid": "8662", "app_name": "novelread", "version_code": "73532",
                "version_name": "7.3.5.32", "manifest_version_code": "73532",
                "update_version_code": "73532", "channel": "update_64",
                "device_platform": "android", "os": "android", "ssmix": "a",
                "device_type": "25053RT47C", "device_brand": "Redmi", "language": "zh",
                "os_api": "36", "os_version": "16", "resolution": "1280*2772",
                "dpi": "520", "ac": "wifi", "device_id": _gen_id(), "iid": _gen_id(),
                "_rticket": str(int(now * 1000)),
            }
            payload = {"video_id": video_id, "content_type": 1,
                       "biz_param": {"need_all_video_definition": True, "video_platform": 3}}
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            s = Session()
            req = Request("POST", API_BASE + API_PATH, params=params, data=body,
                          headers={"User-Agent": HG_UA, "Accept": "application/json",
                                   "X-XS-From-Web": "0", "Sdk-Version": "2",
                                   "Content-Type": "application/json; charset=utf-8"})
            prepared = s.prepare_request(req)
            _sign(prepared, body, now)
            r = s.send(prepared, timeout=30)
            j = r.json()
            if j.get("code") == 0 and j.get("data", {}).get("video_model"):
                vm = j["data"]["video_model"]
                return json.loads(vm) if isinstance(vm, str) else vm
            last = j
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"video_model failed for {video_id}: {last}")


def _b64d(s: str) -> bytes:
    try:
        return base64.b64decode(s, validate=True)
    except Exception:
        return base64.b64decode(s + "=" * (-len(s) % 4))


def content_key(spade_a: str) -> bytes:
    raw = _b64d(spade_a)
    tag_length = (raw[0] ^ raw[1] ^ raw[2]) - 48
    content_length = len(raw) - tag_length - 1
    seed = raw[len(raw) - tag_length - 2] ^ raw[len(raw) - tag_length - 1]
    tag = bytes(raw[len(raw) - tag_length + i] ^ seed for i in range(tag_length))
    decoded = bytearray(content_length)
    pe, po = 250, 85
    for i, cur in enumerate(raw[1:1 + content_length]):
        if i % 2 == 0:
            prev = pe; pe = cur
        else:
            prev = po; po = cur
        decoded[i] = ((prev ^ cur) - 21 - bin(i).count("1")) & 0xFF
    padding = int(chr(decoded[0]), 36)
    if content_length - padding - 1 != 32:
        raise RuntimeError("content key structure mismatch")
    return bytes.fromhex(decoded[1:33].decode("ascii"))


def pick_variant(model: dict, prefer: int) -> dict:
    variants = model.get("video_list")
    if isinstance(variants, dict):
        variants = list(variants.values())
    if not variants:
        raise RuntimeError("no video variants")

    def h(it):
        d = (it.get("video_meta") or {}).get("definition") or ""
        m = "".join(c for c in d if c.isdigit())
        return int(m) if m else 0

    usable = [v for v in variants if (v.get("video_meta") or {}).get("codec_type") != "bytevc2"]
    usable = usable or variants
    if prefer:
        le = [v for v in usable if h(v) <= prefer]
        return max(le or usable, key=h)
    return max(usable, key=h)


# --------------------------------------------------------------------------- #
# series metadata
# --------------------------------------------------------------------------- #
def get_series(series_id: str) -> dict:
    url = f"https://hongguoduanju.com/detail?series_id={series_id}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    h = html.unescape(r.text)
    name = re.search(r'"series_name":"([^"]*)"', h)
    vl = re.search(r'"vid_list":\s*(\[[^\]]*\])', h)
    if not vl:
        raise RuntimeError("vid_list not found; check series_id / page")
    vids = json.loads(vl.group(1))
    total = re.search(r'"episode_cnt":(\d+)', h)
    return {"series_id": series_id, "series_name": name.group(1) if name else series_id,
            "vids": vids, "episode_cnt": int(total.group(1)) if total else len(vids)}


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip().rstrip(".") or "_"


def resolve_media(video_id: str, quality: int) -> tuple[str, str, bool]:
    """Return (media_url, content_key_hex, encrypted)."""
    with url_lock:
        cached = url_cache.get(video_id)
        if cached and cached[0] - time.time() > 60:
            return cached[1], cached[2], cached[3]
    model = fetch_video_model(video_id)
    variant = pick_variant(model, quality)
    enc = variant.get("encrypt_info") or {}
    encrypted = bool(enc.get("spade_a")) or enc.get("encrypt") is True or \
        (enc.get("encryption_method") == "cenc-aes-ctr")
    keyhex = content_key(enc["spade_a"]).hex() if enc.get("spade_a") else ""
    url = variant.get("main_url") or variant.get("backup_url") or ""
    if not url:
        raise RuntimeError("no media url")
    expire = float(model.get("url_expire") or 0)
    with url_lock:
        url_cache[video_id] = (expire, url, keyhex, encrypted)
    return url, keyhex, encrypted


def download_episode(series: dict, index: int, out_dir: str, quality: int, ffmpeg: str):
    ep = index + 1
    dest = os.path.join(out_dir, f"第{ep}集.mp4")
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        log(f"SKIP  第{ep}集.mp4")
        return True
    video_id = series["vids"][index]
    for attempt in range(5):
        try:
            url, keyhex, encrypted = resolve_media(video_id, quality)
            enc_path = dest + ".enc.part"
            headers = {"User-Agent": HG_UA, "Referer": MEDIA_REFERER, "Accept": "*/*"}
            with requests.get(url, headers=headers, stream=True, timeout=(20, 120)) as r:
                r.raise_for_status()
                with open(enc_path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
            tmp_out = dest + ".tmp.mp4"
            if encrypted:
                cmd = [ffmpeg, "-y", "-loglevel", "error", "-decryption_key", keyhex,
                       "-i", enc_path, "-c", "copy", tmp_out]
                p = subprocess.run(cmd, capture_output=True, text=True)
                if p.returncode != 0 or not os.path.exists(tmp_out):
                    raise RuntimeError(f"ffmpeg decrypt failed: {p.stderr[:200]}")
                os.replace(tmp_out, dest)
            else:
                os.replace(enc_path, dest)
            try:
                os.remove(enc_path)
            except OSError:
                pass
            log(f"DONE  第{ep}集.mp4 [{os.path.getsize(dest)//1024} KB]")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"RETRY 第{ep}集 ({attempt+1}/5): {e}")
            time.sleep(min(2 ** attempt, 20))
    log(f"FAIL  第{ep}集")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="RedGuo/红果短剧 downloader")
    ap.add_argument("url", help="hongguoduanju detail/player URL or series_id")
    ap.add_argument("--output", default=os.path.join(SCRIPT_DIR, "downloads"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--quality", type=int, default=1080, help="max height (360/480/540/720/1080)")
    ap.add_argument("--episodes", default="", help="e.g. 1-10 or 1,5,8 (default: all)")
    args = ap.parse_args()

    m = re.search(r"(\d{15,})", args.url)
    if not m:
        print("cannot parse series_id from input")
        return 2
    series_id = m.group(1)
    series = get_series(series_id)
    log(f"series: {series['series_name']} ({series['episode_cnt']} eps)")

    out_dir = os.path.join(args.output, sanitize(series["series_name"]))
    os.makedirs(out_dir, exist_ok=True)

    indices = list(range(len(series["vids"])))
    if args.episodes:
        sel = set()
        for part in args.episodes.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                sel.update(range(int(a) - 1, int(b)))
            elif part:
                sel.add(int(part) - 1)
        indices = [i for i in indices if i in sel]

    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        print("imageio-ffmpeg not available:", e)
        return 2

    ok = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(download_episode, series, i, out_dir, args.quality, ffmpeg): i for i in indices}
        for fut in as_completed(futs):
            if fut.result():
                ok += 1
    log(f"FINISHED {ok}/{len(indices)} -> {out_dir}")
    return 0 if ok == len(indices) else 1


if __name__ == "__main__":
    sys.exit(main())
