import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from schemas import CompanyData


class MetadataStore:
    """
    Append-only jsonl log of generated KP files.
    Поддерживает разделение по годам: kp_metadata_YYYY.jsonl
    """

    def __init__(self, base_dir: Optional[str] = None, year: Optional[int] = None) -> None:
        self.base_dir = Path(base_dir or os.getenv("METADATA_DIR", "metadata"))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.year = year or datetime.now().year
        self.meta_path = self.base_dir / f"kp_metadata_{self.year}.jsonl"
        # Обратная совместимость: если есть старый файл без года, используем его
        self._legacy_path = self.base_dir / "kp_metadata.jsonl"

    def append(self, filename: str, company: Optional[CompanyData], format_: str) -> None:
        record: Dict[str, Any] = {
            "ts": time.time(),
            "filename": filename,
            "format": format_,
            "inn": company.inn if company else None,
            "name": company.name if company else None,
            "year": self.year,
        }
        try:
            with self.meta_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def read_records(self, year: Optional[int] = None) -> List[Dict[str, Any]]:
        path = self.base_dir / f"kp_metadata_{year or self.year}.jsonl"
        if not path.exists():
            return []
        records = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except Exception:
            pass
        return records

    def archive_year(self, year: int) -> Optional[Path]:
        """
        Архивирует метаданные указанного года в подпапку archive/.
        Возвращает путь к архиву или None.
        """
        src = self.base_dir / f"kp_metadata_{year}.jsonl"
        # Также проверяем legacy-файл
        if not src.exists() and self._legacy_path.exists():
            src = self._legacy_path

        if not src.exists():
            return None

        archive_dir = self.base_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        dst = archive_dir / f"kp_metadata_{year}.jsonl"
        try:
            shutil.copy2(str(src), str(dst))
            return dst
        except Exception:
            return None
