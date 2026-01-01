import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
import requests

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QMessageBox, QFormLayout
)

import fitz  # PyMuPDF


# ----------------------------
# Utilities
# ----------------------------
INVALID_CHARS = r'\/:*?"<>|'

def sanitize_component(s: str, max_len: int = 120) -> str:
    """Make a string safe for filenames (Windows/macOS/Linux). Keeps Japanese."""
    if s is None:
        s = ""
    s = s.strip()

    # Normalize Unicode (e.g., full-width chars)
    s = unicodedata.normalize("NFKC", s)

    # Remove control chars
    s = "".join(ch for ch in s if ch.isprintable())

    # Replace invalid filename chars
    for ch in INVALID_CHARS:
        s = s.replace(ch, " ")

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Avoid trailing dots/spaces on Windows
    s = s.rstrip(" .")

    # Limit length
    if len(s) > max_len:
        s = s[:max_len].rstrip()

    return s

def camelize_no_space(s: str) -> str:
    """
    Remove spaces and capitalize the first letter of each word (ASCII letters only).
    Japanese etc. are kept as-is.
    """
    if not s:
        return ""

    parts = re.split(r"\s+", s.strip())
    new_parts = []
    for p in parts:
        if not p:
            continue
        # 英字で始まる場合だけ capitalize
        if re.match(r"[A-Za-z]", p):
            new_parts.append(p[0].upper() + p[1:])
        else:
            new_parts.append(p)
    return "".join(new_parts)

@dataclass
class ExtractedInfo:
    journal: str
    year: str
    title: str

DOI_REGEX = re.compile(r'\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b', re.IGNORECASE)

def find_doi(text: str) -> str:
    if not text:
        return ""
    m = DOI_REGEX.search(text)
    if m:
        return m.group(0)
    return ""

def query_crossref(doi: str) -> ExtractedInfo:
    url = f"https://api.crossref.org/works/{doi}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return ExtractedInfo("", "", "")
        data = r.json()["message"]

        journal = data.get("container-title", [""])[0]
        title = data.get("title", [""])[0]

        year = ""
        if "published-print" in data:
            year = str(data["published-print"]["date-parts"][0][0])
        elif "published-online" in data:
            year = str(data["published-online"]["date-parts"][0][0])

        return ExtractedInfo(
            journal=sanitize_component(journal),
            year=sanitize_component(year, max_len=10),
            title=sanitize_component(title, max_len=160),
        )
    except Exception:
        return ExtractedInfo("", "", "")

def extract_from_pdf(pdf_path: Path) -> ExtractedInfo:
    text = ""
    try:
        doc = fitz.open(str(pdf_path))
        for i in range(min(5, doc.page_count)):  # 最初の5ページだけ見る
            text += doc.load_page(i).get_text("text") + "\n"
        doc.close()
    except Exception:
        pass

    doi = find_doi(text)
    if doi:
        info = query_crossref(doi)
        if info.title or info.journal:
            return info

    # DOIが無い or 失敗したら空で返す（人が入力）
    return ExtractedInfo("", "", "")


def build_filename(info: ExtractedInfo) -> str:
    parts = [info.journal, info.year, info.title]
    parts = [p for p in parts if p]

    base = " ".join(parts) if parts else "untitled"  # まず空白区切りで結合
    base = sanitize_component(base, max_len=220)
    base = camelize_no_space(base)                   # 空白除去＋CamelCase

    return f"{base}.pdf"


