import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from schemas import CompanyData


class MetadataStore:
    """
    Append-only jsonl log of generated KP files.
    """

    def __init__(self, base_dir: Optional[str] = None) -> None:
        self.base_dir = Path(base_dir or os.getenv("METADATA_DIR", "metadata"))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.base_dir / "kp_metadata.jsonl"

    def append(self, filename: str, company: Optional[CompanyData], format_: str) -> None:
        record: Dict[str, Any] = {
            "ts": time.time(),
            "filename": filename,
            "format": format_,
            "inn": company.inn if company else None,
            "name": company.name if company else None,
        }
        try:
            with self.meta_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # logging here would be noisy; best-effort
            pass
 