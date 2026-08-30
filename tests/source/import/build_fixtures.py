"""Build deterministic source-import fixtures without external dependencies."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "data" / "source-cleaning" / "fixtures" / "import"


_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
  <metadata/>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>
    <item id="v1" href="v1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c12" href="c12.xhtml" media-type="application/xhtml+xml"/>
    <item id="v2" href="v2.xhtml" media-type="application/xhtml+xml"/>
    <item id="c3" href="c3.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="toc"/><itemref idref="v1"/><itemref idref="c12"/>
    <itemref idref="v2"/><itemref idref="c3"/>
  </spine>
</package>"""

_NCX = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="n0"><navLabel><text>目录</text></navLabel><content src="toc.xhtml"/></navPoint>
    <navPoint id="n1"><navLabel><text>卷一·起风</text></navLabel><content src="v1.xhtml"/></navPoint>
    <navPoint id="n2"><navLabel><text>第一章　开端</text></navLabel><content src="c12.xhtml"/></navPoint>
    <navPoint id="n3"><navLabel><text>第二章　转折</text></navLabel><content src="c12.xhtml#a2"/></navPoint>
    <navPoint id="n4"><navLabel><text>卷二·落雨</text></navLabel><content src="v2.xhtml"/></navPoint>
    <navPoint id="n5"><navLabel><text>第三章　尾声</text></navLabel><content src="c3.xhtml"/></navPoint>
  </navMap>
