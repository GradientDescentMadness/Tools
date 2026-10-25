#!/usr/bin/env python3
"""
Automatic Agentic JobApplicator - Execution Entrypoint
======================================================
Automates repetitive ATS job applications from LinkedIn notification emails
or direct job postings with resume data mapping, fuzzy skill matching,
and automated screening questionnaire handling.

Usage:
    python run.py                         # Run standard interactive workflow
    python run.py --config ./config       # Custom configuration folder
    python run.py --parse-resume cv.pdf   # Parse PDF/Word CV into YAML template
    python run.py --urls                  # Manual job link input mode (skips email fetch)
    python run.py --headless              # Run browser without visual UI
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
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default: visible browser window)",
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
    from src.utils import load_all_configs

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
            )
        )
    except KeyboardInterrupt:
        print("\n\n[!] Execution paused/interrupted by user. Exiting safely.")
        sys.exit(0)


if __name__ == "__main__":
    main()
