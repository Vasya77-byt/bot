import json
import time

import pytest

from cache import FileTTLCache


@pytest.fixture
def cache(tmp_path):
    return FileTTLCache(name="test_cache", ttl=60.0, dir_path=str(tmp_path))


class TestFileTTLCache:
    def test_set_then_get_returns_value(self, cache):
        cache.set("key1", {"a": 1})
        assert cache.get("key1") == {"a": 1}

    def test_get_missing_key_returns_none(self, cache):
        assert cache.get("nonexistent") is None

    def test_get_expired_returns_none(self, tmp_path):
        cache = FileTTLCache(name="exp", ttl=0.05, dir_path=str(tmp_path))
        cache.set("k", "v")
        time.sleep(0.1)
        assert cache.get("k") is None

    def test_expired_entry_is_removed_from_storage(self, tmp_path):
        cache = FileTTLCache(name="exp2", ttl=0.05, dir_path=str(tmp_path))
        cache.set("k", "v")
        time.sleep(0.1)
        cache.get("k")  # триггерит удаление
        with cache.path.open() as f:
            data = json.load(f)
        assert "k" not in data

    def test_overwrite_existing_key(self, cache):
        cache.set("k", "first")
        cache.set("k", "second")
        assert cache.get("k") == "second"

    def test_persists_to_disk(self, tmp_path):
        cache1 = FileTTLCache(name="persist", ttl=60.0, dir_path=str(tmp_path))
        cache1.set("k", [1, 2, 3])
        # новый инстанс читает тот же файл
        cache2 = FileTTLCache(name="persist", ttl=60.0, dir_path=str(tmp_path))
        assert cache2.get("k") == [1, 2, 3]

    def test_corrupt_file_returns_none(self, tmp_path):
        cache = FileTTLCache(name="corrupt", ttl=60.0, dir_path=str(tmp_path))
        cache.path.write_text("not json {{{")
        assert cache.get("anything") is None

    def test_creates_cache_directory(self, tmp_path):
        nested = tmp_path / "nested" / "dir"
        cache = FileTTLCache(name="c", ttl=60.0, dir_path=str(nested))
        cache.set("k", "v")
        assert nested.exists()
        assert (nested / "c.json").exists()

    def test_multiple_keys_coexist(self, cache):
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        assert cache.get("a") == 1
        assert cache.get("b") == 2
        assert cache.get("c") == 3

    def test_unicode_value_roundtrip(self, cache):
        cache.set("ru", "Сбербанк")
        assert cache.get("ru") == "Сбербанк"
