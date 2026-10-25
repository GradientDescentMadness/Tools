import os
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:
    print("Error: 'pymupdf' library is required. Install it using: pip install pymupdf")
    sys.exit(1)


def get_pdf_path():
    """Prompt user for PDF path, validate and return Path object."""
    while True:
        raw_input_path = input("\n[1] Enter PDF file path (full path or filename in current directory):\n> ").strip()
        # Strip surrounding quotes if user copied path as quoted string
        raw_input_path = raw_input_path.strip('\'"')
        if not raw_input_path:
            print("  [!] Path cannot be empty. Please try again.")
            continue

        pdf_path = Path(raw_input_path).expanduser().resolve()
        if not pdf_path.exists():
            print(f"  [!] File not found: {pdf_path}")
            continue
        if not pdf_path.suffix.lower() == ".pdf":
            print(f"  [!] Warning: '{pdf_path.name}' does not have a .pdf extension.")
            confirm = input("  Continue anyway? (y/n): ").strip().lower()
            if confirm != 'y':
                continue
        return pdf_path


def get_sensitive_strings():
    """Prompt user one-by-one for sensitive strings to redact."""
    print("\n[2] Enter sensitive strings to redact:")
    print("    Note: Quotes or double quotes are NOT needed. Enter plain text directly.")

    sensitive_items = []
    idx = 1
    while True:
        prompt = f"Text to be redacted {idx} (Press enter for None): "
        val = input(prompt).strip()
        # In case user still types quotes around their input, strip them safely
        val = val.strip('\'"')

        if not val:
            if idx == 1:
                print("\nNo text to redact.")
                sys.exit(0)
            else:
                break

        if val in sensitive_items:
            print(f"  [i] '{val}' was already added. Skipping duplicate.")
        else:
            sensitive_items.append(val)
        idx += 1

    # Print summary of all texts that need to be redacted
    print("\n" + "-" * 45)
    print(f"SUMMARY OF TEXTS TO REDACT ({len(sensitive_items)} item(s)):")
    print("-" * 45)
    for i, item in enumerate(sensitive_items, start=1):
        print(f"  {i}. {item}")
    print("-" * 45)
    return sensitive_items


def get_redaction_style():
    """Prompt user for redaction character / style."""
    print("\n[3] Choose redaction character / style:")
    print("    1. Null or \"\" (Clean blank / erase with paper background color)")
    print("    2. 'X' (Replace matching text with 'X')")
    print("    3. '█' with paper background color (Solid box blending into paper background)")
    print("    4. '█' Black block (Classic black redaction bar)")

    while True:
        choice = input("Enter choice (1, 2, 3, 4, or type 'Null', 'X', '█'): ").strip()
        choice_clean = choice.strip('\'"').lower()

        # Option 1: Null or ""
        if choice_clean in ["1", "null", '""', "none", "blank", ""]:
            return {
                "type": "null",
                "label": 'Null ("")',
                "fill": (1.0, 1.0, 1.0),   # White paper background
                "text": "",
                "text_color": (0.0, 0.0, 0.0)
            }
        # Option 2: 'X'
        elif choice_clean in ["2", "x"]:
            return {
                "type": "x",
                "label": "'X'",
                "fill": (1.0, 1.0, 1.0),   # Paper background
                "text": "X",
                "text_color": (0.0, 0.0, 0.0)
            }
        # Option 3: '█' with paper background color
        elif choice_clean in ["3", "█", "block", "paper", "white"]:
            return {
                "type": "block_paper",
                "label": "█ (Paper background color)",
                "fill": (1.0, 1.0, 1.0),   # Paper background solid fill
                "text": "",
                "text_color": (1.0, 1.0, 1.0)
            }
        # Option 4: '█' Black block
        elif choice_clean in ["4", "black", "blackout", "bar"]:
            return {
                "type": "block_black",
                "label": "█ (Solid black block)",
                "fill": (0.0, 0.0, 0.0),   # Black solid fill
                "text": "",
                "text_color": (0.0, 0.0, 0.0)
            }
        else:
            print("  [!] Invalid choice. Please enter 1, 2, 3, 4, or 'Null', 'X', '█'.")


def redact_pdf(pdf_path: Path, sensitive_strings: list[str], redaction_style: dict):
    """Perform redactions on the PDF and save to a new file."""
    doc = pymupdf.open(pdf_path)
    total_redactions = 0
    stats_per_term = {term: 0 for term in sensitive_strings}

    print(f"\nProcessing '{pdf_path.name}' ({len(doc)} pages)...")

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_redaction_count = 0

        for term in sensitive_strings:
            rects = page.search_for(term)
            if not rects:
                continue

            for rect in rects:
                if redaction_style["type"] == "x":
                    # For X, fill the rect with repeating 'X' matching length
                    replacement_text = "X" * max(1, len(term))
                    page.add_redact_annot(
                        rect,
                        text=replacement_text,
                        fill=redaction_style["fill"],
                        text_color=redaction_style["text_color"]
                    )
                else:
                    page.add_redact_annot(
                        rect,
                        text=redaction_style["text"],
                        fill=redaction_style["fill"],
                        text_color=redaction_style["text_color"]
                    )
                page_redaction_count += 1
                stats_per_term[term] += 1

        if page_redaction_count > 0:
            # apply_redactions permanently removes text/graphics underneath
            page.apply_redactions()
            total_redactions += page_redaction_count

    # Determine output file path
    output_filename = f"{pdf_path.stem}_redacted{pdf_path.suffix}"
    output_path = pdf_path.parent / output_filename

    # Save sanitized PDF (garbage=4 scrubs orphaned objects, deflate=True compresses streams)
    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()

    print("\n" + "=" * 50)
    print("REDACTION SUMMARY")
    print("=" * 50)
    print(f"File processed  : {pdf_path}")
    print(f"Redaction style : {redaction_style['label']}")
    print(f"Total instances : {total_redactions}")
    print("Breakdown per string:")
    for term, count in stats_per_term.items():
        print(f"  • '{term}': {count} occurrence(s) redacted")
    print("-" * 50)
    print(f"[✓] Successfully saved redacted PDF to:\n    {output_path.resolve()}")
    print("=" * 50)
    return output_path


def main():
    print("=" * 50)
    print("           PDF SENSITIVE DATA REDACTOR")
    print("=" * 50)
    try:
        # Step 1: PDF path
        pdf_path = get_pdf_path()

        # Step 2: Sensitive strings list
        sensitive_strings = get_sensitive_strings()

        # Step 3: Redaction character / style
        redaction_style = get_redaction_style()

        # Execute redaction
        redact_pdf(pdf_path, sensitive_strings, redaction_style)

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[!] An error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
