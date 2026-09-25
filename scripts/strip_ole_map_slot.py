"""Kosongkan binary OLE2FRAME slot peta di file DXF/DWG mentah — supaya
ukuran file kecil (siap upload lewat panel admin, limit 8MB) dan slot itu
otomatis ditimpa peta dinamis saat generate (lihat
app/services/ole_image_embed.py, _embed_logo_overlays() di
isometric_service.py).

Slot LAIN (logo perusahaan, dll) TIDAK disentuh — cuma slot yang idx-nya
dipilih (default: slot dengan binary_data() TERBESAR, biasanya itu peta
karena resolusinya jauh lebih besar dari logo) yang di-strip.

Juga meng-update assets/ole_templates/<nama>.prefix.bin dari entity yang
di-strip — prefix 128-byte AutoCAD itu per-FILE SUMBER (bukan konstanta
universal), jadi tiap kali template sumber ganti (drafter save ulang dari
AutoCAD), prefix HARUS diperbarui.

PERINGATAN PENTING (bug yang pernah kejadian): --prefix-asset di sini
CUMA update file .prefix.bin, TIDAK ikut update file .bin compound
template yang dipakai apply_ole_image() (assets/ole_templates/<nama>.bin,
lihat ole_image_embed.py) — itu file terpisah, dibuat manual sekali dari
backup lama. Kalau prefix baru (dari file sumber BARU) ke-pasang tapi
compound .bin MASIH dari file sumber LAMA, mismatch terjadi lagi — LOLOS
tanpa error di Python (ezdxf/olefile/PIL semua tetap baca sukses) tapi
bikin ODA File Converter diam-diam DROP entity ini saat convert DWG
(exit 0, tanpa log error, hasil cuma lebih kecil dari seharusnya). Kalau
update prefix di sini, WAJIB juga regenerate compound .bin dari SUMBER
FILE YANG SAMA (lihat cara ekstrak manual di dokumentasi/riwayat sesi,
atau perluas script ini untuk sekalian extract compound-nya kalau perlu).

Pemakaian:
    python scripts/strip_ole_map_slot.py <input.dxf> [--idx N] [--out PATH]
                                          [--prefix-asset PATH]

Contoh (kasus nyata sesi ini):
    python scripts/strip_ole_map_slot.py \\
        "/mnt/c/Users/User/Downloads/SK_POLOS_SLEMAN.dxf" \\
        --idx 1 \\
        --out templates/SK_POLOS_p1.dxf \\
        --prefix-asset assets/ole_templates/ole_map_template_1435x1517.prefix.bin

File input TIDAK diubah (dibaca, hasil ditulis ke --out). Kalau --out
sudah ada, dibackup dulu ke <out>.bak_<timestamp> sebelum ditimpa.
"""

import argparse
import datetime
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ezdxf
from ezdxf.lldxf.tags import Tags
from ezdxf.lldxf.types import dxftag

from app.services.pdf_renderer import collect_ole_frames


def _find_target_idx(doc, explicit_idx: int | None) -> int:
    frames = collect_ole_frames(doc)
    if not frames:
        raise SystemExit("Tidak ada OLE2FRAME ditemukan di file ini.")

    if explicit_idx is not None:
        if not any(f["idx"] == explicit_idx for f in frames):
            available = [f["idx"] for f in frames]
            raise SystemExit(f"idx={explicit_idx} tidak ditemukan. idx tersedia: {available}")
        return explicit_idx

    # Default: slot dengan binary_data() TERBESAR — peta punya resolusi
    # jauh lebih tinggi dari logo, jadi biasanya paling besar. Tetap
    # tampilkan semua ukuran supaya operator bisa cek sebelum lanjut.
    msp = doc.modelspace()
    handle_to_entity = {e.dxf.handle: e for e in msp if e.dxftype() == "OLE2FRAME"}
    sizes = []
    for f in frames:
        entity = handle_to_entity.get(f["handle"])
        size = len(entity.binary_data()) if entity is not None else 0
        sizes.append((f["idx"], f["handle"], size))

    print("Slot OLE2FRAME ditemukan:")
    for idx, handle, size in sizes:
        print(f"  idx={idx} handle={handle} binary_len={size:,}")

    biggest = max(sizes, key=lambda t: t[2])
    print(f"-> Auto-pilih idx={biggest[0]} (binary terbesar, asumsi ini peta)")
    return biggest[0]


def strip_ole_map_slot(
    input_path: Path,
    idx: int | None,
    out_path: Path,
    prefix_asset_path: Path | None,
) -> None:
    doc = ezdxf.readfile(str(input_path))
    msp = doc.modelspace()

    frames = collect_ole_frames(doc)
    target_idx = _find_target_idx(doc, idx)
    target_handle = next(f["handle"] for f in frames if f["idx"] == target_idx)

    target = None
    for e in msp:
        if e.dxftype() == "OLE2FRAME" and e.dxf.handle == target_handle:
            target = e
            break
    if target is None:
        raise SystemExit(f"Entity handle={target_handle} tidak ditemukan di modelspace.")

    old_binary = target.binary_data()
    if len(old_binary) < 128:
        print(f"PERINGATAN: slot idx={target_idx} binary sudah < 128 byte "
              f"({len(old_binary)}) — mungkin sudah pernah di-strip sebelumnya.")
    else:
        if prefix_asset_path is not None:
            prefix_asset_path.parent.mkdir(parents=True, exist_ok=True)
            prefix_asset_path.write_bytes(old_binary[:128])
            print(f"Prefix asset diperbarui: {prefix_asset_path}")

    old_tags = list(target.acdb_ole2frame)
    if len(old_tags) < 10:
        raise SystemExit(
            f"Entity idx={target_idx} punya {len(old_tags)} tags, "
            "diharapkan minimal 10 (9 metadata + 1 akhir) — struktur tidak dikenal."
        )
    meta_tags = old_tags[:9]
    end_tag = old_tags[-1]

    new_meta = [dxftag(90, 0) if t.code == 90 else t for t in meta_tags]
    target.acdb_ole2frame = Tags(new_meta + [end_tag])

    if out_path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = out_path.with_name(f"{out_path.name}.bak_{stamp}")
        shutil.copy2(out_path, backup_path)
        print(f"Backup file lama: {backup_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out_path))

    new_size = out_path.stat().st_size
    old_size = input_path.stat().st_size
    print(f"Selesai. idx={target_idx} handle={target_handle} di-strip.")
    print(f"Ukuran: {old_size:,} bytes -> {new_size:,} bytes")
    print(f"Output: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="File DXF sumber (tidak diubah)")
    parser.add_argument("--idx", type=int, default=None,
                         help="idx slot OLE2FRAME yang mau di-strip (default: auto-pilih binary terbesar)")
    parser.add_argument("--out", type=Path, required=True, help="Path output (ditimpa, backup otomatis kalau sudah ada)")
    parser.add_argument("--prefix-asset", type=Path, default=None,
                         help="Path untuk simpan 128-byte prefix entity yang di-strip (opsional)")
    args = parser.parse_args()

    if not args.input.is_file():
        raise SystemExit(f"File tidak ditemukan: {args.input}")

    strip_ole_map_slot(args.input, args.idx, args.out, args.prefix_asset)


if __name__ == "__main__":
    main()
