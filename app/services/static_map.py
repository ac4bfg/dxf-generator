"""Render peta lokasi statis (PNG) dari koordinat lat/long, di-stitch dari
tile OpenStreetMap. Dipakai fitur "peta di kop dokumen asbuilt" — hasil PNG
disisipkan ke array logo_overlays yang SUDAH ADA (lihat pdf_template_cache.py
/dxf_to_svg.py), bukan mekanisme overlay baru.

Pakai urllib.request (stdlib) sengaja — bukan `requests` — supaya tidak
menambah dependency pip baru yang perlu diinstall manual di server produksi.

Kebijakan OSM Tile Usage (operations.osmfoundation.org/policies/tiles/):
User-Agent WAJIB identifiable, TIDAK BOLEH bulk-scrape tanpa cache. Tile
individual di-cache PERMANEN ke disk (data OSM historis jarang berubah
drastis untuk kebutuhan dokumen asbuilt), jadi request berulang untuk area
yang sama TIDAK memukul server OSM lagi.
"""

import math
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

TILE_SIZE = 256
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "AergasAsbuiltMapGen/1.0 (internal tool, contact: ops@aergas.internal)"

# Warna cincin di sekeliling marker — SAMA PERSIS RGB yang dipakai drafter
# di contoh AutoCAD asli (LIST command: "Color: 147,39,143").
RING_COLOR = (147, 39, 143)

# Warna pin per modul — SK hijau, SR kuning (pembeda visual jenis dokumen).
MARKER_COLOR_BY_MODULE = {
    "SK": (30, 160, 70),
    "SR": (224, 168, 0),
}
MARKER_COLOR_DEFAULT = (214, 40, 40)  # merah, fallback modul tak dikenal


