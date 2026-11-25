"""
Tests for stages 1-3 (loading, reading structure, linking notes).

Each fixture below is a SMALL imitation of one real vatican.va page layout, so a
contributor can see exactly which layout a rule exists for. Run with:

    pip install pytest
    pytest                       # or:  python tests/test_parsing.py

To add support for a new document: copy a fixture, shape it like the new page,
write the test you wish passed, then change DocumentReader until it does.
"""
import html as html_module
import quopri
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vatican_to_pdf import (Reporter, link_notes_to_text, read_document_structure,  # noqa: E402
                            read_saved_page, soften_capital_runs, to_title_case)

QUIET = Reporter(quiet=True)


def parse(html: str, url: str):
    document = read_document_structure(html, url, QUIET)
    link_notes_to_text(document, QUIET)
    return document


def headings(document, level=None):
    """Heading texts (HTML-unescaped so quotes read normally)."""
    return [html_module.unescape(b.html) for b in document.blocks
            if b.kind == "heading" and (level is None or b.level == level)]


# ---------------------------------------------------------------------------
# Laudato Si' (2015): "CHAPTER ONE" is PLAIN text, its title is bold, notes are anchors
# ---------------------------------------------------------------------------
LAUDATO_SI = """<html><head><title>Laudato si' (24 May 2015)</title></head><body><div class="documento">
<p align="center"><b>ENCYCLICAL LETTER<br><i>LAUDATO SI’</i><br>OF THE HOLY FATHER<br>FRANCIS<br>ON CARE FOR OUR COMMON HOME</b></p>
<p>1. “LAUDATO SI’, mi’ Signore”.<a href="#_ftn1" name="_ftnref1">[1]</a></p>
<p><i>Nothing in this world is indifferent to us</i></p>
<p>2. More than fifty years ago.</p>
<p align="center">CHAPTER ONE</p>
<p align="center"><b>WHAT IS HAPPENING TO OUR COMMON HOME</b></p>
<p><b>I. POLLUTION AND CLIMATE CHANGE</b></p>
<p>3. Some forms of pollution.</p>
<p><b>II. THE ISSUE OF WATER</b></p>
<p>4. Other indicators.</p>
<p>CHAPTER TWO</p><p><b>THE GOSPEL OF CREATION</b></p>
<p>5. Why include.</p>
<p>Given in Rome, at Saint Peter’s, on 24 May, in the year 2015.</p>
<p align="center">Franciscus</p>
<h2>The Holy See</h2>
<p><a href="#_ftnref1" name="_ftn1">[1]</a> Canticle.</p>
</div></body></html>"""


def test_laudato_si_title_page_and_chapters():
    doc = parse(LAUDATO_SI, "https://www.vatican.va/content/francesco/en/encyclicals/documents/papa-francesco_20150524_enciclica-laudato-si.html")
    assert doc.title == "Laudato Si’"
    assert doc.genre_lines == ["ENCYCLICAL LETTER", "OF THE HOLY FATHER", "FRANCIS"]
    assert doc.subtitle == "ON CARE FOR OUR COMMON HOME"
    assert doc.issuer == "Francis" and doc.date == "24 May 2015"
    assert headings(doc, 1) == ["Chapter 1: What Is Happening to Our Common Home", "Chapter 2: The Gospel of Creation"]
    assert headings(doc, 2) == ["I. Pollution and Climate Change", "II. The Issue of Water"]


def test_laudato_si_footer_and_notes():
    doc = parse(LAUDATO_SI, "https://www.vatican.va/x/papa-francesco_20150524_x.html")
    assert not any("Holy See" in b.html for b in doc.blocks)            # site footer removed
    assert doc.notes["1"].html == "Canticle."
    assert any('class="note-mark"' in b.html for b in doc.blocks)       # anchor marks kept


