"""
Attach to an already-open Chrome: find the tab that matches a job/application URL and work on it.

Chrome only accepts automation connections when started with --remote-debugging-port, and since
Chrome 136 not on the default profile. `launch_debug_chrome` starts Chrome on a dedicated, persistent
profile (logins to job portals are remembered there); apply in that window, and attach any time.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

import requests

DEFAULT_CDP = "http://127.0.0.1:9222"
JOB_ID_KEYS = re.compile(r"(job|req|requisition|posting|vacancy|position)[_-]?(id|no|num|number)?$|^job$|^jobid$|career_job_req_id", re.I)


def default_profile_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return str(Path(base) / "JobApplicator" / "chrome-profile")


def find_chrome(configured: str = "") -> Optional[str]:
    candidates = [configured] if configured else []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates += [str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                           str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")]
    candidates += [shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("chrome") or "",
                   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    return next((c for c in candidates if c and Path(c).exists()), None)


def cdp_alive(cdp_url: str = DEFAULT_CDP) -> bool:
    try:
        return requests.get(f"{cdp_url.rstrip('/')}/json/version", timeout=2).ok
    except Exception:
        return False


def launch_debug_chrome(cdp_url: str = DEFAULT_CDP, profile_dir: str = "", chrome_path: str = "",
                        url: str = "", extra_args: Optional[List[str]] = None) -> bool:
    """Start Chrome with remote debugging on a dedicated profile (no-op if one is already listening)."""
    if cdp_alive(cdp_url):
        return True
    chrome = find_chrome(chrome_path)
    if not chrome:
        raise RuntimeError("Chrome/Edge not found - set browser.chrome_path in settings.yaml")
    port = urlparse(cdp_url).port or 9222
    profile = profile_dir or default_profile_dir()
    Path(profile).mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", *(extra_args or []), url or "about:blank"]
    flags = 0
    if os.name == "nt":  # keep Chrome running after this script exits
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    for _ in range(40):
        if cdp_alive(cdp_url):
            return True
        time.sleep(0.5)
    return False


def _parts(url: str) -> Tuple[str, str, dict]:
    u = urlparse(url if "://" in url else f"https://{url}")
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host, u.path.rstrip("/").lower(), {k.lower(): v for k, v in parse_qsl(u.query)}


def score_tab(target: str, tab_url: str, tab_title: str = "") -> int:
    """
    How well an open tab matches the URL (or keyword) the user supplied. Robust to the redirects
    portals do mid-application: same host, same job/requisition id, same company parameter, or the
    same long number (job id) anywhere in the tab's URL all count.
    """
    if not target or not tab_url or tab_url.startswith(("devtools://", "chrome://", "about:", "edge://")):
        return 0
    if "://" not in target and "." not in target:  # a keyword such as 'hsbc' or a job title
        hay = f"{tab_url} {tab_title}".lower()
        return 60 if target.lower() in hay else 0
    if tab_url.rstrip("/") == target.rstrip("/"):
        return 100
    t_host, t_path, t_q = _parts(target)
    h, p, q = _parts(tab_url)
    score = 0
    same_site = h == t_host or h.split(".", 1)[-1] == t_host.split(".", 1)[-1]
    if same_site:
        score += 40 if h == t_host else 25
        if p == t_path:
            score += 20
        elif t_path and (p.startswith(t_path) or t_path.startswith(p)) and len(p) > 1:
            score += 10
    for k, v in t_q.items():
        if q.get(k) == v:
            score += 30 if JOB_ID_KEYS.search(k) else (15 if k in ("company", "site", "tenant") else 3)
    ids = set(re.findall(r"\d{5,}", target))
    if ids and any(i in tab_url for i in ids):
        score += 30
    return score


async def list_tabs(browser: Any) -> List[Tuple[Any, str, str]]:
    tabs = []
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append((page, page.url, title))
    return tabs


async def pick_tab(browser: Any, target: str) -> Tuple[Optional[Any], List[Tuple[Any, str, str, int]]]:
    """Best matching tab (score >= 40) and the scored list of all tabs."""
    scored = [(p, u, t, score_tab(target, u, t)) for p, u, t in await list_tabs(browser)]
    scored.sort(key=lambda x: x[3], reverse=True)
    if scored and scored[0][3] >= 40:
        return scored[0][0], scored
    return None, scored
