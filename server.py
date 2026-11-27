#!/usr/bin/env python3
"""
server.py - Çapraz Platform (Windows / Linux / macOS) Çok İstemcili FTP Sunucusu

Standart RFC 959 FTP kontrol ve veri (PASV / EPSV / PORT) bağlantılarını
yalnızca Python standart kütüphanesi (`socket`, `threading`) kullanarak uygular.
Hem bu projedeki `client.py` ile hem de standart FTP istemcileriyle
(Python ftplib, FileZilla, curl, Windows ftp.exe) uyumludur.
"""

import argparse
import os
import posixpath
import socket
import sys
import threading
import time


def configure_console_utf8():
    """Windows CMD / PowerShell dahil tüm terminallerde UTF-8 çıktıyı etkinleştirir."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


class FTPClientSession:
    """Tek bir istemcinin FTP kontrol ve veri oturumunu yönetir."""

    WINDOWS_INVALID_CHARS = set('<>:"|?*')

    def __init__(self, server, conn: socket.socket, addr: tuple[str, int]):
        self.server = server
        self.conn = conn
        self.addr = addr
        self.client_id = f"{addr[0]}:{addr[1]}"
        self.root_dir = os.path.realpath(server.root_dir)
        self.current_dir = self.root_dir

        self.username: str | None = None
        self.authenticated = False
        self.transfer_type = "I"  # 'I' (Binary) veya 'A' (ASCII)
        self.rename_from: str | None = None

        # Veri bağlantısı durumu (PASV/EPSV dinleyici veya PORT hedefi)
        self.pasv_listener: socket.socket | None = None
        self.active_addr: tuple[str, int] | None = None

        self._recv_buffer = b""

    # ------------------------------------------------------------------
    # Kontrol kanalı yardımcıları
    # ------------------------------------------------------------------

    def send_response(self, code: int, message: str):
        """RFC 959 uyumlu tek veya çok satırlı FTP yanıtı gönderir."""
        lines = message.splitlines() or [""]
        if len(lines) == 1:
            payload = f"{code} {lines[0]}\r\n"
        else:
            body = "".join(f"{code}-{line}\r\n" for line in lines[:-1])
            payload = f"{body}{code} {lines[-1]}\r\n"
        self.conn.sendall(payload.encode("utf-8", errors="replace"))

    def read_line(self) -> str | None:
        """Kontrol soketinden CRLF/LF sonlu tek bir komut satırı okur."""
        while b"\n" not in self._recv_buffer:
            chunk = self.conn.recv(4096)
            if not chunk:
                if self._recv_buffer:
                    line = self._recv_buffer.decode("utf-8", errors="replace")
                    self._recv_buffer = b""
                    return line.rstrip("\r\n")
                return None
            self._recv_buffer += chunk
            if len(self._recv_buffer) > 65536:
                raise ValueError("Komut satırı çok uzun")

        raw_line, self._recv_buffer = self._recv_buffer.split(b"\n", 1)
        return raw_line.decode("utf-8", errors="replace").rstrip("\r")

    # ------------------------------------------------------------------
    # Sanal ve yerel dosya yolu güvenliği (Windows & Linux uyumlu)
    # ------------------------------------------------------------------

    def _virtual_cwd(self) -> str:
        rel = os.path.relpath(self.current_dir, self.root_dir)
        if rel in (".", ""):
            return "/"
        return "/" + rel.replace("\\", "/")

    def _resolve_path(self, ftp_path: str) -> str | None:
        """
        İstemcinin gönderdiği FTP yolunu sunucu kök dizini (root_dir) içinde
        güvenli bir yerel yola çevirir. Kök dizin dışına çıkma girişimlerini (..)
        Windows ve Linux'ta engeller.
        """
        cleaned = (ftp_path or "").strip().replace("\\", "/")
        if not cleaned:
            target = self.current_dir
        elif cleaned.startswith("/"):
            rel_norm = posixpath.normpath(cleaned).lstrip("/")
            target = os.path.join(self.root_dir, *rel_norm.split("/")) if rel_norm else self.root_dir
        else:
            parts = [p for p in cleaned.split("/") if p != ""]
            target = os.path.join(self.current_dir, *parts)

        real_target = os.path.realpath(target)
        try:
            common = os.path.commonpath([self.root_dir, real_target])
        except ValueError:
            # Windows'ta farklı sürücü harfleri (örn. C:\ ve D:\) ValueError üretir
            return None

        if os.path.normcase(common) != os.path.normcase(self.root_dir):
            return None
        return real_target

    def _is_valid_leaf_name(self, path_str: str) -> bool:
        leaf = os.path.basename(path_str.rstrip("/\\"))
        if not leaf or leaf in (".", ".."):
            return False
        if any(ch in self.WINDOWS_INVALID_CHARS for ch in leaf):
            return False
        if os.name == "nt" and leaf.rstrip(" .") != leaf:
            return False
        return True

    # ------------------------------------------------------------------
    # Veri kanalı yönetimi (PASV, EPSV ve PORT)
    # ------------------------------------------------------------------

    def _close_data_setup(self):
        if self.pasv_listener is not None:
            try:
                self.pasv_listener.close()
            except OSError:
                pass
            self.pasv_listener = None
        self.active_addr = None

    def _open_data_connection(self) -> socket.socket | None:
        """Hazırlanmış PASV/EPSV veya PORT veri bağlantısını açar."""
        try:
            if self.pasv_listener is not None:
                listener = self.pasv_listener
                self.pasv_listener = None
                try:
                    data_conn, _ = listener.accept()
                    data_conn.settimeout(30.0)
                    return data_conn
                finally:
                    listener.close()

            if self.active_addr is not None:
                target = self.active_addr
                self.active_addr = None
                data_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                data_conn.settimeout(15.0)
                data_conn.connect(target)
                data_conn.settimeout(30.0)
                return data_conn

            self.send_response(425, "Veri baglantisi yok. Once PASV veya PORT kullanin.")
            return None
        except socket.timeout:
            self.send_response(425, "Veri baglantisi zaman asimina ugradi.")
            return None
        except OSError as exc:
            self.send_response(425, f"Veri baglantisi acilamadi: {exc}")
            return None

    def _require_auth(self) -> bool:
        if not self.authenticated:
            self.send_response(530, "Lutfen once giris yapin (USER ve PASS).")
            return False
        return True

    # ------------------------------------------------------------------
    # Oturum döngüsü
    # ------------------------------------------------------------------

    def run(self):
        self.conn.settimeout(300.0)
        try:
            self.send_response(220, "FTP Sunucusuna Hosgeldiniz (Cross-Platform Python FTP Server)")
            while self.server.running:
                line = self.read_line()
                if line is None:
                    self.server.log(f"[{self.client_id}] Bağlantı kapandı")
                    break

                if not line.strip():
                    continue

                parts = line.split(" ", 1)
                cmd = parts[0].upper()
                arg = parts[1] if len(parts) > 1 else ""

                log_arg = "***" if cmd == "PASS" else arg
                self.server.log(f"[{self.client_id}] Komut: {cmd} {log_arg}".rstrip())

                if cmd != "RNTO":
                    self.rename_from = None

                handler = getattr(self, f"cmd_{cmd}", None)
                if handler is not None:
                    if handler(arg) is False:
                        break
                else:
                    self.send_response(502, f"'{cmd}' komutu desteklenmiyor.")
        except socket.timeout:
            self.server.log(f"[{self.client_id}] Oturum zaman aşımı")
            try:
                self.send_response(421, "Zaman asimi, baglanti kapatiliyor.")
            except OSError:
                pass
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.server.log(f"[{self.client_id}] İstemci bağlantıyı sıfırladı")
        except Exception as exc:
            self.server.log(f"[{self.client_id}] Oturum hatası: {exc}")
        finally:
            self._close_data_setup()
            try:
                self.conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # FTP Komut İşleyicileri
    # ------------------------------------------------------------------

    def cmd_USER(self, arg: str):
        username = arg.strip() or "anonymous"
        self.username = username
        self.authenticated = False
        if self.server.allow_anonymous and username.lower() == "anonymous":
            self.send_response(331, "Anonim giris tamam, sifre olarak e-posta veya bos gonderebilirsiniz.")
        else:
            self.send_response(331, f"Kullanici adi '{username}' tamam, sifre bekleniyor.")

    def cmd_PASS(self, arg: str):
        if self.username is None:
            self.send_response(503, "Once USER komutu gonderin.")
            return

        if self.server.check_credentials(self.username, arg):
            self.authenticated = True
            self.server.log(f"[{self.client_id}] Giriş başarılı (kullanıcı: {self.username})")
            self.send_response(230, "Giris basarili.")
        else:
            self.authenticated = False
            self.server.log(f"[{self.client_id}] Hatalı giriş denemesi (kullanıcı: {self.username})")
            self.send_response(530, "Kullanici adi veya sifre hatali.")

    def cmd_SYST(self, _arg: str):
        self.send_response(215, "UNIX Type: L8")

    def cmd_FEAT(self, _arg: str):
        features = "Desteklenen ozellikler:\n PASV\n EPSV\n SIZE\n MDTM\n UTF8\nOzellik listesi sonu"
        self.send_response(211, features)

    def cmd_OPTS(self, arg: str):
        if arg.upper().startswith("UTF8"):
            self.send_response(200, "UTF8 modu etkin.")
        else:
            self.send_response(501, "Desteklenmeyen secenek.")

    def cmd_NOOP(self, _arg: str):
        self.send_response(200, "NOOP tamam.")

    def cmd_TYPE(self, arg: str):
        mode = arg.strip().upper()[:1]
        if mode in ("I", "A", "L"):
            self.transfer_type = "A" if mode == "A" else "I"
            self.send_response(200, f"Transfer tipi {self.transfer_type} olarak ayarlandi.")
        else:
            self.send_response(504, "Desteklenmeyen TYPE parametresi.")

    def cmd_MODE(self, arg: str):
        if arg.strip().upper() == "S":
            self.send_response(200, "Stream modu ayarlandi.")
        else:
            self.send_response(504, "Yalnizca Stream (S) modu desteklenir.")

    def cmd_STRU(self, arg: str):
        if arg.strip().upper() == "F":
            self.send_response(200, "File yapisi ayarlandi.")
        else:
            self.send_response(504, "Yalnizca File (F) yapisi desteklenir.")

    def cmd_PWD(self, _arg: str):
        if not self._require_auth():
            return
        vpath = self._virtual_cwd().replace('"', '""')
        self.send_response(257, f'"{vpath}" mevcut dizin.')

    cmd_XPWD = cmd_PWD

    def cmd_CWD(self, arg: str):
        if not self._require_auth():
            return
        if not arg.strip():
            self.send_response(501, "Dizin adi gerekli.")
            return
        target = self._resolve_path(arg)
        if target is None or not os.path.isdir(target):
            self.send_response(550, "Dizin bulunamadi veya erisim engellendi.")
            return
        self.current_dir = target
        self.send_response(250, f'Dizin degistirildi: "{self._virtual_cwd()}"')

    cmd_XCWD = cmd_CWD

    def cmd_CDUP(self, _arg: str):
        if not self._require_auth():
            return
        parent = self._resolve_path("..")
        if parent is not None and os.path.isdir(parent):
            self.current_dir = parent
        self.send_response(250, f'Ust dizine gecildi: "{self._virtual_cwd()}"')

    cmd_XCUP = cmd_CDUP

    def cmd_PASV(self, _arg: str):
        if not self._require_auth():
            return
        self._close_data_setup()

        local_ip = self.server.pasv_ip or self.conn.getsockname()[0]
        if local_ip in ("0.0.0.0", "::", ""):
            local_ip = "127.0.0.1"

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((local_ip if local_ip != "127.0.0.1" or self.server.host == "127.0.0.1" else "0.0.0.0", 0))
        listener.listen(1)
        listener.settimeout(30.0)
        data_port = listener.getsockname()[1]
        self.pasv_listener = listener

        h1, h2, h3, h4 = local_ip.split(".")
        p1, p2 = divmod(data_port, 256)
        self.send_response(227, f"Entering Passive Mode ({h1},{h2},{h3},{h4},{p1},{p2}).")

    def cmd_EPSV(self, _arg: str):
        if not self._require_auth():
            return
        self._close_data_setup()

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        listener.settimeout(30.0)
        data_port = listener.getsockname()[1]
        self.pasv_listener = listener

        self.send_response(229, f"Entering Extended Passive Mode (|||{data_port}|).")

    def cmd_PORT(self, arg: str):
        if not self._require_auth():
            return
        self._close_data_setup()
        try:
            nums = [int(x) for x in arg.strip().split(",")]
            if len(nums) != 6 or any(n < 0 or n > 255 for n in nums):
                raise ValueError("Geçersiz PORT formatı")
            ip = ".".join(str(n) for n in nums[:4])
            port = nums[4] * 256 + nums[5]
            if port <= 0 or port > 65535:
                raise ValueError("Geçersiz port")
            self.active_addr = (ip, port)
            self.send_response(200, "PORT komutu basarili.")
        except ValueError:
            self.send_response(501, "Gecersiz PORT parametresi.")

    def _format_list_entry(self, dir_path: str, name: str) -> str:
        full_path = os.path.join(dir_path, name)
        st = os.stat(full_path)
        mtime = time.localtime(st.st_mtime)
        # Sabit İngilizce ay kısaltmaları (Windows Türkçe yerel ayarlarında bozulmayı önler)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        mon = months[mtime.tm_mon - 1]
        date_str = f"{mon} {mtime.tm_mday:2d} {mtime.tm_hour:02d}:{mtime.tm_min:02d}"
        if os.path.isdir(full_path):
            return f"drwxr-xr-x 1 owner group {0:>10} {date_str} {name}\r\n"
        return f"-rw-r--r-- 1 owner group {st.st_size:>10} {date_str} {name}\r\n"

    def cmd_LIST(self, arg: str):
        if not self._require_auth():
            return

        # `-l`, `-la` gibi bayrakları ayıkla
        tokens = [t for t in arg.split(" ") if t and not t.startswith("-")]
        path_arg = " ".join(tokens)
        target = self._resolve_path(path_arg) if path_arg else self.current_dir

        if target is None or not os.path.exists(target):
            self._close_data_setup()
            self.send_response(550, "Dizin veya dosya bulunamadi.")
            return

        try:
            if os.path.isdir(target):
                entries = sorted(os.listdir(target))
                listing = "".join(self._format_list_entry(target, item) for item in entries)
            else:
                listing = self._format_list_entry(os.path.dirname(target), os.path.basename(target))
        except OSError as exc:
            self._close_data_setup()
            self.send_response(550, f"Dizin okunamadi: {exc}")
            return

        self.send_response(150, "Dizin listesi icin veri baglantisi aciliyor.")
        data_conn = self._open_data_connection()
        if data_conn is None:
            return
        try:
            data_conn.sendall(listing.encode("utf-8", errors="replace"))
            self.send_response(226, "Dizin listesi gonderildi.")
        except OSError as exc:
            self.send_response(426, f"Veri aktarimi kesildi: {exc}")
        finally:
            data_conn.close()

    def cmd_NLST(self, arg: str):
        if not self._require_auth():
            return

        target = self._resolve_path(arg) if arg.strip() else self.current_dir
        if target is None or not os.path.isdir(target):
            self._close_data_setup()
            self.send_response(550, "Dizin bulunamadi.")
            return

        try:
            names = sorted(os.listdir(target))
        except OSError as exc:
            self._close_data_setup()
            self.send_response(550, f"Dizin okunamadi: {exc}")
            return

        self.send_response(150, "Dosya isim listesi gonderiliyor.")
        data_conn = self._open_data_connection()
        if data_conn is None:
            return
        try:
            payload = "".join(f"{name}\r\n" for name in names)
            data_conn.sendall(payload.encode("utf-8", errors="replace"))
            self.send_response(226, "Liste gonderimi tamamlandi.")
        except OSError as exc:
            self.send_response(426, f"Veri aktarimi kesildi: {exc}")
        finally:
            data_conn.close()

    def cmd_RETR(self, arg: str):
        if not self._require_auth():
            return
        if not arg.strip():
            self._close_data_setup()
            self.send_response(501, "Dosya adi gerekli.")
            return

        filepath = self._resolve_path(arg)
        if filepath is None or not os.path.isfile(filepath):
            self._close_data_setup()
            self.send_response(550, "Dosya bulunamadi.")
            return

        try:
            file_size = os.path.getsize(filepath)
            file_obj = open(filepath, "rb")
        except OSError as exc:
            self._close_data_setup()
            self.send_response(550, f"Dosya acilamadi: {exc}")
            return

        with file_obj:
            self.send_response(150, f"Dosya transferi basliyor ({file_size} byte).")
            data_conn = self._open_data_connection()
            if data_conn is None:
                return
            try:
                bytes_sent = 0
                while True:
                    chunk = file_obj.read(65536)
                    if not chunk:
                        break
                    data_conn.sendall(chunk)
                    bytes_sent += len(chunk)
                self.server.log(f"[{self.client_id}] İndirildi: {os.path.basename(filepath)} ({bytes_sent} byte)")
                self.send_response(226, "Transfer tamamlandi.")
            except OSError as exc:
                self.send_response(426, f"Transfer hatasi: {exc}")
            finally:
                data_conn.close()

    def _receive_file(self, arg: str, append: bool = False):
        if not self._require_auth():
            return
        if not arg.strip() or not self._is_valid_leaf_name(arg):
            self._close_data_setup()
            self.send_response(553, "Gecersiz dosya adi.")
            return

        filepath = self._resolve_path(arg)
        if filepath is None or os.path.isdir(filepath):
            self._close_data_setup()
            self.send_response(553, "Dosya yolu gecersiz veya bir dizin.")
            return

        parent_dir = os.path.dirname(filepath)
        if not os.path.isdir(parent_dir):
            self._close_data_setup()
            self.send_response(550, "Hedef dizin mevcut degil.")
            return

        mode = "ab" if append else "wb"
        try:
            file_obj = open(filepath, mode)
        except OSError as exc:
            self._close_data_setup()
            self.send_response(550, f"Dosya olusturulamadi: {exc}")
            return

        with file_obj:
            self.send_response(150, "Dosya yukleme icin veri baglantisi hazir.")
            data_conn = self._open_data_connection()
            if data_conn is None:
                return
            try:
                total = 0
                while True:
                    chunk = data_conn.recv(65536)
                    if not chunk:
                        break
                    file_obj.write(chunk)
                    total += len(chunk)
                self.server.log(f"[{self.client_id}] Yüklendi: {os.path.basename(filepath)} ({total} byte)")
                self.send_response(226, f"Dosya basariyla kaydedildi ({total} byte).")
            except OSError as exc:
                self.send_response(426, f"Yukleme hatasi: {exc}")
            finally:
                data_conn.close()

    def cmd_STOR(self, arg: str):
        self._receive_file(arg, append=False)

    def cmd_APPE(self, arg: str):
        self._receive_file(arg, append=True)

    def cmd_SIZE(self, arg: str):
        if not self._require_auth():
            return
        filepath = self._resolve_path(arg)
        if filepath is None or not os.path.isfile(filepath):
            self.send_response(550, "Dosya bulunamadi.")
            return
        self.send_response(213, str(os.path.getsize(filepath)))

    def cmd_MDTM(self, arg: str):
        if not self._require_auth():
            return
        filepath = self._resolve_path(arg)
        if filepath is None or not os.path.isfile(filepath):
            self.send_response(550, "Dosya bulunamadi.")
            return
        ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(os.path.getmtime(filepath)))
        self.send_response(213, ts)

    def cmd_DELE(self, arg: str):
        if not self._require_auth():
            return
        filepath = self._resolve_path(arg)
        if filepath is None or not os.path.isfile(filepath):
            self.send_response(550, "Dosya bulunamadi.")
            return
        try:
            os.remove(filepath)
            self.server.log(f"[{self.client_id}] Silindi: {filepath}")
            self.send_response(250, "Dosya silindi.")
        except OSError as exc:
            self.send_response(550, f"Dosya silinemedi: {exc}")

    def cmd_MKD(self, arg: str):
        if not self._require_auth():
            return
        if not arg.strip() or not self._is_valid_leaf_name(arg):
            self.send_response(553, "Gecersiz dizin adi.")
            return
        target = self._resolve_path(arg)
        if target is None:
            self.send_response(550, "Erisim engellendi.")
            return
        try:
            os.mkdir(target)
            rel = "/" + os.path.relpath(target, self.root_dir).replace("\\", "/")
            self.send_response(257, f'"{rel}" dizini olusturuldu.')
        except FileExistsError:
            self.send_response(550, "Dizin zaten mevcut.")
        except OSError as exc:
            self.send_response(550, f"Dizin olusturulamadi: {exc}")

    cmd_XMKD = cmd_MKD

    def cmd_RMD(self, arg: str):
        if not self._require_auth():
            return
        target = self._resolve_path(arg)
        if target is None or target == self.root_dir or not os.path.isdir(target):
            self.send_response(550, "Dizin bulunamadi veya silinemez.")
            return
        try:
            os.rmdir(target)
            self.send_response(250, "Dizin silindi.")
        except OSError as exc:
            self.send_response(550, f"Dizin silinemedi (bos oldugundan emin olun): {exc}")

    cmd_XRMD = cmd_RMD

    def cmd_RNFR(self, arg: str):
        if not self._require_auth():
            return
        target = self._resolve_path(arg)
        if target is None or target == self.root_dir or not os.path.exists(target):
            self.send_response(550, "Kaynak dosya veya dizin bulunamadi.")
            return
        self.rename_from = target
        self.send_response(350, "Kaynak hazir, yeni ad icin RNTO gonderin.")

    def cmd_RNTO(self, arg: str):
        if not self._require_auth():
            return
        if not self.rename_from:
            self.send_response(503, "Once RNFR komutu gonderin.")
            return
        src = self.rename_from
        self.rename_from = None
        if not arg.strip() or not self._is_valid_leaf_name(arg):
            self.send_response(553, "Gecersiz hedef adi.")
            return
        dst = self._resolve_path(arg)
        if dst is None:
            self.send_response(550, "Hedef yol gecersiz.")
            return
        try:
            os.replace(src, dst)
            self.send_response(250, "Yeniden adlandirma basarili.")
        except OSError as exc:
            self.send_response(550, f"Yeniden adlandirilamadi: {exc}")

    def cmd_HELP(self, _arg: str):
        help_msg = (
            "Desteklenen FTP komutlari:\n"
            " USER <ad>    - Kullanici adi gonder\n"
            " PASS <sifre> - Sifre gonder\n"
            " PWD          - Mevcut dizini goster\n"
            " CWD <dizin>  - Dizin degistir\n"
            " CDUP         - Ust dizine cik\n"
            " PASV / EPSV  - Pasif veri baglantisi baslat\n"
            " PORT <...>   - Aktif veri baglantisi ayarla\n"
            " LIST [yol]   - Ayrintili dizin listesi\n"
            " NLST [yol]   - Sadece dosya adlarini listele\n"
            " RETR <dosya> - Sunucudan dosya indir\n"
            " STOR <dosya> - Sunucuya dosya yukle\n"
            " DELE <dosya> - Dosya sil\n"
            " MKD <dizin>  - Yeni dizin olustur\n"
            " RMD <dizin>  - Bos dizin sil\n"
            " RNFR / RNTO  - Dosya/dizin yeniden adlandir\n"
            " SIZE <dosya> - Dosya boyutunu ogren\n"
            " QUIT         - Oturumu kapat\n"
            "Yardim sonu."
        )
        self.send_response(214, help_msg)

    def cmd_QUIT(self, _arg: str):
        self.send_response(221, "Gule gule!")
        return False


class FTPServer:
    """Çoklu istemci destekli çapraz platform FTP Sunucusu."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 2121,
        root_dir: str = "ftp_data",
        users: dict[str, str] | None = None,
        allow_anonymous: bool = True,
        pasv_ip: str | None = None,
        quiet: bool = False,
        seed_sample_files: bool = True,
    ):
        self.host = host
        self.port = port
        self.root_dir = os.path.abspath(root_dir)
        self.users = users if users is not None else {"user": "pass", "admin": "1234"}
        self.allow_anonymous = allow_anonymous
        self.pasv_ip = pasv_ip
        self.quiet = quiet
        self.running = False
        self.socket: socket.socket | None = None

        self._setup_server_directory(seed_sample_files)

    def log(self, message: str):
        if not self.quiet:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {message}", flush=True)

    def _setup_server_directory(self, seed_sample_files: bool):
        os.makedirs(self.root_dir, exist_ok=True)
        if not seed_sample_files:
            return

        data_file = os.path.join(self.root_dir, "data.txt")
        if not os.path.exists(data_file):
            with open(data_file, "w", encoding="utf-8") as f:
                f.write("Bu FTP sunucusu üzerindeki örnek data.txt dosyasıdır.\n")
                f.write("Windows ve Linux üzerinden client.py ile indirilebilir.\n")
                f.write("Ağ Programlama - FTP (RFC 959 PASV/PORT) örnek verisi.\n")
                f.write(f"Oluşturulma tarihi: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

        sub_dir = os.path.join(self.root_dir, "ornekler")
        os.makedirs(sub_dir, exist_ok=True)
        sub_file = os.path.join(sub_dir, "merhaba.txt")
        if not os.path.exists(sub_file):
            with open(sub_file, "w", encoding="utf-8") as f:
                f.write("Alt dizindeki örnek metin dosyası.\nCWD ornekler komutu ile erişebilirsiniz.\n")

    def check_credentials(self, username: str, password: str) -> bool:
        if self.allow_anonymous and username.lower() == "anonymous":
            return True
        return self.users.get(username) == password

    def bind(self):
        """Soketi oluşturur ve belirtilen adres/porta bağlar."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windows'ta SO_REUSEADDR aktif port çakışmasına izin verdiği için yalnızca POSIX'te açılır;
        # Windows'ta ise SO_EXCLUSIVEADDRUSE varsa etkinleştirilir.
        if os.name == "nt":
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        sock.bind((self.host, self.port))
        sock.listen(16)
        # 1 saniyelik timeout sayesinde Windows CMD/PowerShell'de Ctrl+C anında yakalanır
        sock.settimeout(1.0)
        self.port = sock.getsockname()[1]
        self.socket = sock

    def start(self):
        """FTP sunucusunu başlatır ve istemci bağlantılarını dinler."""
        if self.socket is None:
            self.bind()

        self.running = True
        display_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        if not self.quiet:
            print("=" * 62)
            print("  FTP SUNUCUSU BAŞLATILDI (Windows & Linux Uyumlu)")
            print("=" * 62)
            print(f"  Dinlenen Adres : {self.host}:{self.port} (Yerel bağlantı: {display_host}:{self.port})")
            print(f"  Kök Dizin      : {self.root_dir}")
            print(f"  Kullanıcılar   : {', '.join(self.users.keys())}" + (" + anonymous" if self.allow_anonymous else ""))
            print("  Durdurmak için : Ctrl+C")
            print("=" * 62, flush=True)

        try:
            while self.running:
                try:
                    client_sock, address = self.socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if not self.running:
                        break
                    raise

                self.log(f"Yeni bağlantı: {address[0]}:{address[1]}")
                session = FTPClientSession(self, client_sock, address)
                thread = threading.Thread(target=session.run, daemon=True)
                thread.start()
        except KeyboardInterrupt:
            self.log("Sunucu kullanıcı tarafından kapatılıyor (Ctrl+C)...")
        finally:
            self.stop()

    def stop(self):
        """Sunucu dinleme soketini güvenli şekilde kapatır."""
        self.running = False
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None


def parse_args():
    parser = argparse.ArgumentParser(description="Çapraz Platform Python FTP Sunucusu")
    parser.add_argument("--host", default="0.0.0.0", help="Dinlenecek IP adresi (varsayılan: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=2121, help="Kontrol portu (varsayılan: 2121)")
    parser.add_argument("--root", default="ftp_data", help="Sunucu kök dizini (varsayılan: ftp_data)")
    parser.add_argument("--user", default="user", help="Varsayılan kullanıcı adı (varsayılan: user)")
    parser.add_argument("--password", default="pass", help="Varsayılan şifre (varsayılan: pass)")
    parser.add_argument("--no-anon", action="store_true", help="Anonim girişi devre dışı bırak")
    parser.add_argument("--pasv-ip", default=None, help="PASV yanıtında bildirilecek dış IP (NAT/LAN için)")
    return parser.parse_args()


def main():
    configure_console_utf8()
    args = parse_args()
    users = {args.user: args.password}
    if args.user != "user":
        users["user"] = "pass"

    try:
        server = FTPServer(
            host=args.host,
            port=args.port,
            root_dir=args.root,
            users=users,
            allow_anonymous=not args.no_anon,
            pasv_ip=args.pasv_ip,
        )
        server.start()
    except PermissionError:
        print(f"[HATA] Port {args.port} için yönetici/root yetkisi gerekiyor. '--port 2121' kullanmayı deneyin.")
        sys.exit(1)
    except OSError as exc:
        print(f"[HATA] Sunucu başlatılamadı: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
