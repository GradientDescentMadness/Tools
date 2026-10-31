"""
Outlook and Microsoft Graph API email ingestion module using MSAL Device Code Flow.
"""

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
import msal
import requests


def extract_linkedin_job_urls(html_text: str) -> List[str]:
    """Parse HTML body to extract cleaned LinkedIn job URLs."""
    raw_urls = re.findall(r"https?://[^\s\"'<>]+", html_text)
    clean_urls = []
    seen = set()

    for url in raw_urls:
        if "linkedin.com/jobs/view/" in url or "linkedin.com/comm/jobs/view/" in url:
            # Strip tracking tokens
            parsed = urlparse(url)
            clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if clean not in seen:
                seen.add(clean)
                clean_urls.append(clean)

    return clean_urls


class OutlookEmailReader:
    """Reads Outlook notification emails to uncover LinkedIn job opportunities."""

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    SCOPES = ["Mail.Read"]

    def __init__(self, client_id: str, tenant_id: str = "common"):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.access_token: Optional[str] = None
        self.app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
        )

    def authenticate(self) -> None:
        """Authenticate interactively via MSAL Device Code flow."""
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(self.SCOPES, account=accounts[0])
            if result and "access_token" in result:
                self.access_token = result["access_token"]
                return

        flow = self.app.initiate_device_flow(scopes=self.SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to initiate device flow: {flow.get('error_description')}")

        print("\n" + "=" * 60)
        print(" [Microsoft Outlook Sign-In]")
        print(f" 1. Open URL : {flow['verification_uri']}")
        print(f" 2. Enter code: {flow['user_code']}")
        print("=" * 60 + "\n")

        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            self.access_token = result["access_token"]
        else:
            raise RuntimeError(f"Auth failed: {result.get('error_description')}")

    def _get_headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise ValueError("Not authenticated. Call authenticate() first.")
        return {"Authorization": f"Bearer {self.access_token}"}

    def fetch_linkedin_emails(
        self,
        lookback_days: int = 7,
        sender_patterns: Optional[List[str]] = None,
        subject_keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Query Microsoft Graph API for recent LinkedIn notification messages."""
        if sender_patterns is None:
            sender_patterns = ["linkedin.com", "LinkedIn"]
        if subject_keywords is None:
            subject_keywords = ["job", "opportunity", "hiring", "role", "apply"]

        start_time = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            f"{self.GRAPH_BASE}/me/messages?"
            f"$filter=receivedDateTime ge {start_time}&"
            f"$select=subject,from,receivedDateTime,body&$top=50"
        )

        matching_emails = []
        while url:
            resp = requests.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                break

            data = resp.json()
            for msg in data.get("value", []):
                sender_name = msg.get("from", {}).get("emailAddress", {}).get("name", "")
                sender_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "")
                subject = msg.get("subject", "")

                is_sender = any(p.lower() in sender_name.lower() or p.lower() in sender_addr.lower() for p in sender_patterns)
                is_subject = any(k.lower() in subject.lower() for k in subject_keywords)

                if is_sender and is_subject:
                    matching_emails.append({
                        "subject": subject,
                        "from": f"{sender_name} <{sender_addr}>",
                        "date": msg.get("receivedDateTime"),
                        "body_html": msg.get("body", {}).get("content", ""),
                    })

            url = data.get("@odata.nextLink")

        return matching_emails

    def get_job_links_from_emails(
        self,
        lookback_days: int = 7,
        sender_patterns: Optional[List[str]] = None,
        subject_keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        """Fetch emails and extract deduplicated job links."""
        emails = self.fetch_linkedin_emails(lookback_days, sender_patterns, subject_keywords)
        results = []
        seen_links = set()

        for mail in emails:
            extracted = extract_linkedin_job_urls(mail.get("body_html", ""))
            for link in extracted:
                if link not in seen_links:
                    seen_links.add(link)
                    results.append({
                        "subject": mail.get("subject", "LinkedIn Job Match"),
                        "link": link,
                        "from": mail.get("from", "LinkedIn"),
                        "date": mail.get("date", ""),
                    })

        return results

