import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from storage import InputFileStore


class InputFileStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = InputFileStore(self.root / "store")

    def upload(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def stored_files(self) -> list[Path]:
        return [path for path in (self.root / "store").rglob("*") if path.is_file()]

    def test_same_content_is_stored_once_even_with_different_names(self):
        first = self.store.put(self.upload("bao gia.jpg", b"image"))
        second = self.store.put(self.upload("khac.jpeg", b"image"))
        sha256 = hashlib.sha256(b"image").hexdigest()
        self.assertEqual((first.sha256, first.created), (sha256, True))
        self.assertEqual((second.sha256, second.created), (sha256, False))
        self.assertEqual(first.relative_path, f"{sha256[:2]}/{sha256}")
        self.assertEqual(self.stored_files(), [self.store.path_for(sha256)])
        self.assertEqual(self.store.path_for(sha256).read_bytes(), b"image")

    def test_different_content_is_stored_separately(self):
        self.store.put(self.upload("a.jpg", b"one"))
        self.store.put(self.upload("b.jpg", b"two"))
        self.assertEqual(len(self.stored_files()), 2)

    def test_concurrent_puts_of_same_content_keep_one_complete_file(self):
        sources = [self.upload(f"{index}.jpg", b"x" * 1_000_000) for index in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.store.put, sources))
        self.assertEqual(sum(result.created for result in results), 1)
        files = self.stored_files()  # không còn file tạm .tmp sót lại
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_size, 1_000_000)


if __name__ == "__main__":
    unittest.main()