# ---------------------------------------------------------------------------
# Laudate Deum (2023): top-level sections are numbered, bold; sub-headings italic
# ---------------------------------------------------------------------------
LAUDATE_DEUM = """<html><head><title>Laudate Deum</title></head><body><div class="documento">
<p>APOSTOLIC EXHORTATION<br><b><i>LAUDATE DEUM</i></b><br>OF THE HOLY FATHER<br><b>FRANCIS</b><br>TO ALL PEOPLE OF GOOD WILL<br>ON THE CLIMATE CRISIS</p>
<p>1. “Praise God for all his creatures”.</p><p>2. Eight years have passed.</p>
<p>3. This is a global social issue.<a href="#_ftn1" name="_ftnref1">[1]</a></p><p>4. The reflection.</p>
<p><b>1. The Global Climate Crisis</b></p><p>5. Despite all attempts.</p>
<p><i>Resistance and confusion</i></p><p>6. In recent years.</p>
<p>2. A Growing Technocratic Paradigm</p><p>7. In Laudato Si’.</p>
<p>Given in Rome, at the Basilica, in the year 2023.</p><p align="center">FRANCIS</p><p>The Holy See</p>
<p><a href="#_ftnref1" name="_ftn1">[1]</a> UNITED STATES CONFERENCE.</p>
</div></body></html>"""


def test_laudate_deum_numbered_sections_are_headings():
    doc = parse(LAUDATE_DEUM, "https://www.vatican.va/content/francesco/en/apost_exhortations/documents/20231004-laudate-deum.html")
    assert headings(doc, 1) == ["1. The Global Climate Crisis", "2. A Growing Technocratic Paradigm"]
    assert headings(doc, 2) == ["Resistance and confusion"]            # italic sub-heading promoted to a section
    assert doc.title == "Laudate Deum" and doc.date == "4 October 2023"
    assert [b.paragraph_number for b in doc.blocks if b.kind == "paragraph"] == [1, 2, 3, 4, 5, 6, 7]


# ---------------------------------------------------------------------------
# Magnifica Humanitas (2026): on-page contents list, 2-line chapter marker, bold-italic sub-headings
# ---------------------------------------------------------------------------
MAGNIFICA = """<html><head><title>Encyclical Letter of His Holiness Leo XIV Magnifica Humanitas (15 May 2026)</title></head><body><div class="documento">
<p>ENCYCLICAL LETTER<br><b><i>MAGNIFICA HUMANITAS</i></b><br>OF HIS HOLINESS<br><b>POPE LEO XIV</b><br>ON SAFEGUARDING THE HUMAN PERSON<br>IN THE TIME OF ARTIFICIAL INTELLIGENCE</p>
<p>[<a href="https://x.va/m"><b>Multimedia</b></a>]</p><p>___________</p>
<p><b><a href="#INTRODUCTION_">INTRODUCTION</a></b></p>
<p><a href="#The_res">The <i>res novae</i> of our time</a><br><a href="#Two">Two biblical images</a></p>
<p><b><a name="INTRODUCTION_"></a>INTRODUCTION</b></p>
<p>1. Humanity is facing a “pivotal choice”. <a href="#_ftn1" name="_ftnref1">[1]</a> Next ( <i>rerum novarum</i>) ok.</p>
<p><b>The <i>res novae</i> of our time</b></p>
<p>2. While Leo XIII spoke.<a href="#_ftn2" name="_ftnref2">[2]</a></p>
<p>3. Integral.<a href="#_ftn3" name="_ftnref3">[3]</a> [10] stray.</p>
<p><b>CHAPTER ONE<br>A DYNAMIC APPROACH FAITHFUL TO THE GOSPEL</b></p>
<p>4. In this first chapter.</p>
<p><b><i>The wisdom of the word of God</i></b></p><p>5. The Church regards all.</p>
<p><b>CHAPTER THREE</b></p><p><b>TECHNOLOGY AND DOMINANCE.</b></p>
<p><b>THE GRANDEUR OF HUMANITY<br>IN LIGHT OF THE PROMISES OF AI</b></p><p>6. Having recalled.</p>
<p><b>CONCLUSION</b></p><p>7. Final words.</p>
<p>Given in Rome, at Saint Peter’s, on 15 May 2026</p><p>LEO PP. XIV</p>
<div><p><a href="#_ftnref1" name="_ftn1">[1]</a> <i>Gaudium et Spes</i>, 22.</p></div>
<div><p><a href="#_ftnref2" name="_ftn2">[2]</a> Cf. <a href="http://vatican.va/x">Rerum Novarum</a> 1.</p></div>
<div><p><a href="#_ftnref3" name="_ftn3">[3]</a> Paul VI, <i>Populorum Progressio</i> 14.</p></div>
</div></body></html>"""


