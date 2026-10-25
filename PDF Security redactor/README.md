# PDF Security Redactor

A security-focused utility designed to permanently scrub and redact Personally Identifiable Information (PII), confidential credentials, intellectual property, and sensitive numbers from PDF files. Available as both an interactive Command-Line Interface (CLI) and an intuitive Graphical User Interface (GUI).

---

## What is PDF Data Security?

The Portable Document Format (PDF) is a composite file structure containing vector objects, layout matrices, embedded fonts, image streams, metadata blocks, and underlying text layers. 

A common and critical misconception in digital document handling is confusing **visual masking** with **binary data sanitization**:
* **The Illusion of Security:** Drawing a black rectangle over sensitive text in standard PDF viewers, Word, or image editors simply draws a visual layer *on top* of the text. The underlying characters remain intact inside the PDF's content streams and can easily be highlighted, copied, searched, or extracted using automated OCR and script tools.
* **True PDF Data Security:** True PDF security requires parsing the internal document object model (DOM), locating the precise coordinate bounding box of the sensitive string, physically destroying the underlying character glyphs and vector commands from the content stream, and deflating the file while purging orphaned binary objects.

---

## Why True Redaction is Needed

1. **Compliance & Legal Obligations:** Data protection regulations like GDPR, HIPAA, CCPA, and FERPA mandate that sensitive data (SSNs, medical records, financial account numbers, passwords) cannot be exposed or reverse-engineered from shared documents.
2. **Preventing Leaks in Public Filings:** High-profile data leaks frequently occur when court filings, government submissions, or corporate reports use faulty redaction (e.g. highlighting text in black rather than redacting).
3. **Irreversible Scrubbing:** This tool uses the high-performance `PyMuPDF` (MuPDF) redaction engine. When `apply_redactions()` is executed alongside garbage collection (`garbage=4, deflate=True`), sensitive text is physically purged from the PDF binary. The redacted data cannot be recovered by any PDF viewer or extraction script.

---

## How It Works

### 1. Interactive CLI Version (`Python Source/redact_pdf.py`)

The Command-Line Interface is designed for fast, terminal-based workflows. It requires no complex configuration and prompts you step-by-step with plain text inputs (no quotes required).

#### CLI Interactive Walkthrough

```text
==================================================
           PDF SENSITIVE DATA REDACTOR
==================================================

[1] Enter PDF file path (full path or filename in current directory):
> my_document.pdf

[2] Enter sensitive strings to redact:
    Note: Quotes or double quotes are NOT needed. Enter plain text directly.
Text to be redacted 1 (Press enter for None): John Doe
Text to be redacted 2 (Press enter for None): 123-45-6789
Text to be redacted 3 (Press enter for None): secret-token
Text to be redacted 4 (Press enter for None): 

---------------------------------------------
SUMMARY OF TEXTS TO REDACT (3 item(s)):
---------------------------------------------
  1. John Doe
  2. 123-45-6789
  3. secret-token
---------------------------------------------

[3] Choose redaction character / style:
    1. Null or "" (Clean blank / erase with paper background color)
    2. 'X' (Replace matching text with 'X')
    3. '█' with paper background color (Solid box blending into paper background)
    4. '█' Black block (Classic black redaction bar)
Enter choice (1, 2, 3, 4, or type 'Null', 'X', '█'): 1
```

#### Running the CLI
```powershell
cd "Python Source"
pip install -r requirements.txt
python redact_pdf.py
```

---

### 2. Desktop GUI Version (`GUI/redact_gui.py`)

For users who prefer a graphical interface, the GUI application offers a point-and-click workflow built on Python's native Tkinter.

#### GUI Features & Workflow:
1. **File Selection:** Click **Browse...** to pick any PDF file using a native file dialog.
2. **Interactive String Manager:**
   - Type plain text strings into the entry field and hit **Enter** or click **Add to List**.
   - Manage your queued terms in a scrollable list with **Remove Selected** or **Clear All**.
   - Live counter displays how many terms are queued for redaction.
3. **Redaction Style Selection:**
   - **Null / ""**: Clean blank erase matching page background color.
   - **'X'**: Replaces matching sensitive text with repeating 'X' characters.
   - **'█' (Paper color)**: Solid block blending seamlessly into the paper background.
   - **'█' (Black bar)**: Classic black redaction box.
4. **Execution & Feedback:**
   - Multi-threaded processing keeps the UI responsive during redaction of large multi-page documents.
   - Displays real-time status and shows a summary dialog with the exact number of occurrences redacted per string.
   - Safely saves the sanitized file alongside the original as `<filename>_redacted.pdf`.

#### Running the GUI
```powershell
cd "GUI"
pip install -r requirements.txt
python redact_gui.py
```

---

## Redaction Styles Comparison

| Option | Style | What Happens Under the Hood | Visual Result |
| :--- | :--- | :--- | :--- |
| **1** | **Null / `""`** | Deletes character stream; fills rect with paper color | Clean blank space where the text was |
| **2** | **`'X'`** | Deletes original text; writes `X` glyphs matching string length | Text replaced with `XXXXX` |
| **3** | **`'█'` (Paper Color)** | Deletes original text; renders solid rectangle matching background | Solid block mask blending with page |
| **4** | **`'█'` (Black Block)** | Deletes original text; renders solid black rectangle | Classic solid black redaction bar |

---

## Directory Structure

```text
PDF Security redactor/
├── README.md                  # Documentation and security guide
├── Python Source/
│   ├── redact_pdf.py          # Interactive CLI redaction script
│   └── requirements.txt       # CLI dependencies (pymupdf>=1.24.0)
└── GUI/
    ├── redact_gui.py          # Graphical user interface application
    └── requirements.txt       # GUI dependencies (pymupdf>=1.24.0)
```
