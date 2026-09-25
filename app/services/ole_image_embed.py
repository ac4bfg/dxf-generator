"""Embed gambar PNG (peta lokasi pelanggan) LANGSUNG ke dalam entity
OLE2FRAME di file DXF/DWG — BUKAN reference file eksternal seperti
IMAGE/ImageDef ezdxf standar (yang cuma simpan path string, bukan pilihan
untuk kebutuhan ini: hasil harus tetap 1 file utuh, portable).

Ditemukan lewat eksperimen langsung (bukan API resmi ezdxf — ezdxf TIDAK
punya factory method untuk BUAT OLE2FRAME baru dari nol, cuma baca):
struktur binary OLE2FRAME (gabungan seluruh tag DXF code 310) =
    [128-byte prefix khusus AutoCAD] + [OLE Compound Document standar
    Microsoft (spek [MS-CFB]), berisi stream "CONTENTS" = file BMP utuh]

Library `olefile` (baca DAN tulis in-place, TIDAK BISA bikin compound file
baru dari nol) dipakai untuk REPLACE isi stream CONTENTS itu dengan bitmap
baru — asal ukuran BYTE persis sama dengan slot asli. Makanya butuh
TEMPLATE compound file (dibuat sekali dari sample OLE2FRAME nyata,
disimpan sebagai aset statis, lihat assets/ole_templates/) yang menentukan
dimensi pixel tetap untuk tiap slot.

Diverifikasi end-to-end (encode -> assign ke ezdxf entity -> saveas ->
baca ulang -> decode balik ke PNG, gambar tampil benar) sebelum
diintegrasikan — lihat plan/percakapan sesi terkait fitur ini.

BUG YANG PERNAH TERJADI (penting untuk maintenance ke depan): 128-byte
prefix BUKAN konstanta universal seperti dikira awalnya — dia per-FILE
SUMBER (beda file DXF, walau sama-sama hasil AutoCAD, punya prefix beda
di 2-3 byte tertentu, kemungkinan checksum/length field internal yang
harus konsisten dengan compound document yang mengikutinya). ezdxf,
`olefile`, dan PIL semuanya TETAP BISA baca file dengan prefix "salah"
(mismatch dari compound-nya) — TIDAK ADA error di Python sama sekali.
TAPI ODA File Converter (dipakai convert DXF->DWG) SECARA DIAM-DIAM DROP
entity OLE2FRAME yang prefix-nya tidak konsisten dengan compound-nya —
exit code tetap 0, TIDAK ADA error log, hasil DWG cuma lebih kecil dari
seharusnya. Kalau nanti bikin/update template baru: prefix (.prefix.bin)
WAJIB diekstrak dari FILE SUMBER YANG SAMA dengan compound (.bin) —
JANGAN campur prefix dari file A dengan compound dari file B walau
sama-sama terlihat valid di Python.
"""

import io
import struct
from pathlib import Path
from typing import Optional

import olefile
from PIL import Image

_PREFIX_LEN = 128
_CHUNK_SIZE = 32  # ukuran tiap potongan DXF group code 310


def _bmp_dimensions(bmp_bytes: bytes) -> tuple[int, int]:
    """Baca width/height dari header BITMAPINFOHEADER (offset 18/22)."""
    width = struct.unpack("<i", bmp_bytes[18:22])[0]
    height = struct.unpack("<i", bmp_bytes[22:26])[0]
    return width, height


def _png_to_bmp_matching_template(png_bytes: bytes, template_contents: bytes) -> bytes:
    """Convert PNG -> BMP 32-bit BGRA, ukuran byte PERSIS SAMA dengan
    `template_contents` (stream CONTENTS asli) — WAJIB, olefile.write_stream()
    menolak ukuran berbeda. Header 66-byte (BITMAPFILEHEADER +
    BITMAPINFOHEADER + 3 color mask BI_BITFIELDS) DIPERTAHANKAN APA ADANYA
    dari template — cuma pixel data yang diganti, supaya kompatibel dengan
    cara AutoCAD menulis OLE bitmap (beda dari default PIL BMP export, yang
    pakai BI_RGB tanpa color mask, jadi ukuran headernya beda 12 byte)."""
    header = template_contents[:66]
    width, height = _bmp_dimensions(template_contents)

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    if img.size != (width, height):
        img = img.resize((width, height), Image.LANCZOS)

    # BMP row order: bottom-up, per-pixel BGRA — flip vertikal dari PNG
    # (top-down) sebelum serialize raw bytes.
    flipped = img.transpose(Image.FLIP_TOP_BOTTOM)
    pixel_data = flipped.tobytes("raw", "BGRA")

    expected_len = len(template_contents) - len(header)
    if len(pixel_data) != expected_len:
        raise ValueError(
            f"Pixel data size mismatch: got {len(pixel_data)}, expected {expected_len} "
            f"(width={width}, height={height})"
        )

    return header + pixel_data


