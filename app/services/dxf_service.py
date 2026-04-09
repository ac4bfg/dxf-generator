import os
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

    def __init__(self, template_path: str, output_path: str):
        self.template_path = Path(template_path)
        self.output_path = Path(output_path)
        self.output_path.mkdir(parents=True, exist_ok=True)

    def generate_tanggal_indonesia(self) -> str:
        waktu_sekarang = datetime.now()
        return f"{waktu_sekarang.day} {self.BULAN_INDO[waktu_sekarang.month]} {waktu_sekarang.year}"

    def prepare_data(self, request_data: dict) -> dict:
        tanggal = request_data.get("tanggal") or self.generate_tanggal_indonesia()

        materials = request_data.get("materials", {})
        if isinstance(materials, dict):
            mat_coupler = materials.get("coupler", "0")
            mat_elbow = materials.get("elbow", "0")
            mat_pipa = materials.get("pipa", "0")
            mat_sealtape = materials.get("sealtape", "0")
        else:
            mat_coupler = materials.get("19", "0")
            mat_elbow = materials.get("10", "0")
            mat_pipa = materials.get("8", "0")
            mat_sealtape = materials.get("7", "0")

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
            "[19]": str(mat_coupler),
            "[10]": str(mat_elbow),
            "[8]": str(mat_pipa),
            "[7]": str(mat_sealtape),
        }

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

    def generate_single(self, request_data: dict) -> tuple[bool, str, Optional[Path]]:
        data = self.prepare_data(request_data)
        reff_id = request_data.get("reff_id", "unknown")
        # Sanitasi nama file - ganti spasi dan karakter illegal dengan underscore
        safe_reff_id = reff_id.replace(' ', '_').replace('/', '_').replace('\\', '_')
        output_filename = f"ASBUILT_{safe_reff_id}.dxf"

        success, message = self.generate_from_template(data, output_filename)

        if success:
            return True, message, self.output_path / output_filename
        return False, message, None

    def generate_bulk_zip(self, items: list[dict]) -> tuple[bool, str, Optional[Path]]:
        if not items:
            return False, "No items provided", None

        zip_filename = f"ASBUILT_BULK_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path = self.output_path / zip_filename

        try:
            # Load template sekali saja di luar loop
            template_doc = ezdxf.readfile(str(self.template_path))
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for idx, item in enumerate(items):
                    data = self.prepare_data(item)
                    reff_id = item.get("reff_id", f"unknown_{idx}")
                    # Sanitasi nama file
                    safe_reff_id = reff_id.replace(' ', '_').replace('/', '_').replace('\\', '_')
                    temp_filename = f"ASBUILT_{safe_reff_id}.dxf"

                    # Copy doc dari template yang sudah diload
                    import copy
                    temp_doc = copy.deepcopy(template_doc)
                    
                    self.process_modelspace(temp_doc.modelspace(), data)
                    self.process_blocks(temp_doc, data)

                    temp_output = self.output_path / temp_filename
                    temp_doc.saveas(str(temp_output))
                    zipf.write(temp_output, temp_filename)
                    temp_output.unlink()

            return True, f"Created ZIP with {len(items)} files -> {zip_path}", zip_path

        except Exception as e:
            return False, f"Bulk generation error: {e}", None
