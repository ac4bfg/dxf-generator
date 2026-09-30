"""Preview SVG berlapis untuk editor /drawing.

Preview lama (preview-svg / preview-drawing-svg) membaca ulang kop DXF,
mengganti teks pelanggan, merender SELURUH kop + gambar + logo lalu mengirim
±1,2 MB SVG — setiap kali pelanggan dipilih atau gambar diubah. Di sini kop
dipecah jadi tiga lapisan yang ditumpuk editor dengan viewBox sama
(0 0 420 297, mm kertas):

* **kop**    — bingkai/tabel/teks tetap kop, tanpa placeholder. Dirender
               sekali per berkas kop, disimpan di memori.
* **teks**   — hanya teks placeholder yang sudah diganti data pelanggan
               (+ logo/peta region), per pelanggan.
* **gambar** — hanya entity hasil engine (pipa, simbol, dimensi, crossing).

Doc kop dibaca SEKALI per proses dan dipakai ulang untuk semua request
(dikunci — satu request sekaligus per berkas kop): entity teks/gambar
ditambahkan, dirender dengan filter, lalu dihapus lagi. Semua lapisan
dirender dengan ``render_box`` = bbox kop supaya skala & posisinya sama
persis dengan render satu lapis.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext, layout
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration, LineweightPolicy
from ezdxf.addons.drawing.svg import SVGBackend

from app.services import dxf_to_svg as d2s
from app.services.pdf_template_cache import PLACEHOLDER_RE, _get_template_hash

PAPER_W = d2s.PAPER_WIDTH_MM
PAPER_H = d2s.PAPER_HEIGHT_MM

# Naikkan kalau cara render lapisan berubah (dipakai juga sebagai ETag).
VERSI_LAPISAN = "3"


class DiLuarKop(Exception):
    """Isi lapisan melewati batas kop — pemanggil memakai preview satu lapis."""


def _adalah_placeholder(e) -> bool:
    t = e.dxftype()
    if t == "TEXT":
        return bool(PLACEHOLDER_RE.search(e.dxf.text or ""))
    if t == "MTEXT":
        return bool(PLACEHOLDER_RE.search(e.text or ""))
    return False


def _titik_kalibrasi(entities) -> List[Tuple[float, float]]:
    """Titik yang dipakai render_dxf_to_svg() untuk data-dxf-* (kalibrasi
    sketsa editor): LINE, LWPOLYLINE, titik sisip INSERT."""
    out: List[Tuple[float, float]] = []
    for e in entities:
        try:
            t = e.dxftype()
            if t == "LINE":
                out += [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
            elif t == "LWPOLYLINE":
                out += [(p[0], p[1]) for p in e.get_points()]
            elif t == "INSERT":
                out.append((e.dxf.insert.x, e.dxf.insert.y))
        except Exception:
            pass
    return out


def _atribut_kalibrasi(titik: List[Tuple[float, float]]) -> Dict[str, float]:
    """Sama persis dengan hitungan data-dxf-* di render_dxf_to_svg()."""
    m = 0.5
    x_lo, x_hi = -PAPER_W * m, PAPER_W * (1 + m)
    y_lo, y_hi = -PAPER_H * m, PAPER_H * (1 + m)
    xs = [x for x, y in titik if x_lo <= x <= x_hi and y_lo <= y <= y_hi]
    ys = [y for x, y in titik if x_lo <= x <= x_hi and y_lo <= y <= y_hi]
    ext_x, ext_y, min_x, min_y = 200.0, 120.0, 100.0, 80.0
    if xs:
        w = max(xs) - min(xs)
        if w > 1:
            ext_x = float(w)
        min_x = float(min(xs))
    if ys:
        h = max(ys) - min(ys)
        if h > 1:
            ext_y = float(h)
        min_y = float(min(ys))
    return {"x": ext_x, "y": ext_y, "min_x": min_x, "min_y": min_y}


def _svg_lapisan(inner_g: str, kalibrasi: Dict[str, float], latar: bool, ekstra: str = "") -> str:
    style = ' style="background:#1f2937"' if latar else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" '
        f'viewBox="0 0 {PAPER_W} {PAPER_H}" '
        f'data-dxf-extent-x="{kalibrasi["x"]:.2f}" '
        f'data-dxf-extent-y="{kalibrasi["y"]:.2f}" '
        f'data-dxf-min-x="{kalibrasi["min_x"]:.2f}" '
        f'data-dxf-min-y="{kalibrasi["min_y"]:.2f}"{style}>'
        f'{inner_g}{ekstra}</svg>'
    )


def _rapikan(svg_raw: str, awalan: str, buang_latar: bool = False) -> str:
    """Pasca-proses output ezdxf seperti render_dxf_to_svg() + beri awalan
    nama class CSS (C1 … CA, CB — heksadesimal) supaya lapisan yang ditumpuk
    dalam satu halaman tidak saling menimpa gayanya. buang_latar: hapus
    <rect> latar ezdxf (lapisan di atas kop harus transparan — latar tetap
    DEFAULT saat render supaya warna ACI 7 sama dengan preview lama).
    Return <g transform=scale(..)>."""
    if buang_latar:
        svg_raw = re.sub(r'<rect [^>]*x="0" y="0"[^>]*/>', "", svg_raw, count=1)
    f = d2s.STROKE_SHRINK_FACTOR
    if f and f != 1.0:
        def _shrink(m):
            try:
                return f"stroke-width: {float(m.group(1)) / f:g}"
            except ValueError:
                return m.group(0)
        svg_raw = re.sub(r"stroke-width:\s*([\d.]+)", _shrink, svg_raw)
    svg_raw = svg_raw.replace("<def>", "<defs>").replace("</def>", "</defs>")
    svg_raw = re.sub(r"\.C([0-9A-F]+)\b", rf".{awalan}C\1", svg_raw)
    svg_raw = re.sub(r'class="C([0-9A-F]+)"', rf'class="{awalan}C\1"', svg_raw)
    vb = re.search(r'viewBox="([^"]+)"', svg_raw)
    _, _, vb_w, vb_h = map(float, vb.group(1).split())
    inner = re.match(r"^(?:<\?xml[^>]*>\s*)?<svg[^>]*>([\s\S]*)</svg>\s*$", svg_raw).group(1)
    sx = PAPER_W / vb_w if vb_w else 1
    sy = PAPER_H / vb_h if vb_h else 1
    return f'<g transform="scale({sx:.6f} {sy:.6f})">{inner}</g>'


class KopSvg:
    """Doc kop yang sudah disiapkan untuk render SVG + lapisan kop jadi."""

    def __init__(self, template_path: Path, font_dir: Path,
                 blok_standar_path: Optional[str], engine_cls):
        from app.services.pdf_renderer import (
            _apply_ezdxf_patches, configure_ezdxf_fonts, patch_styles,
            replace_dot_blocks, rewrite_mtext_inline_fonts,
        )
        self.template_path = template_path
        self.lock = threading.Lock()
        doc = ezdxf.readfile(str(template_path))
        msp = doc.modelspace()
        self.handles_kop = frozenset(e.dxf.handle for e in msp)
        # Isi cache handle kop engine (auto_fit) dari doc ini — tanpa ini
        # request gambar pertama membaca ulang berkas kop (±0,3–1 dtk).
        from app.services import pdf_template_cache as _ptc
        _ptc._HANDLES_KOP[(str(template_path), template_path.stat().st_mtime_ns)] = self.handles_kop

        # Dua doc dari berkas yang sama:
        # * doc_gen — mentah, tempat engine menyusun gambar. Persis jalur
        #   lama: auto_fit memusatkan gambar dari bbox teks dimensi, yang
        #   bergeser (±0,4 mm) kalau font style sudah diganti patch_styles.
        # * doc     — disiapkan untuk render (font, titik _DotSmall, MTEXT).
        # Entity gambar dipindah doc_gen → doc dengan Importer.
        doc_gen = ezdxf.readfile(str(template_path))

        # Blok simbol dari kop standar — sama dengan engine, tapi sekaligus
        # untuk semua simbol (bukan per request). Blok yang dipakai kop
        # sendiri (logo, tabel) tetap versi region.
        if blok_standar_path:
            eng = engine_cls(str(template_path), blok_standar_path=blok_standar_path)
            nama = self._nama_simbol(doc, blok_standar_path)
            eng._samakan_blok_standar(doc, nama)
            eng._samakan_blok_standar(doc_gen, nama)
        self.doc_gen = doc_gen
        self.blok_awal_gen = frozenset(b.name for b in doc_gen.blocks)

        _apply_ezdxf_patches()
        configure_ezdxf_fonts(Path(font_dir))
        patch_styles(doc)
        replace_dot_blocks(doc)
        rewrite_mtext_inline_fonts(doc)
        self.placeholder = frozenset(e.dxf.handle for e in msp if _adalah_placeholder(e))
        d2s.fix_mtext_for_ezdxf_render(doc, lewati=self.placeholder)
        self.doc = doc
        self.blok_awal = frozenset(b.name for b in doc.blocks)
        self.titik_kop = _titik_kalibrasi(msp)
        self.kalibrasi_kop = _atribut_kalibrasi(self.titik_kop)

        # bbox kop = batas render semua lapisan (placeholder ikut dihitung,
        # teks pelanggan hampir selalu di dalam bingkai kop).
        rekam = self._rekam(lambda e: e.dxf.handle in self.handles_kop, BackgroundPolicy.DEFAULT)
        self.render_box = rekam.player().bbox()
        dasar = self._rekam(
            lambda e: e.dxf.handle in self.handles_kop and e.dxf.handle not in self.placeholder,
            BackgroundPolicy.DEFAULT,
        )
        self.svg_kop = _svg_lapisan(self._jadi_svg(dasar, "k"), self.kalibrasi_kop, latar=True)

    @staticmethod
    def _nama_simbol(doc, blok_standar_path: str) -> set:
        std = ezdxf.readfile(str(blok_standar_path))
        dipakai_kop: set = set()
        antre = [e.dxf.name for e in doc.modelspace().query("INSERT")]
        while antre:
            n = antre.pop()
            if n in dipakai_kop or n not in doc.blocks:
                continue
            dipakai_kop.add(n)
            antre += [e.dxf.name for e in doc.blocks[n].query("INSERT")]
        return {
            b.name for b in std.blocks
            if not b.name.startswith("*") and not b.is_any_layout and b.name not in dipakai_kop
        }

    def _rekam(self, filter_func, background: BackgroundPolicy) -> SVGBackend:
        backend = SVGBackend()
        cfg = Configuration(
            lineweight_policy=LineweightPolicy.ABSOLUTE,
            lineweight_scaling=0.25,
            background_policy=background,
        )
        Frontend(RenderContext(self.doc), backend, config=cfg).draw_layout(
            self.doc.modelspace(), filter_func=filter_func)
        return backend

    def _jadi_svg(self, backend: SVGBackend, awalan: str, buang_latar: bool = False) -> str:
        page = layout.Page(width=PAPER_W, height=PAPER_H, units=layout.Units.mm,
                           margins=layout.Margins.all(0))
        settings = layout.Settings(fit_page=True, min_stroke_width=1, max_stroke_width=200)
        raw = backend.get_string(page, settings=settings, render_box=self.render_box,
                                 xml_declaration=False)
        return _rapikan(raw, awalan, buang_latar)

    def _render_tambahan(self, handles: set, awalan: str) -> str:
        # Latar DEFAULT (gelap) seperti preview lama — BackgroundPolicy.OFF
        # membuat ACI 7 dianggap di atas latar terang → garis jadi hitam.
        # <rect> latarnya dibuang supaya lapisan transparan di atas kop.
        backend = self._rekam(lambda e: e.dxf.handle in handles, BackgroundPolicy.DEFAULT)
        # Isi keluar dari batas kop → preview satu lapis memperkecil SELURUH
        # halaman (kop ikut mengecil), lapisan tidak bisa meniru itu.
        bbox = backend.player().bbox()
        rb, tol = self.render_box, 1e-6
        if bbox.has_data and (bbox.extmin.x < rb.extmin.x - tol or bbox.extmin.y < rb.extmin.y - tol
                              or bbox.extmax.x > rb.extmax.x + tol or bbox.extmax.y > rb.extmax.y + tol):
            raise DiLuarKop(f"lapisan {awalan} keluar dari batas kop")
        return self._jadi_svg(backend, awalan, buang_latar=True)

    # ------------------------------------------------------------------
    # Lapisan per request (dipanggil dengan self.lock dipegang)
    # ------------------------------------------------------------------

    def _siapkan_entity_baru(self, entities) -> None:
        """Koreksi render yang di jalur lama diterapkan ke seluruh doc."""
        from app.services.pdf_renderer import MTEXT_INLINE_FONT_REWRITES, _MTEXT_FONT_CODE_RE

        def _swap(m):
            return f"\\f{MTEXT_INLINE_FONT_REWRITES.get(m.group(1), m.group(1))}{m.group(2)};"

        for e in entities:
            if e.dxftype() != "MTEXT":
                continue
            if MTEXT_INLINE_FONT_REWRITES:
                baru = _MTEXT_FONT_CODE_RE.sub(_swap, e.text)
                if baru != e.text:
                    e.text = baru
            if "pq*;" in (e.text or ""):
                q = d2s._ATTACH_TO_PARA.get(int(e.dxf.get("attachment_point", 1) or 1), "l")
                e.text = d2s._PARA_DEFAULT_RE.sub(lambda _m: chr(92) + "pxq" + q + ";", e.text)
            d2s._fix_middlecenter_mtext(e, self.doc)

    def lapisan_teks(self, replacements: Dict[str, str], logo_tags: str = "") -> str:
        from app.services.dxf_service import DxfService
        msp = self.doc.modelspace()
        salinan = []
        try:
            for h in self.placeholder:
                asli = self.doc.entitydb.get(h)
                if asli is None:
                    continue
                c = asli.copy()
                self.doc.entitydb.add(c)
                msp.add_entity(c)
                DxfService.replace_text_in_entity(None, c, replacements)
                salinan.append(c)
            self._siapkan_entity_baru(salinan)
            inner = self._render_tambahan({c.dxf.handle for c in salinan}, "t")
        finally:
            for c in salinan:
                msp.delete_entity(c)
        return _svg_lapisan(inner, self.kalibrasi_kop, latar=False, ekstra=logo_tags)

    def lapisan_gambar(self, engine, request: Dict[str, Any]) -> str:
        from ezdxf.addons import Importer
        msp = self.doc.modelspace()
        msp_gen = self.doc_gen.modelspace()
        engine._doc_pakai_ulang = self.doc_gen
        try:
            ok, msg, _ = engine.generate(request, None)
            if not ok:
                raise ValueError(msg)
            baru_gen = [e for e in msp_gen if e.dxf.handle not in self.handles_kop]
            imp = Importer(self.doc_gen, self.doc)
            # Blok bernama (simbol, _DotSmall yang sudah disiapkan) pakai yang
            # ada di doc render — default Importer menyalin dengan nama baru.
            # Nama blok DXF tidak peka huruf ("_DOTSMALL" = "_DotSmall"),
            # jadi cek lewat doc.blocks, bukan cocok string.
            import_asli = imp.import_block
            blok_render = self.doc.blocks

            def import_block(nama, rename=True):
                if not nama.startswith("*") and nama in blok_render:
                    return blok_render.get(nama).name
                return import_asli(nama, rename)

            imp.import_block = import_block
            imp.import_entities(baru_gen, msp)
            imp.finalize()
            baru = [e for e in msp if e.dxf.handle not in self.handles_kop]
            blok_baru = [b for b in self.doc.blocks if b.name not in self.blok_awal]
            for b in blok_baru:
                if b.name.upper().startswith(("*D", "*U")):
                    self._siapkan_entity_baru(list(b))
                    for e in b:
                        if e.dxftype() == "MTEXT":
                            e.text = d2s._MTEXT_ALIGN_CODE_RE.sub("", e.text)
            self._siapkan_entity_baru(baru)
            inner = self._render_tambahan({e.dxf.handle for e in baru}, "g")
            kalibrasi = _atribut_kalibrasi(self.titik_kop + _titik_kalibrasi(baru))
            return _svg_lapisan(inner, kalibrasi, latar=False)
        finally:
            engine._doc_pakai_ulang = None
            for e in [e for e in msp_gen if e.dxf.handle not in self.handles_kop]:
                msp_gen.delete_entity(e)
            for b in [b.name for b in self.doc_gen.blocks if b.name not in self.blok_awal_gen]:
                try:
                    self.doc_gen.blocks.delete_block(b, safe=False)
                except Exception:
                    pass
            for e in [e for e in msp if e.dxf.handle not in self.handles_kop]:
                msp.delete_entity(e)
            for b in [b.name for b in self.doc.blocks if b.name not in self.blok_awal]:
                try:
                    self.doc.blocks.delete_block(b, safe=False)
                except Exception:
                    pass


_CACHE: Dict[tuple, KopSvg] = {}
_CACHE_LOCK = threading.Lock()


def kunci_kop(template_path: Path, blok_standar_path: Optional[str]) -> str:
    std = Path(blok_standar_path) if blok_standar_path else None
    std_hash = _get_template_hash(std) if std and std.exists() else "-"
    return f"v{VERSI_LAPISAN}-{_get_template_hash(template_path)}-{std_hash}"


def ambil_kop(template_path: Path, font_dir: Path, blok_standar_path: Optional[str],
              engine_cls) -> KopSvg:
    """KopSvg dari cache proses — dibuat ulang otomatis kalau berkas kop
    (atau kop standar) berubah."""
    kunci = (str(template_path), kunci_kop(template_path, blok_standar_path))
    kop = _CACHE.get(kunci)
    if kop is not None:
        return kop
    with _CACHE_LOCK:
        kop = _CACHE.get(kunci)
        if kop is None:
            kop = KopSvg(template_path, font_dir, blok_standar_path, engine_cls)
            # Versi lama berkas yang sama tidak dipakai lagi — buang.
            for k in [k for k in _CACHE if k[0] == str(template_path)]:
                _CACHE.pop(k, None)
            _CACHE[kunci] = kop
    return kop
