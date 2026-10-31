"""
Utility functions, logging, CLI display formatting, and screenshot handlers.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
import yaml

console = Console()


def load_config(path: str) -> dict:
    """Load a single YAML configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_all_configs(config_dir: str) -> Tuple[dict, dict, dict]:
    """Load settings.yaml, resume_data.yaml, and qa_rules.yaml from directory."""
    dir_path = Path(config_dir)
    settings = load_config(str(dir_path / "settings.yaml"))
    resume_data = load_config(str(dir_path / "resume_data.yaml"))
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
    content = (
        f"[bold white]Title:[/bold white]     [green]{title}[/green]\n"
        f"[bold white]Company:[/bold white]   [yellow]{company}[/yellow]\n"
        f"[bold white]Location:[/bold white]  [magenta]{location}[/magenta]\n"
        f"[bold white]URL:[/bold white]       [blue underline]{url}[/blue underline]"
    )
    console.print(Panel(content, title="🎯 Discovered Opportunity", border_style="blue"))


def ask_user(prompt: str, choices: list[str] | None = None) -> str:
    """Prompt the user for input with optional predefined choices."""
    return Prompt.ask(prompt, choices=choices)


def confirm(prompt: str) -> bool:
    """Ask a standard yes/no confirmation in terminal."""
    return Confirm.ask(prompt)


def wait_for_proceed(message: str = "Press Enter when ready to proceed...") -> None:
    """Pause script execution and await manual user Enter keypress."""
    console.print(f"[bold yellow]⏸️  {message}[/bold yellow]")
    input()


async def take_screenshot(page: Any, name: str, screenshot_dir: str = "./screenshots") -> str:
    """Save a timestamped screenshot of the current Playwright page."""
    try:
        dir_path = Path(screenshot_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        file_path = dir_path / f"{timestamp}_{safe_name}.png"
        await page.screenshot(path=str(file_path), full_page=False)
        return str(file_path)
    except Exception as e:
        console.print(f"[dim red]Screenshot error: {e}[/dim red]")
        return ""


def setup_screenshot_dir(path: str) -> None:
    """Ensure the target screenshot folder exists."""
    Path(path).mkdir(parents=True, exist_ok=True)

