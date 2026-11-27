#!/usr/bin/env python3
"""
client.py - Çapraz Platform (Windows / Linux / macOS) FTP İstemcisi

Yalnızca Python standart kütüphanesi (`socket`) kullanarak RFC 959 FTP
kontrol bağlantısı ve PASV (Pasif Mod) veri bağlantısı üzerinden dosya listeleme,
indirme, yükleme ve dizin yönetimi işlemlerini gerçekleştirir.
"""

import argparse
import os
import re
import shlex
import socket
import sys


def configure_console_utf8():
    """Windows CMD / PowerShell dahil tüm terminallerde UTF-8 çıktıyı etkinleştirir."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


class FTPClient:
    """Ham TCP soketleri üzerinden çalışan RFC 959 uyumlu FTP istemcisi."""

    PASV_REGEX = re.compile(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)")

    def __init__(self, verbose: bool = True, timeout: float = 30.0):
        self.control_sock: socket.socket | None = None
        self.connected = False
        self.logged_in = False
        self.verbose = verbose
        self.timeout = timeout
        self.host = "127.0.0.1"
        self.port = 2121
        self._recv_buffer = b""

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    # ------------------------------------------------------------------
    # Kontrol bağlantısı okuma / yazma (çok satırlı RFC 959 yanıt destekli)
    # ------------------------------------------------------------------

    def _read_line(self) -> str:
        if self.control_sock is None:
            raise ConnectionError("Kontrol soketi açık değil")

        while b"\n" not in self._recv_buffer:
            chunk = self.control_sock.recv(4096)
            if not chunk:
                raise ConnectionError("Sunucu bağlantıyı kapattı")
            self._recv_buffer += chunk

        raw_line, self._recv_buffer = self._recv_buffer.split(b"\n", 1)
        return raw_line.decode("utf-8", errors="replace").rstrip("\r")

    def get_response(self) -> tuple[int, str]:
        """
        Sunucudan tek satırlı (`220 Merhaba`) veya çok satırlı
        (`214-...` -> `214 Son`) FTP yanıtını okur ve `(kod, metin)` döndürür.
        """
        first_line = self._read_line()
        lines = [first_line]

        if len(first_line) >= 4 and first_line[:3].isdigit() and first_line[3] == "-":
            end_prefix = f"{first_line[:3]} "
            while True:
                next_line = self._read_line()
                lines.append(next_line)
                if next_line.startswith(end_prefix):
                    break

        full_text = "\n".join(lines)
        code = int(first_line[:3]) if len(first_line) >= 3 and first_line[:3].isdigit() else 0
        self._log(f"<<< {full_text}")
        return code, full_text

    def send_command(self, cmd: str):
        if self.control_sock is None:
            raise ConnectionError("Sunucuya bağlı değilsiniz")
        shown = "PASS ***" if cmd.upper().startswith("PASS ") else cmd
        self._log(f">>> {shown}")
        self.control_sock.sendall(f"{cmd}\r\n".encode("utf-8"))

    def _check_ready(self) -> bool:
        if not self.connected or self.control_sock is None:
            self._log("[!] Önce sunucuya bağlanmalısınız.")
            return False
        if not self.logged_in:
            self._log("[!] Önce giriş (login) yapmalısınız.")
            return False
        return True

    # ------------------------------------------------------------------
    # Bağlantı, Giriş ve Pasif Mod (PASV)
    # ------------------------------------------------------------------

    def connect(self, host: str = "127.0.0.1", port: int = 2121) -> bool:
        """FTP sunucusuna kontrol bağlantısı kurar."""
        self.host = host
        self.port = port
        self._recv_buffer = b""
        try:
            self._log(f"[*] Sunucuya bağlanılıyor: {host}:{port} ...")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((host, port))
            self.control_sock = sock

            code, _ = self.get_response()
            if code == 220:
                self.connected = True
                return True

            self._log("[!] Beklenmeyen karşılama yanıtı.")
            sock.close()
            self.control_sock = None
            return False
        except ConnectionRefusedError:
            self._log(f"[!] Bağlantı reddedildi ({host}:{port}). Sunucu çalışıyor mu?")
            return False
        except OSError as exc:
            self._log(f"[!] Bağlantı hatası: {exc}")
            return False

    def login(self, username: str = "user", password: str = "pass") -> bool:
        """USER ve PASS komutları ile oturum açar."""
        if not self.connected:
            self._log("[!] Önce sunucuya bağlanmalısınız.")
            return False

        self.send_command(f"USER {username}")
        code, _ = self.get_response()
        if code == 230:
            self.logged_in = True
            self.set_binary_mode()
            return True
        if code != 331:
            return False

        self.send_command(f"PASS {password}")
        code, _ = self.get_response()
        if code == 230:
            self.logged_in = True
            self.set_binary_mode()
            return True
        return False

    def set_binary_mode(self) -> bool:
        self.send_command("TYPE I")
        code, _ = self.get_response()
        return code == 200

    def open_pasv_socket(self) -> socket.socket | None:
        """PASV komutu gönderir ve sunucunun açtığı veri portuna TCP soketi bağlar."""
        self.send_command("PASV")
        code, resp = self.get_response()
        if code != 227:
            return None

        match = self.PASV_REGEX.search(resp)
        if not match:
            self._log("[!] PASV yanıtı ayrıştırılamadı.")
            return None

        nums = [int(g) for g in match.groups()]
        pasv_ip = ".".join(str(n) for n in nums[:4])
        pasv_port = nums[4] * 256 + nums[5]

        # Sunucu NAT arkasında veya 0.0.0.0/127.0.0.1 döndürdüyse kontrol bağlantısının IP'sini kullan
        if pasv_ip == "0.0.0.0" or (pasv_ip.startswith("127.") and self.host not in ("localhost", "127.0.0.1")):
            pasv_ip = self.control_sock.getpeername()[0]

        data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        data_sock.settimeout(self.timeout)
        data_sock.connect((pasv_ip, pasv_port))
        return data_sock

    # ------------------------------------------------------------------
    # Dizin ve Dosya İşlemleri
    # ------------------------------------------------------------------

    def pwd(self) -> str | None:
        if not self._check_ready():
            return None
        self.send_command("PWD")
        code, resp = self.get_response()
        if code == 257:
            match = re.search(r'"(.*)"', resp)
            return match.group(1).replace('""', '"') if match else resp
        return None

    def cwd(self, directory: str) -> bool:
        if not self._check_ready():
            return False
        self.send_command(f"CWD {directory}")
        code, _ = self.get_response()
        return code == 250

    def cdup(self) -> bool:
        if not self._check_ready():
            return False
        self.send_command("CDUP")
        code, _ = self.get_response()
        return code in (200, 250)

    def mkdir(self, dirname: str) -> bool:
        if not self._check_ready():
            return False
        self.send_command(f"MKD {dirname}")
        code, _ = self.get_response()
        return code == 257

    def rmdir(self, dirname: str) -> bool:
        if not self._check_ready():
            return False
        self.send_command(f"RMD {dirname}")
        code, _ = self.get_response()
        return code == 250

    def delete_file(self, filename: str) -> bool:
        if not self._check_ready():
            return False
        self.send_command(f"DELE {filename}")
        code, _ = self.get_response()
        return code == 250

    def rename(self, old_name: str, new_name: str) -> bool:
        if not self._check_ready():
            return False
        self.send_command(f"RNFR {old_name}")
        code, _ = self.get_response()
        if code != 350:
            return False
        self.send_command(f"RNTO {new_name}")
        code, _ = self.get_response()
        return code == 250

    def file_size(self, filename: str) -> int | None:
        if not self._check_ready():
            return None
        self.send_command(f"SIZE {filename}")
        code, resp = self.get_response()
        if code == 213:
            parts = resp.split(None, 1)
            if len(parts) == 2 and parts[1].strip().isdigit():
                return int(parts[1].strip())
        return None

    def list_files(self, path: str = "") -> list[str] | None:
        """PASV veri kanalı üzerinden LIST çıktısını alır ve satır listesi döndürür."""
        if not self._check_ready():
            return None

        data_sock = self.open_pasv_socket()
        if data_sock is None:
            return None

        try:
            cmd = f"LIST {path}".strip()
            self.send_command(cmd)
            code, _ = self.get_response()
            if code not in (125, 150):
                return None

            chunks = []
            while True:
                buf = data_sock.recv(8192)
                if not buf:
                    break
                chunks.append(buf)
        finally:
            data_sock.close()

        end_code, _ = self.get_response()
        if end_code not in (226, 250):
            return None

        text = b"".join(chunks).decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]

        if self.verbose:
            print("-" * 62)
            if lines:
                for line in lines:
                    print(f"  {line}")
            else:
                print("  (Dizin boş)")
            print("-" * 62, flush=True)

        return lines

    def download_file(self, remote_filename: str, local_path: str | None = None) -> bool:
        """Sunucudaki dosyayı PASV veri kanalı üzerinden yerel diske indirir."""
        if not self._check_ready():
            return False

        if local_path is None:
            local_path = os.path.basename(remote_filename.replace("\\", "/"))

        expected_size = self.file_size(remote_filename)
        data_sock = self.open_pasv_socket()
        if data_sock is None:
            return False

        self.send_command(f"RETR {remote_filename}")
        code, _ = self.get_response()
        if code not in (125, 150):
            data_sock.close()
            return False

        local_dir = os.path.dirname(os.path.abspath(local_path))
        os.makedirs(local_dir, exist_ok=True)
        temp_path = f"{local_path}.part"

        received = 0
        try:
            with open(temp_path, "wb") as f:
                while True:
                    chunk = data_sock.recv(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if self.verbose and expected_size and expected_size > 0:
                        pct = (received / expected_size) * 100
                        print(f"\r[*] İndiriliyor: %{pct:5.1f} ({received}/{expected_size} byte)", end="", flush=True)
            if self.verbose and expected_size:
                print()
        except OSError as exc:
            self._log(f"\n[!] Dosya yazma hatası: {exc}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False
        finally:
            data_sock.close()

        end_code, _ = self.get_response()
        if end_code not in (226, 250):
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

        os.replace(temp_path, local_path)
        self._log(f"[+] '{remote_filename}' -> '{os.path.abspath(local_path)}' ({received} byte) indirildi.")
        return True

    def upload_file(self, local_path: str, remote_filename: str | None = None) -> bool:
        """Yerel dosyayı PASV veri kanalı üzerinden sunucuya yükler."""
        if not self._check_ready():
            return False

        if not os.path.isfile(local_path):
            self._log(f"[!] Yerel dosya bulunamadı: {local_path}")
            return False

        if remote_filename is None:
            remote_filename = os.path.basename(local_path)

        total_size = os.path.getsize(local_path)
        data_sock = self.open_pasv_socket()
        if data_sock is None:
            return False

        self.send_command(f"STOR {remote_filename}")
        code, _ = self.get_response()
        if code not in (125, 150):
            data_sock.close()
            return False

        sent = 0
        try:
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    data_sock.sendall(chunk)
                    sent += len(chunk)
                    if self.verbose and total_size > 0:
                        pct = (sent / total_size) * 100
                        print(f"\r[*] Yükleniyor: %{pct:5.1f} ({sent}/{total_size} byte)", end="", flush=True)
            if self.verbose and total_size > 0:
                print()
        except OSError as exc:
            self._log(f"\n[!] Dosya gönderme hatası: {exc}")
            data_sock.close()
            return False

        # Veri bağlantısını kapatarak sunucuya EOF bildir
        data_sock.close()

        end_code, _ = self.get_response()
        if end_code in (226, 250):
            self._log(f"[+] '{local_path}' -> '{remote_filename}' ({sent} byte) yüklendi.")
            return True
        return False

    def help_command(self) -> bool:
        if not self.connected:
            return False
        self.send_command("HELP")
        code, _ = self.get_response()
        return code in (211, 214)

    def quit(self):
        if self.connected and self.control_sock is not None:
            try:
                self.send_command("QUIT")
                self.get_response()
            except OSError:
                pass
            try:
                self.control_sock.close()
            except OSError:
                pass
        self.control_sock = None
        self.connected = False
        self.logged_in = False


# ----------------------------------------------------------------------
# Kullanıcı Arayüzleri: Otomatik Demo, İnteraktif Menü ve FTP Kabuğu
# ----------------------------------------------------------------------


def run_demo(client: FTPClient) -> bool:
    """Sunucu ile temel işlemleri uçtan uca otomatik test/demo olarak çalıştırır."""
    print("\n" + "=" * 62)
    print("  OTOMATİK FTP DEMO AKIŞI")
    print("=" * 62)

    client.pwd()
    client.list_files()

    os.makedirs("downloads", exist_ok=True)
    dl_target = os.path.join("downloads", "downloaded_data.txt")
    if not client.download_file("data.txt", dl_target):
        return False

    sample_upload = os.path.join("downloads", "client_upload.txt")
    with open(sample_upload, "w", encoding="utf-8") as f:
        f.write("Bu dosya FTP istemcisi (client.py) tarafından yüklendi!\n")
        f.write(f"İşletim sistemi: {sys.platform}\n")

    if not client.upload_file(sample_upload, "client_upload.txt"):
        return False

    client.list_files()
    print("[+] Otomatik demo başarıyla tamamlandı!")
    return True


def interactive_shell(client: FTPClient):
    """Komut satırı tabanlı interaktif FTP kabuğu."""
    print("\nİnteraktif FTP Kabuğu ('help' ile komutları görebilir, 'quit' ile çıkabilirsiniz)")
    while True:
        cwd = client.pwd() if client.verbose is False else None
        prompt = f"ftp ({cwd})> " if cwd else "ftp> "
        try:
            raw = input(prompt).strip()
        except EOFError:
            break
        if not raw:
            continue

        try:
            parts = shlex.split(raw, posix=(os.name != "nt"))
            # Windows'ta shlex tırnak işaretlerini koruyabileceği için temizle
            parts = [p.strip('"').strip("'") for p in parts]
        except ValueError as exc:
            print(f"[!] Komut ayrıştırma hatası: {exc}")
            continue

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("ls", "dir", "list"):
            client.list_files(args[0] if args else "")
        elif cmd == "pwd":
            client.pwd()
        elif cmd == "cd":
            if not args:
                print("Kullanım: cd <dizin>")
            else:
                client.cwd(args[0])
        elif cmd in ("cdup", ".."):
            client.cdup()
        elif cmd in ("get", "retr", "download"):
            if not args:
                print("Kullanım: get <sunucu_dosyasi> [yerel_yol]")
            else:
                client.download_file(args[0], args[1] if len(args) > 1 else None)
        elif cmd in ("put", "stor", "upload"):
            if not args:
                print("Kullanım: put <yerel_dosya> [sunucu_dosya_adi]")
            else:
                client.upload_file(args[0], args[1] if len(args) > 1 else None)
        elif cmd == "mkdir":
            if not args:
                print("Kullanım: mkdir <dizin>")
            else:
                client.mkdir(args[0])
        elif cmd == "rmdir":
            if not args:
                print("Kullanım: rmdir <dizin>")
            else:
                client.rmdir(args[0])
        elif cmd in ("rm", "del", "delete"):
            if not args:
                print("Kullanım: rm <dosya>")
            else:
                client.delete_file(args[0])
        elif cmd in ("rename", "mv"):
            if len(args) < 2:
                print("Kullanım: rename <eski_ad> <yeni_ad>")
            else:
                client.rename(args[0], args[1])
        elif cmd == "size":
            if not args:
                print("Kullanım: size <dosya>")
            else:
                sz = client.file_size(args[0])
                if sz is not None:
                    print(f"Boyut: {sz} byte")
        elif cmd == "lpwd":
            print(f"Yerel dizin: {os.getcwd()}")
        elif cmd == "lcd":
            if not args:
                print("Kullanım: lcd <yerel_dizin>")
            else:
                try:
                    os.chdir(args[0])
                    print(f"Yerel dizin değiştirildi: {os.getcwd()}")
                except OSError as exc:
                    print(f"[!] Yerel dizin hatası: {exc}")
        elif cmd == "lls":
            for item in sorted(os.listdir(".")):
                suffix = "/" if os.path.isdir(item) else f" ({os.path.getsize(item)} byte)"
                print(f"  {item}{suffix}")
        elif cmd == "rhelp":
            client.help_command()
        elif cmd in ("help", "?"):
            print(
                "Komutlar:\n"
                "  ls [yol]                  - Sunucudaki dosyaları listele\n"
                "  pwd                       - Sunucudaki mevcut dizini göster\n"
                "  cd <dizin> / cdup         - Sunucuda dizin değiştir / üst dizine çık\n"
                "  get <uzak> [yerel]        - Sunucudan dosya indir\n"
                "  put <yerel> [uzak]        - Sunucuya dosya yükle\n"
                "  mkdir <ad> / rmdir <ad>   - Sunucuda klasör oluştur / sil\n"
                "  rm <dosya>                - Sunucudan dosya sil\n"
                "  rename <eski> <yeni>      - Sunucuda yeniden adlandır\n"
                "  size <dosya>              - Sunucudaki dosya boyutunu göster\n"
                "  lpwd / lcd <yol> / lls    - Yerel makinede dizin işlemleri\n"
                "  rhelp                     - Sunucu HELP çıktısını göster\n"
                "  quit / exit               - Bağlantıyı kapat ve çık"
            )
        elif cmd in ("quit", "exit", "bye"):
            break
        else:
            print(f"[!] Bilinmeyen komut: '{cmd}'. Yardım için 'help' yazın.")


def interactive_menu(client: FTPClient):
    """Numaralı kolay kullanım menüsü."""
    while True:
        print("\n" + "=" * 54)
        print("  FTP İSTEMCİ MENÜSÜ")
        print("=" * 54)
        print("  1. Dosya Listesi (LIST)")
        print("  2. Dosya İndir (RETR)")
        print("  3. Dosya Yükle (STOR)")
        print("  4. Mevcut Dizin (PWD)")
        print("  5. Dizin Değiştir (CWD / CDUP)")
        print("  6. Klasör Oluştur / Sil (MKD / RMD)")
        print("  7. Dosya Sil (DELE)")
        print("  8. İnteraktif FTP Komut Kabuğuna Geç (ls, get, put...)")
        print("  9. Otomatik Demo Çalıştır (data.txt indir & yükle)")
        print("  0. Çıkış (QUIT)")
        print("-" * 54)

        choice = input("Seçiminiz (0-9): ").strip()

        if choice == "1":
            client.list_files()
        elif choice == "2":
            remote = input("İndirilecek sunucu dosyası (varsayılan: data.txt): ").strip() or "data.txt"
            default_local = os.path.join("downloads", os.path.basename(remote))
            local = input(f"Kaydedilecek yerel yol (varsayılan: {default_local}): ").strip() or default_local
            client.download_file(remote, local)
        elif choice == "3":
            local = input("Yüklenecek yerel dosya yolu: ").strip().strip('"')
            if local:
                default_remote = os.path.basename(local)
                remote = input(f"Sunucudaki dosya adı (varsayılan: {default_remote}): ").strip() or default_remote
                client.upload_file(local, remote)
        elif choice == "4":
            client.pwd()
        elif choice == "5":
            target = input("Hedef dizin (üst dizin için '..'): ").strip()
            if target == "..":
                client.cdup()
            elif target:
                client.cwd(target)
        elif choice == "6":
            sub = input("Oluşturmak için '1', silmek için '2': ").strip()
            name = input("Klasör adı: ").strip()
            if sub == "1" and name:
                client.mkdir(name)
            elif sub == "2" and name:
                client.rmdir(name)
        elif choice == "7":
            name = input("Silinecek dosya adı: ").strip()
            if name:
                client.delete_file(name)
        elif choice == "8":
            interactive_shell(client)
            break
        elif choice == "9":
            run_demo(client)
        elif choice == "0":
            break
        else:
            print("[!] Lütfen 0 ile 9 arasında bir seçim yapın.")


def parse_args():
    parser = argparse.ArgumentParser(description="Çapraz Platform Python FTP İstemcisi")
    parser.add_argument("--host", default="127.0.0.1", help="Sunucu IP/adresi (varsayılan: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=2121, help="Sunucu portu (varsayılan: 2121)")
    parser.add_argument("--user", default="user", help="Kullanıcı adı (varsayılan: user)")
    parser.add_argument("--password", default="pass", help="Şifre (varsayılan: pass)")
    parser.add_argument("--shell", action="store_true", help="Doğrudan interaktif FTP kabuğunu başlat")
    parser.add_argument("--demo", action="store_true", help="Otomatik indirme/yükleme demosunu çalıştır ve çık")
    return parser.parse_args()


def main():
    configure_console_utf8()
    args = parse_args()

    print("=" * 62)
    print("  FTP İSTEMCİSİ (Windows & Linux Uyumlu)")
    print("=" * 62)

    client = FTPClient(verbose=True)
    try:
        if not client.connect(args.host, args.port):
            sys.exit(1)
        if not client.login(args.user, args.password):
            print("[!] Giriş başarısız.")
            sys.exit(1)

        if args.demo:
            ok = run_demo(client)
            if not ok:
                sys.exit(1)
        elif args.shell:
            interactive_shell(client)
        else:
            interactive_menu(client)
    except KeyboardInterrupt:
        print("\n[*] Kullanıcı tarafından sonlandırıldı.")
    finally:
        client.quit()


if __name__ == "__main__":
    main()
