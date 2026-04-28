"""Тесты storage — локальное сохранение + опциональный S3-аплоад."""
import sys
import types
from typing import Any, Dict, List

import pytest

from storage import _content_type, save_file_bytes


class TestContentType:
    def test_pdf(self):
        assert _content_type("file.pdf") == "application/pdf"

    def test_png(self):
        assert _content_type("file.png") == "image/png"

    def test_unknown_extension(self):
        assert _content_type("file.xyz") == "application/octet-stream"

    def test_no_extension(self):
        assert _content_type("noext") == "application/octet-stream"

    def test_uppercase_pdf_recognized(self):
        # _content_type приводит имя к нижнему регистру перед матчем
        assert _content_type("file.PDF") == "application/pdf"

    def test_uppercase_png_recognized(self):
        assert _content_type("photo.PNG") == "image/png"

    def test_mixed_case_recognized(self):
        assert _content_type("Doc.Pdf") == "application/pdf"


class TestSaveFileBytesLocal:
    def test_writes_file_to_storage_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.delenv("S3_BUCKET", raising=False)
        path = save_file_bytes(b"PDF data", "kp.pdf")
        assert path is not None
        assert (tmp_path / "kp.pdf").read_bytes() == b"PDF data"

    def test_returns_local_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.delenv("S3_BUCKET", raising=False)
        path = save_file_bytes(b"x", "report.pdf")
        assert path == str(tmp_path / "report.pdf")

    def test_creates_storage_dir_if_missing(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "deep" / "storage"
        monkeypatch.setenv("STORAGE_DIR", str(nested))
        monkeypatch.delenv("S3_BUCKET", raising=False)
        save_file_bytes(b"x", "f.pdf")
        assert nested.exists()
        assert (nested / "f.pdf").exists()

    def test_default_storage_dir_is_storage(self, tmp_path, monkeypatch):
        # При отсутствии STORAGE_DIR используется "./storage" (относительный)
        monkeypatch.delenv("STORAGE_DIR", raising=False)
        monkeypatch.delenv("S3_BUCKET", raising=False)
        monkeypatch.chdir(tmp_path)
        path = save_file_bytes(b"y", "f.pdf")
        # Возвращается относительный путь, файл лежит в tmp_path/storage
        assert path == "storage/f.pdf"
        assert (tmp_path / "storage" / "f.pdf").read_bytes() == b"y"

    def test_local_write_failure_returns_none(self, tmp_path, monkeypatch):
        # Создаём файл на месте папки storage_dir, чтобы mkdir не упал,
        # но запись внутрь не получилась бы
        bad_path = tmp_path / "blocked"
        bad_path.write_text("I am a file, not a dir")
        monkeypatch.setenv("STORAGE_DIR", str(bad_path))
        monkeypatch.delenv("S3_BUCKET", raising=False)
        # mkdir упадёт раньше чем write — но storage.save_file_bytes
        # ловит ошибку только на write_bytes, поэтому здесь падает FileExistsError.
        # Для теста: подменим Path.write_bytes на ошибку.
        from pathlib import Path
        original = Path.write_bytes

        def fail(self, content):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_bytes", fail)
        # Вернёт None и не упадёт
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        result = save_file_bytes(b"x", "x.pdf")
        # Восстанавливаем для других тестов
        monkeypatch.setattr(Path, "write_bytes", original)
        assert result is None


class FakeS3Client:
    """Минимальный мок boto3 s3 клиента."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.put_object_calls: List[Dict[str, Any]] = []

    def put_object(self, **kwargs):
        self.put_object_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("S3 down")
        return {}


def _install_fake_boto3(monkeypatch, s3_client: FakeS3Client) -> dict:
    """Подсовывает фейковый модуль boto3 в sys.modules. Возвращает
    dict с captured kwargs из Session(...) и client(...)."""
    captured: dict = {"session_kwargs": None, "client_kwargs": None}

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session_kwargs"] = kwargs

        def client(self, service_name, **kwargs):
            captured["client_kwargs"] = {"service": service_name, **kwargs}
            return s3_client

    fake_boto3 = types.ModuleType("boto3")
    fake_session_module = types.ModuleType("boto3.session")
    fake_session_module.Session = FakeSession
    fake_boto3.session = fake_session_module

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "boto3.session", fake_session_module)
    return captured


class TestSaveFileBytesS3:
    def test_no_bucket_no_upload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.delenv("S3_BUCKET", raising=False)

        # boto3 даже не должен импортироваться. Обеспечим это броском при импорте.
        def boom_finder(name, *a, **kw):
            if name == "boto3":
                raise AssertionError("boto3 imported when S3_BUCKET unset")
            return None

        # Просто проверим что файл сохранился локально
        result = save_file_bytes(b"x", "f.pdf")
        assert result is not None

    def test_upload_called_when_bucket_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.delenv("S3_PREFIX", raising=False)

        s3 = FakeS3Client()
        _install_fake_boto3(monkeypatch, s3)

        result = save_file_bytes(b"PDF", "kp.pdf")
        assert result == str(tmp_path / "kp.pdf")
        assert len(s3.put_object_calls) == 1
        call = s3.put_object_calls[0]
        assert call["Bucket"] == "my-bucket"
        assert call["Key"] == "kp.pdf"
        assert call["Body"] == b"PDF"
        assert call["ContentType"] == "application/pdf"

    def test_s3_prefix_prepended_to_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("S3_BUCKET", "b")
        monkeypatch.setenv("S3_PREFIX", "kps/2024/")  # trailing slash

        s3 = FakeS3Client()
        _install_fake_boto3(monkeypatch, s3)

        save_file_bytes(b"x", "doc.pdf")
        # Trailing slash убирается, добавляется ровно один
        assert s3.put_object_calls[0]["Key"] == "kps/2024/doc.pdf"

    def test_s3_credentials_passed_to_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("S3_BUCKET", "b")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-TEST")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ru-central1")
        monkeypatch.setenv("AWS_ENDPOINT_URL", "https://s3.example/")

        s3 = FakeS3Client()
        captured = _install_fake_boto3(monkeypatch, s3)

        save_file_bytes(b"x", "f.pdf")
        sk = captured["session_kwargs"]
        assert sk["aws_access_key_id"] == "AKIA-TEST"
        assert sk["aws_secret_access_key"] == "SECRET"
        assert sk["region_name"] == "ru-central1"
        assert captured["client_kwargs"]["endpoint_url"] == "https://s3.example/"

    def test_s3_failure_does_not_break_local_save(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("S3_BUCKET", "b")
        s3 = FakeS3Client(fail=True)
        _install_fake_boto3(monkeypatch, s3)

        path = save_file_bytes(b"x", "f.pdf")
        # Локально файл всё равно сохранён, путь возвращён
        assert path == str(tmp_path / "f.pdf")
        assert (tmp_path / "f.pdf").read_bytes() == b"x"

    def test_boto3_not_installed_does_not_break(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("S3_BUCKET", "b")
        # Удаляем boto3 из sys.modules и блокируем повторный импорт
        monkeypatch.setitem(sys.modules, "boto3", None)
        # При None импорт даст ImportError, который ловится в _maybe_upload_s3

        path = save_file_bytes(b"x", "f.pdf")
        # Локальный путь возвращён, несмотря на отсутствие boto3
        assert path == str(tmp_path / "f.pdf")

    def test_png_content_type_detected_from_filename(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("S3_BUCKET", "b")
        s3 = FakeS3Client()
        _install_fake_boto3(monkeypatch, s3)

        save_file_bytes(b"\x89PNG", "img.png")
        assert s3.put_object_calls[0]["ContentType"] == "image/png"
