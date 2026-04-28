from html import escape
from pathlib import Path
import shutil
import subprocess
import textwrap


TITLE = "Microtransfer Printing Report"
OUTPUT = Path("microtransfer_printing_report.pdf")

REPORT_TEXT = """
Microtransfer Printing: Principles, Process Variants, Applications, and Challenges

Abstract
Microtransfer printing (MTP) is a deterministic assembly method that retrieves microscale or nanoscale material inks from a donor substrate and places them onto a target substrate with high spatial control. It is an important route for heterogeneous integration because it allows separately optimized materials and devices to be assembled onto flexible, stretchable, curved, or photonic target platforms.

1. Introduction
Microtransfer printing emerged as a manufacturing strategy for heterogeneous integration when monolithic fabrication or wafer bonding became too restrictive for systems that combine dissimilar materials, processing temperatures, or substrate formats. Meitl et al. established the key idea of kinetic control of adhesion between an elastomeric stamp and a microstructured ink, enabling deterministic pick-up and release without dedicated adhesive layers [1]. Park et al. extended the method to functional inorganic LEDs on deformable and semitransparent display platforms [2]. Carlson et al. later framed transfer printing as a broad family of assembly techniques for micro- and nanomaterials in electronics, optoelectronics, and bio-integrated systems [3].

2. Operating Principle
The basic workflow contains fabrication of transferable inks on a donor wafer, retrieval by a stamp or transfer head, alignment to the receiver substrate, and controlled release onto the final target. In elastomeric stamp printing, rapid peeling raises interfacial adhesion enough for pick-up, while slow peeling or altered contact conditions favor release to the receiver [1]. Interfacial fracture mechanics strongly affects the outcome. Kim-Lee et al. showed that preferred crack paths depend on interface toughness, stamp geometry, flaw size, and membrane thickness [5]. Review papers further emphasize the roles of seal material, modulus, surface microstructure, preload, peel rate, and thermal state [6,8].

3. Major Process Variants
The most established variant is elastomeric transfer printing, typically using PDMS or related soft stamps. Its strengths are conformal contact, material versatility, and compatibility with fragile thin-film devices; its weaknesses include stamp wear, process sensitivity, and scaling difficulty for massive arrays [3,6]. Laser-driven micro-transfer printing reduces dependence on receiver-surface preparation by using laser-induced delamination for non-contact release [4]. More recently, Park et al. reported a micro-vacuum-assisted selective transfer method with independently controlled suction channels and a reported transfer yield of 98.06 percent for selective heterogeneous assembly [10].

4. Representative Applications
Flexible and stretchable inorganic electronics are the best known application area because transfer printing decouples device growth from final assembly on soft substrates [6]. Displays are a classic demonstration: Park et al. showed printed assemblies of microscale inorganic LEDs for deformable and semitransparent systems [2]. Silicon photonics is another strong application area because active III-V devices and other optimized photonic building blocks can be placed onto silicon photonic backplanes with layout flexibility and potential parallelism [7,9]. Reviews also point to bio-integrated electronics, sensors, photovoltaics, and 3D microsystems as promising targets [3,8].

5. Current Bottlenecks
Industrial deployment still depends on solving several bottlenecks. Throughput and defectivity remain tightly coupled, so the field needs massively parallel transfer with repair strategies and near-zero missing-chip rates. Mechanics-driven variability remains important because small changes in interface energy, stamp aging, or device geometry can alter release behavior [5]. Heterogeneous integration workflows must also connect transfer printing to upstream device singulation and downstream electrical, optical, or thermal interconnection at acceptable cost [3,6,8].

6. Conclusion
Microtransfer printing has evolved from an adhesion-switching concept into a serious heterogeneous integration platform. Early work established deterministic pick-and-place mechanics, later work broadened demonstrations and modeling, and recent studies have focused on scalable, selective, and lower-damage architectures. Its long-term importance is highest where monolithic integration is impossible or economically unattractive, especially in flexible electronics, micro-LED displays, and heterogeneous photonic integrated circuits.

References
[1] M. A. Meitl et al., "Transfer printing by kinetic control of adhesion to an elastomeric stamp," Nature Materials 5, 33-38 (2006). DOI: 10.1038/nmat1532.
[2] S.-I. Park et al., "Printed assemblies of inorganic light-emitting diodes for deformable and semitransparent displays," Science 325(5943), 977-981 (2009). DOI: 10.1126/science.1175690.
[3] A. Carlson et al., "Transfer printing techniques for materials assembly and micro/nanodevice fabrication," Advanced Materials 24(39), 5284-5318 (2012). DOI: 10.1002/adma.201201386.
[4] R. Saeidpourazar et al., "A prototype printer for laser driven micro-transfer printing," Journal of Manufacturing Processes 14(4), 416-424 (2012). DOI: 10.1016/j.jmapro.2012.09.014.
[5] H. J. Kim-Lee et al., "Interface mechanics of adhesiveless microtransfer printing processes," Journal of Applied Physics 115, 143513 (2014). DOI: 10.1063/1.4870873.
[6] C. Linghu et al., "Transfer printing techniques for flexible and stretchable inorganic electronics," npj Flexible Electronics 2, 26 (2018). DOI: 10.1038/s41528-018-0037-x.
[7] S. Keyvaninia et al., "Transfer Printing for Silicon Photonics," Semiconductors and Semimetals 99, 43-70 (2018). DOI: 10.1016/bs.semsem.2018.08.001.
[8] L. Zhang et al., "Research Progress of Microtransfer Printing Technology for Flexible Electronic Integrated Manufacturing," Micromachines 12(11), 1358 (2021). DOI: 10.3390/mi12111358.
[9] G. Roelkens et al., "Micro-Transfer Printing for Heterogeneous Si Photonic Integrated Circuits," IEEE Journal of Selected Topics in Quantum Electronics 29(3), 1-14 (2023). DOI: 10.1109/JSTQE.2022.3222686.
[10] S. H. Park et al., "Universal selective transfer printing via micro-vacuum force," Nature Communications 14, 7744 (2023). DOI: 10.1038/s41467-023-43342-8.
""".strip()


