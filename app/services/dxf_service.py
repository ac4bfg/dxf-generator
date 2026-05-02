import copy
import os
import platform
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import ezdxf


class DxfService:
    BULAN_INDO = [
        "", "JANUARI", "FEBRUARI", "MARET", "APRIL", "MEI", "JUNI",
        "JULI", "AGUSTUS", "SEPTEMBER", "OKTOBER", "NOVEMBER", "DESEMBER"
    ]

    MATERIAL_MAPPING = {
        "coupler": "19",
        "elbow": "10",
        "pipa": "8",
        "sealtape": "7"
    }

    def __init__(self, template_path: str, output_path: str, oda_path: str = "/usr/bin/ODAFileConverter", dwg_version: str = "ACAD2018"):
        self.template_path = Path(template_path)
        self.output_path = Path(output_path)
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.oda_path = oda_path
        self.dwg_version = dwg_version

    def generate_tanggal_indonesia(self) -> str:
        waktu_sekarang = datetime.now()
        return f"{waktu_sekarang.day} {self.BULAN_INDO[waktu_sekarang.month]} {waktu_sekarang.year}"

    def prepare_data(self, request_data: dict) -> dict:
        tanggal = request_data.get("tanggal") or self.generate_tanggal_indonesia()

        materials = request_data.get("materials", {})
        if not isinstance(materials, dict):
            materials = {}

        # Named SR keys (backward compat)
        mat_coupler  = materials.get("coupler",  materials.get("19", "0"))
        mat_elbow    = materials.get("elbow",    materials.get("10", "0"))
        mat_pipa     = materials.get("pipa",     materials.get("8",  "0"))
        mat_sealtape = materials.get("sealtape", materials.get("7",  "0"))
        mat_casing   = materials.get("casing",   materials.get("21", "0"))

        data = {
            "[TANGGAL]": tanggal,
            "[REFF_ID]": request_data.get("reff_id", "-"),
            "[NAMA]": request_data.get("nama", "-"),
            "[ALAMAT]": request_data.get("alamat", "-"),
            "[RT]": request_data.get("rt", "-"),
            "[RW]": request_data.get("rw", "-"),
            "[KELURAHAN]": request_data.get("kelurahan", "-"),
            "[SEKTOR]": request_data.get("sektor", "-"),
            "[NO_MGRT]": request_data.get("no_mgrt", "-"),
            "[SN_AWAL]": request_data.get("sn_awal", "-"),
            "[KOORDINAT_TAPPING]": request_data.get("koordinat_tapping", "-"),
            "[NO_SK]": str(request_data.get("no_sk", "-") or "-"),
            # SR named material placeholders
            "[19]": str(mat_coupler),
            "[10]": str(mat_elbow),
            "[8]": str(mat_pipa),
            "[7]": str(mat_sealtape),
            "[21]": str(mat_casing),
        }

        # Dynamic: add [ID] replacement for every numeric key in materials dict.
        # Covers both SR (keys "7","8","10","19") and SK (keys "1","2","3","5","6")
        # without hardcoding per-module IDs here.
        for mat_id, qty in materials.items():
            placeholder = f"[{mat_id}]"
            if placeholder not in data:
                data[placeholder] = str(qty) if qty is not None else "0"

        return data

    def replace_text_in_entity(self, entity, data: dict) -> bool:
        replaced = False
        if hasattr(entity, "dxf"):
            if hasattr(entity.dxf, "text"):
                text = entity.dxf.text
                if text and isinstance(text, str):
                    for key, value in data.items():
                        if key in text:
                            text = text.replace(key, value)
                            replaced = True
                    if replaced:
                        entity.dxf.text = text
        return replaced

    def process_modelspace(self, msp, data: dict) -> int:
        replaced_count = 0
        # Direct iteration lebih cepat dari query()
        for entity in msp:
            if entity.dxftype() in ('TEXT', 'MTEXT'):
                if self.replace_text_in_entity(entity, data):
                    replaced_count += 1
        return replaced_count

    def process_blocks(self, doc, data: dict) -> int:
        replaced_count = 0
        for block in doc.blocks:
            # Skip layout blocks and anonymous blocks
            if block.is_any_layout or block.name.startswith("*"):
                continue
            # Direct iteration lebih cepat
            for entity in block:
                if entity.dxftype() in ('TEXT', 'MTEXT'):
                    if self.replace_text_in_entity(entity, data):
                        replaced_count += 1
        return replaced_count

    def generate_from_template(self, data: dict, output_filename: str) -> tuple[bool, str]:
        try:
            if not self.template_path.exists():
                return False, f"Template file '{self.template_path}' not found"

            doc = ezdxf.readfile(str(self.template_path))

            replaced_modelspace = self.process_modelspace(doc.modelspace(), data)
            replaced_blocks = self.process_blocks(doc, data)

            output_file = self.output_path / output_filename
            doc.saveas(str(output_file))

            return True, f"Success! Generated {replaced_modelspace + replaced_blocks} replacements -> {output_file}"

        except IOError as e:
            return False, f"IO Error: File template not found - {e}"
        except Exception as e:
            return False, f"System error: {e}"

    def convert_to_dwg(self, dxf_path: Path, delete_dxf: bool = True) -> tuple[bool, str, Optional[Path]]:
        if platform.system() == "Windows":
            return False, "DWG conversion not supported on Windows. Use WSL2 or Linux server.", None

        if not os.path.exists(self.oda_path):
            return False, f"ODA File Converter not found at {self.oda_path}", None

        if not dxf_path.exists():
            return False, f"DXF file not found: {dxf_path}", None

        dxf_path_abs = dxf_path.resolve()
        dxf_filename = dxf_path.name
        dxf_basename = dxf_path.stem
        expected_dwg = self.output_path / f"{dxf_basename}.dwg"

        try:
            with tempfile.TemporaryDirectory() as input_dir:
                with tempfile.TemporaryDirectory() as output_dir:
                    shutil.copy2(str(dxf_path_abs), os.path.join(input_dir, dxf_filename))

                    cmd = [
                        "/usr/bin/xvfb-run", "-a",
                        self.oda_path,
                        input_dir,
                        output_dir,
                        self.dwg_version,
                        "DWG",
                        "0",
                        "1"
                    ]

                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env={**os.environ, 'PATH': '/usr/bin:/bin:/usr/local/bin'}
                    )

                    if result.returncode != 0:
                        return False, f"ODA conversion failed: {result.stderr}", None

                    temp_dwg_path = os.path.join(output_dir, f"{dxf_basename}.dwg")

                    if not os.path.exists(temp_dwg_path):
                        return False, f"DWG output not found: {temp_dwg_path}", None

                    shutil.move(temp_dwg_path, str(expected_dwg))

                    if delete_dxf and dxf_path.exists():
                        dxf_path.unlink()

                    return True, f"Converted to DWG -> {expected_dwg}", expected_dwg

        except subprocess.TimeoutExpired:
            return False, "ODA conversion timeout (120s)", None
        except Exception as e:
            return False, f"Conversion error: {e}", None

    def generate_single(self, request_data: dict, output_format: str = "dwg") -> tuple[bool, str, Optional[Path]]:
        data = self.prepare_data(request_data)
        reff_id = request_data.get("reff_id", "unknown")
        safe_reff_id = reff_id.replace(' ', '_').replace('/', '_').replace('\\', '_')
        
        dxf_filename = f"ASBUILT_{safe_reff_id}.dxf"
        success, message = self.generate_from_template(data, dxf_filename)

        if not success:
            return False, message, None

        dxf_path = self.output_path / dxf_filename

        if output_format.lower() == "dwg":
            dwg_success, dwg_message, dwg_path = self.convert_to_dwg(dxf_path, delete_dxf=True)
            
            if dwg_success:
                return True, dwg_message, dwg_path
            else:
                if dxf_path.exists():
                    dxf_path.unlink()
                return False, dwg_message, None

        return True, message, dxf_path

    def generate_bulk_zip(self, items: list[dict], output_format: str = "dwg") -> tuple[bool, str, Optional[Path]]:
        if not items:
            return False, "No items provided", None

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        extension = "dwg" if output_format.lower() == "dwg" else "dxf"
        zip_filename = f"ASBUILT_BULK_{timestamp}.zip"
        zip_path = self.output_path / zip_filename

        try:
            template_doc = ezdxf.readfile(str(self.template_path))
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for idx, item in enumerate(items):
                    data = self.prepare_data(item)
                    reff_id = item.get("reff_id", f"unknown_{idx}")
                    safe_reff_id = reff_id.replace(' ', '_').replace('/', '_').replace('\\', '_')
                    
                    dxf_filename = f"ASBUILT_{safe_reff_id}.dxf"
                    temp_doc = copy.deepcopy(template_doc)
                    
                    self.process_modelspace(temp_doc.modelspace(), data)
                    self.process_blocks(temp_doc, data)

                    temp_dxf = self.output_path / dxf_filename
                    temp_doc.saveas(str(temp_dxf))

                    if output_format.lower() == "dwg":
                        success, msg, dwg_path = self.convert_to_dwg(temp_dxf, delete_dxf=True)
                        if success and dwg_path:
                            zipf.write(dwg_path, f"ASBUILT_{safe_reff_id}.dwg")
                            if dwg_path.exists():
                                dwg_path.unlink()
                        else:
                            if temp_dxf.exists():
                                temp_dxf.unlink()
                    else:
                        zipf.write(temp_dxf, dxf_filename)
                        if temp_dxf.exists():
                            temp_dxf.unlink()

            return True, f"Created ZIP with {len(items)} files -> {zip_path}", zip_path

        except Exception as e:
            return False, f"Bulk generation error: {e}", None
