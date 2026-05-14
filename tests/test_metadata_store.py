"""Тесты MetadataStore: append-only jsonl-журнал сгенерированных КП."""
import json

import pytest

from metadata_store import MetadataStore
from schemas import CompanyData


@pytest.fixture
def store(tmp_path):
    return MetadataStore(base_dir=str(tmp_path / "meta"))


def _read_lines(store: MetadataStore) -> list[dict]:
    with store.meta_path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


class TestMetadataStore:
    def test_creates_base_dir(self, tmp_path):
        target = tmp_path / "deep" / "meta"
        store = MetadataStore(base_dir=str(target))
        assert target.exists()
        assert store.meta_path.parent == target

    def test_append_writes_jsonl_record(self, store):
        company = CompanyData(inn="7707083893", name="ООО Ромашка")
        store.append("kp_1.pdf", company, "pdf")

        lines = _read_lines(store)
        assert len(lines) == 1
        rec = lines[0]
        assert rec["filename"] == "kp_1.pdf"
        assert rec["format"] == "pdf"
        assert rec["inn"] == "7707083893"
        assert rec["name"] == "ООО Ромашка"
        assert isinstance(rec["ts"], (int, float))

    def test_append_with_none_company_leaves_inn_name_null(self, store):
        store.append("kp_no_company.png", None, "png")
        rec = _read_lines(store)[0]
        assert rec["inn"] is None
        assert rec["name"] is None
        assert rec["filename"] == "kp_no_company.png"

    def test_multiple_appends_each_on_own_line(self, store):
        store.append("a.pdf", CompanyData(inn="1", name="A"), "pdf")
        store.append("b.pdf", CompanyData(inn="2", name="B"), "pdf")
        store.append("c.png", None, "png")

        lines = _read_lines(store)
        assert [r["filename"] for r in lines] == ["a.pdf", "b.pdf", "c.png"]

    def test_unicode_round_trip(self, store):
        store.append("файл.pdf", CompanyData(inn="1", name="ООО «Тест»"), "pdf")
        rec = _read_lines(store)[0]
        assert rec["name"] == "ООО «Тест»"

    def test_uses_metadata_dir_env_when_base_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("METADATA_DIR", str(tmp_path / "from_env"))
        store = MetadataStore()
        assert (tmp_path / "from_env").exists()
        assert store.meta_path.parent == tmp_path / "from_env"

    def test_append_failure_swallowed_silently(self, store, monkeypatch):
        # Эмулируем ошибку открытия файла на запись — append не должен падать
        original_open = type(store.meta_path).open

        def fail_open(self, *args, **kwargs):
            if "a" in (args[0] if args else kwargs.get("mode", "")):
                raise OSError("disk full")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(type(store.meta_path), "open", fail_open)
        # не должно бросить исключение
        store.append("x.pdf", CompanyData(inn="1"), "pdf")
