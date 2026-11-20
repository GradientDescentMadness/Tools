"""
Where job-alert mails come from. Every source offers the same three things: the messages, the job
links in a message, and disposing of a processed message (Deleted Items / Recycle Bin).

- OutlookDesktopSource  : the Outlook desktop app on this PC (classic Outlook, via COM). No Azure setup.
                          The 'new Outlook for Windows' app has no automation interface, so the classic
                          Outlook profile that holds the same mailbox is used.
- SavedEmailFolderSource: a folder of emails saved as files - .docx, .pdf, .html/.htm/.mht, .eml, .msg, .txt.
"""

import asyncio
import email
import os
import re
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email import policy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .email_reader import extract_linkedin_job_urls

SAVED_EMAIL_TYPES = (".docx", ".pdf", ".html", ".htm", ".mht", ".mhtml", ".eml", ".msg", ".txt")
OL_FOLDER_INBOX, OL_FOLDER_DELETED, OL_MAIL = 6, 3, 43


class OutlookDesktopSource:
    """
    Classic Outlook through COM. All COM calls run on one dedicated thread (COM objects belong to the
    thread that created them), so the browser keeps working and a waiting Outlook can't freeze the tool.
    """

    trash_name = "Deleted Items"

    def __init__(self, notify: Callable[[str], None] = print, wait_hint_after: float = 25.0):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="outlook-com")
        self.notify = notify
        self.wait_hint_after = wait_hint_after
        self.folder_name = ""
        self.label = "Outlook"
        self._app = self._ns = self._inbox = self._folder = None

    async def _call(self, fn: Callable, *args: Any) -> Any:
        fut = asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), self.wait_hint_after)
        except asyncio.TimeoutError:
            self.notify("Outlook is waiting for you - if it opened a sign-in or 'Accounts' window, finish it there; "
                        "this continues automatically.")
            return await fut

    # --- runs on the COM thread
    def _open(self) -> None:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            self._app = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            self._app = win32com.client.Dispatch("Outlook.Application")
        self._ns = self._app.GetNamespace("MAPI")
        self._inbox = self._ns.GetDefaultFolder(OL_FOLDER_INBOX)

    def _folders(self) -> List[Dict[str, Any]]:
        return [{"name": f.Name, "total": f.Items.Count} for f in self._inbox.Folders]

    def _use(self, name: str) -> bool:
        for f in self._inbox.Folders:
            if f.Name.lower() == name.lower():
                self._folder, self.folder_name = f, f.Name
                self.label = f"Outlook: Inbox/{f.Name}"
                return True
        return False

    def _messages(self) -> List[Dict[str, Any]]:
        items = self._folder.Items
        items.Sort("[ReceivedTime]", False)
        out = []
        for item in list(items):  # snapshot first: moving items while iterating skips some
            if getattr(item, "Class", None) != OL_MAIL:
                continue
            received = item.ReceivedTime
            out.append({"id": item.EntryID, "store_id": self._folder.StoreID, "subject": item.Subject or "",
                        "received": received.isoformat() if hasattr(received, "isoformat") else str(received),
                        "from": getattr(item, "SenderName", ""), "body": item.HTMLBody or item.Body or ""})
        return out

    def _dispose(self, entry_id: str, store_id: str) -> bool:
        item = self._ns.GetItemFromID(entry_id, store_id)
        item.Move(self._ns.GetDefaultFolder(OL_FOLDER_DELETED))
        return True

    # --- public, awaitable
    async def open(self) -> None:
        await self._call(self._open)

    async def inbox_folders(self) -> List[Dict[str, Any]]:
        return await self._call(self._folders)

    async def use_folder(self, name: str) -> bool:
        return await self._call(self._use, name)

    async def messages(self) -> List[Dict[str, Any]]:
        return await self._call(self._messages)

    def links(self, msg: Dict[str, Any]) -> List[str]:
        return extract_linkedin_job_urls(msg.get("body", ""))

    async def dispose(self, msg: Dict[str, Any]) -> bool:
        try:
            return await self._call(self._dispose, msg["id"], msg["store_id"])
        except Exception:
            return False

    async def close(self) -> None:
        self.pool.shutdown(wait=False)


# ---------------------------------------------------------------------------- saved files

def _docx_text(path: Path) -> str:
    """Hyperlink targets (relationships) plus the document text/field codes of a .docx."""
    parts = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.startswith("word/") and (name.endswith(".xml") or name.endswith(".rels")):
                parts.append(z.read(name).decode("utf-8", "ignore"))
    return "\n".join(re.sub(r"&amp;", "&", p) for p in parts)


