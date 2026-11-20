"""
Main workflow coordinator: orchestrates Outlook email discovery, LinkedIn parsing,
user confirmations, ATS redirection, and intelligent section-by-section autofill.
"""

import asyncio
import json
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from rich.markup import escape

from . import utils
from .email_reader import OutlookEmailReader, job_id_from_url
from .form_filler import FormFiller
from .jd_screener import linkedin_fit, read_job_page, required_years
from .job_tracker import JobTracker, now
from .linkedin_scanner import LinkedInScanner
from .mail_sources import GraphMailSource, OutlookDesktopSource, SavedEmailFolderSource
from .question_answerer import LocationAwareAnswerer
from .site_handlers.base_handler import detect_ats, get_handler
from .skill_matcher import SkillMatcher
from .utils import (
    ask_user,
    confirm,
    console,
    load_all_configs,
    print_banner,
    print_job_info,
    setup_screenshot_dir,
    take_screenshot,
    wait_for_proceed,
)

ATS_KEYS = {"workday", "successfactors", "oracle", "taleo", "greenhouse", "lever", "icims", "smartrecruiters"}


class JobAutoApply:
    """Orchestrator for the semi-autonomous job application lifecycle."""

    def __init__(
        self,
        config_dir: str = "config",
        manual_urls: bool = False,
        headless: Optional[bool] = None,
        screenshots: bool = True,
        direct_url: Optional[str] = None,
        resume_path: str = "",
        settings_path: str = "",
        auto: bool = False,
        max_steps: int = 20,
        verification_link_provider: Any = None,
        verification_code_provider: Any = None,
    ):
        self.config_dir = config_dir
        self.manual_urls = manual_urls
        self.headless = headless
        self.enable_screenshots = screenshots
        self.direct_url = direct_url
        self.resume_path = resume_path
        self.settings_path = settings_path
        self.auto = auto
        self.max_steps = max_steps
        self.verification_link_provider = verification_link_provider  # async () -> link (tests: disposable inbox)
        self.verification_code_provider = verification_code_provider  # async () -> one-time code

        self.settings: Dict[str, Any] = {}
        self.resume_data: Dict[str, Any] = {}
        self.qa_rules: Dict[str, Any] = {}

        self.playwright: Any = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.skill_matcher: Optional[SkillMatcher] = None
        self.job_links: List[Dict[str, str]] = []
        self.reports: List[Dict[str, Any]] = []

    async def initialize(self) -> None:
        """Load YAML configuration files and initialize matcher."""
        console.print("\n[bold cyan]⚙️  Loading configuration profiles...[/bold cyan]")
        self.settings, self.resume_data, self.qa_rules = load_all_configs(
            self.config_dir, self.settings_path, self.resume_path)
        utils.set_auto_mode(self.auto)

        skills_cfg = self.resume_data.get("skills", {}) or {}
        all_skills = list(skills_cfg.get("technical", []) or []) + list(skills_cfg.get("soft", []) or [])
        self.skill_matcher = SkillMatcher(all_skills)
        console.print(f"  [green]✓[/green] Loaded profile: [bold]{self.resume_data.get('personal', {}).get('full_name')}[/bold]")
        console.print(f"  [green]✓[/green] Indexed [bold]{len(all_skills)}[/bold] skills for fuzzy matching")

        app_cfg = self.settings.get("application", {}) or {}
        if self.enable_screenshots and app_cfg.get("screenshots", True):
            setup_screenshot_dir(app_cfg.get("screenshot_dir", "./screenshots"))
        else:
            self.enable_screenshots = False

    async def start_browser(self) -> None:
        """Launch Playwright browser context."""
        console.print("\n[bold cyan]🌐 Starting automation browser...[/bold cyan]")
        self.playwright = await async_playwright().start()

        browser_cfg = self.settings.get("browser", {}) or {}
        headless = self.headless if self.headless is not None else bool(browser_cfg.get("headless", False))
        slow_mo = browser_cfg.get("slow_mo", 150)
        user_data_dir = browser_cfg.get("user_data_dir", "")

        if user_data_dir:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir, headless=headless, slow_mo=slow_mo, viewport={"width": 1280, "height": 900})
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        else:
            self.browser = await self.playwright.chromium.launch(headless=headless, slow_mo=slow_mo)
            self.context = await self.browser.new_context(viewport={"width": 1280, "height": 900})
            self.page = await self.context.new_page()

        self.context.set_default_timeout(browser_cfg.get("timeout", 30000))
        self.context.on("page", lambda p: p.on("dialog", lambda d: asyncio.ensure_future(d.dismiss())))
        self.page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        console.print("  [green]✓[/green] Browser initialized")

    async def fetch_job_links(self) -> List[Dict[str, str]]:
        """Job links for a direct run (--url / --urls). Outlook mail is handled by run_email_pipeline."""
        if self.direct_url:
            return [{"link": self.direct_url, "subject": "Direct Target Opportunity"}]
        return self._prompt_manual_urls()

    def _prompt_manual_urls(self) -> List[Dict[str, str]]:
        if self.auto:
            return []
        console.print("[bold]Paste LinkedIn or ATS Job URLs (press Enter with empty line to finish):[/bold]")
        items = []
        while True:
            u = ask_user("Job URL").strip()
            if not u:
                break
            items.append({"link": u, "subject": f"Manual Opportunity #{len(items) + 1}"})
        return items

    def _ask_job_country(self, message: str) -> Optional[str]:
        """Asked once per job when the job's country can't be determined (work-authorization answers)."""
        console.print(f"\n[yellow]🌍 {message}[/yellow]")
        reply = ask_user("Which country is this job in? (blank = leave authorization questions for you)", default="")
        return reply or None

    async def _close_extra_tabs(self) -> None:
        """Keep a single tab between jobs (in an attached Chrome: open a fresh tab, never close yours)."""
        if getattr(self, "attached", False):
            self.page = await self.context.new_page()
            return
        pages = self.context.pages
        for p in pages[1:]:
            try:
                await p.close()
            except Exception:
                pass
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def _shot(self, name: str) -> None:
        if self.enable_screenshots:
            await take_screenshot(self.page, name)

    async def process_job(self, job: Dict[str, str], skip_confirm: bool = False) -> Dict[str, Any]:
        """Navigate to job, confirm with user, click Apply, and navigate ATS forms."""
        url = job["link"]
        report: Dict[str, Any] = {"url": url, "status": "started", "steps": [], "auth": None}
        await self._close_extra_tabs()
        scanner = LinkedInScanner(self.page)

        console.print(f"\n[bold blue]Navigating to:[/bold blue] {url}")
        job_info = await scanner.navigate_to_job(url)
        report["job"] = job_info
        print_job_info(title=job_info.get("title", "Job Role"), company=job_info.get("company", "Company"),
                       location=job_info.get("location") or "Unknown", url=url)
        await self._shot("job_page")

        if not skip_confirm and not confirm("Apply to this job?"):
            console.print("  ⏭️  Skipping application upon user request.\n")
            report["status"] = "skipped"
            return report

        pre_ats = await detect_ats(self.page)
        await get_handler(self.page, None, self.resume_data, self.settings, ats=pre_ats).prepare()

        console.print("  🖱️  Starting application (Apply)...")
        apply_result = await scanner.click_apply()
        report["apply_result"] = apply_result
        if scanner.active_page:
            self.page = scanner.active_page
            await self.page.bring_to_front()

        qa_engine = LocationAwareAnswerer(self.qa_rules, self.resume_data, job_info.get("location", ""),
                                          f"{url} {self.page.url}", ask=self._ask_job_country)
        auto_answer = (self.settings.get("application", {}) or {}).get("auto_answer_questions", True)

        if apply_result == "easy_apply":
            form_filler = FormFiller(self.page, self.resume_data, self.skill_matcher, qa_engine if auto_answer else None, self.settings)
            return await self._run_easy_apply(form_filler, report)

        if apply_result == "already_on_form":
            console.print("  📝 This link opens the application form itself - filling it directly.")
        elif apply_result in ("not_found", "menu_unresolved"):
            on_ats_form = pre_ats in ATS_KEYS and (await scanner.is_login_required() or "apply" in self.page.url.lower())
            if not on_ats_form:
                console.print(f"  [yellow]Couldn't start the application automatically ({apply_result}).[/yellow]")
                if self.auto:
                    report["status"] = f"failed:{apply_result}"
                    return report
                wait_for_proceed("Click Apply in the browser yourself, then press Enter here...")
                if len(self.context.pages) > 1:
                    self.page = self.context.pages[-1]

        site_type = await detect_ats(self.page)
        report["ats"] = site_type
        console.print(f"  🏢 Detected ATS platform: [bold cyan]{site_type.upper()}[/bold cyan]")

        form_filler = FormFiller(self.page, self.resume_data, self.skill_matcher, qa_engine if auto_answer else None, self.settings)
        handler = get_handler(self.page, form_filler, self.resume_data, self.settings, ats=site_type)
        handler.job_url = url
        handler.verification_link_provider = self.verification_link_provider
        handler.verification_code_provider = self.verification_code_provider
        await handler.wait_for_page_ready()
        await handler.prepare()

        return await self._application_loop(handler, report)

    async def _application_loop(self, handler: Any, report: Dict[str, Any], single_page: bool = False) -> Dict[str, Any]:
        """
        Work through the application section by section: authenticate when asked, fill the page,
        move on, and stop at the review stage (never submitting). With single_page, stop after the
        current page unless the user chooses to continue.
        """
        stuck = auth_attempts = empty_waits = job_pages = 0
        for step in range(1, self.max_steps + 1):
            if job_pages < 2 and await handler.start_from_job_description():
                job_pages += 1
                console.print("  ↪️  The portal's own job page - pressed its Apply to start the application.")
                continue
            if await handler.is_auth_page():
                auth_attempts += 1
                try:
                    outcome = await handler.handle_authentication_if_present() if auth_attempts <= 3 else "failed"
                except Exception as e:
                    console.print(f"  [yellow]Sign-in step error: {str(e).splitlines()[0][:150]}[/yellow]")
                    outcome = "failed"
                self.page = handler.page  # the handler may have restarted the application in a new tab
                report["auth"] = outcome
                console.print(f"  🔐 Authentication: [bold]{outcome}[/bold]")
                await self._shot(f"auth_{step}")
                if outcome == "failed":
                    if self.auto:
                        report["status"] = "failed:auth"
                        report["auth_errors"] = await handler.validation_errors()
                        return report
                    wait_for_proceed("Log in or create your account in the browser, then press Enter here...")
                    auth_attempts = 0
                await handler.wait_for_page_ready()
                continue

            if await handler.is_review_page():
                console.print("\n[bold green]🎯 Reached final Review & Submit page![/bold green]")
                await self._shot(f"final_review_{step}")
                report["status"] = "reached_review"
                console.print("[bold yellow]🛑 Handing over to you for final review and submission (the tool never submits).[/bold yellow]")
                wait_for_proceed("Check all inputs in browser. When done, press Enter to conclude this job...")
                break

            sec_name = await handler.get_current_section()
            console.print(f"\n  [bold white]Section {step}:[/bold white] [cyan]{sec_name}[/cyan]")
            fill = await handler.fill_current_section()
            if not any(fill.get(k) for k in ("filled_count", "kept_count", "skipped_count", "failed_count")) and empty_waits < 3:
                empty_waits += 1  # no fields at all: the next page is usually still loading
                console.print("    [dim]No fields found yet - waiting for the page to finish loading...[/dim]")
                await asyncio.sleep(4)
                await handler.wait_for_page_ready()
                continue
            self._print_fill_summary(fill)
            report["steps"].append({"section": sec_name, "url": self.page.url, **{k: v for k, v in fill.items()}})
            await self._shot(f"step_{step}")

            if single_page:
                if not confirm("    Continue with the next sections as well?", default=False):
                    report["status"] = "filled_current_page"
                    console.print("  [green]✓ Current page filled. The browser and tab stay open for you.[/green]")
                    break
                single_page = False

            before = await handler.page_signature()
            if confirm("    Proceed to next section?"):
                if not await handler.go_to_next_section():
                    console.print("    [dim yellow]No Next button found.[/dim yellow]")
                    if self.auto:
                        report["status"] = "stopped:no_next_button"
                        break
                    wait_for_proceed("Navigate to the next section in the browser, then press Enter here...")
                    continue
                if await handler.is_review_page():
                    stuck = 0
                elif await handler.page_signature() == before:
                    errors = await handler.validation_errors()
                    stuck += 1
                    console.print(f"    [yellow]Page did not advance.{' Errors: ' + ' | '.join(errors[:4]) if errors else ''}[/yellow]")
                    report["steps"][-1]["blocked_by"] = errors
                    if stuck >= 2:
                        if self.auto:
                            report["status"] = "stopped:validation"
                            break
                        wait_for_proceed("Fix the highlighted fields in the browser and move on, then press Enter here...")
                        stuck = 0
                else:
                    stuck = 0
            else:
                wait_for_proceed("Make your changes and move to the next section yourself, then press Enter here...")
        else:
            report["status"] = "stopped:max_steps"

        if report["status"] == "started":
            report["status"] = "stopped"
        return report

    async def run_attached(self, target: str, cdp_url: str = "", all_sections: bool = False) -> List[Dict[str, Any]]:
        """
        Attach to a Chrome you already have open (started with --launch-chrome), find the tab matching
        `target` (URL or keyword), work out which page/section it is on and fill it from resume_data.yaml.
        Your browser and tabs are left open.
        """
        from .attach import DEFAULT_CDP, cdp_alive, pick_tab

        print_banner()
        await self.initialize()
        cdp_url = cdp_url or (self.settings.get("browser", {}) or {}).get("cdp_url") or DEFAULT_CDP
        if not cdp_alive(cdp_url):
            console.print(f"[red]No Chrome is accepting connections at {cdp_url}.[/red]\n"
                          "Start it with:  [bold]python run.py --launch-chrome[/bold]  (your portal logins are kept in that window),\n"
                          "open the application there, then run --attach again.")
            return []
        self.attached = True
        self.playwright = await async_playwright().start()
        try:
            browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
            page, scored = await pick_tab(browser, target) if target else (None, [])
            if page is None:
                tabs = [(p, u, t) for p, u, t, _ in scored] or [(p, p.url, "") for c in browser.contexts for p in c.pages]
                tabs = [x for x in tabs if not x[1].startswith(("devtools://", "chrome://"))]
                if len(tabs) == 1:
                    page = tabs[0][0]
                elif tabs and not self.auto:
                    console.print("[bold]Open tabs:[/bold]")
                    for i, (_, u, t) in enumerate(tabs, 1):
                        console.print(f"  {i}. {t[:60]}  [dim]{u[:100]}[/dim]")
                    choice = ask_user("Which tab should be filled? (number)", default="1")
                    page = tabs[int(choice) - 1][0] if choice.isdigit() and 0 < int(choice) <= len(tabs) else None
            if page is None:
                console.print(f"[yellow]No open tab matches '{target}'.[/yellow]")
                return []
            self.page = page
            self.context = page.context
            await page.bring_to_front()
            console.print(f"\n[bold green]🔗 Attached to tab:[/bold green] {await page.title()}\n   [dim]{page.url}[/dim]")

            scanner = LinkedInScanner(page)
            job_info = await scanner.extract_job_info()
            site_type = await detect_ats(page)
            console.print(f"  🏢 Detected ATS platform: [bold cyan]{site_type.upper()}[/bold cyan]")
            report: Dict[str, Any] = {"url": page.url, "status": "started", "steps": [], "auth": None,
                                      "job": job_info, "ats": site_type, "mode": "attach"}
            qa_engine = LocationAwareAnswerer(self.qa_rules, self.resume_data, job_info.get("location", ""),
                                              f"{target} {page.url} {await page.title()}", ask=self._ask_job_country)
            auto_answer = (self.settings.get("application", {}) or {}).get("auto_answer_questions", True)
            form_filler = FormFiller(page, self.resume_data, self.skill_matcher, qa_engine if auto_answer else None, self.settings)
            handler = get_handler(page, form_filler, self.resume_data, self.settings, ats=site_type)
            handler.job_url = target if "://" in (target or "") else page.url
            handler.verification_link_provider = self.verification_link_provider
            handler.verification_code_provider = self.verification_code_provider
            await handler.wait_for_page_ready()
            await handler.prepare()
            report = await self._application_loop(handler, report, single_page=not all_sections)
            self.reports.append(report)
            self._write_report()
            return self.reports
        finally:
            await self.cleanup()

    # ================================================================== email pipeline
    async def start_pipeline_browser(self) -> None:
        """
        The pipeline needs LinkedIn signed in (fit details, Easy Apply): use the --launch-chrome window if it is
        open, otherwise a persistent browser profile that keeps your LinkedIn and portal logins between runs.
        """
        from .attach import DEFAULT_CDP, cdp_alive
        browser_cfg = self.settings.get("browser", {}) or {}
        cdp = browser_cfg.get("cdp_url") or DEFAULT_CDP
        self.playwright = await async_playwright().start()
        if cdp_alive(cdp):
            self.attached = True
            self.browser = await self.playwright.chromium.connect_over_cdp(cdp)
            self.context = self.browser.contexts[0]
            self.page = await self.context.new_page()
            console.print(f"  [green]✓[/green] Using your open Chrome at {cdp} (new tabs; yours are left alone)")
        else:
            base = os.environ.get("LOCALAPPDATA") or str(Path.home())
            profile = browser_cfg.get("user_data_dir") or str(Path(base) / "JobApplicator" / "automation-profile")
            self.context = await self.playwright.chromium.launch_persistent_context(
                profile, headless=bool(self.headless), slow_mo=browser_cfg.get("slow_mo", 150),
                viewport={"width": 1280, "height": 900})
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            console.print(f"  [green]✓[/green] Browser profile: {profile} (sign in to LinkedIn once; it is remembered)")
        self.context.set_default_timeout(browser_cfg.get("timeout", 30000))

    async def run_email_pipeline(self, skip_email: bool = False) -> List[Dict[str, Any]]:
        """Outlook job alerts -> screen each job -> apply list (JSON) -> apply / defer / cancel."""
        print_banner()
        await self.initialize()
        tracker_file = (self.settings.get("pipeline", {}) or {}).get("tracker_file", "data/job_tracker.json")
        tracker = JobTracker(tracker_file)
        console.print(f"  [green]✓[/green] Job tracker: {tracker_file} {tracker.counts() or ''}")
        try:
            await self.start_pipeline_browser()
            if not skip_email:
                await self.collect_from_mail(tracker)
            await self.apply_from_tracker(tracker)
            return self.reports
        finally:
            await self.cleanup()

    async def choose_mail_source(self) -> Any:
        """Ask where the job alerts are: Outlook on this PC (default), a folder of saved emails, or skip."""
        cfg = self.settings.get("mail", {}) or {}
        options = {"o": "Outlook on this PC", "f": "a folder of saved emails (DOCX/PDF/HTML/EML/MSG)"}
        if cfg.get("graph_client_id"):
            options["w"] = "Outlook on the web"
        default = {"outlook": "o", "folder": "f", "graph": "w"}.get(str(cfg.get("source", "outlook")).lower(), "o")
        prompt = "\nRead new job-alert emails from (" + ", ".join(f"{k} = {v}" for k, v in options.items()) + ", n = skip)"
        choice = ask_user(prompt, choices=list(options) + ["n"], default=default if default in options else "o")
        if choice == "n":
            return None

        if choice == "f":
            suggested = cfg.get("saved_emails_folder") or str(Path.home() / "Documents" / "JobAlerts")
            folder = ask_user("Folder that holds the saved emails", default=suggested).strip().strip('"')
            source = SavedEmailFolderSource(folder)
            if not source.folder.is_dir():
                console.print(f"[red]Folder not found: {source.folder}[/red]")
                return None
            return source

        if choice == "o":
            if not confirm("Allow this tool to open Outlook on this PC and read your job-alert folder?"):
                return None
            source = OutlookDesktopSource(notify=lambda m: console.print(f"  [yellow]{m}[/yellow]"))
            try:
                console.print("  [cyan]Opening Outlook...[/cyan]")
                await source.open()
            except ImportError:
                console.print("[red]Reading Outlook needs pywin32:  pip install pywin32[/red]")
                return None
            except Exception as e:
                console.print(f"[red]Could not open Outlook ({str(e).splitlines()[0][:120]}).[/red]\n"
                              "  The new 'Outlook for Windows' app cannot be automated; this uses classic Outlook, which must have\n"
                              "  your account set up (open classic Outlook once and sign in). Or save the mails to a folder (option f).")
                return None
        else:
            if not confirm("Allow this tool to connect to Outlook on the web and read your job-alert folder?"):
                return None
            source = GraphMailSource(OutlookEmailReader(cfg["graph_client_id"], cfg.get("graph_tenant_id", "common")))
            await source.open()

        folders = await source.inbox_folders()
        console.print("\n[bold]Folders under Inbox:[/bold] " + ", ".join(f"{f['name']} ({f['total']})" for f in folders))
        default_folder = cfg.get("outlook_folder", "LinkedIn")
        if default_folder.lower() not in {f["name"].lower() for f in folders} and folders:
            default_folder = folders[0]["name"]
        name = ask_user("Which folder under Inbox should be processed?", default=default_folder).strip()
        if not await source.use_folder(name):
            console.print(f"[red]No folder named '{name}' under Inbox.[/red]")
            await source.close()
            return None
        return source

    async def collect_from_mail(self, tracker: JobTracker) -> None:
        """Screen every job link in every message of the chosen source; dispose of fully processed messages."""
        source = await self.choose_mail_source()
        if source is None:
            return
        cfg = self.settings.get("mail", {}) or {}
        dispose = bool(cfg.get("move_processed_to_trash", True))
        try:
            messages = await source.messages()
            console.print(f"\n[bold green]{len(messages)} mail(s) in {source.label}[/bold green]")
            stop = False
            for i, msg in enumerate(messages, 1):
                links = source.links(msg)
                console.print(f"\n[bold magenta]✉ Mail {i}/{len(messages)}[/bold magenta] {escape(str(msg['subject'])[:80])} "
                              f"[dim]({str(msg['received'])[:10]}, {len(links)} job link(s))[/dim]")
                if msg.get("error"):
                    console.print(f"   [red]• could not read this file: {msg['error'][:120]} (kept)[/red]")
                    continue
                complete = True
                for url in links:
                    jid = job_id_from_url(url)
                    if tracker.known(jid):
                        console.print(f"   [dim]• already tracked ({tracker.get(jid)['status']}): {url}[/dim]")
                        continue
                    try:
                        if await self.screen_job(url, msg, source.label, tracker) == "stop":
                            stop, complete = True, False
                            break
                    except Exception as e:
                        complete = False
                        console.print(f"   [red]• could not screen {url}: {str(e).splitlines()[0][:120]}[/red]")
                if complete and dispose:
                    moved = await source.dispose(msg)
                    console.print(f"   [dim]🗑  {'moved to ' + source.trash_name if moved else 'could NOT be moved (kept)'}[/dim]")
                if stop:
                    console.print("[yellow]Stopped reading mail; the remaining mails stay where they are.[/yellow]")
                    break
        finally:
            await source.close()

    async def screen_job(self, url: str, msg: Dict[str, Any], folder: str, tracker: JobTracker) -> str:
        """Experience -> still open -> (your OK) LinkedIn fit -> your decision. Records the outcome."""
        jid = job_id_from_url(url)
        page = self.page
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        job = await read_job_page(page)
        if job["signed_out"] and not self.auto and not getattr(self, "_linkedin_prompted", False):
            self._linkedin_prompted = True
            wait_for_proceed("LinkedIn is not signed in in this browser. Sign in there (once - it is remembered), then press Enter...")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)
            job = await read_job_page(page)
        info = await LinkedInScanner(page).extract_job_info()
        common = {"url": url, "title": info.get("title"), "company": info.get("company"), "location": info.get("location"),
                  "email_subject": msg.get("subject"), "email_received": msg.get("received"), "source": folder}

        threshold = int((self.settings.get("screening", {}) or {}).get("min_years_experience", 12))
        req = required_years(job["description"])
        verdict = req.more_than(threshold)
        common.update({"required_years": req.describe(), "years_evidence": req.evidence[:3]})
        console.print(f"   [bold]• {info.get('title', '?')[:70]}[/bold] — {info.get('company', '?')} — {info.get('location') or '?'}")
        console.print(f"     Experience asked: {req.describe()}  [dim]{'; '.join(req.evidence[:2])}[/dim]")

        if verdict is False or (verdict is None and (self.settings.get("screening", {}) or {}).get("drop_if_years_not_stated", False)):
            tracker.add(jid, "dropped", note=f"experience {req.describe()} (needs more than {threshold})", **common)
            console.print(f"     [dim]✗ dropped: needs more than {threshold} years[/dim]")
            return "dropped"
        if job["already_applied"]:
            tracker.add(jid, "applied", note="LinkedIn shows you already applied", **common)
            console.print("     [dim]✓ LinkedIn says you already applied[/dim]")
            return "applied"
        if not job["accepting"]:
            tracker.add(jid, "dropped", note="no longer accepting applications", **common)
            console.print("     [dim]✗ dropped: no longer accepting applications[/dim]")
            return "dropped"

        console.print("     ✓ Still accepting applications")
        choice = ask_user("     Check your LinkedIn fit for this job? (y = yes, n = skip, q = stop reading mail)",
                          choices=["y", "n", "q"], default="y")
        if choice == "q":
            return "stop"
        if choice == "n":
            tracker.add(jid, "not_interested", note="skipped before the fit check", **common)
            return "skipped"
        fit = await linkedin_fit(page) or "LinkedIn showed no match/fit details (sign in to LinkedIn to see them)"
        console.print(f"     [cyan]LinkedIn fit:[/cyan] {fit}")
        if confirm("     Add this job to your apply list?"):
            tracker.add(jid, "pending", fit=fit, **common)
            console.print("     [green]➕ added (pending)[/green]")
            return "pending"
        tracker.add(jid, "not_interested", fit=fit, note="declined after the fit check", **common)
        return "not_interested"

    async def apply_from_tracker(self, tracker: JobTracker) -> None:
        """Offer every pending and deferred job: apply / defer / cancel. Applying runs the full automation."""
        queue = tracker.with_status("pending", "deferred")
        if not queue:
            console.print("\n[dim]Nothing pending or deferred in the job tracker.[/dim]")
            return
        console.print(f"\n[bold]Jobs waiting for a decision ({len(queue)}):[/bold]")
        for i, j in enumerate(queue, 1):
            line = f"  {i}. [{j['status']}] {str(j.get('title'))[:60]} — {j.get('company')}  "
            meta = f"(found {str(j.get('scraped_at'))[:10]}, fit: {str(j.get('fit', '-'))[:60]})"
            console.print(escape(line) + f"[dim]{escape(meta)}[/dim]")  # job text may contain [brackets]
        if not confirm("\nStart applying now?"):
            return
        for i, job in enumerate(queue, 1):
            choice = ask_user(f"\n[{i}/{len(queue)}] {str(job.get('title'))[:60]} — {job.get('company')} ({job['status']}). "
                              "a = apply, d = defer, c = cancel, s = stop", choices=["a", "d", "c", "s"], default="a")
            if choice == "s":
                break
            if choice == "d":
                tracker.set_status(job["job_id"], "deferred", "deferred by you")
                continue
            if choice == "c":
                tracker.set_status(job["job_id"], "cancelled", "cancelled by you")
                continue
            try:
                report = await self.process_job({"link": job["url"], "subject": job.get("email_subject", "")}, skip_confirm=True)
            except Exception as e:
                report = {"status": f"error:{str(e).splitlines()[0][:120]}"}
                console.print(f"[red]Error while applying: {report['status']}[/red]")
            self.reports.append(report)
            job["last_attempt"] = {"at": now(), "result": report.get("status"), "ats": report.get("ats"), "auth": report.get("auth")}
            if confirm("Did you submit this application?", default=False):
                tracker.set_status(job["job_id"], "applied", f"submitted ({report.get('ats') or 'portal'})")
            else:
                later = ask_user("Keep it for later (d = defer) or cancel it (c)?", choices=["d", "c"], default="d")
                tracker.set_status(job["job_id"], "deferred" if later == "d" else "cancelled",
                                   f"not submitted: {report.get('status')}")
        self._write_report()
        console.print(f"\n[bold]Job tracker:[/bold] {tracker.counts()}")

    async def _run_easy_apply(self, form_filler: FormFiller, report: Dict[str, Any]) -> Dict[str, Any]:
        """Fill each LinkedIn Easy Apply step, stopping before 'Submit application'."""
        console.print("  ⚡ Handling LinkedIn Easy Apply modal...")
        for step in range(1, 12):
            await asyncio.sleep(1.5)
            fill = await form_filler.fill_all_fields()
            self._print_fill_summary(fill)
            report["steps"].append({"section": f"easy_apply_{step}", **fill})
            submit = self.page.locator("button:has-text('Submit application')")
            if await submit.count() and await submit.first.is_visible():
                report["status"] = "reached_review"
                break
            nxt = self.page.locator("button[aria-label*='Continue to next step'], button:has-text('Next'), button:has-text('Review')")
            if not await nxt.count() or not await nxt.first.is_visible():
                report["status"] = "stopped:no_next_button"
                break
            await nxt.first.click()
        wait_for_proceed("Review the Easy Apply form and press Submit in the browser yourself. Press Enter here when finished...")
        return report

    @staticmethod
    def _print_fill_summary(fill: Dict[str, Any]) -> None:
        console.print(f"    ✓ Filled: [green]{fill.get('filled_count', 0)}[/green] | Kept: {fill.get('kept_count', 0)} | "
                      f"Skipped: [dim]{fill.get('skipped_count', 0)}[/dim] | Failed: [red]{fill.get('failed_count', 0)}[/red]")
        for d in fill.get("details", []):
            if d["status"] in ("failed", "unanswered-required"):
                console.print(f"      [yellow]• {d['label'][:70]}: {d['status']} {d.get('reason', '')}[/yellow]")

    async def run(self) -> List[Dict[str, Any]]:
        """Execute full application automation lifecycle."""
        print_banner()
        try:
            await self.initialize()
            await self.start_browser()
            self.job_links = await self.fetch_job_links()

            if not self.job_links:
                console.print("[yellow]No job links to process. Exiting.[/yellow]")
                return self.reports

            console.print(f"\n[bold green]Found {len(self.job_links)} opportunity(s) to process.[/bold green]\n")

            for i, job in enumerate(self.job_links, 1):
                console.print(f"\n[bold magenta]══════ Process Job {i} of {len(self.job_links)} ══════[/bold magenta]")
                try:
                    self.reports.append(await self.process_job(job))
                except Exception as e:
                    console.print(f"[bold red]Error processing job: {e}[/bold red]")
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                    self.reports.append({"url": job.get("link"), "status": f"error:{e}"})

                if i < len(self.job_links):
                    if not confirm(f"\nContinue to next opportunity ({i+1}/{len(self.job_links)})?"):
                        break

            self._write_report()
            console.print("\n[bold green]✓ Application session completed.[/bold green]\n")
            return self.reports
        finally:
            await self.cleanup()

    def _write_report(self) -> None:
        try:
            path = Path(utils.SCREENSHOT_DIR) / f"run_report_{datetime.now():%Y%m%d_%H%M%S}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.reports, indent=1, default=str), encoding="utf-8")
            console.print(f"[dim]Run report: {path}[/dim]")
        except Exception:
            pass

    async def cleanup(self) -> None:
        """Close browser resources (an attached Chrome and its tabs are only disconnected, never closed)."""
        if getattr(self, "attached", False):
            try:
                await self.playwright.stop()
            except Exception:
                pass
            return
        for closer in (lambda: self.context and self.context.close(), lambda: self.browser and self.browser.close(),
                       lambda: self.playwright and self.playwright.stop()):
            try:
                awaitable = closer()
                if awaitable:
                    await awaitable
            except Exception:
                pass


async def main(config_dir: str = "config", manual_urls: bool = False, headless: Optional[bool] = None,
               screenshots: bool = True, direct_url: Optional[str] = None, resume_path: str = "",
               settings_path: str = "", auto: bool = False, attach: bool = False, cdp_url: str = "",
               all_sections: bool = False, email_pipeline: bool = False, skip_email: bool = False) -> List[Dict[str, Any]]:
    app = JobAutoApply(config_dir=config_dir, manual_urls=manual_urls, headless=headless, screenshots=screenshots,
                       direct_url=direct_url, resume_path=resume_path, settings_path=settings_path, auto=auto)
    if attach:
        return await app.run_attached(direct_url or "", cdp_url, all_sections)
    if email_pipeline:
        return await app.run_email_pipeline(skip_email=skip_email)
    return await app.run()
