#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Public-share downloader for Quark Pan (pan.quark.cn), with Baidu Pan detection.

The Quark flow is fully anonymous: the public share page issues anonymous
cookies (ctoken) and a share token (stoken) that allow listing and downloading
shared files directly, with no user login. Baidu Pan's public API blocks
anonymous listing/download (errno -9), so a Baidu link is only probed and
reported unless an alternate Quark link is supplied.

Anonymous Quark guests are capped at roughly 50 MB per file. Larger files
return error 23018; pass a logged-in Quark cookie (--cookie / --cookie-file /
$QUARK_COOKIE / ./cookie.txt) to use the authenticated save-and-download flow,
which lifts the cap.

Usage:
    python downloader.py "<SHARE_URL>" --output "<DIR>"
    python downloader.py "<BAIDU_SHARE_URL>" --pwd "<CODE>" --output "<DIR>"
    python downloader.py "<QUARK_URL>" --alt-url "<BAIDU_URL>" --output "<DIR>"

Run `python downloader.py --help` for the full option list.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "download.log")
MANIFEST_FILE = os.path.join(SCRIPT_DIR, "manifest.json")
FAILED_FILE = os.path.join(SCRIPT_DIR, "failed.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Quark PC-client User-Agent. Needed to lift the guest/free per-file download
# size cap (error 23018) on the logged-in `file/download` path.
QUARK_PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
    "Safari/537.36 Channel/pckk_other_ch"
)


class QuarkSizeLimit(RuntimeError):
    """Anonymous/guest per-file download size cap hit (Quark code 23018)."""