def _pdf_text(path: Path) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts += [h.get("uri", "") for h in (page.hyperlinks or [])]
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _eml_text(path: Path) -> tuple:
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    bodies = []
    for part in msg.walk():
        if part.get_content_type() in ("text/html", "text/plain"):
            try:
                bodies.append(part.get_content())
            except Exception:
                pass
    return msg.get("subject", ""), "\n".join(bodies)


def _msg_text(path: Path) -> tuple:
    """Outlook .msg (OLE): subject, HTML body and plain body streams."""
    import olefile
    with olefile.OleFileIO(str(path)) as ole:
        def stream(name: str, encoding: str) -> str:
            return ole.openstream(name).read().decode(encoding, "ignore") if ole.exists(name) else ""
        subject = stream("__substg1.0_0037001F", "utf-16-le")
        html = stream("__substg1.0_10130102", "utf-8")
        body = stream("__substg1.0_1000001F", "utf-16-le")
    return subject, html + "\n" + body


def read_saved_email(path: Path) -> Dict[str, Any]:
    """Subject and text (including hyperlink targets) of a saved email file."""
    suffix = path.suffix.lower()
    subject = path.stem
    if suffix == ".docx":
        text = _docx_text(path)
    elif suffix == ".pdf":
        text = _pdf_text(path)
    elif suffix == ".eml":
        subject, text = _eml_text(path)
    elif suffix == ".msg":
        subject, text = _msg_text(path)
    else:
        text = path.read_bytes().decode("utf-8", "ignore")
    return {"subject": subject or path.stem, "body": text}


def send_to_recycle_bin(path: Path) -> bool:
    """Delete a file to the Windows Recycle Bin (recoverable). Elsewhere: move it into 'processed/'."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path.resolve()) + "\0", None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
        return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not path.exists()
    target = path.parent / "processed"
    target.mkdir(exist_ok=True)
    shutil.move(str(path), str(target / path.name))
    return True


class SavedEmailFolderSource:
    """Job-alert emails saved as files in a folder (oldest first). Processed files go to the Recycle Bin."""

    trash_name = "the Recycle Bin"

    def __init__(self, folder: str):
        self.folder = Path(folder).expanduser()
        self.label = f"Folder: {self.folder}"

    def files(self) -> List[Path]:
        return sorted((p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() in SAVED_EMAIL_TYPES),
                      key=lambda p: p.stat().st_mtime)

    async def messages(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.files():
            try:
                data = read_saved_email(p)
            except Exception as e:
                data = {"subject": p.name, "body": "", "error": str(e)}
            out.append({"id": str(p), "path": p, "subject": data["subject"], "body": data["body"],
                        "received": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                        "from": p.name, "error": data.get("error")})
        return out

    def links(self, msg: Dict[str, Any]) -> List[str]:
        return extract_linkedin_job_urls(msg.get("body", ""))

    async def dispose(self, msg: Dict[str, Any]) -> bool:
        try:
            return send_to_recycle_bin(Path(msg["path"]))
        except Exception:
            return False

    async def close(self) -> None:
        pass


class GraphMailSource:
    """Optional: Outlook on the web through Microsoft Graph (needs an Azure app id; not required otherwise)."""

    trash_name = "Deleted Items"

    def __init__(self, reader: Any):
        self.reader = reader
        self.folder_id: Optional[str] = None
        self.label = "Outlook on the web"

    async def open(self) -> None:
        await asyncio.to_thread(self.reader.authenticate)

    async def inbox_folders(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.reader.inbox_folders)

    async def use_folder(self, name: str) -> bool:
        for f in await self.inbox_folders():
            if f["name"].lower() == name.lower():
                self.folder_id, self.label = f["id"], f"Outlook on the web: Inbox/{f['name']}"
                return True
        return False

    async def messages(self) -> List[Dict[str, Any]]:
        msgs = await asyncio.to_thread(lambda: list(self.reader.messages(self.folder_id)))
        return [{**m, "body": m.get("body_html", "")} for m in msgs]

    def links(self, msg: Dict[str, Any]) -> List[str]:
        return extract_linkedin_job_urls(msg.get("body", ""))

    async def dispose(self, msg: Dict[str, Any]) -> bool:
        return await asyncio.to_thread(self.reader.move_to_trash, msg["id"])

    async def close(self) -> None:
        pass