def test_magnifica_masthead_and_issuer():
    doc = parse(MAGNIFICA, "https://www.vatican.va/content/leo-xiv/en/encyclicals/documents/20260515-magnifica-humanitas.html")
    assert doc.title == "Magnifica Humanitas"
    assert doc.genre_lines == ["ENCYCLICAL LETTER", "OF HIS HOLINESS", "POPE LEO XIV"]
    assert doc.subtitle == "ON SAFEGUARDING THE HUMAN PERSON\nIN THE TIME OF ARTIFICIAL INTELLIGENCE"
    assert doc.issuer == "Leo XIV"


def test_magnifica_structure():
    doc = parse(MAGNIFICA, "https://www.vatican.va/x/20260515-magnifica-humanitas.html")
    assert headings(doc, 1) == ["Introduction", "Chapter 1: A Dynamic Approach Faithful to the Gospel",
                                "Chapter 3: Technology and Dominance. The Grandeur of Humanity in Light of the Promises of AI",
                                "Conclusion"]                         # on-page contents list NOT duplicated
    assert headings(doc, 3) == ["The wisdom of the word of God"]
    assert "[10]" in [b for b in doc.blocks if b.paragraph_number == 3][0].html   # stray [10] is not a note mark
    assert len(doc.notes) == 3


# ---------------------------------------------------------------------------
# Lumen Gentium (1964): language selector, "(12)" marks, NOTES list + per-chapter "(1*)" notes
# ---------------------------------------------------------------------------
LUMEN_GENTIUM = """<html><head><title>Lumen gentium</title></head><body>
<p>[<a href="x">AR</a> - <a href="x">BE</a> - <a href="x">CS</a> - <a href="x">EN</a> - <a href="x">ZH</a>]</p>
<p>DOGMATIC CONSTITUTION ON THE CHURCH<br><b><i>LUMEN GENTIUM</i></b><br>SOLEMNLY PROMULGATED BY HIS HOLINESS<br>POPE PAUL VI<br>ON NOVEMBER 21, 1964</p>
<p><b>CHAPTER I</b></p><p><b>THE MYSTERY OF THE CHURCH</b></p>
<p>1. Christ is the Light of nations, to every creature,(1) to bring the light.</p>
<p>2. The eternal Father created.(2) Covenant.(1*) And "from Abel,"(2*) will be gathered.</p>
<p><b>CHAPTER II</b></p><p><b>ON THE PEOPLE OF GOD</b></p>
<p>3. At all times God welcomes (3) whosoever fears Him. (1*)</p>
<p>CHAPTER VII</p><p><b>THE ESCHATOLOGICAL NATURE OF THE PILGRIM CHURCH<br>AND ITS UNION WITH THE CHURCH IN HEAVEN</b></p>
<p>4. The Church, to which we are called.(4)</p>
<p align="center">NOTES</p>
<p align="center">1 Cf. Mk. 16:15.</p><p align="center">2 Col. 1:15.</p><p align="center">3 Rom. 8:29.</p><p align="center">4 Cf. Eph. 1:4-5.</p>
<p align="center">SUPPLEMENTARY NOTES (*)</p>
<p align="center">Chapter I</p>
<p>(1) Cfr. S. Cyprianus, Epist. 64, 4: PL 3, 1017.</p><p>(2) Cfr. S. Gregorius M., Hom in Evang.</p>
<p align="center">Chapter II</p><p>(1) Cfr. S. Cyprianus, Epist. 69, 6.</p>
</body></html>"""


