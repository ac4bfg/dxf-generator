# DXF Generator Microservice

Microservice untuk generate file DXF/DWG asbuilt SR menggunakan FastAPI dan ezdxf dengan konversi ke DWG via ODA File Converter.

## Requirements

- Python 3.10+
- ezdxf library
- ODA File Converter (untuk konversi DWG)
- xvfb (untuk headless ODA di Linux)

## Instalasi

### 1. Python Dependencies

```bash
cd dxf-generator
pip install -r requirements.txt
```

### 2. ODA File Converter (Linux/WSL2)

Untuk menghasilkan file DWG, perlu install ODA File Converter:

```bash
# Run setup script (requires sudo)
sudo bash setup_oda.sh
```

Atau manual:

```bash
# Install dependencies
sudo apt-get update
sudo apt-get install -y libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
    libxcb-shape0 xvfb

# Install ODA (copy .deb file ke folder ini)
sudo dpkg -i ODAFileConverter_QT6_lnxX64_8.3dll_27.1.deb
sudo apt-get install -f -y
```

## Konfigurasi

Copy `.env.example` ke `.env` dan sesuaikan:

```env
TEMPLATE_PATH=templates/ASBUILT_SR.dxf
OUTPUT_PATH=output
HOST=0.0.0.0
PORT=8099
DEBUG=false
API_KEY=

# ODA File Converter settings
ODA_PATH=/usr/bin/ODAFileConverter
ODA_ENABLED=true
DEFAULT_OUTPUT_FORMAT=dwg
DWG_VERSION=ACAD2018
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TEMPLATE_PATH` | `templates/ASBUILT_SR.dxf` | Path ke template DXF |
| `OUTPUT_PATH` | `output` | Folder output file |
| `HOST` | `0.0.0.0` | Host binding |
| `PORT` | `8099` | Port service |
| `DEBUG` | `false` | Debug mode |
| `API_KEY` | - | Optional API key |
| `ODA_PATH` | `/usr/bin/ODAFileConverter` | Path ke ODA binary |
| `ODA_ENABLED` | `true` | Enable DWG conversion |
| `DEFAULT_OUTPUT_FORMAT` | `dwg` | Default output format |
| `DWG_VERSION` | `ACAD2018` | Versi DWG output |

## Menjalankan (Development)

```bash
python run.py
```

Atau langsung dengan uvicorn:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8099 --reload
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check root |
| GET | `/api/dxf/health` | Health check dengan status ODA |
| POST | `/api/dxf/generate` | Generate single file (DXF/DWG) |
| GET | `/api/dxf/download/{reff_id}` | Download generated file |
| POST | `/api/dxf/bulk-generate` | Generate bulk files (ZIP) |
| GET | `/api/dxf/bulk-download/{filename}` | Download bulk ZIP |

## API Usage

### Generate Single DWG

```bash
curl -X POST http://localhost:8099/api/dxf/generate \
  -H "Content-Type: application/json" \
  -d '{
    "reff_id": "044.10.01.22.0040",
    "nama": "ISTANTO BUDI SANTOSO",
    "alamat": "Perum Griya Harapan Desa Penyangkringan RT 03 Rw 15",
    "rt": "01",
    "rw": "07",
    "kelurahan": "044 - Nawangsari",
    "sektor": "07",
    "no_mgrt": "25110607153",
    "sn_awal": "0.025",
    "koordinat_tapping": "-6.9672, 110.0741",
    "materials": {
      "coupler": "1",
      "elbow": "1",
      "pipa": "20.00",
      "sealtape": "6"
    },
    "output_format": "dwg"
  }'
```

### Generate DXF (tanpa konversi)

```bash
curl -X POST http://localhost:8099/api/dxf/generate \
  -H "Content-Type: application/json" \
  -d '{
    "...",
    "output_format": "dxf"
  }'
```

### Bulk Generate

```bash
curl -X POST http://localhost:8099/api/dxf/bulk-generate \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "zip",
    "output_format": "dwg",
    "items": [
      { ...item1 },
      { ...item2 }
    ]
  }'
```

### Health Check Response

```json
{
  "status": "healthy",
  "version": "1.1.0",
  "template_found": true,
  "oda_available": true,
  "default_format": "dwg"
}
```