log = logging.getLogger("downloader")
_manifest_lock = threading.Lock()
_print_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def human(n: int | float | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip().rstrip(".")
    return name or "_"


def detect_provider(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "quark.cn" in host:
        return "quark"
    if "baidu.com" in host:
        return "baidu"
    return "unknown"


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    try:
        from urllib3.util.retry import Retry

        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,
            raise_on_status=False,
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    except Exception:
        pass
    return s


# --------------------------------------------------------------------------- #
# Quark anonymous client
# --------------------------------------------------------------------------- #
class QuarkClient:
    provider = "quark"
    BASE = "https://drive-pc.quark.cn"

    def __init__(self, share_url: str, pwd: str = "", cookie: str = ""):
        self.share_url = share_url
        m = re.search(r"/s/([0-9a-zA-Z]+)", share_url)
        if not m:
            raise ValueError(f"Cannot parse Quark pwd_id from {share_url!r}")
        self.pwd_id = m.group(1)
        self.pwd = pwd or ""
        self.cookie = (cookie or "").strip()
        self.session = _new_session()
        self.session.headers.update(
            {
                "Referer": "https://pan.quark.cn/",
                "Origin": "https://pan.quark.cn",
            }
        )
        if self.cookie:
            self.session.headers["Cookie"] = self.cookie
        self.stoken: str | None = None
        self.share_title: str | None = None

    # -- token ------------------------------------------------------------- #
    def get_stoken(self) -> str:
        # Seed anonymous cookies from the public share page.
        try:
            self.session.get(
                f"https://pan.quark.cn/s/{self.pwd_id}", timeout=30
            )
        except requests.RequestException:
            pass

        r = self.session.post(
            f"{self.BASE}/1/clouddrive/share/sharepage/token",
            params={"pr": "ucpro", "fr": "pc"},
            json={
                "pwd_id": self.pwd_id,
                "passcode": self.pwd,
                "support_visit_limit_private_share": True,
            },
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0 or not j.get("data"):
            raise RuntimeError(f"Quark token failed: {j}")
        self.stoken = j["data"]["stoken"]
        self.share_title = j["data"].get("title")
        log.info("Quark share token acquired (anonymous). title=%r", self.share_title)
        return self.stoken

    # -- listing ----------------------------------------------------------- #
    def _list_dir(self, pdir_fid: str, page: int, size: int = 100) -> dict:
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "ver": 2,
            "pwd_id": self.pwd_id,
            "stoken": self.stoken,
            "pdir_fid": pdir_fid,
            "force": 0,
            "_page": page,
            "_size": size,
            "_fetch_banner": 1 if pdir_fid == "0" else 0,
            "_fetch_share": 1 if pdir_fid == "0" else 0,
            "fetch_relate_conversation": 1 if pdir_fid == "0" else 0,
            "_fetch_total": 1,
            "_sort": "file_type:asc,file_name:asc",
        }
        r = self.session.get(
            f"{self.BASE}/1/clouddrive/share/sharepage/detail",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"Quark list failed: {j}")
        return j["data"]

    def enumerate(self) -> list[dict]:
        if not self.stoken:
            self.get_stoken()
        files: list[dict] = []
        page_size = 100

        def walk(pdir_fid: str, rel: str) -> None:
            page = 1
            while True:
                data = self._list_dir(pdir_fid, page, page_size)
                items = data.get("list") or []
                for it in items:
                    name = it.get("file_name") or it.get("fid")
                    rel_path = f"{rel}/{name}" if rel else name
                    if it.get("dir"):
                        walk(it["fid"], rel_path)
                    else:
                        files.append(
                            {
                                "file_name": name,
                                "relative_path": rel_path,
                                "remote_file_id": it["fid"],
                                "expected_size": int(it.get("size") or 0),
                                "category": it.get("category"),
                                "share_fid_token": it.get("share_fid_token"),
                            }
                        )
                if len(items) < page_size:
                    break
                page += 1
                if page > 10000:
                    break

        walk("0", "")
        return files

    # -- download url (anonymous share flow) ------------------------------- #
    def get_download_urls(self, fids: list[str]) -> dict:
        r = self.session.post(
            f"{self.BASE}/1/clouddrive/file/share/download",
            params={"pr": "ucpro", "fr": "pc"},
            json={"fids": fids, "pwd_id": self.pwd_id, "stoken": self.stoken},
            timeout=60,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("code") == 23018:
            raise QuarkSizeLimit(j.get("message") or "download file size limit")
        if j.get("code") != 0:
            raise RuntimeError(f"Quark download-url failed: {j}")
        return {x["fid"]: x for x in (j.get("data") or [])}

    # -- download url (logged-in flow: save -> poll task -> file/download) -- #
    def save_and_get_download_url(self, fid: str, fid_token: str) -> dict:
        """Bypass the guest 23018 size cap using an authenticated cookie.

        Mirrors the proven flow: sharepage/save (转存) -> poll /1/clouddrive/task
        -> file/download (with the Quark PC client User-Agent). The temporarily
        saved copy is deleted afterwards by the caller.
        """
        if not self.cookie:
            raise QuarkSizeLimit("no cookie provided; cannot bypass 23018")
        if not fid_token:
            raise RuntimeError(f"missing share_fid_token for fid {fid}")

        # 1) 转存 into the account's root.
        save = self.session.post(
            f"{self.BASE}/1/clouddrive/share/sharepage/save",
            params={"pr": "ucpro", "fr": "pc"},
            json={
                "fid_list": [fid],
                "fid_token_list": [fid_token],
                "to_pdir_fid": "0",
                "pwd_id": self.pwd_id,
                "stoken": self.stoken,
                "pdir_fid": "0",
                "scene": "link",
            },
            timeout=60,
        ).json()
        if save.get("code") != 0:
            raise RuntimeError(f"Quark save(转存) failed: {save}")

        saved = ((save.get("data") or {}).get("task_resp") or {}).get("data", {}).get(
            "save_as", {}
        ).get("save_as_top_fids") or []
        task_id = (save.get("data") or {}).get("task_id")

        # 2) If not returned synchronously, poll the task.
        if not saved and task_id:
            for _ in range(60):
                time.sleep(1)
                t = self.session.get(
                    f"{self.BASE}/1/clouddrive/task",
                    params={"pr": "ucpro", "fr": "pc", "task_id": task_id},
                    timeout=30,
                ).json()
                td = t.get("data") or {}
                if td.get("status") in (1, 2):
                    saved = (td.get("save_as") or {}).get("save_as_top_fids") or []
                    break
        if not saved:
            raise RuntimeError("Quark save(转存) produced no fids")

        # 3) Get the direct URL for the saved (owned) file with the PC UA.
        r = self.session.post(
            f"{self.BASE}/1/clouddrive/file/download",
            params={"pr": "ucpro", "fr": "pc"},
            headers={"User-Agent": QUARK_PC_UA},
            json={"fids": saved},
            timeout=60,
        ).json()
        if r.get("code") == 23018:
            raise QuarkSizeLimit(r.get("message") or "download file size limit")
        if r.get("code") != 0:
            raise RuntimeError(f"Quark file/download failed: {r}")
        for item in r.get("data") or []:
            if item.get("download_url"):
                item["_saved_fid"] = saved[0]
                return item
        raise RuntimeError("Quark file/download returned no url")

    def delete_saved(self, fids: list[str]) -> None:
        try:
            self.session.post(
                f"{self.BASE}/1/clouddrive/file/delete",
                params={"pr": "ucpro", "fr": "pc"},
                json={"action_type": 2, "filelist": fids, "exclude_fids": []},
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Baidu client (detection / best-effort anonymous probe)
# --------------------------------------------------------------------------- #
class BaiduClient:
    provider = "baidu"

    def __init__(self, share_url: str, pwd: str = ""):
        self.share_url = share_url
        self.pwd = pwd or ""
        self.session = _new_session()
        self.session.headers.update({"Referer": "https://pan.baidu.com/"})

    def enumerate(self) -> list[dict]:
        """Baidu Pan does not allow anonymous listing of shared files.

        The public share page only exposes metadata (shareid/uk). The list and
        download APIs return errno -9 (login / extraction-code verification)
        for anonymous visitors -- a BDUSS cookie from a logged-in account is
        required. We probe once and raise a clear, actionable error.
        """
        m = re.search(r"/s/([0-9a-zA-Z_-]+)", self.share_url)
        surl = m.group(1) if m else ""
        r = self.session.get(self.share_url, timeout=30)
        html = r.text
        sm = re.search(r"locals\.mset\((\{.*?\})\)", html, re.S)
        info = json.loads(sm.group(1)) if sm else {}
        shareid = info.get("shareid")
        uk = info.get("share_uk")
        log.info("Baidu probe: loginstate=%s shareid=%s uk=%s", info.get("loginstate"), shareid, uk)
        if not shareid or not uk:
            raise RuntimeError("Baidu: could not parse share metadata (link may be invalid/expired).")

        t = int(time.time() * 1000)
        verify = self.session.post(
            f"https://pan.baidu.com/share/verify?surl={surl}&t={t}"
            "&channel=chunlei&web=1&app_id=250528&clienttype=0",
            data={"pwd": self.pwd},
            timeout=30,
        )
        log.info("Baidu verify response: %s", verify.text[:200])
        if "BDCLND" not in self.session.cookies.get_dict():
            raise RuntimeError(
                "Baidu: extraction-code verification did not yield an anonymous "
                "BDCLND cookie (errno 105). Anonymous download unsupported."
            )

        lr = self.session.get(
            "https://pan.baidu.com/share/list",
            params={
                "uk": uk, "shareid": shareid, "order": "other", "desc": "1",
                "showempty": "0", "web": "1", "page": "1", "num": "100",
                "dir": "/", "t": t, "channel": "chunlei", "app_id": "250528",
                "clienttype": "0", "bdstoken": "",
            },
            timeout=30,
        )
        body = lr.text
        log.info("Baidu list response: %s", body[:200])
        if '"errno":0' not in body.replace(" ", ""):
            raise RuntimeError(
                "Baidu: anonymous share listing/download is blocked (errno != 0). "
                "A logged-in BDUSS cookie is required. Use the Quark link instead."
            )
        data = lr.json().get("list") or []
        return [
            {
                "file_name": it.get("server_filename"),
                "relative_path": it.get("path", "/").lstrip("/"),
                "remote_file_id": it.get("fs_id"),
                "expected_size": int(it.get("size") or 0),
            }
            for it in data
            if not it.get("isdir")
        ]


# --------------------------------------------------------------------------- #
# Download engine
# --------------------------------------------------------------------------- #
class DownloadManager:
    def __init__(self, output_dir: str, workers: int = 3, use_aria2: bool = False):
        self.output_dir = os.path.abspath(output_dir)
        self.workers = max(1, workers)
        self.use_aria2 = use_aria2 and shutil.which("aria2c") is not None
        os.makedirs(self.output_dir, exist_ok=True)
        self.aria2c = shutil.which("aria2c") if self.use_aria2 else None

    # -- single attempt ---------------------------------------------------- #
    def _attempt_download(
        self, client, url: str, dest: str, expected: int, name: str
    ) -> int:
        """One download attempt with a fresh connection/session."""
        part = dest + ".part"
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        pos = os.path.getsize(part) if os.path.exists(part) else 0
        if expected and pos >= expected:
            os.replace(part, dest)
            return pos

        if self.aria2c:
            return self._download_with_aria2(client, url, dest, name)

        session = self.download_session(client)
        dl_ua = QUARK_PC_UA if getattr(client, "cookie", "") else UA
        headers = {"User-Agent": dl_ua, "Referer": "https://pan.quark.cn/"}
        if pos > 0:
            headers["Range"] = f"bytes={pos}-"

        with session.get(url, headers=headers, stream=True, timeout=(20, 120)) as r:
            if r.status_code == 416:
                os.replace(part, dest)
                return expected or pos
            if r.status_code not in (200, 206):
                body = r.raw.read(200)
                raise RuntimeError(
                    f"HTTP {r.status_code} via {r.headers.get('Via')} "
                    f"body={body[:80]!r}"
                )
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype or "application/json" in ctype:
                raise RuntimeError(f"unexpected Content-Type: {ctype}")

            if pos > 0 and r.status_code == 200:
                pos = 0
                mode = "wb"
            else:
                mode = "ab" if pos > 0 else "wb"

            cl = r.headers.get("Content-Length")
            total = expected
            if cl:
                total = int(cl) + (pos if r.status_code == 206 else 0)

            done = pos
            last_report = 0.0
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_report >= 5 and total:
                        log.info(
                            "  %s: %s/%s (%.1f%%)",
                            name, human(done), human(total), done * 100.0 / total,
                        )
                        last_report = now

        if expected and done != expected:
            raise RuntimeError(f"size mismatch: got {done} bytes, expected {expected}")
        os.replace(part, dest)
        return done

    def _download_with_aria2(
        self, client, url: str, dest: str, name: str
    ) -> int:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            jar = client.session.cookies.get_dict()
        except Exception:
            jar = {}
        cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
        cmd = [
            self.aria2c, "-x4", "-s4", "-k1M", "-c",
            "--auto-file-renaming=false", "--allow-overwrite=true",
            "--file-allocation=none", "--console-log-level=warn",
            "--summary-interval=0", "--user-agent", UA,
            "--header", "Referer: https://pan.quark.cn/",
        ]
        if cookie:
            cmd += ["--header", f"Cookie: {cookie}"]
        cmd += ["-d", os.path.dirname(dest), "-o", os.path.basename(dest), url]
        subprocess.run(cmd, check=True)
        return os.path.getsize(dest) if os.path.exists(dest) else 0

    @staticmethod
    def download_session(client) -> requests.Session:
        """Fresh session carrying the client's anonymous cookies + referer."""
        s = _new_session()
        s.headers.update(
            {
                "Referer": "https://pan.quark.cn/",
                "Origin": "https://pan.quark.cn",
                "Accept": "*/*",
            }
        )
        try:
            s.cookies.update(client.session.cookies)
        except Exception:
            pass
        if getattr(client, "cookie", ""):
            s.headers["Cookie"] = client.cookie
        return s

    # -- refreshable url --------------------------------------------------- #
    def download_file(self, client: "QuarkClient", record: dict) -> None:
        name = record["file_name"]
        rel = record["relative_path"]
        dest = os.path.join(self.output_dir, *[sanitize(p) for p in rel.split("/")])
        expected = record.get("expected_size") or 0
        part = dest + ".part"

        # Already complete?
        if os.path.exists(dest) and (not expected or os.path.getsize(dest) == expected):
            record["downloaded_size"] = os.path.getsize(dest)
            record["status"] = "complete"
            log.info("SKIP (complete): %s [%s]", rel, human(record["downloaded_size"]))
            return

        with _print_lock:
            log.info("START: %s [%s]", rel, human(expected) if expected else "?")

        attempts = 8
        last_err: Exception | None = None
        saved_fids: list[str] = []
        for attempt in range(attempts):
            try:
                # Fetch a fresh (unexpired) URL. On the guest size cap (23018)
                # fall back to the authenticated save->download flow if a
                # Quark cookie was supplied.
                try:
                    info = client.get_download_urls([record["remote_file_id"]]).get(
                        record["remote_file_id"]
                    )
                except QuarkSizeLimit as e:
                    if not getattr(client, "cookie", ""):
                        raise
                    log.info(
                        "  %s: guest limit hit (%s); using authenticated 转存 flow",
                        name, e,
                    )
                    info = client.save_and_get_download_url(
                        record["remote_file_id"], record.get("share_fid_token")
                    )
                    if info.get("_saved_fid"):
                        saved_fids = [info["_saved_fid"]]

                if not info or not info.get("download_url"):
                    raise RuntimeError(f"no download_url for {name}")
                url = info["download_url"]
                record["_url"] = url
                record["url_fetched_at"] = time.time()

                size = self._attempt_download(client, url, dest, expected, name)
                record["downloaded_size"] = size
                record["status"] = "complete"
                log.info("DONE : %s [%s]", rel, human(size))
                if saved_fids:
                    client.delete_saved(saved_fids)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait = min(2 ** attempt, 30)
                log.warning(
                    "  %s: attempt %d/%d failed (%s); retry in %ss",
                    name, attempt + 1, attempts, e, wait,
                )
                time.sleep(wait)
        raise RuntimeError(f"download failed for {name}: {last_err}")


# --------------------------------------------------------------------------- #
# Manifest helpers
# --------------------------------------------------------------------------- #
def build_manifest(provider: str, share_url: str, files: list[dict]) -> dict:
    return {
        "provider": provider,
        "share_url": share_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_files": len(files),
        "total_size": sum(f.get("expected_size") or 0 for f in files),
        "files": files,
    }


def save_manifest(manifest: dict) -> None:
    with _manifest_lock:
        tmp = MANIFEST_FILE + ".tmp"
        # strip internal-only keys
        clean = json.loads(json.dumps(manifest, default=str))
        for f in clean.get("files", []):
            f.pop("_url", None)
            f.pop("url_fetched_at", None)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, MANIFEST_FILE)


def save_failed(failed: list[dict]) -> None:
    with open(FAILED_FILE, "w", encoding="utf-8") as fh:
        json.dump(
            {"count": len(failed), "files": failed},
            fh, ensure_ascii=False, indent=2,
        )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def make_client(url: str, pwd: str, cookie: str = ""):
    p = detect_provider(url)
    if p == "quark":
        return QuarkClient(url, pwd, cookie)
    if p == "baidu":
        return BaiduClient(url, pwd)
    raise ValueError(f"Unsupported share URL: {url}")


def resolve_cookie(args) -> str:
    """Cookie from --cookie, --cookie-file, $QUARK_COOKIE, or ./cookie.txt."""
    if getattr(args, "cookie", ""):
        return args.cookie.strip()
    if getattr(args, "cookie_file", ""):
        try:
            return open(args.cookie_file, encoding="utf-8").read().strip()
        except OSError as e:
            log.warning("cannot read cookie file: %s", e)
    env = os.environ.get("QUARK_COOKIE", "").strip()
    if env:
        return env
    default = os.path.join(SCRIPT_DIR, "cookie.txt")
    if os.path.exists(default):
        try:
            return open(default, encoding="utf-8").read().strip()
        except OSError:
            pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Quark/Baidu public-share downloader")
    ap.add_argument("url", help="Public share URL")
    ap.add_argument("--output", default=os.path.join(SCRIPT_DIR, "downloads"))
    ap.add_argument("--pwd", default="", help="Extraction/passcode")
    ap.add_argument("--provider", choices=["auto", "quark", "baidu"], default="auto")
    ap.add_argument("--alt-url", action="append", default=[],
                    help="Alternate provider URL used only if primary fails")
    ap.add_argument("--enumerate-only", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="Download one small file then exit")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--aria2", action="store_true")
    ap.add_argument("--cookie", default="",
                    help="Logged-in Quark cookie (bypasses the ~50MB guest cap)")
    ap.add_argument("--cookie-file", default="",
                    help="File containing the Quark cookie")
    args = ap.parse_args()

    setup_logging()
    log.info("=" * 70)
    log.info("downloader start | url=%s | output=%s", args.url, args.output)
    cookie = resolve_cookie(args)
    if cookie:
        log.info("Quark cookie provided: yes (%d chars)", len(cookie))
    else:
        log.info("Quark cookie provided: no (anonymous only)")

    candidates = [args.url] + list(args.alt_url)
    if args.provider != "auto":
        candidates = [u for u in candidates if detect_provider(u) == args.provider]
        if not candidates:
            candidates = [args.url]

    client = None
    files: list[dict] = []
    chosen_url = None
    errors: list[str] = []

    for u in candidates:
        prov = detect_provider(u)
        log.info("Trying provider=%s url=%s", prov, u)
        try:
            c = make_client(u, args.pwd, cookie)
            fs = c.enumerate()
            if not fs:
                raise RuntimeError("no files discovered")
            client, files, chosen_url = c, fs, u
            log.info("Provider %s usable: %d files", prov, len(fs))
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Provider %s failed: %s", prov, e)
            errors.append(f"{prov}: {e}")

    if client is None:
        log.error("No provider could enumerate the share. Errors:")
        for e in errors:
            log.error("  - %s", e)
        return 2

    provider = client.provider
    total_size = sum(f.get("expected_size") or 0 for f in files)
    folders = len({os.path.dirname(f["relative_path"]) for f in files})
    log.info("-" * 70)
    log.info("PROVIDER      : %s", provider)
    log.info("TOTAL FOLDERS : %d", folders)
    log.info("TOTAL FILES   : %d", len(files))
    log.info("TOTAL SIZE    : %s (%d bytes)", human(total_size), total_size)
    log.info("-" * 70)

    for f in files:
        f["status"] = "pending"
        f["downloaded_size"] = 0
    manifest = build_manifest(provider, chosen_url, files)
    save_manifest(manifest)

    if args.enumerate_only:
        return 0

    # --- test download: smallest video (fallback: smallest file) ---------- #
    videos = [f for f in files if f["file_name"].lower().endswith((".mp4", ".mkv", ".avi", ".ts", ".mov"))]
    pool = videos or files
    smallest = sorted(pool, key=lambda f: f.get("expected_size") or 0)[0]
    log.info("TEST-DOWNLOAD: %s [%s]", smallest["relative_path"],
             human(smallest.get("expected_size")))
    mgr = DownloadManager(args.output, workers=args.workers, use_aria2=args.aria2)
    try:
        mgr.download_file(client, smallest)
        save_manifest(manifest)
        if smallest.get("status") != "complete":
            raise RuntimeError("test file not complete")
        log.info("TEST-DOWNLOAD OK")
    except Exception as e:  # noqa: BLE001
        log.error("TEST-DOWNLOAD FAILED: %s", e)
        save_manifest(manifest)
        return 3

    if args.test:
        log.info("--test requested; stopping after test download.")
        return 0

    # --- full download ---------------------------------------------------- #
    pending = [f for f in files if f.get("status") != "complete"]
    log.info("FULL DOWNLOAD: %d files remaining, %d workers",
             len(pending), mgr.workers)
    failed: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=mgr.workers) as ex:
        futures = {ex.submit(mgr.download_file, client, f): f for f in pending}
        for fut in as_completed(futures):
            rec = futures[fut]
            try:
                fut.result()
                done += 1
            except Exception as e:  # noqa: BLE001
                log.error("FAIL: %s -> %s", rec["relative_path"], e)
                rec["status"] = "failed"
                rec["error"] = str(e)
                failed.append(rec)
            save_manifest(manifest)
            save_failed(failed)
            log.info("Progress: %d/%d done, %d failed",
                     done, len(pending), len(failed))

    save_manifest(manifest)
    save_failed(failed)

    completed = [f for f in files if f.get("status") == "complete"]
    total_done = sum(f.get("downloaded_size") or 0 for f in completed)
    log.info("=" * 70)
    log.info("FINISHED provider=%s complete=%d failed=%d size=%s",
             provider, len(completed), len(failed), human(total_done))
    log.info("Output: %s", os.path.abspath(args.output))
    log.info("Resume: python downloader.py \"%s\" --output \"%s\"",
             args.url, args.output)
    log.info("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
