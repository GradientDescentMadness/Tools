#!/usr/bin/env python3
"""

#  J O B           A P P L I C A T I O N       A U T O M A T I O N
#
#   ███                   ██                            ██               
#  ██ ██                  ██                            ██     ██          
# ██   ██                 ██     ██                     ██
# ██   ██ █████  █████    ██           ████    ████   ██████   ██     ████   █████  
# ███████ ██  ██ ██  ██   ██     ██   ██  ██      ██    ██     ██    ██  ██  ██  ██ 
# ██   ██ ██   ████   ██  ██     ██   ██       █████    ██     ██    ██  ██  ██  ██ 
# ██   ██ ██  ██ ██  ██   ██     ██   ██      ██  ██    ██     ██    ██  ██  ██  ██ 
# ██   ██ █████  █████    ██     ██   ██  ██  ██  ██    ██     ██    ██  ██  ██  ██ 
# ██   ██ ██     ██       ██     ██    ████    █████     ███   ██     ████   ██  ██ 
#         ██     ██                                                                 

#   ███                                               ██               
#  ██ ██            ██                                ██     ██               
# ██   ██           ██                                ██                       
# ██   ██  ██  ██ ██████    ████   ██████    ████   ██████   ██     ████   █████  
# ███████  ██  ██   ██     ██  ██  ██ █ ██      ██    ██     ██    ██  ██  ██  ██ 
# ██   ██  ██  ██   ██     ██  ██  ██ █ ██   █████    ██     ██    ██  ██  ██  ██ 
# ██   ██  ██  ██   ██     ██  ██  ██ █ ██  ██  ██    ██     ██    ██  ██  ██  ██ 
# ██   ██  ██  ██   ██     ██  ██  ██ █ ██  ██  ██    ██     ██    ██  ██  ██  ██ 
# ██   ██   █████    ███    ████   ██ █ ██   █████     ███   ██     ████   ██  ██
# 
# 


Automatic Agentic JobApplicator - Execution Entrypoint
======================================================
Automates repetitive ATS job applications from LinkedIn notification emails
or direct job postings with resume data mapping, fuzzy skill matching,
and automated screening questionnaire handling.

Usage:
    python run.py                         # Outlook job alerts -> screen -> apply list -> apply/defer/cancel
    python run.py --skip-email            # Only work through pending/deferred jobs in the tracker
    python run.py --status                # Show the job tracker
    python run.py --config ./config       # Custom configuration folder
    python run.py --parse-resume cv.pdf   # Parse PDF/Word CV into YAML template
    python run.py --urls                  # Manual job link input mode (skips email fetch)
    python run.py --headless              # Run browser without visual UI
    python run.py --url URL --auto --resume-data tests/regression/resume_data.yaml
                                          # Unattended run (regression); never submits
    python run.py --launch-chrome         # Start Chrome that the tool can attach to (apply in this window)
    python run.py --attach --url URL      # Fill the page you are on in the open tab matching URL
"""