## Placeholder di Template DXF

Template DXF harus mengandung placeholder berikut:

| Placeholder | Description |
|-------------|--------------|
| `[TANGGAL]` | Tanggal generate (format Indonesia) |
| `[REFF_ID]` | Customer reference ID |
| `[NAMA]` | Nama pelanggan |
| `[ALAMAT]` | Alamat pelanggan |
| `[RT]` | RT number |
| `[RW]` | RW number |
| `[KELURAHAN]` | Kelurahan |
| `[SEKTOR]` | Kode sektor |
| `[NO_MGRT]` | No Seri MGRT |
| `[SN_AWAL]` | Initial serial number |
| `[KOORDINAT_TAPPING]` | Koordinat tapping |
| `[19]` | Qty Coupler |
| `[10]` | Qty Elbow |
| `[8]` | Qty Pipa (meter) |
| `[7]` | Qty Sealtape |

## Flow Konversi DXF → DWG

```
[Request output_format=dwg]
        ↓
[Generate DXF dari template]
        ↓
[ODA File Converter via xvfb-run]
        ↓
[DWG file di output folder]
        ↓
[Delete DXF perantara]
        ↓
[Return DWG file]
```

## Template DXF

Letakkan file template `ASBUILT_SR.dxf` di folder `templates/`.

## Output

File hasil generate akan disimpan di folder `output/`. File DXF perantara akan dihapus otomatis setelah konversi ke DWG.

## Deployment (Production)

### Server Setup

```bash
# 1. Create dedicated user
sudo useradd -r -m -s /bin/false dxf-service

# 2. Create directory
sudo mkdir -p /opt/dxf-generator
sudo chown dxf-service:dxf-service /opt/dxf-generator

# 3. Clone/copy files
cd /opt/dxf-generator
# Copy all project files

# 4. Setup virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Install ODA File Converter
sudo bash setup_oda.sh

# 6. Create systemd service
sudo nano /etc/systemd/system/dxf-generator.service
```

Systemd service file:
```ini
[Unit]
Description=DXF Generator FastAPI Service
After=network.target

[Service]
User=dxf-service
Group=dxf-service
WorkingDirectory=/opt/dxf-generator
ExecStart=/opt/dxf-generator/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8099
Restart=always
RestartSec=5
Environment="PATH=/opt/dxf-generator/venv/bin"

[Install]
WantedBy=multi-user.target
```

```bash
# 7. Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable dxf-generator
sudo systemctl start dxf-generator

# 8. Check status
sudo systemctl status dxf-generator
curl http://localhost:8099/api/dxf/health
```

## Testing di WSL2

```bash
# Masuk ke WSL2 Ubuntu
wsl -d Ubuntu

# Install dependencies
sudo apt-get update
sudo apt-get install -y libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
    libxcb-shape0 xvfb

# Install ODA
sudo dpkg -i /mnt/c/Users/User/Documents/Kerja/aergas_apbn/ODAFileConverter_QT6_lnxX64_8.3dll_27.1.deb
sudo apt-get install -f -y

# Test ODA
/usr/bin/ODAFileConverter --help

# Run service
cd /mnt/c/Users/User/Documents/Kerja/aergas_apbn/dxf-generator
python run.py
```

## Troubleshooting

### ODA tidak tersedia

Jika `oda_available: false` di health check:

1. Pastikan sudah install ODA File Converter
2. Cek path di `ODA_PATH` di `.env`
3. Pastikan bukan di Windows (ODA Windows berbeda)

### Error xvfb-run

```bash
sudo apt-get install xvfb
```

### Error library Qt/XCB

```bash
sudo apt-get install -y libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0
```

## CI/CD

Repository menggunakan GitHub Actions untuk auto-deploy ke server.

### Required Secrets (in GitHub repo Settings → Secrets):

| Secret | Value |
|--------|-------|
| `SERVER_IP` | IP server |
| `SERVER_USERNAME` | SSH username |
| `SSH_PRIVATE_KEY` | Private key untuk SSH |

### Workflow

Setiap push ke branch `main` akan auto-deploy ke server.

## Development

```bash
# Install in development mode
pip install -r requirements.txt

# Run with hot reload
uvicorn app.main:app --host 0.0.0.0 --port 8099 --reload
```