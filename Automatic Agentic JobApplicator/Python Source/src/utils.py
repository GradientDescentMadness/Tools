"""
Utility functions, logging, CLI display formatting, and screenshot handlers.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
import yaml

console = Console()

# Unattended mode (regression runs): confirmations default to yes, pauses are skipped,
# free-text questions return their default. Final submission is never automated either way.
AUTO_MODE = False
SCREENSHOT_DIR = "./screenshots"


def set_auto_mode(enabled: bool) -> None:
    global AUTO_MODE
    AUTO_MODE = enabled


def load_config(path: str) -> dict:
    """Load a single YAML configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_all_configs(config_dir: str, settings_path: str = "", resume_path: str = "") -> Tuple[dict, dict, dict]:
    """Load settings.yaml, resume_data.yaml, and qa_rules.yaml (individual files can be overridden)."""
    dir_path = Path(config_dir)
    settings = load_config(settings_path or str(dir_path / "settings.yaml"))
    resume_data = load_config(resume_path or str(dir_path / "resume_data.yaml"))
    qa_rules = load_config(str(dir_path / "qa_rules.yaml"))
    return settings, resume_data, qa_rules


def print_banner() -> None:
    """Display an interactive banner for the CLI."""
    banner = (
        "[bold cyan]====================================================================[/bold cyan]\n"
        "[bold white]       AUTOMATIC AGENTIC JOB APPLICATOR & ATS AUTOFILL              [/bold white]\n"
        "[dim]  Streamlining repetitive application portals with fuzzy matching & AI [/dim]\n"
        "[bold cyan]====================================================================[/bold cyan]"
    )
    console.print(Panel(banner, border_style="cyan"))


def print_job_info(title: str, company: str, location: str, url: str) -> None:
    """Pretty-print scraped job parameters in a Rich panel."""
    title, company, location, url = (escape(str(x)) for x in (title, company, location, url))  # may contain [brackets]
    content = (
        f"[bold white]Title:[/bold white]     [green]{title}[/green]\n"
        f"[bold white]Company:[/bold white]   [yellow]{company}[/yellow]\n"
        f"[bold white]Location:[/bold white]  [magenta]{location}[/magenta]\n"
        f"[bold white]URL:[/bold white]       [blue underline]{url}[/blue underline]"
    )
    console.print(Panel(content, title="🎯 Discovered Opportunity", border_style="blue"))


def ask_user(prompt: str, choices: Optional[List[str]] = None, default: str = "") -> str:
    """Prompt the user for input with optional predefined choices."""
    if AUTO_MODE:
        return default
    return Prompt.ask(prompt, choices=choices, default=default or None) or ""


def confirm(prompt: str, default: bool = True) -> bool:
    """Ask a standard yes/no confirmation in terminal."""
    if AUTO_MODE:
        console.print(f"{prompt} [dim](auto: {'yes' if default else 'no'})[/dim]")
        return default
    return Confirm.ask(prompt, default=default)


def wait_for_proceed(message: str = "Press Enter when ready to proceed...") -> None:
    """Pause script execution and await manual user Enter keypress."""
    console.print(f"[bold yellow]⏸️  {message}[/bold yellow]")
    if AUTO_MODE:
        console.print("[dim](auto mode: not waiting)[/dim]")
        return
    input()


async def take_screenshot(page: Any, name: str, screenshot_dir: str = "") -> str:
    """Save a timestamped screenshot of the current Playwright page."""
    try:
        dir_path = Path(screenshot_dir or SCREENSHOT_DIR)
        dir_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        file_path = dir_path / f"{timestamp}_{safe_name}.png"
        await page.screenshot(path=str(file_path), full_page=True)
        return str(file_path)
    except Exception as e:
        console.print(f"[dim red]Screenshot error: {e}[/dim red]")
        return ""


def setup_screenshot_dir(path: str) -> None:
    """Ensure the target screenshot folder exists and make it the default for take_screenshot."""
    global SCREENSHOT_DIR
    SCREENSHOT_DIR = path
    Path(path).mkdir(parents=True, exist_ok=True)

