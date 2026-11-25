# vatican-to-pdf

Turn a document from [vatican.va](https://www.vatican.va) (encyclicals, exhortations,
council documents ...) into a clean, print-ready PDF: title page, "About this edition",
contents with page numbers, numbered paragraphs, and linked notes.
Default page size is **half-letter (5.5 × 8.5 in)**; `--size a5|a4|a6|letter` also work.

## Quick start

```bash
pip install -r requirements.txt
playwright install chromium

python vatican_to_pdf.py https://www.vatican.va/content/francesco/en/encyclicals/documents/papa-francesco_20150524_enciclica-laudato-si.html
python vatican_to_pdf.py saved_page.mhtml          # a page saved from your browser
python vatican_to_pdf.py --list urls.txt -o pdfs   # many documents
```

Useful options: `-v` (explain what was detected), `-q` (quiet), `--dump-json`
(show the parsed structure, no PDF), `--save-html` (keep the print-ready HTML),
`--browser` (download with Chromium if plain download is refused), `--no-page-numbers` (faster).

## How it works

| Stage | What happens | Where in the code |
|---|---|---|
| 1 Load | download, or read a saved `.html` / `.mhtml` | `download_page`, `read_saved_page` |
| 2 Read structure | rebuild masthead, chapters, headings, paragraphs, notes | `DocumentReader` |
| 3 Link footnotes | connect marks in the text to note texts | `link_notes_to_text` |
| 4 Build layout | one styled HTML document | `build_front_matter`, `build_body_and_notes`, `STYLE_SHEET` |
| 5 Write PDF | Chromium prints it, 3 passes for contents page numbers | `render_pdf` |

Each stage prints `[n/5] ... ✓` as it runs, with a progress bar while reading text and a
checklist at the end.

## Contributing

Vatican pages were written by hand over many decades, so every new document can reveal a
layout we have not seen. The workflow:

1. Save the page (`Save as > Webpage, Single File` gives `.mhtml`) and run it with `-v`
   or `--dump-json` to see what went wrong.
2. Add a **small** HTML fixture imitating that layout to `tests/test_parsing.py`, and write
   the test you wish passed.
3. Change `DocumentReader` (stage 2) or `link_notes_to_text` (stage 3) until it does.
4. Run `pytest` (or `python tests/test_parsing.py`) - all existing layouts must still pass.

Currently covered layouts: Laudato Si', Laudate Deum, Magnifica Humanitas, Lumen Gentium,
Veritatis Splendor.

## Known limits

* Notes are collected at the end of the document (Chromium cannot do page-bottom footnotes).
* Only English-language pages have been tested.
* Fonts: install *Libertinus Serif* for the closest match to the reference edition;
  otherwise Georgia / DejaVu Serif is used.