def find_browser() -> str | None:
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def build_html(title: str, text: str) -> str:
    paragraphs = []
    for block in text.split("\n\n"):
        content = escape(block.strip())
        if content:
            paragraphs.append(f"<p>{content.replace(chr(10), '<br>')}</p>")
    body = "\n".join(paragraphs)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
  <style>
    @page {{
      size: A4;
      margin: 18mm 16mm 18mm 16mm;
    }}
    body {{
      font-family: "Segoe UI", Arial, sans-serif;
      color: #111;
      line-height: 1.5;
      font-size: 11pt;
    }}
    h1 {{
      font-size: 18pt;
      margin: 0 0 14px;
    }}
    p {{
      margin: 0 0 10px;
      overflow-wrap: break-word;
    }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  {body}
</body>
</html>
"""


def write_pdf(path: Path, title: str, text: str) -> None:
    browser = find_browser()
    if browser:
        if _try_browser_pdf(browser, path, title, text):
            return
    _write_basic_pdf(path, text)


def _try_browser_pdf(browser: str, path: Path, title: str, text: str) -> bool:
    html = build_html(title, text)
    html_path = path.with_suffix(".html")
    try:
        html_path.write_text(html, encoding="utf-8")
        command = [
            browser,
            "--headless",
            "--disable-gpu",
            "--allow-file-access-from-files",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={path.resolve()}",
            html_path.resolve().as_uri(),
        ]
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode == 0 and path.exists()
    finally:
        if html_path.exists():
            html_path.unlink()


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_pages(text: str, width: int = 95, lines_per_page: int = 46) -> list[list[str]]:
    wrapped: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(raw_line, width=width, break_long_words=False))
    pages = []
    for i in range(0, len(wrapped), lines_per_page):
        pages.append(wrapped[i:i + lines_per_page])
    return pages


def _make_content_stream(lines: list[str], page_number: int) -> bytes:
    commands = ["BT", "/F1 11 Tf", "50 792 Td"]
    for idx, line in enumerate(lines):
        if idx == 0:
            commands.append(f"({_pdf_escape(line)}) Tj")
        else:
            commands.append("0 -15 Td")
            commands.append(f"({_pdf_escape(line)}) Tj")
    commands.append("0 -22 Td")
    commands.append(f"(Page {page_number}) Tj")
    commands.append("ET")
    return "\n".join(commands).encode("ascii")


def _write_basic_pdf(path: Path, text: str) -> None:
    pages = _build_pages(text)
    objects: list[bytes] = []

    def add_object(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_tree_id = add_object(b"<< /Type /Pages /Count 0 /Kids [] >>")
    page_ids = []

    for page_num, lines in enumerate(pages, start=1):
        content = _make_content_stream(lines, page_num)
        content_id = add_object(
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"
        )
        page_obj = (
            f"<< /Type /Page /Parent {pages_tree_id} 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        page_ids.append(add_object(page_obj))

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[pages_tree_id - 1] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode("ascii")
    catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_tree_id} 0 R >>".encode("ascii"))

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_pos = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    path.write_bytes(pdf)


if __name__ == "__main__":
    write_pdf(OUTPUT, TITLE, REPORT_TEXT)
    print(f"Wrote {OUTPUT}")