import argparse
import asyncio
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automatic Agentic JobApplicator - Intelligent ATS Application Autofill",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py                         Standard automated run
  python run.py --urls                  Manually paste LinkedIn/ATS links
  python run.py --parse-resume cv.pdf   Convert resume to config/resume_data.yaml
        """,
    )

    parser.add_argument(
        "--config",
        type=str,
        default="config",
        help="Path to configuration directory (default: ./config)",
    )

    parser.add_argument(
        "--parse-resume",
        type=str,
        metavar="FILE",
        help="Extract structured text from a PDF/DOCX resume file",
    )

    parser.add_argument(
        "--urls",
        action="store_true",
        help="Bypass Outlook email parsing and input job links manually",
    )

    parser.add_argument(
        "--url",
        type=str,
        help="Directly apply to a specific job posting URL",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="Run browser in headless mode (default: settings.yaml browser.headless)",
    )

    parser.add_argument(
        "--resume-data",
        type=str,
        default="",
        metavar="FILE",
        help="Use this resume YAML instead of <config>/resume_data.yaml (e.g. the regression profile)",
    )

    parser.add_argument(
        "--settings",
        type=str,
        default="",
        metavar="FILE",
        help="Use this settings YAML instead of <config>/settings.yaml",
    )

    parser.add_argument(
        "--auto",
        action="store_true",
        help="Unattended mode: auto-confirm prompts and skip pauses. Final submission is never clicked.",
    )

    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="Do not read Outlook; go straight to the pending/deferred jobs in the tracker",
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the job tracker (pending / deferred / applied / ...) and exit",
    )

    parser.add_argument(
        "--launch-chrome",
        action="store_true",
        help="Start Chrome with remote debugging on a dedicated profile (logins are kept); apply in that window",
    )

    parser.add_argument(
        "--attach",
        action="store_true",
        help="Attach to the open Chrome, pick the tab matching --url (URL or keyword) and fill the page it is on",
    )

    parser.add_argument(
        "--cdp",
        type=str,
        default="",
        metavar="URL",
        help="Chrome debugging address for --attach/--launch-chrome (default: settings browser.cdp_url or http://127.0.0.1:9222)",
    )

    parser.add_argument(
        "--all-sections",
        action="store_true",
        help="With --attach: continue through the following sections instead of only the current page",
    )

    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Disable automatic step screenshots",
    )

    return parser.parse_args()


def handle_parse_resume(file_path: str) -> None:
    from src.resume_parser import parse_resume_file
    import yaml

    path = Path(file_path)
    if not path.exists():
        print(f"[!] Error: File not found: {file_path}")
        sys.exit(1)

    print(f"[*] Parsing resume file: {file_path} ...")
    result = parse_resume_file(str(path))
    output = yaml.dump(result, default_flow_style=False, sort_keys=False)
    print("\n--- Extracted Resume Data (YAML) ---")
    print(output)
    print("--- End of Extraction ---\n")
    print("Copy desired sections into config/resume_data.yaml")


def main() -> None:
    args = parse_args()

    if args.parse_resume:
        handle_parse_resume(args.parse_resume)
        return

    from src.main import main as async_main

    if args.status:
        from src.job_tracker import JobTracker
        from src.utils import load_config
        cfg = load_config(args.settings or str(Path(args.config) / "settings.yaml"))
        path = (cfg.get("pipeline", {}) or {}).get("tracker_file", "data/job_tracker.json")
        tracker = JobTracker(path)
        print(f"Job tracker: {path}  {tracker.counts()}")
        for j in sorted(tracker.jobs.values(), key=lambda x: x.get("status_updated_at", "")):
            print(f"  [{j.get('status'):>14}] {str(j.get('status_updated_at'))[:16]}  {str(j.get('title'))[:55]} - "
                  f"{j.get('company')}  ({j.get('required_years', '?')})")
        return

    if args.launch_chrome:
        from src.attach import DEFAULT_CDP, default_profile_dir, launch_debug_chrome
        from src.utils import load_config
        browser_cfg = (load_config(args.settings or str(Path(args.config) / "settings.yaml")).get("browser", {}) or {})
        cdp = args.cdp or browser_cfg.get("cdp_url") or DEFAULT_CDP
        profile = browser_cfg.get("debug_profile_dir") or default_profile_dir()
        ok = launch_debug_chrome(cdp, profile, browser_cfg.get("chrome_path", ""), args.url or "")
        print(f"[*] Chrome {'is ready' if ok else 'did not start'} at {cdp} (profile: {profile}).")
        if ok:
            print("    Log in and apply in that window; run  python run.py --attach --url <link>  to fill the page you're on.")
        return

    config_dir = Path(args.config)
    if not config_dir.exists():
        print(f"[!] Config directory not found: {config_dir}")
        print("Run from the 'Python Source' directory or supply --config path.")
        sys.exit(1)

    try:
        asyncio.run(
            async_main(
                config_dir=str(config_dir),
                manual_urls=args.urls,
                headless=args.headless,
                screenshots=not args.no_screenshots,
                direct_url=args.url,
                resume_path=args.resume_data,
                settings_path=args.settings,
                auto=args.auto,
                attach=args.attach,
                cdp_url=args.cdp,
                all_sections=args.all_sections,
                email_pipeline=not (args.url or args.urls or args.attach),
                skip_email=args.skip_email,
            )
        )
    except KeyboardInterrupt:
        print("\n\n[!] Execution paused/interrupted by user. Exiting safely.")
        sys.exit(0)


if __name__ == "__main__":
    main()

