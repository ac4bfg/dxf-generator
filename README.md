# DXF Generator Microservice

Microservice untuk generate file DXF asbuilt SR menggunakan FastAPI dan ezdxf.

## Requirements

- Python 3.10+
- ezdxf library

## Instalasi

```bash
cd dxf-generator
pip install -r requirements.txt
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
```

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
| GET | `/api/dxf/health` | Health check service |
| POST | `/api/dxf/generate` | Generate single DXF |
| GET | `/api/dxf/download/{reff_id}` | Download generated DXF |
| POST | `/api/dxf/bulk-generate` | Generate bulk DXF (ZIP) |
| GET | `/api/dxf/bulk-download/{filename}` | Download bulk ZIP |

## API Usage

### Generate Single DXF

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
    }
  }'
```

### Bulk Generate

```bash
curl -X POST http://localhost:8099/api/dxf/bulk-generate \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "zip",
    "items": [
      { ...item1 },
      { ...item2 }
    ]
  }'
```

## Placeholder di Template DXF

Template DXF harus mengandung placeholder berikut:

| Placeholder | Description |
|-------------|-------------|
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

## Template DXF

Letakkan file template `ASBUILT_SR.dxf` di folder `templates/`.

## Output

File hasil generate akan disimpan di folder `output/`. File akan dihapus secara otomatis setelah didownload.

## Deployment (Production)

### Server Setup

```bash
# 1. Create dedicated user
sudo useradd -r -m -s /bin/false dxf-service

# 2. Create directory
sudo mkdir -p /opt/dxf-generator
sudo chown dxf-service:dxf-service /opt/dxf-generator

# 3. Clone repo (sekali saja)
cd /opt/dxf-generator
sudo -u dxf-service git init
sudo -u dxf-service git remote add origin git@github.com:ac4bfg/dxf-generator.git
sudo -u dxf-service git pull origin main

# 4. Setup virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Create systemd service
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
# 6. Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable dxf-generator
sudo systemctl start dxf-generator

# 7. Check status
sudo systemctl status dxf-generator
curl http://localhost:8099/api/dxf/health
```

## CI/CD

Repository menggunakan GitHub Actions untuk auto-deploy ke server.

### Required Secrets (in GitHub repo Settings → Secrets):

| Secret | Value |
|--------|-------|
| `SERVER_IP` | IP server (e.g. 202.155.157.59) |
| `SERVER_USERNAME` | SSH username (e.g. root) |
| `SSH_PRIVATE_KEY` | Private key untuk SSH ke server |

### Workflow

Setiap push ke branch `main` akan auto-deploy ke server.

## Development

```bash
# Install in development mode
pip install -r requirements.txt

# Run with hot reload
uvicorn app.main:app --host 0.0.0.0 --port 8099 --reload
```




