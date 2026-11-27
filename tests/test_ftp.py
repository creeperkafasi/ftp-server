#!/usr/bin/env python3
"""
tests/test_ftp.py - FTP Sunucu ve İstemci Uçtan Uca Test Paketi

Hem projedeki ham soket tabanlı `FTPClient` hem de Python standart kütüphanesindeki
`ftplib.FTP` (Pasif ve Aktif Mod) ile sunucuyu test eder.
"""

import ftplib
import io
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from client import FTPClient
from server import FTPServer


class FTPServerClientTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory()
        self.temp_local = tempfile.TemporaryDirectory()

        self.server = FTPServer(
            host="127.0.0.1",
            port=0,  # İşletim sisteminden boş port al
            root_dir=self.temp_root.name,
            users={"user": "pass", "deniz": "gizli123"},
            allow_anonymous=True,
            quiet=True,
            seed_sample_files=True,
        )
        self.server.bind()
        self.port = self.server.port

        self.server_thread = threading.Thread(target=self.server.start, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.stop()
        self.server_thread.join(timeout=3.0)
        self.temp_root.cleanup()
        self.temp_local.cleanup()

    def test_custom_client_full_workflow_and_binary_integrity(self):
        client = FTPClient(verbose=False)
        try:
            self.assertTrue(client.connect("127.0.0.1", self.port))
            self.assertFalse(client.login("user", "yanlis_sifre"))
            self.assertTrue(client.login("user", "pass"))

            self.assertEqual(client.pwd(), "/")
            entries = client.list_files()
            self.assertIsNotNone(entries)
            self.assertTrue(any("data.txt" in line for line in entries))

            # Klasör oluşturma ve gezinme
            self.assertTrue(client.mkdir("proje_dosyalari"))
            self.assertTrue(client.cwd("proje_dosyalari"))
            self.assertEqual(client.pwd(), "/proje_dosyalari")

            # İçinde sahte EOF işaretçileri, null byte ve Türkçe karakterler barındıran ikili veri
            payload = (
                "Merhaba Dünya! ğüşöçıİĞÜŞÖÇ\r\n".encode("utf-8")
                + b"\x00\xff\xfe\r\nEOF\r\n\r\nEND\r\n"
                + os.urandom(150_000)
            )
            src_file = os.path.join(self.temp_local.name, "ikili_veri.bin")
            dst_file = os.path.join(self.temp_local.name, "indirilen_veri.bin")
            with open(src_file, "wb") as f:
                f.write(payload)

            self.assertTrue(client.upload_file(src_file, "yuklenen.bin"))
            self.assertEqual(client.file_size("yuklenen.bin"), len(payload))
            self.assertTrue(client.rename("yuklenen.bin", "yeni_ad.bin"))
            self.assertTrue(client.download_file("yeni_ad.bin", dst_file))

            with open(dst_file, "rb") as f:
                downloaded = f.read()
            self.assertEqual(downloaded, payload)

            # Silme ve üst dizine dönüş
            self.assertTrue(client.delete_file("yeni_ad.bin"))
            self.assertTrue(client.cdup())
            self.assertEqual(client.pwd(), "/")
            self.assertTrue(client.rmdir("proje_dosyalari"))
            self.assertTrue(client.help_command())
        finally:
            client.quit()

    def test_path_traversal_blocked(self):
        client = FTPClient(verbose=False)
        try:
            self.assertTrue(client.connect("127.0.0.1", self.port))
            self.assertTrue(client.login("user", "pass"))

            # Kök dizin dışına çıkma denemeleri engellenmeli
            self.assertFalse(client.cwd("../../"))
            self.assertFalse(client.cwd("/../../etc"))
            self.assertEqual(client.pwd(), "/")

            # Windows geçersiz dosya karakterleri reddedilmeli
            sample = os.path.join(self.temp_local.name, "ok.txt")
            with open(sample, "w", encoding="utf-8") as f:
                f.write("test")
            self.assertFalse(client.upload_file(sample, "gecersiz:ad?.txt"))
        finally:
            client.quit()

    def test_stdlib_ftplib_passive_and_active_modes(self):
        """Standart RFC 959 istemcisi (ftplib) ile hem PASV hem PORT modunu doğrular."""
        with ftplib.FTP() as ftp:
            ftp.connect("127.0.0.1", self.port, timeout=10)
            ftp.login("anonymous", "anon@example.com")

            # Pasif mod (PASV)
            ftp.set_pasv(True)
            names = ftp.nlst()
            self.assertIn("data.txt", names)

            buf = io.BytesIO()
            ftp.retrbinary("RETR data.txt", buf.write)
            self.assertIn("data.txt", buf.getvalue().decode("utf-8"))

            # Aktif mod (PORT)
            ftp.set_pasv(False)
            active_payload = b"Aktif mod (PORT) uzerinden yuklenen veri!\n"
            ftp.storbinary("STOR aktif_test.txt", io.BytesIO(active_payload))

            verify_buf = io.BytesIO()
            ftp.retrbinary("RETR aktif_test.txt", verify_buf.write)
            self.assertEqual(verify_buf.getvalue(), active_payload)

    def test_concurrent_clients(self):
        errors = []

        def worker(idx: int):
            c = FTPClient(verbose=False)
            try:
                if not c.connect("127.0.0.1", self.port):
                    errors.append(f"Client {idx} connect failed")
                    return
                if not c.login("deniz", "gizli123"):
                    errors.append(f"Client {idx} login failed")
                    return
                local_in = os.path.join(self.temp_local.name, f"up_{idx}.txt")
                local_out = os.path.join(self.temp_local.name, f"down_{idx}.txt")
                content = f"İstemci #{idx} verisi\n" * 200
                with open(local_in, "w", encoding="utf-8") as f:
                    f.write(content)
                if not c.upload_file(local_in, f"parallel_{idx}.txt"):
                    errors.append(f"Client {idx} upload failed")
                    return
                if not c.download_file(f"parallel_{idx}.txt", local_out):
                    errors.append(f"Client {idx} download failed")
                    return
                with open(local_out, "r", encoding="utf-8") as f:
                    if f.read() != content:
                        errors.append(f"Client {idx} content mismatch")
            finally:
                c.quit()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
