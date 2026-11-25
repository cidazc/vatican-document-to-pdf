#!/usr/bin/env python3
"""
vatican_to_pdf.py - turn a document from vatican.va into a clean, print-ready PDF.

    python vatican_to_pdf.py URL_OR_SAVED_FILE [MORE ...]  [options]

Examples
    python vatican_to_pdf.py https://www.vatican.va/content/francesco/en/encyclicals/documents/papa-francesco_20150524_enciclica-laudato-si.html
    python vatican_to_pdf.py LS.mhtml MH.mhtml --size a5 -o my_pdfs
    python vatican_to_pdf.py --list urls.txt
    python vatican_to_pdf.py URL --dump-json        # look at what was parsed, no PDF
    python vatican_to_pdf.py URL --save-html        # also keep the print-ready HTML

THE PIPELINE (each step is labelled in the program output)
    Stage 1  LOAD     download the page, or read a saved .html / .mhtml file
    Stage 2  READ     walk the page and rebuild its structure: masthead, chapters,
                      headings, numbered paragraphs, closing lines, footnotes
    Stage 3  NOTES    attach footnote marks in the text to the footnote texts
    Stage 4  LAYOUT   build the title page, "About this edition", contents page,
                      body and notes as one styled HTML document
    Stage 5  PDF      print that HTML to PDF with Chromium (3 passes, so the
                      contents page can show real page numbers)

WHERE TO CHANGE THINGS
    Support a new page layout ........ Stage 2 (class DocumentReader)
    A new kind of footnote ........... Stage 2 (collect_note_entry) + Stage 3
    Look and feel (fonts, colours) ... STYLE_SHEET and the settings just below
    Add a command-line option ........ build_argument_parser()

Setup
    pip install -r requirements.txt
    playwright install chromium
"""
from __future__ import annotations

import argparse
import html
import json
import quopri
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# =============================================================================
# SETTINGS - the things most people want to tweak
# =============================================================================

ACCENT_COLOUR = "#2f6b4a"          # headings, rules and links
# name on the command line -> CSS page size, and the wording used on the "About this edition" page
PAGE_SIZES = {"half-letter": "5.5in 8.5in", "letter": "Letter", "a4": "A4", "a5": "A5", "a6": "A6"}
PAGE_SIZE_LABELS = {"half-letter": "Half-letter (5.5 × 8.5 in)", "letter": "US Letter", "a4": "A4", "a5": "A5", "a6": "A6"}
DEFAULT_PAGE_SIZE = "half-letter"
BODY_FONT_STACK = ("'Libertinus Serif','Linux Libertine','Palatino Linotype',"
                   "Georgia,'DejaVu Serif',serif")
AUTO_TOC_DEEP_BELOW = 15           # contents get a 3rd level if the 2-level list is shorter than this
USER_AGENT = "Mozilla/5.0 (compatible; vatican-to-pdf/3.0)"

# =============================================================================
# SMALL SHARED HELPERS
# =============================================================================

ROMAN_NUMERAL = re.compile(r"^M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$")
NUMBER_WORDS = {w: i for i, w in enumerate(
    "ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE THIRTEEN "
    "FOURTEEN FIFTEEN SIXTEEN SEVENTEEN EIGHTEEN NINETEEN TWENTY".split(), 1)}
SMALL_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "for", "at", "by",
               "in", "of", "on", "to", "up", "as", "from", "with"}


def roman_to_int(text: str) -> int:
    """'XIV' -> 14"""
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for char, next_char in zip(text, text[1:] + " "):
        value = values[char]
        total += -value if values.get(next_char, 0) > value else value
    return total


