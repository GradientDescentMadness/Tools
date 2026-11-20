"""
Outlook (Microsoft Graph) access for LinkedIn job-alert mails: device-code sign-in with a remembered
token, folder selection under Inbox, reading every message in a folder, and moving processed mail
to Deleted Items.

Requires an Azure app registration (public client flows enabled) with the delegated permission
Mail.ReadWrite (read + move). Personal Outlook.com accounts work with tenant 'consumers' or 'common'.
"""

import os
import re
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite"]
LINKEDIN_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[\w%-]*?-)?(\d{6,})", re.I)


def token_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "JobApplicator" / "msal_token_cache.json"


def _unwrap(url: str) -> str:
    """Undo Outlook Safe Links / redirect wrappers so the real LinkedIn URL can be read."""
    url = unescape(url)
    host = urlparse(url).hostname or ""
    if "safelinks.protection.outlook.com" in host:
        inner = parse_qs(urlparse(url).query).get("url", [""])[0]
        return unquote(inner) or url
    return url


def extract_linkedin_job_urls(html_text: str) -> List[str]:
    """Canonical LinkedIn job URLs (one per job id) found in an email body."""
    seen, out = set(), []
    for raw in re.findall(r"https?://[^\s\"'<>]+", html_text or ""):
        m = LINKEDIN_JOB.search(_unwrap(raw))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(f"https://www.linkedin.com/jobs/view/{m.group(1)}/")
    return out


def job_id_from_url(url: str) -> str:
    m = LINKEDIN_JOB.search(url or "")
    return f"linkedin:{m.group(1)}" if m else url


class OutlookEmailReader:
    """Reads LinkedIn job alerts from an Outlook folder and tidies them away afterwards."""

    def __init__(self, client_id: str, tenant_id: str = "common", http: Any = None):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.access_token: Optional[str] = None
        self.http = http or requests.Session()
        self.cache = msal.SerializableTokenCache()
        self.cache_file = token_cache_path()
        if self.cache_file.exists():
            self.cache.deserialize(self.cache_file.read_text(encoding="utf-8"))
        self.app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant_id}",
                                                token_cache=self.cache)

    # ------------------------------------------------------------------ auth
    def authenticate(self) -> None:
        """Silent sign-in from the remembered token, else the device-code flow (once)."""
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        if not result or "access_token" not in result:
            flow = self.app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(f"Failed to initiate device flow: {flow.get('error_description')}")
            print("\n" + "=" * 60)
            print(" [Microsoft Outlook Sign-In]")
            print(f" 1. Open URL : {flow['verification_uri']}")
            print(f" 2. Enter code: {flow['user_code']}")
            print("=" * 60 + "\n")
            result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Auth failed: {result.get('error_description')}")
        self.access_token = result["access_token"]
        if self.cache.has_state_changed:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(self.cache.serialize(), encoding="utf-8")

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise ValueError("Not authenticated. Call authenticate() first.")
        return {"Authorization": f"Bearer {self.access_token}", "Prefer": 'outlook.body-content-type="html"'}

    def _get(self, url: str) -> Dict[str, Any]:
        r = self.http.get(url if url.startswith("http") else f"{GRAPH}{url}", headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Outlook request failed ({r.status_code}): {r.text[:200]}")
        return r.json()

    # ------------------------------------------------------------------ folders and messages
    def inbox_folders(self) -> List[Dict[str, Any]]:
        """Folders directly under Inbox: [{'id', 'name', 'total'}]."""
        out, url = [], "/me/mailFolders/inbox/childFolders?$top=100&$select=id,displayName,totalItemCount"
        while url:
            data = self._get(url)
            out += [{"id": f["id"], "name": f["displayName"], "total": f.get("totalItemCount", 0)} for f in data.get("value", [])]
            url = data.get("@odata.nextLink")
        return out

    def messages(self, folder_id: str) -> Iterator[Dict[str, Any]]:
        """Every message in the folder, oldest first."""
        url = (f"/me/mailFolders/{folder_id}/messages?$top=25&$orderby=receivedDateTime asc"
               "&$select=id,subject,from,receivedDateTime,body")
        while url:
            data = self._get(url)
            for m in data.get("value", []):
                yield {"id": m["id"], "subject": m.get("subject", ""), "received": m.get("receivedDateTime", ""),
                       "from": (m.get("from", {}) or {}).get("emailAddress", {}).get("address", ""),
                       "body_html": (m.get("body", {}) or {}).get("content", "")}
            url = data.get("@odata.nextLink")

    def move_to_trash(self, message_id: str) -> bool:
        r = self.http.post(f"{GRAPH}/me/messages/{message_id}/move", headers=self._headers(),
                           json={"destinationId": "deleteditems"}, timeout=30)
        return r.status_code in (200, 201)
