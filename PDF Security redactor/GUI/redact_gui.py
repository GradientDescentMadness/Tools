import os
import sys
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import pymupdf
except ImportError:
    pymupdf = None


class PDFRedactorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF Sensitive Data Redactor")
        self.root.geometry("680x640")
        self.root.minsize(580, 560)

        self.sensitive_strings = []
        self.pdf_path_var = tk.StringVar()
        self.style_var = tk.StringVar(value="null")
        self.new_string_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        self._setup_style()
        self._build_ui()

    def _setup_style(self):
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self.style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
        self.style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        self.style.configure("Action.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        # Header
        header_frame = ttk.Frame(self.root, padding="12 10 12 5")
        header_frame.pack(fill=tk.X)
        ttk.Label(
            header_frame,
            text="PDF Sensitive Data Redactor",
            style="Header.TLabel"
        ).pack(anchor=tk.W)
        ttk.Label(
            header_frame,
            text="Permanently sanitize and redact sensitive PII and confidential terms from PDF files.",
            foreground="#555555"
        ).pack(anchor=tk.W, pady=(2, 0))

        # Main container
        main_frame = ttk.Frame(self.root, padding="12 5 12 10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. File Selection Frame
        file_frame = ttk.LabelFrame(main_frame, text=" 1. Select PDF File ", style="Section.TLabelframe", padding="10")
        file_frame.pack(fill=tk.X, pady=(0, 10))

        entry_file = ttk.Entry(file_frame, textvariable=self.pdf_path_var)
        entry_file.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        btn_browse = ttk.Button(file_frame, text="Browse...", command=self._browse_pdf)
        btn_browse.pack(side=tk.RIGHT)

        # 2. Sensitive Strings Frame
        strings_frame = ttk.LabelFrame(main_frame, text=" 2. Sensitive Strings to Redact ", style="Section.TLabelframe", padding="10")
        strings_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        ttk.Label(
            strings_frame,
            text="Enter plain text string (quotes or double quotes are NOT needed):",
            foreground="#444444"
        ).pack(anchor=tk.W, pady=(0, 4))

        input_row = ttk.Frame(strings_frame)
        input_row.pack(fill=tk.X, pady=(0, 6))

        entry_string = ttk.Entry(input_row, textvariable=self.new_string_var)
        entry_string.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        entry_string.bind("<Return>", lambda e: self._add_string())

        btn_add = ttk.Button(input_row, text="Add to List", command=self._add_string)
        btn_add.pack(side=tk.RIGHT)

        # Listbox with Scrollbar
        list_container = ttk.Frame(strings_frame)
        list_container.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL)
        self.listbox = tk.Listbox(
            list_container,
            yscrollcommand=scrollbar.set,
            selectmode=tk.EXTENDED,
            font=("Consolas", 10),
            height=5
        )
        scrollbar.config(command=self.listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        btn_row = ttk.Frame(strings_frame)
        btn_row.pack(fill=tk.X)
        self.lbl_count = ttk.Label(btn_row, text="Strings queued: 0", foreground="#666666")
        self.lbl_count.pack(side=tk.LEFT)

        btn_clear = ttk.Button(btn_row, text="Clear All", command=self._clear_strings)
        btn_clear.pack(side=tk.RIGHT, padx=(4, 0))
        btn_remove = ttk.Button(btn_row, text="Remove Selected", command=self._remove_selected)
        btn_remove.pack(side=tk.RIGHT)

        # 3. Redaction Character / Style Frame
        style_frame = ttk.LabelFrame(main_frame, text=" 3. Redaction Style ", style="Section.TLabelframe", padding="10")
        style_frame.pack(fill=tk.X, pady=(0, 10))

        styles = [
            ("null", 'Null or "" (Clean blank / erase with paper background color)'),
            ("x", "'X' (Replace matching text with repeating 'X')"),
            ("block_paper", "'█' with paper background color (Solid box matching page background)"),
            ("block_black", "'█' Solid black block (Classic black redaction bar)"),
        ]

        for val, label in styles:
            ttk.Radiobutton(
                style_frame,
                text=label,
                value=val,
                variable=self.style_var
            ).pack(anchor=tk.W, pady=2)

        # 4. Action & Status
        action_frame = ttk.Frame(main_frame)
        action_frame.pack(fill=tk.X, pady=(5, 0))

        self.btn_run = ttk.Button(
            action_frame,
            text="Execute PDF Redaction",
            style="Action.TButton",
            command=self._start_redaction_thread
        )
        self.btn_run.pack(fill=tk.X, ipady=4)

        self.progress_bar = ttk.Progressbar(action_frame, mode="indeterminate")

        status_bar = ttk.Frame(self.root, padding="8 4 8 4", relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(status_bar, textvariable=self.status_var, font=("Segoe UI", 9)).pack(side=tk.LEFT)

    def _browse_pdf(self):
        path = filedialog.askopenfilename(
            title="Select PDF File to Redact",
            filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")]
        )
        if path:
            self.pdf_path_var.set(path)

    def _add_string(self):
        val = self.new_string_var.get().strip().strip('\'"')
        if not val:
            return
        if val in self.sensitive_strings:
            messagebox.showinfo("Duplicate", f"'{val}' is already in the list.")
            return
        self.sensitive_strings.append(val)
        self.listbox.insert(tk.END, f"{len(self.sensitive_strings)}.  {val}")
        self.new_string_var.set("")
        self._update_count()

    def _remove_selected(self):
        selected = list(self.listbox.curselection())
        if not selected:
            return
        for idx in reversed(selected):
            del self.sensitive_strings[idx]
        self._refresh_listbox()

    def _clear_strings(self):
        self.sensitive_strings.clear()
        self._refresh_listbox()

    def _refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for i, s in enumerate(self.sensitive_strings, start=1):
            self.listbox.insert(tk.END, f"{i}.  {s}")
        self._update_count()

    def _update_count(self):
        self.lbl_count.config(text=f"Strings queued: {len(self.sensitive_strings)}")

    def _start_redaction_thread(self):
        if pymupdf is None:
            messagebox.showerror("Missing Dependency", "'pymupdf' is not installed.\nPlease run: pip install pymupdf")
            return

        pdf_path_str = self.pdf_path_var.get().strip().strip('\'"')
        if not pdf_path_str:
            messagebox.showwarning("Input Required", "Please select a PDF file first.")
            return
        pdf_path = Path(pdf_path_str)
        if not pdf_path.exists():
            messagebox.showerror("Error", f"File not found:\n{pdf_path}")
            return
        if not self.sensitive_strings:
            messagebox.showwarning("Input Required", "Please add at least one sensitive string to redact.")
            return

        self.btn_run.config(state=tk.DISABLED)
        self.progress_bar.pack(fill=tk.X, pady=(6, 0))
        self.progress_bar.start(10)
        self.status_var.set("Redacting sensitive data from PDF...")

        thread = threading.Thread(target=self._run_redaction, args=(pdf_path,), daemon=True)
        thread.start()

    def _run_redaction(self, pdf_path: Path):
        try:
            doc = pymupdf.open(str(pdf_path))
            total_redactions = 0
            stats = {term: 0 for term in self.sensitive_strings}
            style_key = self.style_var.get()

            # Configure style mapping
            fill_color = (1.0, 1.0, 1.0)
            text_color = (0.0, 0.0, 0.0)
            if style_key == "block_black":
                fill_color = (0.0, 0.0, 0.0)

            for page in doc:
                page_redaction_count = 0
                for term in self.sensitive_strings:
                    rects = page.search_for(term)
                    if not rects:
                        continue
                    for rect in rects:
                        if style_key == "x":
                            repl_text = "X" * max(1, len(term))
                            page.add_redact_annot(rect, text=repl_text, fill=fill_color, text_color=text_color)
                        elif style_key == "null":
                            page.add_redact_annot(rect, text="", fill=fill_color, text_color=text_color)
                        elif style_key == "block_paper":
                            page.add_redact_annot(rect, text="", fill=(1.0, 1.0, 1.0), text_color=(1.0, 1.0, 1.0))
                        elif style_key == "block_black":
                            page.add_redact_annot(rect, text="", fill=(0.0, 0.0, 0.0), text_color=(0.0, 0.0, 0.0))
                        page_redaction_count += 1
                        stats[term] += 1

                if page_redaction_count > 0:
                    page.apply_redactions()
                    total_redactions += page_redaction_count

            output_path = pdf_path.parent / f"{pdf_path.stem}_redacted{pdf_path.suffix}"
            doc.save(str(output_path), garbage=4, deflate=True)
            doc.close()

            breakdown_str = "\n".join([f"  • '{t}': {c} found" for t, c in stats.items()])
            msg = (
                f"Redaction Complete!\n\n"
                f"Total instances redacted: {total_redactions}\n\n"
                f"Breakdown:\n{breakdown_str}\n\n"
                f"Saved to:\n{output_path}"
            )
            self.root.after(0, self._on_success, msg, output_path)

        except Exception as e:
            self.root.after(0, self._on_error, str(e))

    def _on_success(self, message: str, output_path: Path):
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        self.btn_run.config(state=tk.NORMAL)
        self.status_var.set(f"Saved redacted PDF to {output_path.name}")
        messagebox.showinfo("Redaction Successful", message)

    def _on_error(self, err_msg: str):
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        self.btn_run.config(state=tk.NORMAL)
        self.status_var.set("Redaction failed.")
        messagebox.showerror("Error During Redaction", f"An error occurred:\n{err_msg}")


def main():
    root = tk.Tk()
    app = PDFRedactorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