def collapse_whitespace(text: str) -> str:
    """Squash runs of spaces/newlines/non-breaking spaces and tidy '( x )' brackets."""
    text = re.sub(r"[ \t\r\n\xa0]+", " ", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip()


def to_title_case(text: str) -> str:
    """'WHAT IS HAPPENING TO OUR COMMON HOME' -> 'What Is Happening to Our Common Home'."""
    result, previous_word = [], ""
    for position, word in enumerate(text.split()):
        letters_only = word.strip(".,;:!?\"'’“”()[]")
        starts_sentence = position == 0 or previous_word.endswith((".", ":", ";", "?", "!"))
        if len(letters_only) > 1 and ROMAN_NUMERAL.fullmatch(letters_only):
            result.append(word)                          # keep roman numerals: XIV
        elif word.lower() in SMALL_WORDS and not starts_sentence:
            result.append(word.lower())
        else:
            lowered = word.lower()
            for index, char in enumerate(lowered):       # capitalise the first LETTER
                if char.isalpha():                       # (skips opening quotes)
                    lowered = lowered[:index] + char.upper() + lowered[index + 1:]
                    break
            result.append(lowered)
        previous_word = word
    return " ".join(result).replace(" Ai", " AI")


ROMAN_HEADING_PREFIX = re.compile(r"^([IVXLC]+\.)\s+(.*)$")      # "II. THE ISSUE OF WATER"
NUMBER_HEADING_PREFIX = re.compile(r"^\d{1,3}\.\s+")             # "1. The Global Climate Crisis"


def tidy_heading_text(text: str) -> str:
    """ALL-CAPS source headings become Title Case; mixed-case headings are left alone."""
    text = collapse_whitespace(text)
    if not (text == text.upper() and re.search(r"[A-Z]", text)):
        return text
    roman = ROMAN_HEADING_PREFIX.match(text)
    if roman:                                            # keep "II." then title-case the rest
        return f"{roman.group(1)} {to_title_case(roman.group(2))}"
    return to_title_case(text)


def soften_capital_runs(text: str) -> str:
    """Title-case every run of 2+ ALL-CAPS words inside otherwise normal text.

    '"TEACHER, WHAT GOOD MUST I DO...?" (Mt 19:16) - Christ and the answer'
    becomes '"Teacher, What Good Must I Do...?" (Mt 19:16) - Christ and the answer'.
    """
    finished: list[str] = []
    current_run: list[str] = []

    def end_run() -> None:
        finished.append(to_title_case(" ".join(current_run)) if len(current_run) >= 2 else " ".join(current_run))
        current_run.clear()

    for word in text.split(" "):
        letters = re.sub(r"[^A-Za-z]", "", word)
        is_capitalised_word = bool(letters) and letters.isupper() and (len(letters) > 1 or bool(current_run))
        if is_capitalised_word:
            current_run.append(word)
        else:
            if current_run:
                end_run()
            finished.append(word)
    if current_run:
        end_run()
    return " ".join(part for part in finished if part != "")


# =============================================================================
# PROGRESS REPORTING - everything the user sees while the program runs
# =============================================================================

class Reporter:
    """Prints labelled stages, progress bars and a final checklist.

    Nothing else in the program calls print(), so changing how output looks (or
    silencing it for a library/GUI) only means editing this class.
    """

    def __init__(self, quiet: bool = False, verbose: bool = False):
        self.quiet = quiet
        self.verbose = verbose
        self.stage_results: list[tuple[str, bool, str]] = []   # (title, ok, detail)
        self._is_terminal = sys.stderr.isatty()

    # -- plain messages -----------------------------------------------------
    def info(self, message: str) -> None:
        if not self.quiet:
            print(message, file=sys.stderr)

    def detail(self, message: str) -> None:
        """Extra information, shown only with --verbose."""
        if self.verbose and not self.quiet:
            print(f"      · {message}", file=sys.stderr)

    def warn(self, message: str) -> None:
        print(f"  ! {message}", file=sys.stderr)

    # -- stages ---------------------------------------------------------------
    @contextmanager
    def stage(self, number: int, total: int, title: str) -> Iterator["StageHandle"]:
        """Wrap one pipeline stage:   with reporter.stage(2, 5, 'Read structure') as s: ..."""
        handle = StageHandle()
        self.info(f"\n[{number}/{total}] {title} ...")
        started = time.time()
        try:
            yield handle
        except Exception:
            self.stage_results.append((title, False, "failed"))
            self.info(f"      ✗ failed after {time.time() - started:.1f}s")
            raise
        summary = handle.summary
        self.stage_results.append((title, True, summary))
        self.info(f"      ✓ done in {time.time() - started:.1f}s" + (f" - {summary}" if summary else ""))

    def checklist(self, heading: str) -> None:
        """Final overview of which stages finished."""
        if self.quiet:
            return
        print(f"\n{heading}", file=sys.stderr)
        for title, ok, summary in self.stage_results:
            mark = "✓" if ok else "✗"
            print(f"  {mark} {title}" + (f"  ({summary})" if summary else ""), file=sys.stderr)
        self.stage_results.clear()

    # -- progress bar ---------------------------------------------------------
    def track(self, items: Iterable, description: str, total: int | None = None) -> Iterator:
        """Yield the items of `items`, drawing a progress bar as we go."""
        items = list(items) if total is None else items
        total = len(items) if total is None else total
        if self.quiet or total == 0:
            yield from items
            return
        width, next_milestone = 28, 25
        for count, item in enumerate(items, 1):
            yield item
            percent = int(100 * count / total)
            if self._is_terminal:
                filled = int(width * count / total)
                bar = "█" * filled + "░" * (width - filled)
                print(f"\r      {description:<22} {bar} {percent:3d}%  ({count}/{total})",
                      end="", file=sys.stderr, flush=True)
            elif percent >= next_milestone:             # logs / CI: one line per quarter
                print(f"      {description}: {percent}%", file=sys.stderr)
                next_milestone += 25
        if self._is_terminal:
            print(file=sys.stderr)


class StageHandle:
    """Lets a stage attach a one-line summary, e.g.  stage.summary = '73 paragraphs'."""
    summary: str = ""


# =============================================================================
# DATA MODEL - what Stage 2 produces and Stages 3-5 consume
# =============================================================================

@dataclass
class Block:
    """One piece of the document body, in reading order."""
    kind: str                       # "heading" | "paragraph" | "quote" | "closing"
    html: str = ""                  # cleaned inline HTML (plain text for headings)
    level: int = 0                  # headings: 1 = chapter, 2 = section, 3 = sub-section
    paragraph_number: int = 0       # the "17." in numbered paragraphs (0 = unnumbered)
    heading_id: str = ""            # anchor id, used by the contents page
    chapter_index: int = 0          # which chapter this block sits in (index into chapter_titles)
    centred: bool = False           # closing lines only


@dataclass
class Note:
    """One footnote or endnote."""
    key: str                        # unique id, e.g. "12" or "s3_2" (supplementary note 2, chapter 3)
    label: str                      # what the reader sees: "12" or "2*"
    html: str                       # the note text
    chapter_number: int | None = None   # for per-chapter notes


@dataclass
class Document:
    url: str = ""
    title: str = ""
    genre_lines: list[str] = field(default_factory=list)   # lines above the title ("ENCYCLICAL LETTER", "FRANCIS")
    subtitle: str = ""                                      # lines below the title ("ON CARE FOR OUR COMMON HOME")
    issuer: str = ""
    date: str = ""
    blocks: list[Block] = field(default_factory=list)
    notes: dict[str, Note] = field(default_factory=dict)
    chapter_titles: list[str] = field(default_factory=lambda: [""])      # index 0 = text before chapter 1
    chapter_numbers: list[int | None] = field(default_factory=lambda: [None])


# =============================================================================
# STAGE 1 - LOAD  (download a page, or read a saved .html / .mhtml file)
# =============================================================================

def download_page(url: str, use_browser: bool = False, reporter: Reporter | None = None) -> str:
    """Fetch a page. Plain HTTP first; a real Chromium if the site refuses."""
    if not use_browser:
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
            if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding   # older pages: latin-1 / cp1252
            return response.text
        except requests.RequestException as error:
            if reporter:
                reporter.warn(f"plain download failed ({error}); retrying with a browser")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        text = page.content()
        browser.close()
    return text


def decode_html_bytes(data: bytes, declared_charset: str | None) -> str:
    """Bytes -> text. UTF-8 first (what vatican.va serves), then declared/meta charset, then cp1252.

    Chrome's "Save as MHTML" often omits the charset; assuming ASCII would turn
    every curly quote or accented letter into U+FFFD, so we decide ourselves.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    meta = re.search(rb"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", data[:4096], re.I)
    tried: list[str] = []
    for charset in (declared_charset, meta.group(1).decode() if meta else None, "cp1252", "latin-1"):
        if charset and charset.lower() not in tried:
            tried.append(charset.lower())
            try:
                return data.decode(charset)
            except (UnicodeDecodeError, LookupError):
                continue
    return data.decode("utf-8", "replace")


def undo_quoted_printable_if_needed(text: str) -> str:
    """Repair HTML that was saved as raw quoted-printable ('=E2=80=9C', '=3D', soft '=\\n' breaks)."""
    if text.count("=3D") > 5 and text.count('="') < 5:
        return quopri.decodestring(text.encode("latin-1", "ignore")).decode("utf-8", "replace")
    return text


def read_saved_page(path: Path) -> tuple[str, str]:
    """Read a saved page. Returns (html_text, original_url_if_known)."""
    data = path.read_bytes()
    looks_like_mime = path.suffix.lower() in (".mhtml", ".mht", ".eml") or \
        "mime-version:" in data[:600].decode("latin-1", "ignore").lower()
    if looks_like_mime:
        message = BytesParser(policy=policy.default).parsebytes(data)
        original_url = str(message.get("Snapshot-Content-Location") or message.get("Content-Location") or "").strip()
        for part in message.walk():
            if part.get_content_type() == "text/html":
                raw_bytes = part.get_payload(decode=True) or b""    # undoes quoted-printable/base64
                return decode_html_bytes(raw_bytes, part.get_content_charset()), original_url
        raise RuntimeError(f"{path}: this MHTML file contains no HTML part")
    return undo_quoted_printable_if_needed(decode_html_bytes(data, None)), ""


# =============================================================================
# STAGE 2 - READ  (rebuild the document structure from the HTML)
# =============================================================================
# Vatican pages are written by hand over many decades, so there is no single
# layout. DocumentReader therefore classifies every <p>/<h*>/<li> by what it
# LOOKS like (bold? italic? all caps? centred? starts with "12."?) and keeps a
# little state (are we still in the masthead? in the footnotes yet?).

# --- patterns ---------------------------------------------------------------

NOTE_MARK_ANCHOR_NAME = re.compile(r"^_?ftnref(\d+)$", re.I)     # <a name="_ftnref3">  in the text
NOTE_TEXT_ANCHOR_NAME = re.compile(r"^_?ftn(\d+)$", re.I)        # <a name="_ftn3">     in the notes
NOTE_ANCHOR_HREF = re.compile(r"^#_?ftn(?:ref)?(\d+)$", re.I)
NUMBERED_PARAGRAPH = re.compile(r"^(\d{1,3})\.\s+(.*)$", re.S)
CHAPTER_MARKER = re.compile(
    r"^(CHAPTER|PART|BOOK)\s+(" + "|".join(NUMBER_WORDS) + r"|\d+|[IVXLC]+)\b[\s.:\-–—]*(.*)$")
CLOSING_LINE = re.compile(r"^(Given|Done|Issued)\s+(in|at|on)\b", re.I)
NOTES_HEADING = re.compile(r"^(supplementary\s+|explanatory\s+)?(foot|end)?notes\b(\s+to the text)?(\s*\(\*\))?$", re.I)
NUMBERED_NOTE = re.compile(r"^\(?(\d{1,3})\)?\s*[.,:]?\s+(.*)$", re.S)
PARENTHESISED_NOTE = re.compile(r"^\(\s*(\d{1,3})\s*\*?\s*\)\s*(.*)$", re.S)
CHAPTER_LINE_IN_NOTES = re.compile(r"^Chapter\s+([IVXLC]+|\d+)\.?$", re.I)
LANGUAGE_SELECTOR = re.compile(r"^\[?\s*(?:[A-Za-z]{2}\s*-\s*)+[A-Za-z]{2}\s*\]?$")
SITE_NOISE = re.compile(
    r"^[\[\]\s_\-–—*•|]*(multimedia|video|audio)?[\[\]\s_\-–—*•|]*$"       # "[Multimedia]", "_____"
    r"|^(the )?holy see$|libreria editrice vaticana|^copyright\b|^©"        # footer
    r"|^\[?\s*(en|it|fr|es|de|pt|la)\s*\]?$", re.I)
FRONT_OR_BACK_HEADING = re.compile(r"^(a (christian )?prayer|introduction|preamble|conclusion|final|appendix)", re.I)

GENRE_STARTS = ("ENCYCLICAL LETTER", "APOSTOLIC EXHORTATION", "APOSTOLIC LETTER",
                "APOSTOLIC CONSTITUTION", "MOTU PROPRIO", "PASTORAL CONSTITUTION",
                "DOGMATIC CONSTITUTION", "DECLARATION", "DECREE", "MESSAGE")
ISSUER_LINE = re.compile(
    r"^(OF|BY)\s+(HIS|THE)\b|^(POPE|SAINT|BLESSED)\b|^(IOANNES|PAULUS|FRANCISCUS|BENEDICTUS|LEO)\b"
    r"|\bPP\.|^(SOLEMNLY|PROMULGATED|APPROVED)\b")
SUBJECT_LINE = re.compile(r"^(ON|TO|FOR|ABOUT|CONCERNING)\b")

KNOWN_ISSUERS_IN_URLS = (
    ("francesco", "Francis"), ("benedict", "Benedict XVI"), ("john-paul", "John Paul II"),
    ("john_paul", "John Paul II"), ("paul-vi", "Paul VI"), ("paul_vi", "Paul VI"),
    ("john-xxiii", "John XXIII"), ("john_xxiii", "John XXIII"), ("leo-xiv", "Leo XIV"),
    ("leo_xiv", "Leo XIV"), ("vat-ii", "Second Vatican Council"))


# --- inline HTML helpers ----------------------------------------------------

def visible_lines(element: Tag) -> list[str]:
    """The element's text split at <br> line breaks (HTML comments ignored)."""
    lines = [""]
    for node in element.descendants:
        if isinstance(node, Comment):
            continue
        if isinstance(node, Tag) and node.name == "br":
            lines.append("")
        elif isinstance(node, NavigableString):
            lines[-1] += str(node)
    return [collapse_whitespace(line) for line in lines if collapse_whitespace(line)]


def visible_text(element: Tag) -> str:
    return collapse_whitespace(" ".join(visible_lines(element)))


def note_mark_html(key: str, label: str) -> str:
    """The little raised number in the text that links down to the note."""
    return f'<sup class="note-mark" id="m{key}"><a href="#n{key}">{label}</a></sup>'


def clean_inline_html(element: Tag) -> str:
    """Re-emit an element's content keeping only <em>, <strong>, <sup>, <br> and external links.
    Footnote anchors (<a name="_ftnref3">) become note marks."""
    pieces: list[str] = []
    for node in element.children:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            pieces.append(html.escape(str(node)))
            continue
        if not isinstance(node, Tag):
            continue
        tag = node.name.lower()
        if tag == "a":
            anchor_match = NOTE_MARK_ANCHOR_NAME.match(node.get("name") or node.get("id") or "") \
                or NOTE_ANCHOR_HREF.match(node.get("href", ""))
            if anchor_match:
                number = anchor_match.group(1)
                pieces.append(note_mark_html(number, number))
                continue
            href = node.get("href", "")
            inner = clean_inline_html(node)
            pieces.append(f'<a href="{html.escape(href)}">{inner}</a>' if href.startswith("http") else inner)
        elif tag in ("i", "em", "cite"):
            pieces.append(f"<em>{clean_inline_html(node)}</em>")
        elif tag in ("b", "strong"):
            pieces.append(f"<strong>{clean_inline_html(node)}</strong>")
        elif tag in ("sup", "sub"):
            pieces.append(f"<{tag}>{clean_inline_html(node)}</{tag}>")
        elif tag == "br":
            pieces.append("<br>")
        else:                                            # span, font, u, ... : keep the text only
            pieces.append(clean_inline_html(node))
    return tidy_inline_html("".join(pieces))


def tidy_inline_html(markup: str) -> str:
    markup = re.sub(r"[ \t\r\n\xa0]+", " ", markup)
    markup = re.sub(r"\(\s+", "(", markup)
    markup = re.sub(r"\s+\)", ")", markup)
    markup = re.sub(r"\s+(<sup class=\"note-mark\")", r"\1", markup)    # no space before a note mark
    markup = re.sub(r"(<br>\s*)+$", "", markup)
    markup = re.sub(r"<(em|strong)>\s*</\1>", "", markup)
    return markup.strip()


def share_of_text_inside(element: Tag, tag_names: tuple[str, ...]) -> float:
    """What fraction of the visible characters sit inside e.g. <b>/<strong>? (0.0 - 1.0)"""
    total = inside = 0
    for text_node in element.descendants:
        if not isinstance(text_node, NavigableString) or isinstance(text_node, Comment) or not text_node.strip():
            continue
        length = len(text_node.strip())
        total += length
        for ancestor in text_node.parents:
            if ancestor is element.parent:
                break
            if ancestor.name in tag_names:
                inside += length
                break
    return inside / total if total else 0.0


def share_of_text_in_page_links(element: Tag) -> float:
    """Fraction of text inside links to '#anchors' on the same page (an on-page contents list)."""
    total = inside = 0
    for text_node in element.descendants:
        if not isinstance(text_node, NavigableString) or isinstance(text_node, Comment) or not text_node.strip():
            continue
        length = len(text_node.strip())
        total += length
        link = text_node.find_parent("a")
        if link is not None and link.get("href", "").startswith("#") and not NOTE_ANCHOR_HREF.match(link["href"]):
            inside += length
    return inside / total if total else 0.0


def is_centred(element: Tag) -> bool:
    if (element.get("align") or "").lower() == "center":
        return True
    return "text-align:center" in (element.get("style") or "").lower().replace(" ", "")


def chapter_marker_label(kind: str, number_text: str) -> tuple[str, int | None]:
    """('CHAPTER', 'ONE') -> ('Chapter 1', 1);  ('CHAPTER', 'IV') -> ('Chapter 4', 4)"""
    upper = number_text.upper()
    if upper in NUMBER_WORDS:
        number: int | None = NUMBER_WORDS[upper]
    elif upper.isdigit():
        number = int(upper)
    else:
        number = roman_to_int(upper)
    return f"{kind.title()} {number}", number


# --- masthead (title page lines) --------------------------------------------

def split_masthead(lines: list[str], likely_title: str) -> tuple[list[str], str, str]:
    """Sort the all-caps lines at the top of the page into (lines above title, title, subtitle).

    `likely_title` (from the page <title> / URL) breaks ties, e.g. in
    ['IOANNES PAULUS PP. II', 'VERITATIS SPLENDOR'] the title is the second line.
    """
    def normalised(text: str) -> str:
        return re.sub(r"[^a-z]", "", text.lower())

    candidates = [line for line in lines
                  if not any(line.upper().startswith(g) for g in GENRE_STARTS)
                  and not ISSUER_LINE.search(line.upper()) and not SUBJECT_LINE.match(line.upper())]
    title = ""
    for line in candidates:                       # best match with the page's own title
        if likely_title and normalised(line) and normalised(line) in normalised(likely_title):
            title = line
            break
    if not title and candidates:
        title = candidates[0]

    above, subtitle_lines = [], []
    title_seen = False
    for line in lines:
        upper = line.upper()
        if line == title:
            title_seen = True
        elif subtitle_lines or (title_seen and SUBJECT_LINE.match(upper)
                                and not any(upper.startswith(g) for g in GENRE_STARTS)):
            subtitle_lines.append(line)           # "ON CARE FOR ..." and anything after it
        else:
            above.append(line)                    # genre + issuer lines
    return above, title, "\n".join(subtitle_lines)


# --- the reader ---------------------------------------------------------------

class DocumentReader:
    """Walks the page's elements in order and fills a Document."""

    def __init__(self, soup: BeautifulSoup, url: str, reporter: Reporter):
        self.soup = soup
        self.reporter = reporter
        self.document = Document(url=url)

        # --- reading state ------------------------------------------------------
        self.masthead_lines: list[str] = []
        self.masthead_done = False
        self.last_paragraph_number = 0
        self.open_chapter: Block | None = None       # chapter heading still collecting ALL-CAPS title lines
        self.open_chapter_label = ""
        self.open_chapter_title_parts: list[str] = []
        self.in_closing = False                      # after "Given in Rome ..."
        self.notes_mode: str | None = None           # None | "numbered" | "supplementary"
        self.last_note: Note | None = None
        self.last_note_number = 0
        self.supplementary_chapter: int | None = None
        self.heading_counter = 0
        self.current_chapter = 0

    # ---- entry point ---------------------------------------------------------
    def read(self) -> Document:
        self._remove_page_furniture()
        main_area = (self.soup.select_one("div.documento") or self.soup.select_one("div.text")
                     or self.soup.select_one("#corpo") or self.soup.body or self.soup)
        self._read_metadata()
        self._collect_anchor_footnotes(main_area)

        elements = [el for el in main_area.descendants
                    if isinstance(el, Tag) and el.name in ("h1", "h2", "h3", "h4", "p", "blockquote", "li")]
        for element in self.reporter.track(elements, "Reading text"):
            self._read_element(element)

        self._finish_masthead()
        self._promote_subheadings_if_no_sections()
        return self.document

    # ---- preparation -------------------------------------------------------
    def _remove_page_furniture(self) -> None:
        for tag in self.soup(["script", "style", "nav", "header", "footer", "noscript", "form", "iframe"]):
            tag.decompose()

    def _read_metadata(self) -> None:
        document = self.document
        page_title = collapse_whitespace(self.soup.title.get_text(" ")) if self.soup.title else ""
        self.page_title_without_date = re.sub(r"\s*\(.*?\)\s*", " ", page_title).strip()
        self.page_title_without_date = re.sub(
            r"\s*[-|–]\s*(Vatican|Francis|Benedict|John|Paul|Leo).*$", "", self.page_title_without_date, flags=re.I).strip()

        # date: from the URL (…20231004-…, …_19641121_…) or from "(6 August 1993)" in the page title
        url_date = re.search(r"(19|20)\d{2}(\d{2})(\d{2})", urlparse(document.url).path)
        if url_date:
            try:
                parsed = datetime(int(url_date.group(0)[:4]), int(url_date.group(2)), int(url_date.group(3)))
                document.date = f"{parsed.day} {parsed.strftime('%B %Y')}"
            except ValueError:
                pass
        if not document.date:
            title_date = re.search(r"\((\d{1,2}\s+[A-Za-z]+\s+\d{4})\)", page_title)
            if title_date:
                document.date = title_date.group(1)

        lowered_url = document.url.lower()
        for fragment, name in KNOWN_ISSUERS_IN_URLS:
            if fragment in lowered_url:
                document.issuer = name
                break

        # Words that probably appear in the title: the page <title> plus the file name in the URL
        # ("hf_jp-ii_enc_06081993_veritatis-splendor" -> "... veritatis splendor").
        slug = Path(urlparse(document.url).path).stem
        self.likely_title = f"{self.page_title_without_date} {re.sub(r'[-_]+', ' ', slug)}"

    def _collect_anchor_footnotes(self, main_area: Tag) -> None:
        """Notes written as <a name="_ftn1" href="#_ftnref1">[1]</a> text...  (modern pages)."""
        for anchor in list(main_area.find_all("a")):
            if anchor.attrs is None:                  # removed together with an earlier note
                continue
            match = NOTE_TEXT_ANCHOR_NAME.match(anchor.get("name") or anchor.get("id") or "")
            if not match:
                continue
            number = match.group(1)
            container = anchor.find_parent(["p", "div", "li"]) or anchor.parent
            anchor.decompose()
            self.document.notes[number] = Note(number, number, clean_inline_html(container))
            container.decompose()

    # ---- one element ---------------------------------------------------------
    def _read_element(self, element: Tag) -> None:
        if element.name != "blockquote" and element.find_parent("blockquote") is not None:
            return                                     # already handled as part of the blockquote
        text = visible_text(element)
        if self._is_page_noise(element, text):
            return

        if self.notes_mode:
            self._read_note_entry(element, text)
            return
        if NOTES_HEADING.match(text) and self.masthead_done and len(text) < 60:
            self.notes_mode = "supplementary" if text.lower().startswith("supplementary") else "numbered"
            self.in_closing = False
            return

        if not self.masthead_done and self._read_masthead_line(element, text):
            return

        if CLOSING_LINE.match(text) and re.search(r"\b(1\d{3}|20\d{2})\b", text):   # "Given in Rome ... 1993"
            self.in_closing = True
        if self.in_closing:
            first_note = NUMBERED_NOTE.match(text)
            if first_note and int(first_note.group(1)) == 1:
                self.notes_mode = "numbered"           # notes listed after the signature ("1. Second Vatican ...")
                self._read_note_entry(element, text)
                return
            self.document.blocks.append(Block(
                "closing", clean_inline_html(element), chapter_index=self.current_chapter,
                centred=is_centred(element) or len(text) < 60))
            return

        if self._read_chapter_marker(element, text):
            return
        if self._read_chapter_title_line(element, text):
            return
        if self._read_heading(element, text):
            return
        self._read_body_text(element, text)

    def _is_page_noise(self, element: Tag, text: str) -> bool:
        if not text or SITE_NOISE.search(text) or LANGUAGE_SELECTOR.match(text):
            return True
        # an on-page contents list: every word is a link to a #anchor
        return share_of_text_in_page_links(element) >= 0.9 and not NUMBERED_PARAGRAPH.match(text)

    # ---- masthead -----------------------------------------------------------
    def _read_masthead_line(self, element: Tag, text: str) -> bool:
        """Collect the ALL-CAPS lines at the very top. Returns True if this element was one."""
        lines = visible_lines(element)

        def is_all_caps(line: str) -> bool:
            letters = re.sub(r"[^A-Za-z]", "", line)
            return bool(letters) and letters.isupper() and len(line) < 120

        looks_like_chapter_start = bool(lines) and (FRONT_OR_BACK_HEADING.match(lines[0])
                                                    or CHAPTER_MARKER.match(lines[0]))
        if element.name == "h1" and not text.isupper():
            self.document.title = text                 # a mixed-case <h1> is the title
            return True
        if lines and all(is_all_caps(line) for line in lines) and not NUMBERED_PARAGRAPH.match(text) \
                and not looks_like_chapter_start:
            self.masthead_lines.extend(lines)
            return True
        self._finish_masthead()
        return False

    def _finish_masthead(self) -> None:
        if self.masthead_done:
            return
        self.masthead_done = True
        document = self.document
        above, title, subtitle = split_masthead(self.masthead_lines, self.likely_title)
        document.genre_lines = above
        document.subtitle = subtitle
        if title:
            document.title = to_title_case(title)
        if not document.title:
            document.title = self.page_title_without_date or "Untitled document"
        # "POPE PAUL VI" / "SAINT JOHN PAUL II" in the masthead beats the URL guess
        for line in above:
            pope = re.match(r"^(POPE|SAINT|BLESSED)\s+(.+)$", line.upper())
            if pope:
                document.issuer = to_title_case(pope.group(2))
                break

    # ---- chapters -----------------------------------------------------------
    def _read_chapter_marker(self, element: Tag, text: str) -> bool:
        """'CHAPTER ONE' (bold or plain), optionally with the title on the same line."""
        if element.name in ("blockquote", "li") or NUMBERED_PARAGRAPH.match(text) or len(text) > 260:
            return False
        match = CHAPTER_MARKER.match(text)
        if not match:
            return False
        label, number = chapter_marker_label(match.group(1), match.group(2))
        same_line_title = soften_capital_runs(match.group(3))
        same_line_title = re.sub(r"\s+-\s+", " – ", same_line_title).strip(" -–—")
        same_line_title = re.sub(r"(?<=[\w?!.,])\s+(?=[\"”](?:\s|\)|$))", "", same_line_title)   # no space before a closing quote
        self.open_chapter_label = label
        self.open_chapter_title_parts = [same_line_title] if same_line_title else []
        full_title = f"{label}: {same_line_title}" if same_line_title else label
        self.open_chapter = self._add_heading(1, html.escape(full_title), full_title, number)
        self.masthead_done = True
        return True

    def _read_chapter_title_line(self, element: Tag, text: str) -> bool:
        """ALL-CAPS lines directly after 'CHAPTER ONE' are that chapter's title."""
        if self.open_chapter is None:
            return False
        is_title_line = (text.isupper() and len(text) < 140 and not NUMBERED_PARAGRAPH.match(text)
                         and not ROMAN_HEADING_PREFIX.match(text) and element.name not in ("blockquote", "li"))
        if not is_title_line:
            self.open_chapter = None                    # something else follows: the title is complete
            return False
        self.open_chapter_title_parts.append(text)
        full_title = f"{self.open_chapter_label}: {tidy_heading_text(' '.join(self.open_chapter_title_parts))}"
        self.open_chapter.html = html.escape(full_title)
        self.document.chapter_titles[self.open_chapter.chapter_index] = full_title
        return True

    # ---- headings -----------------------------------------------------------
    def _read_heading(self, element: Tag, text: str) -> bool:
        if element.name in ("blockquote", "li") or len(text) >= 160:
            return False
        is_bold = share_of_text_inside(element, ("b", "strong")) >= 0.95
        italic_share = share_of_text_inside(element, ("i", "em"))
        is_italic = italic_share >= 0.95 or (is_bold and italic_share >= 0.6)    # "bold-italic" style
        has_number = bool(NUMBERED_PARAGRAPH.match(text))
        number_fits_sequence = has_number and \
            self.last_paragraph_number < int(NUMBERED_PARAGRAPH.match(text).group(1)) <= self.last_paragraph_number + 10

        looks_like_heading = (
            element.name in ("h1", "h2", "h3", "h4") or is_bold
            or (is_centred(element) and share_of_text_inside(element, ("b", "strong")) >= 0.8)
            or (is_italic and len(text) < 100 and not text.endswith((".", ";", ",", "!"))))
        # "2. The Weakness of International Politics": a short numbered line whose number cannot
        # be the next paragraph number is a section heading even if it is not bold.
        numbered_section = (has_number and not number_fits_sequence and len(text) < 90
                            and not text.rstrip().endswith((".", ";", ",", "”", '"')))
        if not ((looks_like_heading and (not has_number or is_bold or is_italic)) or numbered_section):
            return False

        self.open_chapter = None
        plain_title = tidy_heading_text(text)
        if is_italic and not has_number:
            level = 3                                              # italic or bold-italic: sub-section
        elif ROMAN_HEADING_PREFIX.match(text):
            level = 2                                              # "II. Conscience and truth"
        elif has_number:
            level = 1                                              # "1. The Global Climate Crisis"
        elif text.isupper() or FRONT_OR_BACK_HEADING.match(text):
            level = 1                                              # INTRODUCTION, CONCLUSION
        else:
            level = 2                                              # plain bold line: section
        if text == text.upper():
            heading_html = html.escape(plain_title)
        else:
            heading_html = clean_inline_html(element)
            heading_html = re.sub(r"</?strong>", "", heading_html)
            if level == 3:
                heading_html = re.sub(r"</?em>", "", heading_html)
            heading_html = heading_html.replace("<br>", " ")
        self._add_heading(level, heading_html, plain_title)
        return True

    def _add_heading(self, level: int, heading_html: str, plain_title: str, chapter_number: int | None = None) -> Block:
        self.heading_counter += 1
        block = Block("heading", heading_html, level=level, heading_id=f"h{self.heading_counter}",
                      chapter_index=self.current_chapter)
        if level == 1:
            self.document.chapter_titles.append(plain_title)
            self.document.chapter_numbers.append(chapter_number)
            self.current_chapter = len(self.document.chapter_titles) - 1
            block.chapter_index = self.current_chapter
        self.document.blocks.append(block)
        self.masthead_done = True
        return block

    # ---- ordinary text --------------------------------------------------------
    def _read_body_text(self, element: Tag, text: str) -> None:
        self.open_chapter = None
        self.masthead_done = True
        markup = clean_inline_html(element)
        chapter = self.current_chapter
        if element.name == "blockquote":
            self.document.blocks.append(Block("quote", markup, chapter_index=chapter))
            return
        if element.name == "li":
            self.document.blocks.append(Block("paragraph", "• " + markup, chapter_index=chapter))
            return
        number = 0
        match = NUMBERED_PARAGRAPH.match(collapse_whitespace(element.get_text(" ")))
        if match:
            candidate = int(match.group(1))
            if self.last_paragraph_number < candidate <= self.last_paragraph_number + 10:   # must follow the sequence
                number = self.last_paragraph_number = candidate
                markup = re.sub(r"^\s*(?:<strong>)?\d{1,3}\.(?:</strong>)?\s*", "", markup, count=1)
        self.document.blocks.append(Block("paragraph", markup, paragraph_number=number, chapter_index=chapter))

    # ---- notes written as ordinary paragraphs --------------------------------------
    def _read_note_entry(self, element: Tag, text: str) -> None:
        """Parse one paragraph of a notes section (see the formats in the module notes below).

        numbered       "12 Cf. Jn 3:5."    or "12. Cf. Jn 3:5."          (plain numbered list)
        supplementary  "(3) Cfr. S. Cyprianus ..."  under "Chapter II"   (numbers restart per chapter)
        """
        if NOTES_HEADING.match(text) and len(text) < 60:
            self.notes_mode = "supplementary" if text.lower().startswith("supplementary") else "numbered"
            self.supplementary_chapter = None
            self.last_note_number = 0
            return
        chapter_line = CHAPTER_LINE_IN_NOTES.match(text)
        if self.notes_mode == "supplementary" and chapter_line:
            word = chapter_line.group(1)
            self.supplementary_chapter = int(word) if word.isdigit() else roman_to_int(word.upper())
            self.last_note_number = 0
            return

        markup = clean_inline_html(element)
        if self.notes_mode == "supplementary":
            match = PARENTHESISED_NOTE.match(text)
            if match and self.supplementary_chapter is not None:
                number = int(match.group(1))
                key = f"s{self.supplementary_chapter}_{number}"
                body = re.sub(r"^\(\s*\d+\s*\*?\s*\)\s*", "", markup)
                self.last_note = Note(key, f"{number}*", body, chapter_number=self.supplementary_chapter)
                self.document.notes[key] = self.last_note
                self.last_note_number = number
                return
        else:
            match = NUMBERED_NOTE.match(text)
            if match:
                number = int(match.group(1))
                if self.last_note_number < number <= self.last_note_number + 3:   # next in sequence (allow small gaps)
                    body = re.sub(r"^\(?\d{1,3}\)?\s*[.,:]?\s+", "", markup)
                    self.last_note = Note(str(number), str(number), body)
                    self.document.notes[str(number)] = self.last_note
                    self.last_note_number = number
                    return
        if self.last_note is not None:                # a continuation line of the previous note
            self.last_note.html = tidy_inline_html(self.last_note.html + " " + markup)

    # ---- tidy-up ----------------------------------------------------------------
    def _promote_subheadings_if_no_sections(self) -> None:
        """Documents whose only sub-headings are italic (Laudate Deum): treat them as sections."""
        headings = [b for b in self.document.blocks if b.kind == "heading"]
        if headings and not any(b.level == 2 for b in headings):
            for block in headings:
                if block.level == 3:
                    block.level = 2


def read_document_structure(raw_html: str, url: str, reporter: Reporter) -> Document:
    """STAGE 2 entry point."""
    return DocumentReader(BeautifulSoup(raw_html, "lxml"), url, reporter).read()


# =============================================================================
# STAGE 3 - NOTES  (connect the marks in the text to the note texts)
# =============================================================================
# Notes appear in the text in several styles. Marks already made from
# <a name="_ftnref1"> anchors are left alone; otherwise we try, in order:
#     <sup>12</sup>      raised digits
#     (12)  or  (3*)     parenthesised numbers (Lumen Gentium; "*" = per-chapter note)
#     "love".12          plain digits after punctuation, counting up 1, 2, 3 ... in order

def link_notes_to_text(document: Document, reporter: Reporter) -> dict[str, int]:
    """Replace in-text note numbers by clickable marks. Returns statistics."""
    stats = {"notes": len(document.notes), "marks": 0}
    if not document.notes:
        return stats

    def count_marks() -> int:
        return sum(block.html.count('class="note-mark"') for block in document.blocks)

    text_blocks = [b for b in document.blocks if b.kind in ("paragraph", "quote")]
    already_linked = count_marks()
    reporter.detail(f"{len(document.notes)} notes found, {already_linked} marks already linked from anchors")

    # -- raised digits <sup>12</sup>
    def convert_sup(block: Block) -> None:
        def replace(match: re.Match) -> str:
            return note_mark_html(match.group(1), match.group(1)) if match.group(1) in document.notes else match.group(0)
        block.html = re.sub(r"<sup>\s*(\d{1,3})\s*</sup>", replace, block.html)
    for block in text_blocks:
        convert_sup(block)

    # -- parenthesised (12) and (3*)  - only if they clearly are note marks
    chapter_numbers = document.chapter_numbers

    def key_for(number: str, starred: bool, block: Block) -> str | None:
        if starred:
            chapter = chapter_numbers[block.chapter_index] if block.chapter_index < len(chapter_numbers) else None
            key = f"s{chapter}_{number}"
        else:
            key = number
        return key if key in document.notes else None

    paren_pattern = re.compile(r"\(\s*(\d{1,3})(\*?)\s*\)")
    paren_hits = sum(1 for b in text_blocks for m in paren_pattern.finditer(b.html)
                     if key_for(m.group(1), bool(m.group(2)), b))
    if paren_hits >= max(3, 0.3 * len(document.notes)):
        reporter.detail(f"parenthesised note marks like (12) detected ({paren_hits})")

        def convert_parentheses(block: Block) -> None:
            def replace(match: re.Match) -> str:
                key = key_for(match.group(1), bool(match.group(2)), block)
                return note_mark_html(key, document.notes[key].label) if key else match.group(0)
            block.html = paren_pattern.sub(replace, block.html)
        for block in text_blocks:
            convert_parentheses(block)

    # -- plain digits after punctuation, in strictly increasing order
    if count_marks() == 0:
        reporter.detail("no marks yet: looking for plain digits after punctuation, in order")
        expected = 1
        after_punctuation = re.compile(r"(?<=[.,;:!?”\"’')\]])(\d{1,3})(?=[\s<]|$)")
        for block in text_blocks:
            parts = re.split(r"(<[^>]+>)", block.html)           # never touch text inside tags
            for index, part in enumerate(parts):
                if part.startswith("<"):
                    continue

                def replace(match: re.Match) -> str:
                    nonlocal expected
                    if int(match.group(1)) == expected and str(expected) in document.notes:
                        expected += 1
                        return note_mark_html(match.group(1), match.group(1))
                    return match.group(0)
                parts[index] = after_punctuation.sub(replace, part)
            block.html = "".join(parts)

    stats["marks"] = count_marks()
    return stats


# =============================================================================
# STAGE 4 - LAYOUT  (one styled HTML document, ready for printing)
# =============================================================================

STYLE_SHEET = """
/* ---- page setup: size, margins, running header and page number ---- */
@page {
  size: __PAGE_SIZE__; margin: 18mm 15mm 20mm 15mm;
  @top-center { content: "__RUNNING_TITLE__"; font: italic 8pt __FONT__; color: __ACCENT__;
                border-bottom: .4pt solid __ACCENT__; width: 100%; padding-bottom: 2mm; }
  @bottom-center { content: counter(page); font: 9pt __FONT__; }
}
@page front { @top-center { content: none; border: 0; } @bottom-center { content: none; } }  /* no header on title/about/contents */
html { font: 10.5pt/1.5 __FONT__; color: #1a1a1a; hyphens: auto; }
body { margin: 0; }
.front { page: front; break-after: page; }

/* ---- title page ---- */
.title-page { text-align: center; padding-top: 38mm; }
.genre { letter-spacing: .18em; font-size: 8.5pt; text-transform: uppercase; line-height: 1.6; }
.title-page h1 { font-style: italic; font-weight: 400; font-size: 26pt; color: __ACCENT__; margin: 18mm 0 10mm; }
.dots { letter-spacing: 1.2em; margin: 0 0 22mm 1.2em; }
.subtitle { letter-spacing: .14em; font-size: 8.5pt; text-transform: uppercase; }

/* ---- about page ---- */
.about h2, .toc h2, .notes h2 { font-style: italic; font-weight: 400; color: __ACCENT__; font-size: 15pt; }
.about h3 { letter-spacing: .15em; font-size: 8pt; text-transform: uppercase; color: __ACCENT__;
            border-bottom: .4pt solid __ACCENT__; padding-bottom: 1mm; margin: 8mm 0 3mm; }
.about dl { display: grid; grid-template-columns: 28mm 1fr; gap: 1.5mm 4mm; margin: 0; font-size: 9.5pt; }
.about dt { font-weight: 600; } .about dd { margin: 0; overflow-wrap: anywhere; }

/* ---- contents page: title ........ page number ---- */
.toc ul { list-style: none; margin: 0; padding: 0; font-size: 10pt; }
.toc li { margin: 1mm 0; display: flex; align-items: baseline; gap: 1mm; }
.toc .title { flex: 0 1 auto; }
.toc .leader { flex: 1 1 auto; border-bottom: 1px dotted #777; transform: translateY(-.15em); min-width: 4mm; }
.toc .page { flex: 0 0 auto; min-width: 6mm; text-align: right; }
.toc li.level2 { padding-left: 5mm; font-size: 9.5pt; } .toc li.level3 { padding-left: 10mm; font-size: 9pt; }
.toc a { color: inherit; text-decoration: none; }

/* ---- body text ---- */
main h2 { font-size: 15pt; font-weight: 400; color: __ACCENT__; margin: 0 0 6mm; text-align: center;
          break-before: page; break-after: avoid; }
main h2.first-chapter { break-before: auto; }
main h3 { font-size: 11.5pt; font-weight: 700; margin: 6mm 0 2mm; break-after: avoid; }
main h4 { font-style: italic; font-size: 10.5pt; font-weight: 400; margin: 4mm 0 1mm; break-after: avoid; }
main p { margin: 0 0 2.2mm; text-align: justify; orphans: 3; widows: 3; }
.paragraph-number { font-weight: 700; }
blockquote { margin: 2mm 0 3mm 6mm; font-size: 10pt; } blockquote p { text-indent: 0; }
.closing { margin-top: 8mm; text-align: center; break-inside: avoid; }
.closing p { text-align: center; margin: 0 0 1.5mm; } .closing p.left { text-align: left; font-style: italic; }
a, sup.note-mark a, .notes a.back { color: __ACCENT__; text-decoration: none; }
sup.note-mark { font-size: 7pt; line-height: 0; }

/* ---- notes at the end ---- */
.notes { break-before: page; font-size: 9pt; line-height: 1.4; }
.notes h3 { font-size: 10pt; color: __ACCENT__; margin: 5mm 0 2mm; }
.notes ol { padding: 0; margin: 0; list-style: none; }
.notes li { margin: 0 0 1.4mm; padding-left: 6mm; text-indent: -6mm; }
.notes .label { display: inline-block; width: 6mm; text-indent: 0; color: __ACCENT__; font-weight: 600; }
"""


def choose_contents_depth(document: Document, requested: str) -> int:
    """'auto' -> 2 levels, or 3 when the document has few headings (e.g. Veritatis Splendor)."""
    if requested != "auto":
        return int(requested)
    two_level_entries = sum(1 for b in document.blocks if b.kind == "heading" and b.level <= 2)
    return 2 if two_level_entries >= AUTO_TOC_DEEP_BELOW else 3


def build_front_matter(document: Document, page_numbers: dict[str, int] | None, page_size: str,
                       contents_depth: int) -> str:
    """Title page + About page + Contents page."""
    genre_html = "<br>".join(html.escape(line) for line in document.genre_lines)
    if document.issuer and document.issuer.upper() not in genre_html.upper():
        prefix = genre_html + "<br>" if genre_html else "OF THE HOLY FATHER<br>"
        genre_html = prefix + html.escape(document.issuer.upper())

    contents_rows = []
    for block in document.blocks:
        if block.kind == "heading" and block.level <= contents_depth:
            page = f'<span class="page">{page_numbers[block.heading_id]}</span>' \
                if page_numbers and block.heading_id in page_numbers else ""
            leader = '<span class="leader"></span>' if page else ""
            contents_rows.append(
                f'<li class="level{block.level}"><span class="title">'
                f'<a href="#{block.heading_id}">{block.html}</a></span>{leader}{page}</li>')

    about_rows = [("Title", html.escape(document.title))]
    if document.issuer:
        about_rows.append(("Issued By", html.escape(document.issuer)))
    if document.date:
        about_rows.append(("Promulgated", html.escape(document.date)))
    about_rows.append(("Original", f'<a href="{html.escape(document.url)}">{html.escape(document.url)}</a>'))
    about_list = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in about_rows)

    return f"""
<section class="front title-page">
  <div class="genre">{genre_html}</div><h1>{html.escape(document.title)}</h1><div class="dots">· · ·</div>
  <div class="subtitle">{html.escape(document.subtitle).replace(chr(10), "<br>")}</div></section>
<section class="front about"><h2>About this edition</h2>
  <p>This edition preserves the wording of the Vatican's HTML text and rebuilds its structure
  for reading: clear headings, a contents page, notes kept together, and typesetting for print.</p>
  <h3>Document</h3><dl>{about_list}</dl>
  <h3>Edition</h3><dl><dt>Format</dt><dd>{PAGE_SIZE_LABELS[page_size]} reader PDF</dd></dl></section>
<section class="front toc"><h2>Contents</h2><ul>{"".join(contents_rows)}</ul></section>"""


