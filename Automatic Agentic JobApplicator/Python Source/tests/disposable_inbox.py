"""
Disposable inbox (mail.tm) for regression runs: creates a throwaway address and reads
verification mails/links. Only used by tests - never with real candidate data.
"""

import re
import secrets
import string
import time
from typing import Dict, List, Optional

import requests

API = "https://api.mail.tm"


def strong_password() -> str:
    """Satisfies typical ATS policies: upper, lower, digit, special, length >= 12."""
    core = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    return f"Rg{core}!7q"


class DisposableInbox:
    def __init__(self, address: str = "", password: str = "", token: str = ""):
        self.address, self.password, self.token = address, password, token

    @classmethod
    def create(cls) -> "DisposableInbox":
        domain = requests.get(f"{API}/domains", timeout=20).json()["hydra:member"][0]["domain"]
        local = "atsreg" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        inbox = cls(f"{local}@{domain}", strong_password())
        r = requests.post(f"{API}/accounts", json={"address": inbox.address, "password": inbox.password}, timeout=20)
        r.raise_for_status()
        inbox.login()
        return inbox

    def login(self) -> None:
        r = requests.post(f"{API}/token", json={"address": self.address, "password": self.password}, timeout=20)
        r.raise_for_status()
        self.token = r.json()["token"]

    def _get(self, path: str) -> Dict:
        r = requests.get(f"{API}{path}", headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
        if r.status_code == 401:
            self.login()
            r = requests.get(f"{API}{path}", headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
        r.raise_for_status()
        return r.json()

    def messages(self) -> List[Dict]:
        return self._get("/messages").get("hydra:member", [])

    def wait_for_message(self, subject_pattern: str = "", timeout: int = 120, seen: Optional[set] = None,
                         since: str = "") -> Optional[Dict]:
        """Newest unseen message matching the subject (created at/after `since`, ISO UTC)."""
        seen = seen or set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.messages():
                if m["id"] in seen or (since and m.get("createdAt", "") < since):
                    continue
                if not subject_pattern or re.search(subject_pattern, m.get("subject", ""), re.I):
                    return self._get(f"/messages/{m['id']}")
            time.sleep(5)
        return None

    @staticmethod
    def links(message: Dict) -> List[str]:
        body = " ".join(message.get("html") or []) + " " + (message.get("text") or "")
        return list(dict.fromkeys(re.findall(r"https?://[^\s\"'<>]+", body.replace("&amp;", "&"))))

    @staticmethod
    def codes(message: Dict) -> List[str]:
        text = message.get("text") or ""
        digits = re.findall(r"\b\d{6}\b", text)
        if digits:
            return digits
        # e.g. 'Your one-time password: K7P2QX' - letters and digits after the word
        return re.findall(r"(?:passcode|password|code)\W{1,20}([A-Za-z0-9]{4,10})\b", text, re.I)
