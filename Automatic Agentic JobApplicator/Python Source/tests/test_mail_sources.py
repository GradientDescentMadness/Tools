"""
Tests for the mail sources: saved email files (DOCX/PDF/HTML/EML) with hyperlinks, the Recycle Bin
disposal, and the Outlook desktop (COM) source against a fake Outlook object model.
"""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mail_sources import OutlookDesktopSource, SavedEmailFolderSource, read_saved_email, send_to_recycle_bin  # noqa: E402
from src.email_reader import extract_linkedin_job_urls  # noqa: E402


def li(jid: str) -> str:
    return f"https://www.linkedin.com/comm/jobs/view/{jid}/?trackingId=abc&refId=xyz"


# ---------------------------------------------------------------- file builders (shared with the e2e test)
def make_docx(path: Path, links: dict) -> None:
    """A Word file whose job links are real hyperlinks hidden behind their titles."""
    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.shared import OxmlElement, qn
    d = docx.Document()
    d.add_paragraph("Jobs you may be interested in")
    for title, url in links.items():
        p = d.add_paragraph()
        h = OxmlElement("w:hyperlink")
        h.set(qn("r:id"), d.part.relate_to(url, RT.HYPERLINK, is_external=True))
        r, t = OxmlElement("w:r"), OxmlElement("w:t")
        t.text = title
        r.append(t)
        h.append(r)
        p._p.append(h)
    d.save(str(path))


def make_pdf(path: Path, links: dict) -> None:
    """A one-page PDF with clickable link annotations (as 'Print to PDF' of an email produces)."""
    lines = list(links)
    stream = ("BT /F1 12 Tf 72 740 Td " + " ".join(f"({t}) Tj 0 -24 Td" for t in lines) + " ET").encode()
    annots = []
    for i, url in enumerate(links.values()):
        y = 740 - 24 * i
        annots.append(f"<</Type/Annot/Subtype/Link/Rect[72 {y - 4} 400 {y + 12}]/Border[0 0 0]/A<</S/URI/URI({url})>>>>".encode())
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>/Annots["
            + b" ".join(b"%d 0 R" % (6 + i) for i in range(len(annots))) + b"]>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"] + annots
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % x for x in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


def make_eml(path: Path, links: dict) -> None:
    m = EmailMessage()
    m["Subject"] = "Your job alert: " + ", ".join(links)
    m["From"] = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
    m.set_content("View in a browser that supports HTML.")
    m.add_alternative("".join(f'<p><a href="{u}">{t}</a></p>' for t, u in links.items()), subtype="html")
    path.write_bytes(bytes(m))


def make_html(path: Path, links: dict) -> None:
    path.write_text("<html><body>" + "".join(f'<a href="{u.replace("&", "&amp;")}">{t}</a>' for t, u in links.items())
                    + "</body></html>", encoding="utf-8")


class SavedFileTests(unittest.TestCase):
    def test_hyperlinks_in_every_format(self):
        tmp = Path(tempfile.mkdtemp(prefix="aaj-saved-"))
        builders = {"alert.docx": make_docx, "alert.pdf": make_pdf, "alert.eml": make_eml, "alert.html": make_html}
        for name, build in builders.items():
            build(tmp / name, {"Head of Data": li("4200000001"), "VP Analytics": li("4200000002")})
            urls = extract_linkedin_job_urls(read_saved_email(tmp / name)["body"])
            self.assertEqual(urls, ["https://www.linkedin.com/jobs/view/4200000001/",
                                    "https://www.linkedin.com/jobs/view/4200000002/"], name)
        (tmp / "notes.xlsx").write_text("ignored")
        src = SavedEmailFolderSource(str(tmp))
        self.assertEqual(sorted(p.name for p in src.files()), sorted(builders))

    def test_recycle_bin(self):
        f = Path(tempfile.mkdtemp(prefix="aaj-bin-")) / "aaj-recycle-test.txt"
        f.write_text("throwaway test file")
        self.assertTrue(send_to_recycle_bin(f))
        self.assertFalse(f.exists())


# ---------------------------------------------------------------- fake Outlook object model
class FakeItem:
    def __init__(self, entry_id, subject, received, body, cls=43):
        self.EntryID, self.Subject, self.ReceivedTime, self.HTMLBody, self.Body = entry_id, subject, received, body, ""
        self.Class, self.SenderName, self.moved_to = cls, "LinkedIn", None

    def Move(self, folder):
        self.moved_to = folder.Name


class FakeItems(list):
    def Sort(self, key, descending):
        self.sort(key=lambda i: i.ReceivedTime, reverse=descending)

    @property
    def Count(self):
        return len(self)


class FakeFolder:
    def __init__(self, name, items=(), store="store-1"):
        self.Name, self.Items, self.StoreID, self.Folders = name, FakeItems(items), store, []


class FakeNamespace:
    def __init__(self, inbox, deleted):
        self.inbox, self.deleted = inbox, deleted

    def GetDefaultFolder(self, n):
        return {6: self.inbox, 3: self.deleted}[n]

    def GetItemFromID(self, entry_id, store_id):
        return next(i for f in self.inbox.Folders for i in f.Items if i.EntryID == entry_id)


class OutlookDesktopTests(unittest.TestCase):
    def test_folders_messages_and_move_to_deleted_items(self):
        newer = FakeItem("e2", "VP Analytics", datetime(2026, 10, 31, 9), f'<a href="{li("4200000002")}">VP</a>')
        older = FakeItem("e1", "Head of Data", datetime(2026, 10, 30, 9), f'<a href="{li("4200000001")}">Head</a>')
        meeting = FakeItem("e3", "Calendar invite", datetime(2026, 10, 29), "", cls=53)
        linkedin = FakeFolder("LinkedIn", [newer, older, meeting])
        inbox = FakeFolder("Inbox")
        inbox.Folders = [FakeFolder("Receipts"), linkedin]
        ns = FakeNamespace(inbox, FakeFolder("Deleted Items"))
        hints = []

        async def run():
            src = OutlookDesktopSource(notify=hints.append, wait_hint_after=0.2)

            def slow_open():  # e.g. classic Outlook showing a sign-in window
                import time
                time.sleep(0.6)
                src._ns, src._inbox = ns, inbox
            src._open = slow_open
            await src.open()
            folders = await src.inbox_folders()
            self.assertTrue(await src.use_folder("linkedin"))
            msgs = await src.messages()
            moved = await src.dispose(msgs[0])
            await src.close()
            return src, folders, msgs, moved

        src, folders, msgs, moved = asyncio.run(run())
        self.assertEqual(folders, [{"name": "Receipts", "total": 0}, {"name": "LinkedIn", "total": 3}])
        self.assertEqual(src.label, "Outlook: Inbox/LinkedIn")
        self.assertEqual([m["subject"] for m in msgs], ["Head of Data", "VP Analytics"], "oldest first, mail items only")
        self.assertEqual(src.links(msgs[0]), ["https://www.linkedin.com/jobs/view/4200000001/"])
        self.assertTrue(moved)
        self.assertEqual(older.moved_to, "Deleted Items")
        self.assertIsNone(newer.moved_to)
        self.assertTrue(hints and "Outlook is waiting for you" in hints[0])


if __name__ == "__main__":
    unittest.main()