def build_body_and_notes(document: Document) -> str:
    """The main text, then the Notes section."""
    parts: list[str] = []
    seen_first_chapter = False
    closing_open = False
    for block in document.blocks:
        if closing_open and block.kind != "closing":
            parts.append("</div>")
            closing_open = False
        if block.kind == "heading":
            css_class = ' class="first-chapter"' if (block.level == 1 and not seen_first_chapter) else ""
            seen_first_chapter = seen_first_chapter or block.level == 1
            tag = f"h{block.level + 1}"
            parts.append(f'<{tag} id="{block.heading_id}"{css_class}>{block.html}</{tag}>')
        elif block.kind == "quote":
            parts.append(f"<blockquote><p>{block.html}</p></blockquote>")
        elif block.kind == "closing":
            if not closing_open:
                parts.append('<div class="closing">')
                closing_open = True
            parts.append(f'<p{"" if block.centred else " class=left"}>{block.html}</p>')
        else:
            number = f'<span class="paragraph-number">{block.paragraph_number}.</span> ' if block.paragraph_number else ""
            parts.append(f"<p>{number}{block.html}</p>")
    if closing_open:
        parts.append("</div>")
    return f"<main>{''.join(parts)}</main>{build_notes_section(document)}"


def build_notes_section(document: Document) -> str:
    if not document.notes:
        return ""
    # a note belongs to the chapter where it is first cited
    first_cited_in: dict[str, int] = {}
    for block in document.blocks:
        for key in re.findall(r'id="m([^"]+)"', block.html):
            first_cited_in.setdefault(key, block.chapter_index)

    def sort_key(note: Note) -> tuple:
        return (1, note.chapter_number or 0, int(note.label.rstrip("*"))) if note.key.startswith("s") \
            else (0, 0, int(note.key))

    groups: dict[int, list[Note]] = {}
    for note in sorted(document.notes.values(), key=sort_key):
        groups.setdefault(first_cited_in.get(note.key, 0), []).append(note)

    sections = []
    for chapter_index in sorted(groups):
        if len(groups) > 1 and document.chapter_titles[chapter_index]:
            sections.append(f"<h3>{html.escape(document.chapter_titles[chapter_index])}</h3>")
        items = "".join(
            f'<li id="n{n.key}"><span class="label">{n.label}</span>{n.html} <a class="back" href="#m{n.key}">↩</a></li>'
            for n in groups[chapter_index])
        sections.append(f"<ol>{items}</ol>")
    return f'<section class="notes"><h2>Notes</h2>{"".join(sections)}</section>'


