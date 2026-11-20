"""
Live regression run against real ATS portals with the GARBAGE profile in tests/regression/.

- Uses a disposable mail.tm inbox for portal accounts (saved in tests/regression/state.json so a
  re-run exercises the 'already have an account -> sign in' path).
- Runs the real app in --auto mode: no prompts, never clicks a final Submit button.
- Writes a JSON report + screenshots to tests/regression/out/<site>/.

Usage (from 'Python Source'):
    python tests/regression_live.py                 # all sites in tests/regression/sites.yaml
    python tests/regression_live.py workday_citi    # one site
    python tests/regression_live.py --headed ...    # watch the browser
"""

import asyncio
import copy
import json
import re
from datetime import datetime, timedelta, timezone
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from disposable_inbox import DisposableInbox  # noqa: E402
from src.main import JobAutoApply  # noqa: E402

REG = ROOT / "tests" / "regression"
STATE = REG / "state.json"  # default identity; sites may name their own (portals rate-limit per email)


def state_file(identity: str = "") -> Path:
    return STATE if not identity else REG / f"state_{identity}.json"

def minimal_pdf(lines) -> bytes:
    """A small valid one-page PDF (correct xref offsets) for resume upload fields."""
    text = " ".join(f"({l}) Tj 0 -22 Td" for l in lines)
    stream = f"BT /F1 14 Tf 72 740 Td {text} ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


MINIMAL_PDF = minimal_pdf(["Zorblat Quixley - REGRESSION TEST RESUME", "Garbage data. Not a real candidate.",
                           "Experience: Acme Widgets, Globex, Initech", "Education: MS Analytics, MBA, B.Tech"])


def load_state(identity: str = "") -> dict:
    f = state_file(identity)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def prepare_profile(identity: str = "") -> tuple:
    """Disposable inbox + runtime settings/resume files for the garbage persona."""
    state = load_state(identity)
    if not state.get("address"):
        inbox = DisposableInbox.create()
        # Portals deduplicate accounts by phone too, so every identity gets its own number.
        phone = "+91-9" + "".join(secrets.choice("0123456789") for _ in range(9))
        state = {"address": inbox.address, "password": inbox.password, "portal_password": inbox.password, "phone": phone}
        state_file(identity).write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(f"[regression] Disposable identity ({identity or 'default'}): {state['address']}")

    resume = yaml.safe_load((REG / "resume_data.yaml").read_text(encoding="utf-8"))
    resume["personal"]["email"] = state["address"]
    resume["personal"]["phone"] = state.get("phone", resume["personal"]["phone"])
    (REG / "dummy_resume.pdf").write_bytes(MINIMAL_PDF)
    resume.setdefault("additional", {})["resume_file"] = str(REG / "dummy_resume.pdf")
    resume_rt = REG / f"resume_runtime{'_' + identity if identity else ''}.yaml"
    resume_rt.write_text(yaml.safe_dump(resume, sort_keys=False, allow_unicode=True), encoding="utf-8")

    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    settings = copy.deepcopy(settings)
    p = resume["personal"]
    settings["auth"] = {
        "auto_login": True, "auto_create_account": True, "email": state["address"],
        "password": state["portal_password"], "first_name": p["first_name"], "last_name": p["last_name"],
        "phone_country_code": "India (+91)", "phone": p["phone"], "country": p["address"]["country"],
        "salutation": p["salutation"], "pronouns": p["pronouns"],
    }
    settings_rt = REG / f"settings_runtime{'_' + identity if identity else ''}.yaml"
    settings_rt.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    return str(resume_rt), str(settings_rt)


async def run_site(name: str, url: str, resume: str, settings: str, headed: bool, identity: str = "") -> dict:
    out = REG / "out" / name
    out.mkdir(parents=True, exist_ok=True)
    s = yaml.safe_load(Path(settings).read_text(encoding="utf-8"))
    s.setdefault("application", {})["screenshot_dir"] = str(out)
    site_settings = REG / f"settings_{name}.yaml"
    site_settings.write_text(yaml.safe_dump(s, sort_keys=False), encoding="utf-8")

    async def next_message(subject: str):
        since = (datetime.now(timezone.utc) - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S")
        state = load_state(identity)
        inbox = DisposableInbox(state["address"], state["password"])
        inbox.login()
        msg = await asyncio.to_thread(inbox.wait_for_message, subject, 75, set(state.get("seen_messages", [])), since)
        if msg:
            state.setdefault("seen_messages", []).append(msg["id"])
            state_file(identity).write_text(json.dumps(state, indent=1), encoding="utf-8")
        return msg

    async def verification_link():
        """Newest verification/activation link from the disposable inbox."""
        msg = await next_message(r"verif|activat|confirm|welcome|account")
        links = [l for l in DisposableInbox.links(msg) if re.search(r"verif|activat|confirm|token", l, re.I)] if msg else []
        return links[0] if links else None

    async def verification_code():
        """Newest one-time code (e.g. Oracle's PIN) from the disposable inbox."""
        msg = await next_message(r"code|pin|verif|confirm|one.time|password")
        codes = DisposableInbox.codes(msg) if msg else []
        return codes[0] if codes else None

    app = JobAutoApply(config_dir=str(ROOT / "config"), direct_url=url, resume_path=resume,
                       settings_path=str(site_settings), auto=True, headless=not headed,
                       verification_link_provider=verification_link, verification_code_provider=verification_code)
    reports = await app.run()
    report = reports[0] if reports else {"status": "no-report"}
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return report


def summarize(name: str, rep: dict) -> None:
    steps = rep.get("steps", [])
    filled = sum(s.get("filled_count", 0) for s in steps)
    failed = sum(s.get("failed_count", 0) for s in steps)
    print(f"\n[regression] {name}: status={rep.get('status')} auth={rep.get('auth')} steps={len(steps)} filled={filled} failed={failed}")
    for s in steps:
        print(f"   - {s.get('section', '')[:40]:40} filled={s.get('filled_count')} failed={s.get('failed_count')} "
              f"skipped={s.get('skipped_count')} blocked_by={s.get('blocked_by', [])[:2]}")
        for d in s.get("details", []):
            if d["status"] in ("failed", "unanswered-required"):
                print(f"       ! {d['label'][:60]} -> {d['status']} {d.get('reason', '')[:80]}")


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    headed = "--headed" in sys.argv
    sites = yaml.safe_load((REG / "sites.yaml").read_text(encoding="utf-8"))["sites"]
    if args:
        sites = [s for s in sites if s["name"] in args]
    for site in sites:
        print(f"\n================ {site['name']} ({site['ats']}) ================")
        identity = site.get("identity", "")
        resume, settings = prepare_profile(identity)
        try:
            rep = await run_site(site["name"], site["url"], resume, settings, headed, identity)
        except Exception as e:  # keep going with other sites
            rep = {"status": f"harness-error: {e}"}
        summarize(site["name"], rep)


if __name__ == "__main__":
    asyncio.run(main())