def test_lumen_gentium_masthead_ignores_language_selector():
    doc = parse(LUMEN_GENTIUM, "https://www.vatican.va/archive/hist_councils/ii_vatican_council/documents/vat-ii_const_19641121_lumen-gentium_en.html")
    assert doc.title == "Lumen Gentium"
    assert doc.genre_lines[0] == "DOGMATIC CONSTITUTION ON THE CHURCH"
    assert doc.issuer == "Paul VI" and doc.date == "21 November 1964"
    assert headings(doc, 1)[0] == "Chapter 1: The Mystery of the Church"
    assert headings(doc, 1)[2].startswith("Chapter 7: The Eschatological Nature of the Pilgrim Church")


def test_lumen_gentium_two_kinds_of_notes():
    doc = parse(LUMEN_GENTIUM, "https://www.vatican.va/x/vat-ii_const_19641121_lumen-gentium_en.html")
    assert doc.notes["2"].html == "Col. 1:15."
    assert doc.notes["s1_2"].label == "2*" and "Gregorius" in doc.notes["s1_2"].html
    assert doc.notes["s2_1"].html.startswith("Cfr. S. Cyprianus, Epist. 69")         # numbering restarts per chapter
    body = " ".join(b.html for b in doc.blocks)
    assert 'href="#n1"' in body and 'href="#ns1_1"' in body and 'href="#ns2_1"' in body
    assert "(1)" not in body and "(1*)" not in body


# ---------------------------------------------------------------------------
# Veritatis Splendor (1993): masthead title is the 2nd line, long chapter headings,
# plain "1."-style notes after the signature, plain digits as note marks
# ---------------------------------------------------------------------------
VERITATIS_SPLENDOR = """<html><head><title>Veritatis Splendor (6 August 1993)</title></head><body><div class="documento">
<p>IOANNES PAULUS PP. II</p><p><b><i>VERITATIS SPLENDOR</i></b></p>
<p><b><i>Blessing</i></b></p><p><i>Venerable Brothers in the Episcopate,<br>Health and the Apostolic Blessing!</i></p>
<p>The splendour of truth shines forth (cf. <i>Gen</i> 1:26).</p>
<p><b>INTRODUCTION</b></p><p><b><i>Jesus Christ, the true light that enlightens everyone</i></b></p>
<p>1. Called to salvation, "the true light" (<i>Jn</i> 1:9).</p>
<p>2. Christ unfolds his noble calling by revealing the mystery of the Father and the Father's love".1</p>
<p>Jesus Christ, the "light of the nations" (cf. <i>Mk</i> 16:15).2 Hence the Church, as the People of God among the nations,3 offers an answer.</p>
<p><b>CHAPTER I</b> - <b>"TEACHER, WHAT GOOD MUST I DO...? " <i>(Mt 19:16)</i> - Christ and the answer to the question about morality</b></p>
<p><b><i>"Someone came to him..."</i> (<i>Mt</i> 19:16)</b></p><p>3. The dialogue of Jesus with the rich young man.</p>
<p><b>CHAPTER II</b> - <b>"DO NOT BE CONFORMED TO THIS WORLD</b> " <b><i>(Rom 12:2)</i></b> - <b>The Church and the discernment</b></p>
<p><b><i>Teaching what befits sound doctrine</i></b></p><p>4. Our meditation.</p>
<p><b>I. Freedom and Law</b></p><p><b><i>"Of the tree of the knowledge"</i></b></p><p>5. In the Book of Genesis.</p>
<p><b>CONCLUSION</b></p><p>6. Final words.</p>
<p>Given in Rome, at Saint Peter's, on 6 August, in the year 1993, the fifteenth of my Pontificate.</p><p>JOHN PAUL II</p>
<p>1. Second Vatican Ecumenical Council, Pastoral Constitution Gaudium et Spes 22.</p>
<p>2. Cf. Mk 16:15.</p><p>3. Cf. Ad Gentes 1.</p>
</div></body></html>"""