</ncx>"""

_TOC = "<html><body><p>卷一 第一章 第二章 卷二 第三章</p></body></html>"
_V1 = "<html><body></body></html>"
_C12 = """<html><body>
<h2>第一章　开端</h2>
<p>少年推开门，风把桌上的稿纸吹得四散。</p>
<p>他弯腰去捡，却看见纸上多了一行不是自己写的字。</p>
<h2 id="a2">第二章　转折</h2>
<p>那行字说：把书合上，立刻离开。</p>
<p>他没有听，于是故事从这里开始。</p>
</body></html>"""
_V2 = "<html><body></body></html>"
_C3 = """<html><body>
<h2>第三章　尾声</h2>
<p>雨停的时候，他把最后一页纸放回了原处。</p>
<p>桌上再没有多出过任何字。</p>
</body></html>"""


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for index, (name, data) in enumerate(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_STORED if index == 0 and name == "mimetype" else zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


def _replace(raw: bytes, name: str, payload: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw), "r") as source, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for index, info in enumerate(source.infolist()):
            replacement = payload if info.filename == name else source.read(info)
            new_info = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            new_info.create_system = 3
            new_info.external_attr = 0o644 << 16
            new_info.compress_type = zipfile.ZIP_STORED if index == 0 and info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            target.writestr(new_info, replacement)
    return output.getvalue()


def _write(name: str, data: bytes) -> None:
    (FIXTURES / name).write_bytes(data)


def build() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    txt_utf8 = "\ufeff第一章\r\n组合 e\u0301 与 emoji 😀\r\n正文保留 CRLF。\r\n"
    txt_gb18030 = "第一章　中文标题\r\n中文正文：风从窗外来。\r\n"
    paste = "粘贴文本\nemoji 😀\ne\u0301\n"
    _write("txt_utf8_bom_crlf.txt", txt_utf8.encode("utf-8"))
    _write("txt_gb18030.txt", txt_gb18030.encode("gb18030"))
    _write("paste_utf8.txt", paste.encode("utf-8"))
    _write("binary.txt", b"plain\x00text\x01\x02")
    _write("invalid_utf8.txt", b"UTF-8 invalid: \xff\xfe")
    _write("zip_masquerade.txt", _zip([("readme.txt", b"not an epub")]))
    ordered = _zip([
        ("mimetype", b"application/epub+zip"),
        ("META-INF/container.xml", _CONTAINER.encode()),
        ("OEBPS/content.opf", _OPF.encode()),
        ("OEBPS/toc.ncx", _NCX.encode()),
        ("OEBPS/toc.xhtml", _TOC.encode()),
        ("OEBPS/v1.xhtml", _V1.encode()),
        ("OEBPS/c12.xhtml", _C12.encode()),
        ("OEBPS/v2.xhtml", _V2.encode()),
        ("OEBPS/c3.xhtml", _C3.encode()),
    ])
    _write("ordered.epub", ordered)
    directory_opf = '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>'
    directory_chapter = '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2>目录项安全</h2><p>显式目录项不应阻止 EPUB 导入。</p></body></html>'
    directory_epub = _zip([
        ("mimetype", b"application/epub+zip"),
        ("META-INF/", b""),
        ("META-INF/container.xml", _CONTAINER.encode()),
        ("OEBPS/", b""),
        ("OEBPS/content.opf", directory_opf.encode()),
        ("OEBPS/chapter.xhtml", directory_chapter.encode()),
    ])
    _write("epub_directories.epub", directory_epub)
    declared_container = '<?xml version="1.0" encoding="gb18030"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    declared_opf = '<?xml version="1.0" encoding="gb18030"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine toc="ncx"><itemref idref="c1"/></spine></package>'
    declared_ncx = '<?xml version="1.0" encoding="gb18030"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint><navLabel><text>第一章 开端</text></navLabel><content src="c1.xhtml"/></navPoint></navMap></ncx>'
    declared_chapter = '<?xml version="1.0" encoding="gb18030"?><html xmlns="http://www.w3.org/1999/xhtml"><body><h2>第一章　开端</h2><p>中文正文没有经过替换。</p></body></html>'
    declared = _zip([
        ("mimetype", b"application/epub+zip"),
        ("META-INF/container.xml", declared_container.encode("gb18030")),
        ("OPS/package.opf", declared_opf.encode("gb18030")),
        ("OPS/toc.ncx", declared_ncx.encode("gb18030")),
        ("OPS/c1.xhtml", declared_chapter.encode("gb18030")),
    ])
    _write("declared_gb18030.epub", declared)
    nav_opf = '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest><item id="nav" href="navigation/contents.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>'
    nav_xhtml = '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="../chapter.xhtml">导航给出的章名</a></nav></body></html>'
    nav_chapter = '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2>正文原标题</h2><p>标准 EPUB3 导航正文。</p></body></html>'
    nav_epub = _zip([
        ("mimetype", b"application/epub+zip"),
        ("META-INF/container.xml", _CONTAINER.encode()),
        ("OEBPS/content.opf", nav_opf.encode()),
        ("OEBPS/navigation/contents.xhtml", nav_xhtml.encode()),
        ("OEBPS/chapter.xhtml", nav_chapter.encode()),
    ])
    _write("nav_relative.epub", nav_epub)
    _write("epub_traversal.epub", _replace(nav_epub, "OEBPS/navigation/contents.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="../../../escape.xhtml">bad</a></nav></body></html>'.encode()))
    _write("epub_bad_xml.epub", _replace(ordered, "OEBPS/c3.xhtml", b"<html><body><p>broken"))
    _write("epub_invalid_bytes.epub", _replace(ordered, "OEBPS/c3.xhtml", b"<html><body><p>\xff</p></body></html>"))
    _write("epub_replacement.epub", _replace(ordered, "OEBPS/c3.xhtml", "<html><body><p>literal �</p></body></html>".encode()))
    bomb_payload = b"<html><body>" + (b"A" * 200_000) + b"</body></html>"
    _write("epub_bomb.epub", _zip([
        ("mimetype", b"application/epub+zip"),
        ("META-INF/container.xml", _CONTAINER.encode()),
        ("OEBPS/content.opf", _OPF.encode()),
        ("OEBPS/huge.xhtml", bomb_payload),
    ]))
    cases = {
        "txt_utf8_bom_crlf.txt": {"kind": "txt", "encoding": "utf-8", "contains": ["组合 e\u0301 与 emoji 😀", "正文保留 CRLF。"]},
        "txt_gb18030.txt": {"kind": "txt", "encoding": "gb18030", "contains": ["中文正文：风从窗外来。"]},
        "paste_utf8.txt": {"kind": "paste", "encoding": "utf-8", "contains": ["粘贴文本", "emoji 😀"]},
        "ordered.epub": {"kind": "epub", "encoding": "utf-8@default,utf-8@xml", "contains": ["第1卷 卷一·起风", "第1章　开端", "第2章　转折", "第2卷 卷二·落雨", "第1章　尾声"]},
        "epub_directories.epub": {"kind": "epub", "contains": ["目录项安全", "显式目录项不应阻止 EPUB 导入"]},
        "declared_gb18030.epub": {"kind": "epub", "encoding": "gb18030@xml", "contains": ["第1章　开端", "　中文正文没有经过替换。"]},
        "nav_relative.epub": {"kind": "epub", "contains": ["第1章　导航给出的章名", "　标准 EPUB3 导航正文。"]},
    }
    manifest = {}
    for path in sorted(FIXTURES.iterdir(), key=lambda p: p.name.encode("utf-8")):
        if path.name == "golden_cases.json":
            continue
        data = path.read_bytes()
        manifest[path.name] = hashlib.sha256(data).hexdigest()
    cases["fixture_sha256"] = manifest
    (FIXTURES / "golden_cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    build()