def build_ole2frame_binary(png_bytes: bytes, template_path: Path, prefix_bytes: bytes) -> bytes:
    """PNG bytes -> full OLE2FRAME binary blob siap di-chunk jadi tag DXF
    code 310 (128-byte prefix + OLE compound doc dengan stream CONTENTS
    berisi gambar baru).

    `prefix_bytes` WAJIB diambil dari entity OLE2FRAME ASLI yang mau
    ditimpa (`entity.binary_data()[:128]`) — bukan konstanta atau file
    terpisah. Prefix ini AutoCAD-specific per-object (bukan cuma per
    template), dan mengisinya dengan nol membuat AutoCAD anggap OLE
    object itu korup."""
    template_compound = template_path.read_bytes()

    ole = olefile.OleFileIO(io.BytesIO(template_compound))
    original_contents = ole.openstream("CONTENTS").read()
    ole.close()

    new_contents = _png_to_bmp_matching_template(png_bytes, original_contents)

    # olefile.write_stream() WAJIB file path asli (write_mode diabaikan
    # untuk file-like object/bytes) — tulis ke file temp, modifikasi,
    # baca ulang sebagai bytes, hapus temp.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp.write(template_compound)
        tmp_path = tmp.name

    try:
        ole_write = olefile.OleFileIO(tmp_path, write_mode=True)
        ole_write.write_stream("CONTENTS", new_contents)
        ole_write.close()

        new_compound = Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if len(prefix_bytes) != _PREFIX_LEN:
        raise ValueError(f"prefix_bytes harus {_PREFIX_LEN} byte, dapat {len(prefix_bytes)}")

    return prefix_bytes + new_compound


def apply_ole_image(entity, png_bytes: bytes, template_path: Path) -> None:
    """Assign XDATA acdb_ole2frame BARU ke `entity` (OLE2FRAME ezdxf) berisi
    gambar `png_bytes`, pakai template ukuran dari `template_path`.

    Pola: ambil 9 tag metadata awal (100/70/3/10/11/71/72/73/90) dari
    entity YANG SUDAH ADA (posisi/corner tetap dipertahankan — cuma isi
    gambarnya yang diganti), replace SELURUH tag code 310 dengan chunk
    binary baru, update tag 90 (byte count) ke ukuran baru, tag akhir '1'
    (marker "OLE") dipertahankan dari entity lama.
    """
    from ezdxf.lldxf.tags import Tags
    from ezdxf.lldxf.types import dxftag

    old_tags = list(entity.acdb_ole2frame) if entity.acdb_ole2frame else []
    # 9 tag metadata standar: 100,70,3,10,11,71,72,73,90 — SELALU di awal,
    # SEBELUM tag 310 pertama mulai (dikonfirmasi dari struktur sample).
    meta_tags = [t for t in old_tags[:9]] if len(old_tags) >= 9 else []
    end_tag = old_tags[-1] if old_tags and old_tags[-1].code == 1 else dxftag(1, "OLE")

    if not meta_tags:
        raise ValueError(
            "Entity OLE2FRAME tidak punya metadata tags (kosong/corrupt) — "
            "tidak bisa tentukan posisi/corner tanpa itu."
        )

    # Prefix 128-byte diambil dari entity ITU SENDIRI kalau masih ADA
    # binary-nya (paling akurat — prefix per-FILE SUMBER, lihat catatan
    # panjang di docstring modul ini). Beberapa template produksi slot
    # peta-nya SUDAH di-strip binary (dikosongkan untuk hemat ukuran file
    # sebelum fitur ini ada) — untuk kasus itu, fallback ke prefix yang
    # disimpan terpisah sebagai aset di samping template compound.
    # WAJIB: file .prefix.bin itu HARUS diekstrak dari FILE SUMBER YANG
    # SAMA dengan template compound .bin di sebelahnya — mismatch sumber
    # LOLOS tanpa error di ezdxf/olefile/PIL tapi bikin ODA File Converter
    # diam-diam DROP entity ini saat convert DXF->DWG (lihat docstring
    # modul, bug yang pernah kejadian).
    old_binary = entity.binary_data()
    if len(old_binary) >= _PREFIX_LEN:
        prefix_bytes = old_binary[:_PREFIX_LEN]
    else:
        prefix_path = template_path.with_suffix(".prefix.bin")
        if not prefix_path.is_file():
            raise ValueError(
                f"Entity OLE2FRAME binary kosong/pendek ({len(old_binary)} byte) "
                f"dan file prefix fallback tidak ada: {prefix_path}"
            )
        prefix_bytes = prefix_path.read_bytes()

    new_binary = build_ole2frame_binary(png_bytes, template_path, prefix_bytes)

    chunks = [new_binary[i:i + _CHUNK_SIZE] for i in range(0, len(new_binary), _CHUNK_SIZE)]
    new_tags = list(meta_tags)
    for i, t in enumerate(new_tags):
        if t.code == 90:
            new_tags[i] = dxftag(90, len(new_binary))
            break
    new_tags.extend(dxftag(310, c) for c in chunks)
    new_tags.append(end_tag)

    entity.acdb_ole2frame = Tags(new_tags)