def test_veritatis_splendor_title_and_chapters():
    doc = parse(VERITATIS_SPLENDOR, "https://www.vatican.va/content/john-paul-ii/en/encyclicals/documents/hf_jp-ii_enc_06081993_veritatis-splendor.html")
    assert doc.title == "Veritatis Splendor"                       # not "Ioannes Paulus Pp. II"
    assert doc.genre_lines == ["IOANNES PAULUS PP. II"]
    assert doc.issuer == "John Paul II" and doc.date == "6 August 1993"
    chapters = headings(doc, 1)
    assert chapters[1].startswith('Chapter 1: "Teacher, What Good Must I Do...?"') and "Christ and the answer" in chapters[1]
    assert chapters[2].startswith('Chapter 2: "Do Not Be Conformed to This World')
    assert "I. Freedom and Law" in headings(doc, 2)
    assert '"Someone came to him..." (Mt 19:16)' in headings(doc, 3)     # bold + mostly italic = sub-section


def test_veritatis_splendor_salutation_is_not_a_heading():
    doc = parse(VERITATIS_SPLENDOR, "https://www.vatican.va/x/hf_jp-ii_enc_06081993_veritatis-splendor.html")
    assert not any("Venerable Brothers" in h for h in headings(doc))


def test_veritatis_splendor_notes_after_signature_and_plain_digit_marks():
    doc = parse(VERITATIS_SPLENDOR, "https://www.vatican.va/x/hf_jp-ii_enc_06081993_veritatis-splendor.html")
    assert list(doc.notes) == ["1", "2", "3"]
    assert doc.notes["1"].html.startswith("Second Vatican Ecumenical Council")
    body = " ".join(b.html for b in doc.blocks)
    assert body.count('class="note-mark"') == 3
    assert "(Jn 1:9)" in body.replace("<em>", "").replace("</em>", "")       # scripture references untouched
    assert any(b.kind == "closing" and "Given in Rome" in b.html for b in doc.blocks)
    assert not any("Second Vatican" in b.html for b in doc.blocks)            # notes are not body text


# ---------------------------------------------------------------------------
# Stage 1: saved pages
# ---------------------------------------------------------------------------
def test_mhtml_without_charset_keeps_curly_quotes(tmp_path=None):
    import tempfile
    folder = Path(tmp_path or tempfile.mkdtemp())
    html = "<html><head><title>T</title></head><body><p>1. “Quoted” — café.</p></body></html>"
    body = quopri.encodestring(html.encode("utf-8")).decode("ascii")
    path = folder / "page.mhtml"
    path.write_text("From: <Saved by Blink>\r\nSnapshot-Content-Location: https://www.vatican.va/x/20200101-a.html\r\n"
                    "MIME-Version: 1.0\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n" + body)
    text, url = read_saved_page(path)
    assert "“Quoted” — café" in text and "\ufffd" not in text
    assert url == "https://www.vatican.va/x/20200101-a.html"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_title_casing_helpers():
    assert to_title_case("WHAT IS HAPPENING TO OUR COMMON HOME") == "What Is Happening to Our Common Home"
    assert to_title_case("POPE LEO XIV") == "Pope Leo XIV"
    assert soften_capital_runs('"TEACHER, WHAT GOOD MUST I DO...?" (Mt 19:16) - Christ') == \
        '"Teacher, What Good Must I Do...?" (Mt 19:16) - Christ'


if __name__ == "__main__":                       # run without pytest
    failures = 0
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print(f"PASS {name}")
            except Exception as error:           # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(error).__name__}: {error}")
    sys.exit(1 if failures else 0)