def latlng_to_global_pixel(lat: float, lng: float, zoom: int) -> Tuple[float, float]:
    """Web Mercator projection: lat/lng -> pixel coords di peta dunia penuh
    pada zoom level ini (0,0 = pojok kiri-atas / 180W,~85.05N)."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lng + 180.0) / 360.0 * n * TILE_SIZE
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return x, y


def _fetch_tile(z: int, x: int, y: int, tile_cache_dir: Path) -> Optional[Image.Image]:
    """Fetch 1 tile OSM, cache PERMANEN ke disk. Return None kalau gagal
    (tile di luar cakupan dunia, network error, dst) — pemanggil biarkan
    area itu kosong, tidak fatal untuk keseluruhan peta."""
    n = 2 ** z
    if not (0 <= y < n):
        return None
    x = x % n  # wrap horizontal (antimeridian)

    cache_path = tile_cache_dir / f"{z}_{x}_{y}.png"
    if cache_path.is_file():
        try:
            return Image.open(cache_path).convert("RGBA")
        except Exception:
            pass  # cache korup, fetch ulang di bawah

    url = OSM_TILE_URL.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None

    tile_cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_bytes(data)
    except OSError:
        pass  # cache write gagal (disk penuh dll) — tetap lanjut render dari memory

    try:
        import io
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def _draw_pin_marker(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int,
                     color: Tuple[int, int, int] = MARKER_COLOR_DEFAULT) -> None:
    """Pin lokasi bentuk tetes air (teardrop) — bulat di atas, meruncing ke
    bawah, TITIK RUNCING PERSIS di (cx, cy) (itu koordinat pelanggan
    sesungguhnya, jangan pusat lingkaran atas). Gaya sama seperti pin peta
    pada umumnya (Google Maps dkk), lebih halus dari ellipse+segitiga polos.
    `color` — pembeda modul (SK hijau, SR kuning, lihat MARKER_COLOR_BY_MODULE)."""
    head_r = size * 0.62
    head_cy = cy - size * 1.35  # pusat lingkaran kepala pin, DI ATAS titik acuan

    # Badan pin: gabungan lingkaran (kepala) + segitiga (ekor meruncing),
    # digambar sebagai 1 polygon halus (banyak titik di kurva kepala) supaya
    # sambungan kepala->ekor tidak patah.
    import math as _m
    points = []
    # Kurva atas kepala pin (270° -> ekor kiri-bawah, searah jarum jam)
    tail_half_angle = _m.asin(min(1.0, (size * 0.30) / head_r))
    start_angle = _m.pi / 2 + tail_half_angle
    end_angle = _m.pi / 2 - tail_half_angle + 2 * _m.pi
    steps = 28
    for i in range(steps + 1):
        a = start_angle + (end_angle - start_angle) * i / steps
        points.append((cx + head_r * _m.cos(a), head_cy + head_r * _m.sin(a)))
    points.append((cx, cy))  # ujung runcing di titik koordinat sesungguhnya

    shadow_offset = max(1, size // 12)
    draw.polygon([(px + shadow_offset, py + shadow_offset) for px, py in points],
                 fill=(0, 0, 0, 60))
    draw.polygon(points, fill=color + (255,), outline=(255, 255, 255, 255))
    draw.ellipse(
        [cx - size * 0.24, head_cy - size * 0.24, cx + size * 0.24, head_cy + size * 0.24],
        fill=(255, 255, 255, 255),
    )


def _font(size_px: int) -> ImageFont.ImageFont:
    """Font label nama — coba Arial (konsisten dengan font kop DXF/PDF di
    project ini), fallback font bawaan Pillow kalau tidak ada di server."""
    for candidate in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size_px)
        except Exception:
            continue
    return ImageFont.load_default()


def render_static_map(lat: float, lng: float, *,
                       zoom: int = 17,
                       width_px: int = 640,
                       height_px: int = 480,
                       tile_cache_dir: Path,
                       label: Optional[str] = None,
                       module: Optional[str] = None,
                       supersample: int = 2) -> Image.Image:
    """Stitch tile OSM jadi 1 canvas berpusat di (lat, lng): pin + cincin
    ungu (RING_COLOR, sama seperti contoh AutoCAD) PERSIS di tengah (canvas
    selalu di-crop mengelilingi titik ini), opsional label nama pelanggan
    di bawah-kanan marker. Warna pin dari `module` (SK hijau, SR kuning,
    lihat MARKER_COLOR_BY_MODULE), fallback merah kalau modul tak dikenal.

    `supersample` — tile OSM gratis TIDAK punya varian @2x/retina (beda
    dari Mapbox/Stadia berbayar), jadi ketajaman PETA LATAR didapat dengan
    stitch tile di `supersample`x ukuran target (tile native lebih banyak
    per area), lalu DOWNSCALE ke ukuran akhir pakai LANCZOS — hasil lebih
    halus tanpa mengubah area geografis yang terlihat. Marker/cincin/teks
    (SHAPE VEKTOR yang saya gambar sendiri, bukan foto tile) SENGAJA
    DIGAMBAR SESUDAH downscale, di resolusi FINAL — supersample+LANCZOS
    pada shape solid justru bikin tepi jadi blur/soft (anti-aliasing
    berlebih), beda dari tile foto yang memang diuntungkan proses itu.

    Return PIL.Image RGB (tanpa alpha, konsisten dengan PNG logo biasa)."""
    render_w = width_px * supersample
    render_h = height_px * supersample

    center_px_x, center_px_y = latlng_to_global_pixel(lat, lng, zoom)
    origin_x = center_px_x - render_w / 2
    origin_y = center_px_y - render_h / 2

    canvas = Image.new("RGBA", (render_w, render_h), (240, 240, 240, 255))

    first_tile_x = int(origin_x // TILE_SIZE)
    first_tile_y = int(origin_y // TILE_SIZE)
    last_tile_x = int((origin_x + render_w) // TILE_SIZE)
    last_tile_y = int((origin_y + render_h) // TILE_SIZE)

    for tx in range(first_tile_x, last_tile_x + 1):
        for ty in range(first_tile_y, last_tile_y + 1):
            tile_img = _fetch_tile(zoom, tx, ty, tile_cache_dir)
            if tile_img is None:
                continue
            paste_x = int(tx * TILE_SIZE - origin_x)
            paste_y = int(ty * TILE_SIZE - origin_y)
            canvas.paste(tile_img, (paste_x, paste_y), tile_img)

    if supersample > 1:
        canvas = canvas.resize((width_px, height_px), Image.LANCZOS)

    # Marker/cincin/label digambar DI SINI (resolusi akhir width_px x
    # height_px, TANPA resampling lagi) — tepi tetap tajam/solid.
    draw = ImageDraw.Draw(canvas, "RGBA")
    cx, cy = width_px // 2, height_px // 2

    # Marker size proporsional ke canvas (bukan angka pixel tetap) supaya
    # tetap terlihat wajar di ukuran OLE frame apa pun, tapi jangan sampai
    # mendominasi kanvas (dibatasi max juga, bukan cuma min).
    marker_size = max(9, min(18, min(width_px, height_px) // 30))

    # Cincin ungu mengelilingi marker — 2 lingkaran konsentris (outline
    # tebal + tipis) meniru 2 CIRCLE radius ~8.5/9.3mm di contoh AutoCAD,
    # cuma sekarang di-generate proporsional, bukan ukuran mm tetap.
    ring_outer = marker_size * 2.4
    ring_inner = marker_size * 2.15
    draw.ellipse(
        [cx - ring_outer, cy - ring_outer, cx + ring_outer, cy + ring_outer],
        outline=RING_COLOR + (230,), width=max(2, marker_size // 6),
    )
    draw.ellipse(
        [cx - ring_inner, cy - ring_inner, cx + ring_inner, cy + ring_inner],
        outline=RING_COLOR + (140,), width=max(1, marker_size // 10),
    )

    pin_color = MARKER_COLOR_BY_MODULE.get((module or "").upper(), MARKER_COLOR_DEFAULT)
    _draw_pin_marker(draw, cx, cy, marker_size, pin_color)

    if label:
        font_size = max(11, int(marker_size * 0.85))
        font = _font(font_size)
        text_x = cx + marker_size * 0.9
        text_y = cy + marker_size * 0.3

        # Clamp horizontal: kalau label kepanjangan dan bakal keluar tepi
        # kanan canvas, geser ke KIRI marker sebagai gantinya (bukan
        # terpotong tanpa terbaca).
        text_w = draw.textlength(label, font=font)
        if text_x + text_w > width_px - 4:
            text_x = cx - marker_size * 0.9 - text_w
        text_x = max(4, min(text_x, width_px - text_w - 4))
        text_y = max(4, min(text_y, height_px - font_size - 4))

        # Outline putih tipis di belakang teks (halo) supaya tetap terbaca
        # di atas peta apa pun warna dasarnya, tanpa kotak background.
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    draw.text((text_x + dx, text_y + dy), label, font=font, fill=(255, 255, 255, 255))
        draw.text((text_x, text_y), label, font=font, fill=(30, 30, 30, 255))

    return canvas.convert("RGB")


def _composite_cache_paths(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.png"


def render_static_map_cached(lat: float, lng: float, zoom: int,
                             width_px: int, height_px: int,
                             output_dir: Path,
                             label: Optional[str] = None,
                             module: Optional[str] = None) -> bytes:
    """Cache PNG KOMPOSIT final (bukan cuma tile individual) — key dari
    koordinat DIBULATKAN (bukan per-reff_id customer), supaya dua alamat
    berdekatan atau generate ulang dokumen yang sama share 1 cache. `label`
    (nama pelanggan) IKUT masuk key — koordinat sama TAPI label beda (mis.
    1 keluarga, alamat sama) harus dapat PNG beda, bukan saling timpa
    cache. `module` juga masuk key (warna pin beda per modul). Return PNG
    bytes siap di-base64-encode."""
    map_cache_dir = output_dir / "map_cache"
    tile_cache_dir = output_dir / "map_tile_cache"

    label_part = ""
    if label:
        import hashlib
        label_part = f"_{hashlib.md5(label.encode()).hexdigest()[:10]}"
    module_part = f"_{module.upper()}" if module else ""
    key = f"map_{round(lat, 6)}_{round(lng, 6)}_{zoom}_{width_px}x{height_px}{label_part}{module_part}"
    cache_path = _composite_cache_paths(map_cache_dir, key)

    if cache_path.is_file():
        return cache_path.read_bytes()

    img = render_static_map(
        lat, lng, zoom=zoom, width_px=width_px, height_px=height_px,
        tile_cache_dir=tile_cache_dir, label=label, module=module,
    )

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    map_cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_bytes(png_bytes)
    except OSError:
        pass  # cache write gagal — tetap return hasil render dari memory

    return png_bytes
