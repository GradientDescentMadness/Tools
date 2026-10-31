"""
Main workflow coordinator: orchestrates Outlook email discovery, LinkedIn parsing,
user confirmations, ATS redirection, and intelligent section-by-section autofill.
"""

import asyncio
from typing import Any, Dict, List, Optional
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .email_reader import OutlookEmailReader
from .form_filler import FormFiller
from .linkedin_scanner import LinkedInScanner
from .question_answerer import LocationAwareAnswerer
from .site_handlers.base_handler import get_handler
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


class JobAutoApply:
    """Orchestrator for the semi-autonomous job application lifecycle."""

    def __init__(
        self,
        config_dir: str = "config",
        manual_urls: bool = False,
        headless: bool = False,
        screenshots: bool = True,
    ):
        self.config_dir = config_dir
        self.manual_urls = manual_urls
        self.headless = headless
        self.enable_screenshots = screenshots

        self.settings: Dict[str, Any] = {}
        self.resume_data: Dict[str, Any] = {}
        self.qa_rules: Dict[str, Any] = {}

        self.playwright: Any = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.skill_matcher: Optional[SkillMatcher] = None
        self.job_links: List[Dict[str, str]] = []

    async def initialize(self) -> None:
        """Load YAML configuration files and initialize matcher."""
        console.print("\n[bold cyan]⚙️  Loading configuration profiles...[/bold cyan]")
        self.settings, self.resume_data, self.qa_rules = load_all_configs(self.config_dir)

        # Build list of candidate skills
        skills_cfg = self.resume_data.get("skills", {})
        all_skills = []
        all_skills.extend(skills_cfg.get("technical", []))
        all_skills.extend(skills_cfg.get("soft", []))
        self.skill_matcher = SkillMatcher(all_skills)
        console.print(f"  [green]✓[/green] Loaded profile: [bold]{self.resume_data.get('personal', {}).get('full_name')}[/bold]")
        console.print(f"  [green]✓[/green] Indexed [bold]{len(all_skills)}[/bold] skills for fuzzy matching")

        if self.enable_screenshots:
            s_dir = self.settings.get("application", {}).get("screenshot_dir", "./screenshots")
            setup_screenshot_dir(s_dir)

    async def start_browser(self) -> None:
        """Launch Playwright browser context."""
        console.print("\n[bold cyan]🌐 Starting automation browser...[/bold cyan]")
        self.playwright = await async_playwright().start()

        browser_cfg = self.settings.get("browser", {})
        slow_mo = browser_cfg.get("slow_mo", 400)
        user_data_dir = browser_cfg.get("user_data_dir", "")

        if user_data_dir:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=self.headless,
                slow_mo=slow_mo,
                viewport={"width": 1280, "height": 900},
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        else:
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                slow_mo=slow_mo,
            )
            self.context = await self.browser.new_context(viewport={"width": 1280, "height": 900})
            self.page = await self.context.new_page()

        self.page.set_default_timeout(self.settings.get("browser", {}).get("timeout", 30000))
        console.print("  [green]✓[/green] Browser initialized")

    async def fetch_job_links(self) -> List[Dict[str, str]]:
        """Retrieve job opportunities via Outlook MS Graph API or interactive manual input."""
        if self.manual_urls:
            return self._prompt_manual_urls()

        outlook_cfg = self.settings.get("outlook", {})
        client_id = outlook_cfg.get("client_id", "")

        if not client_id or client_id == "YOUR_CLIENT_ID_HERE":
            console.print("\n[yellow]⚠️  Outlook client_id is not set. Switching to manual URL entry mode.[/yellow]\n")
            return self._prompt_manual_urls()

        reader = OutlookEmailReader(client_id, outlook_cfg.get("tenant_id", "common"))
        console.print("  [cyan]Authenticating with Microsoft Outlook...[/cyan]")
        await asyncio.to_thread(reader.authenticate)

        links = reader.get_job_links_from_emails(
            lookback_days=outlook_cfg.get("lookback_days", 7),
            sender_patterns=self.settings.get("linkedin", {}).get("sender_patterns"),
            subject_keywords=self.settings.get("linkedin", {}).get("subject_keywords"),
        )

        if not links:
            console.print("[yellow]No unread LinkedIn job links found in Outlook inbox.[/yellow]")
            return self._prompt_manual_urls()

        return links

    def _prompt_manual_urls(self) -> List[Dict[str, str]]:
        console.print("[bold]Paste LinkedIn or ATS Job URLs (press Enter with empty line to finish):[/bold]")
        items = []
        while True:
            u = ask_user("Job URL").strip()
            if not u:
                break
            items.append({"link": u, "subject": f"Manual Opportunity #{len(items) + 1}"})
        return items

    async def process_job(self, job: Dict[str, str]) -> Dict[str, Any]:
        """Navigate to job, confirm with user, click Apply, and navigate ATS forms."""
        url = job["link"]
        scanner = LinkedInScanner(self.page)

        console.print(f"\n[bold blue]Navigating to:[/bold blue] {url}")
        job_info = await scanner.navigate_to_job(url)
        print_job_info(
            title=job_info.get("title", "Job Role"),
            company=job_info.get("company", "Company"),
            location=job_info.get("location", "Location"),
            url=url,
        )

        if self.enable_screenshots:
            await take_screenshot(self.page, "job_page")

        # Human-in-the-loop: ask user before initiating application
        if not confirm("Apply to this job?"):
            console.print("  ⏭️  Skipping application upon user request.\n")
            return {"status": "skipped", "job": job_info}

        console.print("  🖱️  Clicking Apply button...")
        apply_result = await scanner.click_apply()

        # Handle authentication if required
        if await scanner.is_login_required():
            console.print("\n[bold yellow]⚠️  Login or account creation required on this portal.[/bold yellow]")
            wait_for_proceed("Log in or create your credentials in the open browser, then press Enter here...")

        # If LinkedIn Easy Apply
        if apply_result == "easy_apply":
            console.print("  ⚡ Handling LinkedIn Easy Apply modal...")
            await scanner.handle_easy_apply(self.resume_data)
            wait_for_proceed("Review fields and press Submit in browser if satisfied. Press Enter here when finished...")
            return {"status": "completed", "job": job_info}

        # Otherwise ATS external site (Workday, Oracle Cloud, etc.)
        site_type = await scanner.detect_external_site()
        console.print(f"  🏢 Detected ATS platform: [bold cyan]{site_type.upper()}[/bold cyan]")

        # Setup Answerer with target location context
        qa_engine = LocationAwareAnswerer(self.qa_rules, self.resume_data, job_info.get("location", ""))
        form_filler = FormFiller(self.page, self.resume_data, self.skill_matcher, qa_engine)
        handler = get_handler(self.page, form_filler, self.resume_data, self.settings)

        # Step through all ATS sections
        await handler.wait_for_page_ready()
        max_steps = 15
        current_step = 0

        while current_step < max_steps:
            current_step += 1

            if await handler.is_review_page():
                console.print("\n[bold green]🎯 Reached final Review & Submit page![/bold green]")
                if self.enable_screenshots:
                    await take_screenshot(self.page, f"final_review_{current_step}")
                console.print("[bold yellow]🛑 Handing over to candidate for final manual review and submission.[/bold yellow]")
                wait_for_proceed("Check all inputs in browser. When done, press Enter to conclude this job...")
                break

            sec_name = await handler.get_current_section()
            console.print(f"\n  [bold white]Section {current_step}:[/bold white] [cyan]{sec_name}[/cyan]")

            # Populate current section
            fill_summary = await handler.fill_current_section()
            console.print(f"    ✓ Inputs filled: [green]{fill_summary.get('filled_count', 0)}[/green] | Skipped: [dim]{fill_summary.get('skipped_count', 0)}[/dim]")

            if self.enable_screenshots:
                await take_screenshot(self.page, f"step_{current_step}")

            # Ask user before navigating forward
            console.print("    [dim]Review filled values in browser. Make edits if needed.[/dim]")
            if not confirm("    Proceed to next section?"):
                wait_for_proceed("Make adjustments in browser, then press Enter to continue...")

            went_next = await handler.go_to_next_section()
            if not went_next:
                console.print("    [dim yellow]No explicit Next button detected. Handing over for manual navigation.[/dim yellow]")
                wait_for_proceed("Navigate to next section in browser, then press Enter here...")

        return {"status": "completed", "job": job_info}

    async def run(self) -> None:
        """Execute full application automation lifecycle."""
        print_banner()
        try:
            await self.initialize()
            await self.start_browser()
            self.job_links = await self.fetch_job_links()

            if not self.job_links:
                console.print("[yellow]No job links to process. Exiting.[/yellow]")
                return

            console.print(f"\n[bold green]Found {len(self.job_links)} opportunity(s) to process.[/bold green]\n")

            for i, job in enumerate(self.job_links, 1):
                console.print(f"\n[bold magenta]══════ Process Job {i} of {len(self.job_links)} ══════[/bold magenta]")
                try:
                    await self.process_job(job)
                except Exception as e:
                    console.print(f"[bold red]Error processing job: {e}[/bold red]")

                if i < len(self.job_links):
                    if not confirm(f"\nContinue to next opportunity ({i+1}/{len(self.job_links)})?"):
                        break

            console.print("\n[bold green]✓ Application session completed successfully![/bold green]\n")

        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        """Close browser resources."""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass


async def main(config_dir: str = "config", manual_urls: bool = False, headless: bool = False, screenshots: bool = True) -> None:
    app = JobAutoApply(config_dir=config_dir, manual_urls=manual_urls, headless=headless, screenshots=screenshots)
    await app.run()

