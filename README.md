# Cross-Platform Python FTP Server & Client

Yalnızca Python standart kütüphanesi (`socket`, `threading`) kullanılarak sıfırdan geliştirilmiş, **Windows**, **Linux** ve **macOS** üzerinde sorunsuz çalışan çok istemcili **RFC 959 FTP Sunucusu (`server.py`)** ve **FTP İstemcisi (`client.py`)**.

[zyavuz610/learn-network-programming (`05-ftp`)](https://github.com/zyavuz610/learn-network-programming/tree/main/_2025/05-ftp) ders örneklerindeki mimari temel alınarak gerçek kontrol + veri kanalı (`PASV` / `EPSV` / `PORT`), çoklu istemci desteği, ikili (binary) dosya bütünlüğü ve tam Windows uyumluluğu ile geliştirilmiştir.

## Özellikler

- **Gerçek FTP Mimarisi (Kontrol + Veri Soketi):** Komutlar kontrol bağlantısından (`2121`), dosya ve dizin listesi aktarımları ise ayrı TCP veri bağlantısından (**Pasif Mod `PASV`/`EPSV`** ve **Aktif Mod `PORT`**) yapılır. Büyük veya ikili (resim, zip, exe, pdf) dosyalar bozulmadan aktarılır.
- **Tam Windows & Linux Uyumluluğu:**
  - Yönetici/Root yetkisi gerektirmeyen varsayılan port (`2121`).
  - Windows CMD / PowerShell Türkçe karakter (`UTF-8`) otomatik yapılandırması.
  - Windows'ta `Ctrl+C` ile sunucuyu anında kapatabilme ve güvenli soket bağlama (`SO_EXCLUSIVEADDRUSE` / `SO_REUSEADDR`).
  - Çift tıklamayla çalıştırılabilen `start_server.bat` ve `start_client.bat` başlatıcıları.
  - Kök dizin dışına çıkmayı (`../../`) ve geçersiz Windows dosya adlarını engelleyen sandbox yol doğrulaması.
- **Çoklu İstemci (Multi-threading):** Her istemci bağlantısı ayrı bir iş parçacığında (`threading.Thread`) eşzamanlı olarak yönetilir.
- **Standart İstemcilerle Uyumlu:** Kendi `client.py` istemcisinin yanı sıra FileZilla, `curl`, Python `ftplib` ve Windows `ftp.exe` ile de kullanılabilir.
- **Sıfır Harici Bağımlılık:** `pip install` gerektirmez; Python 3.10+ yüklü her sistemde doğrudan çalışır.

## Proje Yapısı

```text
ftp-server/
├── server.py           # Çok istemcili FTP sunucusu
├── client.py           # Menü, interaktif kabuk ve demo modlu FTP istemcisi
├── start_server.bat    # Windows için çift tıkla sunucu başlatıcı
├── start_client.bat    # Windows için çift tıkla istemci başlatıcı
└── tests/
    └── test_ftp.py     # Uçtan uca birim ve protokol uyumluluk testleri
```

## Kurulum ve Çalıştırma

Herhangi bir paket kurulumuna gerek yoktur (Python 3.10+ yeterlidir).

### Windows'ta Çalıştırma

**Yöntem 1 — Çift Tıklayarak:**
1. Önce `start_server.bat` dosyasına çift tıklayın (Sunucu `2121` portunda başlar ve örnek `ftp_data/data.txt` dosyasını hazırlar).
2. Ardından `start_client.bat` dosyasına çift tıklayın.

**Yöntem 2 — PowerShell veya CMD ile:**
```bat
REM 1. Terminal - Sunucuyu başlat
python server.py

REM 2. Terminal - İstemciyi başlat (Menülü mod)
python client.py
```

### Linux / macOS'ta Çalıştırma

```bash
# 1. Terminal - Sunucuyu başlat
python3 server.py

# 2. Terminal - İstemciyi başlat
python3 client.py
```

## İstemci Kullanım Modları (`client.py`)

```bash
# Numaralı interaktif menü (varsayılan)
python client.py

# Doğrudan interaktif komut kabuğu (ls, cd, get, put, mkdir, rm...)
python client.py --shell

# Otomatik demo (dosyaları listeler, data.txt indirir, örnek dosya yükler)
python client.py --demo

# Farklı bir bilgisayardaki sunucuya bağlanma
python client.py --host 192.168.1.25 --port 2121 --user user --password pass
```

### Sunucu Parametreleri (`server.py`)

```bash
python server.py --host 0.0.0.0 --port 2121 --root ftp_data --user user --password pass
```

Varsayılan giriş bilgileri:
- **Kullanıcı adı:** `user` — **Şifre:** `pass`
- **Anonim giriş:** `anonymous` (açık, `--no-anon` ile kapatılabilir)

## Desteklenen FTP Komutları

| Komut | Açıklama |
| :--- | :--- |
| `USER`, `PASS` | Kullanıcı doğrulama (Normal ve `anonymous` giriş) |
| `PASV`, `EPSV`, `PORT` | Pasif ve Aktif veri kanalı kurulumu |
| `LIST`, `NLST` | Ayrıntılı dizin listesi / dosya adı listesi |
| `RETR`, `STOR`, `APPE` | Dosya indirme, yükleme ve sonuna ekleme |
| `PWD`, `CWD`, `CDUP` | Mevcut dizini görme, dizin değiştirme, üst dizine çıkma |
| `MKD`, `RMD` | Klasör oluşturma ve silme |
| `DELE`, `RNFR`, `RNTO` | Dosya silme ve yeniden adlandırma |
| `SIZE`, `MDTM`, `TYPE` | Dosya boyutu, değiştirilme zamanı ve aktarım modu (`I`/`A`) |
| `SYST`, `FEAT`, `OPTS`, `NOOP`, `HELP`, `QUIT` | Sistem/özellik sorgulama ve oturum kontrolü |

## Testleri Çalıştırma

```bash
python -m unittest discover -s tests -v
```