def unique_path(target: Path) -> Path:
    """If target exists, append (1), (2), ..."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for i in range(1, 1000):
        cand = parent / f"{stem}({i}){suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError("Could not find a unique filename after 999 attempts.")


# ----------------------------
# GUI
# ----------------------------
class DropLabel(QLabel):
    def __init__(self, on_file_dropped):
        super().__init__()
        self.on_file_dropped = on_file_dropped
        self.setAcceptDrops(True)
        self.setText("ここにPDFをドラッグ＆ドロップ\n（または「PDFを選択」ボタン）")
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #888;
                padding: 20px;
                border-radius: 10px;
                font-size: 14px;
            }
        """)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".pdf") for u in urls):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        pdfs = [Path(u.toLocalFile()) for u in urls if u.toLocalFile().lower().endswith(".pdf")]
        if not pdfs:
            return
        # For now: take the first PDF
        self.on_file_dropped(pdfs[0])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Renamer (Journal+Year+Title)")
        self.resize(720, 420)

        self.current_pdf: Path | None = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(12)

        self.drop = DropLabel(self.load_pdf)
        layout.addWidget(self.drop)

        # Path label
        self.path_label = QLabel("PDF: （未選択）")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.path_label)

        form = QFormLayout()
        self.journal_edit = QLineEdit()
        self.year_edit = QLineEdit()
        self.title_edit = QLineEdit()

        form.addRow("雑誌名 (Journal)", self.journal_edit)
        form.addRow("出版年 (Year)", self.year_edit)
        form.addRow("タイトル (Title)", self.title_edit)
        layout.addLayout(form)

        # Preview
        self.preview_label = QLabel("新しいファイル名: （未生成）")
        self.preview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.preview_label)

        # Buttons
        btn_row = QHBoxLayout()
        self.pick_btn = QPushButton("PDFを選択")
        self.pick_btn.clicked.connect(self.pick_pdf)

        self.refresh_btn = QPushButton("再推定")
        self.refresh_btn.clicked.connect(self.refresh_guess)

        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self.rename_pdf)

        btn_row.addWidget(self.pick_btn)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.run_btn)
        layout.addLayout(btn_row)

        # Live update preview when edits change
        self.journal_edit.textChanged.connect(self.update_preview)
        self.year_edit.textChanged.connect(self.update_preview)
        self.title_edit.textChanged.connect(self.update_preview)

    def pick_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "PDFを選択", "", "PDF Files (*.pdf)")
        if path:
            self.load_pdf(Path(path))

    def load_pdf(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, "エラー", "ファイルが見つかりません。")
            return
        if path.suffix.lower() != ".pdf":
            QMessageBox.warning(self, "エラー", "PDFのみ対応しています。")
            return

        self.current_pdf = path
        self.path_label.setText(f"PDF: {str(path)}")

        info = extract_from_pdf(path)
        # Put guesses into fields (user can edit)
        self.journal_edit.setText(info.journal)
        self.year_edit.setText(info.year)
        self.title_edit.setText(info.title)
        self.update_preview()

    def refresh_guess(self):
        if not self.current_pdf:
            return
        info = extract_from_pdf(self.current_pdf)
        # Overwrite with new guess (still editable)
        self.journal_edit.setText(info.journal)
        self.year_edit.setText(info.year)
        self.title_edit.setText(info.title)
        self.update_preview()

    def current_info(self) -> ExtractedInfo:
        return ExtractedInfo(
            journal=sanitize_component(self.journal_edit.text()),
            year=sanitize_component(self.year_edit.text(), max_len=10),
            title=sanitize_component(self.title_edit.text(), max_len=160),
        )

    def update_preview(self):
        if not self.current_pdf:
            self.preview_label.setText("新しいファイル名: （未生成）")
            return
        filename = build_filename(self.current_info())
        self.preview_label.setText(f"新しいファイル名: {filename}")

    def rename_pdf(self):
        if not self.current_pdf:
            QMessageBox.information(self, "確認", "先にPDFを読み込んでください。")
            return

        info = self.current_info()
        new_name = build_filename(info)

        src = self.current_pdf
        dst = src.with_name(new_name)
        dst = unique_path(dst)

        # Basic validation: if nothing extracted and user left empty, avoid renaming to "untitled.pdf" silently
        if new_name == "untitled.pdf":
            ret = QMessageBox.question(
                self,
                "確認",
                "ファイル名が 'untitled.pdf' になります。続行しますか？",
                QMessageBox.Yes | QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return

        try:
            src.rename(dst)
        except PermissionError:
            QMessageBox.warning(self, "エラー", "ファイルが他のアプリで開かれている可能性があります。閉じてから再実行してください。")
            return
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"名前の変更に失敗しました:\n{e}")
            return

        self.current_pdf = dst
        self.path_label.setText(f"PDF: {str(dst)}")
        QMessageBox.information(self, "完了", f"名前を変更しました:\n{dst.name}")


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