def assemble_html_page(document: Document, front_matter: str, body: str, page_size: str) -> str:
    style = (STYLE_SHEET.replace("__PAGE_SIZE__", PAGE_SIZES[page_size]).replace("__ACCENT__", ACCENT_COLOUR)
             .replace("__FONT__", BODY_FONT_STACK).replace("__RUNNING_TITLE__", document.title.replace('"', "'")))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(document.title)}</title>'
            f"<style>{style}</style></head><body>{front_matter}{body}</body></html>")


# =============================================================================
# STAGE 5 - PDF  (Chromium prints the HTML; three passes give the contents page numbers)
# =============================================================================

def print_html_to_pdf(browser, html_text: str, output_path: Path) -> None:
    page = browser.new_page()
    page.set_content(html_text, wait_until="load")
    try:
        page.pdf(path=str(output_path), prefer_css_page_size=True, print_background=True,
                 outline=True, tagged=True)                    # bookmarks + accessible tags
    except TypeError:                                          # older Playwright without these options
        page.pdf(path=str(output_path), prefer_css_page_size=True, print_background=True)
    page.close()


def letters_only(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find_heading_pages(pdf_path: Path, first_body_page: int, document: Document) -> dict[str, int]:
    """Which page (1-based) does each heading land on?  Search page text in reading order."""
    from pypdf import PdfReader
    page_texts = [letters_only(page.extract_text() or "") for page in PdfReader(str(pdf_path)).pages]
    found: dict[str, int] = {}
    search_from = first_body_page
    for block in document.blocks:
        if block.kind != "heading":
            continue
        wanted = letters_only(re.sub(r"<[^>]+>", "", html.unescape(block.html)))
        for index in range(search_from, len(page_texts)):
            if wanted and wanted in page_texts[index]:
                found[block.heading_id] = index + 1
                search_from = index
                break
    return found


def render_pdf(document: Document, body: str, output_path: Path, page_size: str, contents_depth: int,
               with_page_numbers: bool, reporter: Reporter, html_copy_path: Path | None = None) -> None:
    from playwright.sync_api import sync_playwright
    plain_front = build_front_matter(document, None, page_size, contents_depth)

    if html_copy_path:
        html_copy_path.write_text(assemble_html_page(document, plain_front, body, page_size), encoding="utf-8")
        reporter.detail(f"print-ready HTML saved: {html_copy_path}")

    with sync_playwright() as playwright, tempfile.TemporaryDirectory() as scratch_dir:
        scratch = Path(scratch_dir)
        browser = playwright.chromium.launch()
        page_numbers = None
        if with_page_numbers:
            try:
                from pypdf import PdfReader
                reporter.info("      pass 1/3: measuring the front matter")
                print_html_to_pdf(browser, assemble_html_page(document, plain_front, "", page_size), scratch / "front.pdf")
                first_body_page = len(PdfReader(str(scratch / "front.pdf")).pages)
                reporter.info("      pass 2/3: finding where each heading lands")
                print_html_to_pdf(browser, assemble_html_page(document, plain_front, body, page_size), scratch / "draft.pdf")
                page_numbers = find_heading_pages(scratch / "draft.pdf", first_body_page, document)
                reporter.detail(f"{len(page_numbers)} headings located")
            except ImportError:
                reporter.warn("pypdf is not installed: the contents page will have no page numbers (pip install pypdf)")
        reporter.info("      pass 3/3: writing the final PDF" if with_page_numbers else "      writing the PDF")
        final_front = build_front_matter(document, page_numbers, page_size, contents_depth)
        print_html_to_pdf(browser, assemble_html_page(document, final_front, body, page_size), output_path)
        browser.close()


# =============================================================================
# PIPELINE + COMMAND LINE
# =============================================================================

def file_stem_for(source: str, url: str) -> str:
    stem = Path(urlparse(url).path).stem if urlparse(url).scheme else Path(source).stem
    return re.sub(r"[^A-Za-z0-9_-]+", "-", stem or "document").strip("-")


def convert_one_document(source: str, options: argparse.Namespace, reporter: Reporter) -> Path | None:
    """Run all five stages for one URL or saved file."""
    total_stages = 3 if options.dump_json else 5
    saved_file = Path(source)

    with reporter.stage(1, total_stages, "Load page") as stage:
        if saved_file.exists():
            raw_html, remembered_url = read_saved_page(saved_file)
            url = options.url or remembered_url or saved_file.name
            stage.summary = f"read {saved_file.name} ({len(raw_html) // 1024} KB)"
        else:
            raw_html, url = download_page(source, options.browser, reporter), source
            stage.summary = f"downloaded {len(raw_html) // 1024} KB"
        if raw_html.count("\ufffd") > 5:
            reporter.warn(f"{raw_html.count(chr(0xfffd))} undecodable characters in the source; "
                          "try --browser or save the page again")

    with reporter.stage(2, total_stages, "Read structure") as stage:
        document = read_document_structure(raw_html, url, reporter)
        paragraphs = sum(1 for b in document.blocks if b.kind == "paragraph")
        headings = sum(1 for b in document.blocks if b.kind == "heading")
        if not paragraphs:
            raise RuntimeError("no body paragraphs found - the page layout may need a new rule in DocumentReader")
        stage.summary = f"'{document.title}': {paragraphs} paragraphs, {headings} headings, {len(document.notes)} notes"
        reporter.detail(f"issuer={document.issuer!r} date={document.date!r} subtitle={document.subtitle!r}")

    with reporter.stage(3, total_stages, "Link footnotes") as stage:
        stats = link_notes_to_text(document, reporter)
        stage.summary = f"{stats['marks']} marks linked to {stats['notes']} notes" if stats["notes"] else "no footnotes"
        if stats["notes"] and stats["marks"] == 0:
            reporter.warn("notes were found but no marks in the text; notes will appear only at the end")

    options.output_dir.mkdir(parents=True, exist_ok=True)
    stem = file_stem_for(source, url)
    if options.dump_json:
        json_path = options.output_dir / f"{stem}.json"
        json_path.write_text(json.dumps(asdict(document), ensure_ascii=False, indent=1), encoding="utf-8")
        reporter.info(f"\nParsed structure written to {json_path}")
        return json_path

    contents_depth = choose_contents_depth(document, options.toc_depth)
    with reporter.stage(4, total_stages, "Build layout") as stage:
        body_html = build_body_and_notes(document)
        stage.summary = f"{PAGE_SIZE_LABELS[options.size]} pages, contents depth {contents_depth}"

    pdf_path = options.output_dir / f"{stem}-{options.size}.pdf"
    html_path = options.output_dir / f"{stem}-{options.size}.html" if options.save_html else None
    with reporter.stage(5, total_stages, "Write PDF") as stage:
        render_pdf(document, body_html, pdf_path, options.size, contents_depth,
                   not options.no_page_numbers, reporter, html_path)
        stage.summary = str(pdf_path)
    return pdf_path


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="*", metavar="URL_OR_FILE",
                        help="vatican.va URLs, or paths to saved .html / .mhtml files")
    parser.add_argument("--list", metavar="FILE", help="text file with one URL per line (# starts a comment)")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("pdfs"), help="where to write results (default: pdfs/)")
    parser.add_argument("--size", choices=PAGE_SIZES, default=DEFAULT_PAGE_SIZE, help="page size (default: half-letter, 5.5 x 8.5 in)")
    parser.add_argument("--toc-depth", choices=["auto", "1", "2", "3"], default="auto",
                        help="heading levels in the contents page (default: auto)")
    parser.add_argument("--url", help="original URL, when reading a saved file that does not record it")
    parser.add_argument("--browser", action="store_true", help="download with Chromium instead of plain HTTP")
    parser.add_argument("--no-page-numbers", action="store_true", help="skip the 3-pass render (faster, no contents page numbers)")
    parser.add_argument("--dump-json", action="store_true", help="stop after stage 3 and write the parsed structure as JSON")
    parser.add_argument("--save-html", action="store_true", help="also keep the print-ready HTML next to the PDF")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="print extra detail about what was detected")
    parser.add_argument("--file", type=Path, help=argparse.SUPPRESS)       # old spelling, still accepted
    return parser


def main() -> int:
    options = build_argument_parser().parse_args()
    sources = list(options.sources)
    if options.list:
        sources += [line.strip() for line in Path(options.list).read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
    if options.file:
        sources.append(str(options.file))
    if not sources:
        build_argument_parser().error("give at least one URL or file (or --list FILE)")

    reporter = Reporter(options.quiet, options.verbose)
    failures = 0
    for number, source in enumerate(sources, 1):
        reporter.info(f"\n{'=' * 70}\nDocument {number}/{len(sources)}: {source}\n{'=' * 70}")
        try:
            convert_one_document(source, options, reporter)
        except Exception as error:                      # keep going when converting several documents
            failures += 1
            reporter.warn(f"{source}: {error}")
            if options.verbose:
                raise
        reporter.checklist(f"Summary for document {number}/{len(sources)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
