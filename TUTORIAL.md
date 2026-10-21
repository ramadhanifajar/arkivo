# arkivo — tutorial pengoperasian

Aplikasi backup untuk database (PostgreSQL, MySQL/MariaDB) dan project/file,
dengan banyak target penyimpanan, jadwal otomatis di latar belakang, dashboard
pemantauan, dan notifikasi Telegram.

---

## Daftar isi

1. [Cara kerja singkat](#1-cara-kerja-singkat)
2. [Pasang](#2-pasang)
3. [Konfigurasi](#3-konfigurasi)
4. [Dashboard](#4-dashboard)
5. [Menambah server / job](#5-menambah-server--job)
6. [Beberapa database & beberapa server](#6-beberapa-database--beberapa-server)
7. [Backup ke server lain (SSH)](#7-backup-ke-server-lain-ssh)
8. [Target penyimpanan](#8-target-penyimpanan)
9. [Jadwal otomatis di latar belakang](#9-jadwal-otomatis-di-latar-belakang)
10. [Notifikasi Telegram](#10-notifikasi-telegram)
11. [Diagnostik](#11-diagnostik)
12. [Restore](#12-restore)
13. [Perintah CLI](#13-perintah-cli)
14. [Mencari dan memulihkan arsip](#14-mencari-dan-memulihkan-arsip)
15. [Pindah ke server lain](#15-pindah-ke-server-lain)
16. [Penyelesaian masalah](#16-penyelesaian-masalah)

---

## 1. Cara kerja singkat

```
   config.toml + dashboard
             │
             ▼
    arkivo.py tick  ◄── systemd timer, tiap 5 menit
             │
   ┌─────────┼──────────┐
   ▼         ▼          ▼
pg_dump   mysqldump   tar | zstd
   └─────────┼──────────┘
             ▼
     SATU berkas arsip per database/job
             │
   ┌─────────┼──────────┬──────────┐
   ▼         ▼          ▼          ▼
 lokal    gdrive    SMB/S3    SSH/NAS
             │
             ▼
    notifikasi Telegram + riwayat di dashboard
```

**Prinsip penting:** setiap job menghasilkan **satu berkas arsip**, tidak pernah
pohon direktori berisi banyak file kecil. Ini yang membuat unggahan ke SMB dan
Google Drive andal — kegagalan paling umum pada backup manual adalah mengunggah
ribuan file kecil.

---

## 2. Pasang

```bash
cd /var/www/arkivo
sudo ./install.sh
```

Installer akan:

- mendeteksi distro (Ubuntu/Debian, Fedora/RHEL, Arch, openSUSE, Alpine)
- membaca `config.toml` dan **hanya memasang yang benar-benar dipakai**
  (mis. tidak memasang `mysqldump` kalau tidak ada job MySQL)
- memasang unit systemd: timer tiap 5 menit + layanan dashboard
- membuat `dashboard_token` acak

Lihat rencananya tanpa mengubah apa pun:

```bash
sudo ./install.sh --dry-run
```

Pakai user lain untuk layanan:

```bash
sudo ./install.sh --user www-data
```

---

## 3. Konfigurasi

Salin contoh lalu sunting:

```bash
cp config.example.toml config.toml
nano config.toml
```

### Bagian umum

```toml
[general]
data_dir = "/var/www/arkivo/data"   # state.db, penampungan sementara
retention_days = 14                    # umur arsip sebelum dihapus otomatis
min_free_percent = 15                  # peringatan kalau sisa disk di bawah ini
dashboard_port = 8787
dashboard_host = "127.0.0.1"           # biarkan localhost
dashboard_token = ""                   # diisi otomatis oleh installer
```

### Satu job database

```toml
[[job]]
name = "pg-main"          # nama unik, jadi nama berkas arsip
type = "postgres"         # postgres | mysql | mariadb | path
host = "127.0.0.1"
port = 5432
user = "postgres"
password = ""             # kosongkan kalau pakai ~/.pgpass
databases = ["*"]         # "*" = semua, atau ["app", "crm"]
schedule = "daily 02:00"
retention_days = 14
```

### Satu job project/file

```toml
[[job]]
name = "webapp-files"
type = "path"
path = "/var/www/arkivo"
exclude = [".git/objects", "node_modules", "vendor"]
schedule = "daily 03:00"
retention_days = 7
```

Setelah mengubah `config.toml`, cek hasilnya:

```bash
python3 arkivo.py doctor
python3 arkivo.py list
```

> **Catatan tentang urutan prioritas:** `config.toml` tidak pernah ditulis ulang
> oleh aplikasi. Untuk pengaturan yang bisa diubah dari dashboard (jadwal, retensi,
> aktif/nonaktif, daftar database), nilai dari dashboard yang menang. Untuk field
> lain pada job dengan nama yang sama — termasuk `password` — nilai di
> `config.toml` yang menang, kecuali password itu diisi dari dashboard (lihat
> bagian 5).

---

## 4. Dashboard

```bash
sudo systemctl start arkivo-dashboard     # kalau belum jalan
python3 arkivo.py serve                     # atau jalankan manual untuk lihat token
```

Buka:

```
http://127.0.0.1:8787/?token=<dashboard_token>
```

Token ada di `config.toml` (`dashboard_token`). Kalau dikosongkan, token acak
dibuat setiap kali dijalankan dan dicetak di terminal.

Lewat SSH dari komputer lain:

```bash
ssh -L 8787:127.0.0.1:8787 user@server
# lalu buka http://127.0.0.1:8787/?token=<token> di browser sendiri
```

Isi dashboard (tab di bilah atas):

| Bagian | Fungsi |
|---|---|
| **Ikhtisar** | murni informasi (tanpa tombol tambah job): job aktif, backup terakhir, jumlah arsip, tingkat keberhasilan, daftar job, aktivitas terbaru, dan sisa kapasitas tiap target penyimpanan (dengan peringatan bila di bawah `min_free_percent`) |
| **Job** | daftar job; status run terakhir, jadwal berikutnya, saklar aktif/nonaktif, tombol backup/arsip/edit/uji/password; panel Target penyimpanan dengan bar kapasitas dan tombol Edit jalur (lewat dialog konfirmasi) |
| **Arsip** | semua arsip tersimpan, dikelompokkan per hari, bisa difilter per tanggal dan dicari per nama job; klik nama job untuk daftar arsip job itu |
| **Riwayat** | status tiap run, ukuran, durasi, tombol log / unduh / hapus |
| **Tambah job** | tambah job baru tanpa menyentuh config.toml |
| **Ikon tema** | pilih terang / gelap / ikut sistem (default ikut sistem) |
| **Diagnostik** | cek kesehatan sistem (lihat bagian 11) |
| **Tes Telegram** | kirim pesan percobaan |
| **Panduan** | ringkasan ini di dalam dashboard |

Judul halaman dan tombol utama di kanan judul ikut berubah sesuai tab: **Tambah job**
di Ikhtisar dan Job, **Muat ulang** di Arsip dan Riwayat. Jadwal dan retensi tidak lagi
diedit langsung di tabel — keduanya ada di dialog **Edit**.

Tombol per job:

- **Backup** — jalankan sekarang (berjalan di latar belakang, browser tidak menunggu);
  tombol berhenti muncul selama job berjalan
- **Proses** — muncul selama job berjalan: membuka log yang tumbuh dan menampilkan
  fase, byte tertulis/terkirim, laju, dan waktu berjalan. Persentase berawalan `±`
  karena ukuran akhir arsip belum diketahui; angka itu memakai ukuran run sebelumnya.
- **Arsip** (ikon dokumen) — daftar arsip job ini, dikelompokkan per hari
- **Edit** (ikon pensil) — ubah tipe, host, database, jadwal, retensi, dll. Job dari
  `config.toml` tidak bisa diganti nama (ubah di config.toml); field yang dikelola
  dashboard menang.
- **Uji** (ikon corong) — coba koneksi ke sumber sekarang tanpa membuat arsip. Hasilnya
  muncul sebagai kotak status di dalam dialog; di dialog edit, job harus disimpan dulu.
- **Kunci** — isi/ubah/hapus sandi database (lihat bagian 5)
- **Tempat sampah** — hanya untuk job yang dibuat dari dashboard

Untuk mencari backup tanggal tertentu, lihat [Mencari dan memulihkan arsip](#14-mencari-dan-memulihkan-arsip).

Halaman ini sengaja tanpa aset dari internet: tidak ada font atau skrip eksternal, jadi
dashboard tetap tampil utuh di server yang tidak punya akses keluar.

---

## 5. Menambah server / job

Tombol **Tambah job** → isi form → **Simpan**.

| Kolom | Keterangan |
|---|---|
| Nama | nama unik; dipakai sebagai nama berkas arsip |
| Tipe | `postgres`, `mysql`, `mariadb`, atau `project / file` |
| Sumber | `mesin ini` atau `server lain (ssh)` |
| Host / Port / User | alamat database |
| Database | `*` untuk semua, atau `app, crm` untuk beberapa |
| Path | untuk tipe project; harus absolut |
| Jadwal | `manual`, `every30m`, `every6h`, `daily 02:00`, `weekly 6 23:00` |
| Retensi | umur arsip dalam hari |

Password diisi setelah job dibuat, lewat tombol **Password** di baris job
(lihat bagian di bawah), atau di `config.toml`.

**Password database bisa diisi dari dashboard.** Tombol **Password** pada baris job
membuka dialog isi password. Nilainya ditulis ke `data/secrets.json` dengan izin
`600` (hanya pemilik) — sengaja tidak ke `state.db` agar kredensial tidak bercampur
dengan riwayat backup. Nilai yang tersimpan **tidak pernah dikirim kembali ke
browser**; dashboard hanya menampilkan status sudah ada / belum ada.

Aturan siapa yang menang:

| Keadaan | Password yang dipakai |
|---|---|
| Belum pernah diisi dari dashboard | `config.toml`, atau `~/.pgpass` / `~/.my.cnf` |
| Diisi dari dashboard | **dashboard** — mengalahkan `config.toml` |
| Dikosongkan dari dashboard | **kosong** — `config.toml` tidak dipakai lagi |

Baris terakhir disengaja: kalau `config.toml` diam-diam memasok sandi lamanya
setelah kamu menghapusnya di dashboard, kamu akan mengira kredensial sudah dibuang
padahal job masih jalan dengan sandi itu. Untuk mengembalikan kendali ke
`config.toml`, pilih **Hapus password** lalu ubah kepemilikannya di
`data/secrets.json` (hapus nama job dari daftar `_dashboard`).

Alternatif tanpa dashboard: sandi di `config.toml`, `~/.pgpass` untuk PostgreSQL,
atau `~/.my.cnf` untuk MySQL — semuanya masih didukung.

Job yang dibuat dari dashboard langsung ikut terjadwal oleh timer systemd.

---

## 6. Beberapa database & beberapa server

### Beberapa database dalam satu job

Isi kolom **Database** (atau `databases` di config) dengan daftar dipisah koma:

```
app, crm, billing
```

Setiap database jadi **satu berkas arsip sendiri**, jadi bisa dipulihkan
terpisah:

```
pg-main_20261018-020000__app.dump
pg-main_20261018-020000__crm.dump
pg-main_20261018-020000__billing.dump
```

Kosongkan atau isi `*` untuk mengambil semua database.

### Beberapa server

Tiap `[[job]]` berdiri sendiri. Untuk server berbeda, tambah job dengan `host`
berbeda:

```toml
[[job]]
name = "pg-prod-01"
type = "postgres"
host = "10.0.0.5"
user = "backup_user"
password = "rahasia"
databases = ["app", "crm"]
schedule = "daily 04:00"
```

Bisa juga dari dashboard: ulangi **Tambah job** untuk setiap server.
Tidak ada batas jumlah job maupun jumlah server.

---

## 7. Backup ke server lain (SSH)

Dua kegunaan: mengambil **project** dari server lain, dan mem-backup **database**
yang ada di server lain, tanpa memasang apa pun di server itu.

### Prasyarat

Masuk tanpa sandi dari mesin ini ke server tujuan:

```bash
ssh-copy-id root@10.0.0.9          # sekali saja
ssh root@10.0.0.9 true && echo OK  # harus tercetak OK
```

Penting: kunci harus bisa dipakai oleh **user yang menjalankan layanan systemd**
(biasanya `root` atau user yang kamu tentukan saat `install.sh`). Kalau kunci ada
di home user lain, isi kolom **ssh kunci** dengan jalur absolutnya.

### Project di server lain

```toml
[[job]]
name = "web-prod"
type = "path"
transport = "ssh"
ssh_host = "10.0.0.9"
ssh_user = "root"
ssh_key = "/root/.ssh/id_ed25519"   # opsional
path = "/var/www/app"
exclude = ["node_modules", ".git"]
schedule = "every6h"
```

`tar` dijalankan **di server jauh**, hasilnya dialirkan lewat SSH, dikompres di
mesin ini. Tidak ada berkas sementara yang menumpuk di server jauh.

### Database di server lain

```toml
[[job]]
name = "db-prod-01"
type = "postgres"
transport = "ssh"
ssh_host = "10.0.0.9"
ssh_user = "root"
host = "127.0.0.1"      # alamat database DILIHAT DARI server ssh tersebut
port = 5432
user = "backup_user"
password = "rahasia"
databases = ["*"]
schedule = "daily 05:00"
```

Saat job berjalan, arkivo membuat **terowongan SSH** otomatis ke server itu,
lalu menjalankan `pg_dump`/`mysqldump` seperti biasa. Terowongan ditutup setelah
selesai. Database tidak perlu terbuka ke internet.

---

## 8. Target penyimpanan

Semua target menerima berkas arsip yang sama. Atur di `[[targets]]`, atau
aktif/nonaktifkan dari dashboard.

```toml
[[targets]]
name = "lokal"
type = "local"
path = "/var/www/arkivo/data/local"

[[targets]]
name = "gdrive"
type = "rclone"
path = "gdrive:backup/server1"
```

Semua tujuan jarak jauh memakai **rclone** — satu alat untuk Google Drive, SMB,
S3, dan SSH/SFTP. Tambahkan remote:

```bash
rclone config          # pilih provider: drive / smb / s3 / sftp
rclone listremotes     # lihat daftar
rclone lsd gdrive:     # uji koneksi
```

Contoh jalur per provider:

| Nama target | `type` | Contoh `path` |
|---|---|---|
| Local | `local` | `/mnt/backup` |
| Google Drive | `rclone` | `gdrive:backup/server1` |
| SMB / Windows share | `rclone` | `winserver:backup/server1` |
| S3 Compatible | `rclone` | `s3remote:bucket/backup` |
| SSH/SFTP | `rclone` | `sshnas:/mnt/backup` |
| NFS | `local` | `/mnt/nfs/backup` |

**NFS tidak didukung rclone.** Mount dulu di level OS, lalu pakai `type = "local"`:

```bash
sudo mkdir -p /mnt/nfs/backup
# /etc/fstab, tambahkan:
#   10.0.0.20:/export/backup  /mnt/nfs/backup  nfs  defaults,_netdev  0 0
sudo mount -a && df -h /mnt/nfs/backup
```

Pakai `_netdev` supaya sistem menunggu jaringan siap sebelum me-mount.

Retensi berjalan otomatis di setiap target: arsip lebih tua dari
`retention_days` dihapus.

---

## 9. Jadwal otomatis di latar belakang

Timer systemd memanggil `arkivo.py tick` tiap 5 menit. Setiap panggilan hanya
menjalankan job yang **jatuh tempo**.

Format jadwal:

| Nilai | Arti |
|---|---|
| `manual` | hanya lewat tombol/CLI |
| `every30m` | tiap 30 menit sejak run terakhir |
| `every6h` | tiap 6 jam |
| `hourly` | tiap jam |
| `daily 02:00` | tiap hari pukul 02:00 |
| `weekly 6 23:00` | Sabtu 23:00 (0=Minggu … 6=Sabtu) |

Perintah berguna:

```bash
python3 arkivo.py list                              # jadwal + status jatuh tempo
python3 arkivo.py tick                              # jalankan yang jatuh tempo
systemctl status arkivo.timer                    # status timer
systemctl list-timers arkivo.timer               # kapan berikutnya
journalctl -u arkivo.service -n 50               # log run terjadwal
journalctl -u arkivo.service -f                  # ikuti langsung
```

Ingin lebih presisi dari 5 menit? Sunting `systemd/arkivo.timer`, lalu:

```bash
sudo cp systemd/arkivo.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart arkivo.timer
```

---

## 10. Notifikasi Telegram

1. Buat bot: kirim pesan ke **@BotFather**, ikuti perintah, catat token.
2. Dapatkan `chat_id`: kirim pesan ke bot-mu, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` dan lihat `chat.id`.
   Untuk grup, tambahkan bot ke grup dan pakai id bernilai negatif.

Isi di `config.toml`:

```toml
[telegram]
bot_token = "123456:ABC-DEF..."
chat_id = "-1001234567890"
```

Uji dari dashboard: tombol **Tes Telegram**.

Notifikasi dikirim saat: job selesai (berhasil/gagal), sebagian target gagal, dan
sisa storage target di bawah `min_free_percent`.

---

## 11. Diagnostik

Tujuan: mendeteksi hal yang membuat backup gagal **sebelum** kamu menyadarinya.

### Di terminal

```bash
python3 arkivo.py doctor
```

### Di dashboard

Tombol **Diagnostik**. Yang diperiksa:

- alat wajib ada: `tar`, `zstd`, `pg_dump`, `psql`, `mysqldump`, `rclone`, `ssh`
- **versi `pg_dump` dan `pg_restore` sinkron** — kalau tidak, arsip bisa tidak
  bisa dipulihkan sama sekali
- remote rclone yang dipakai config benar-benar ada
- direktori target lokal ada dan bisa ditulis, sisa disk di atas batas
- `state.db` utuh (`PRAGMA integrity_check`)
- berkas sementara yang tertinggal dari job gagal/dibatalkan
- sumber tiap job ada; koneksi SSH tiap job SSH berhasil
- job database tanpa password dan tanpa berkas kredensial klien

Ada juga tombol **Uji arsip terbaru** — membaca ulang arsip terakhir setiap job
untuk memastikan isinya benar-benar bisa dipulihkan (`tar -t` sekali lintas,
`pg_restore -l`). Ini lebih lambat karena membaca berkas besar.

Jalankan juga setelah perubahan besar:

```bash
python3 test_arkivo.py        # 87 pemeriksaan mandiri
```

---

## 12. Restore

### Database PostgreSQL

`pg_dump -Fc` dipulihkan dengan `pg_restore`, bukan `psql`:

```bash
createdb -h 127.0.0.1 -U postgres namadb          # database tujuan harus ada dulu
./restore.sh pg arsip.dump namadb
```

Untuk server lain, isi variabel koneksi:

```bash
PGHOST=10.0.0.5 PGPORT=5432 PGUSER=postgres PGPASSWORD=rahasia \
  ./restore.sh pg arsip.dump namadb
```

Versi klien dipilih otomatis agar cocok dengan arsip. Paksa manual bila perlu:

```bash
PGVERSION=16 ./restore.sh pg arsip.dump namadb
```

### Database MySQL/MariaDB

```bash
./restore.sh my arsip.sql.zst                       # pulihkan semua database
./restore.sh my arsip.sql.zst namadb                # satu database
```

Untuk server lain:

```bash
MYSQL_HOST=10.0.0.5 MYSQL_PORT=3306 MYSQL_USER=root \
  ./restore.sh my arsip.sql.zst
```

### Project / file

```bash
mkdir -p /tmp/pulih
./restore.sh tar arsip.tar.zst /tmp/pulih
```

Periksa isinya dulu tanpa menulis apa pun:

```bash
tar --zstd -tf arsip.tar.zst | head
```

### Ambil arsip dari target

Arsip lokal bisa diunduh dari dashboard (tombol unduh di riwayat) atau langsung
dari `data_dir/<target>/<nama-job>/`. Untuk target jarak jauh:

```bash
rclone ls gdrive:backup/server1/pg-main
rclone copy gdrive:backup/server1/pg-main/pg-main_20261018-020000__app.dump .
```

---

## 13. Perintah CLI

```bash
python3 arkivo.py doctor            # periksa prasyarat & kesehatan
python3 arkivo.py list              # daftar job, jadwal, status jatuh tempo
python3 arkivo.py run <nama-job>    # jalankan satu job sekarang
python3 arkivo.py tick              # jalankan semua job yang jatuh tempo
python3 arkivo.py serve             # jalankan dashboard
python3 test_arkivo.py              # uji mandiri
```

Kode keluar: `0` berhasil, `1` gagal/ada masalah, `2` salah pemakaian.

---

## 14. Mencari dan memulihkan arsip

Tab **Arsip** menampilkan semua arsip yang masih tersimpan di target lokal: nama job,
waktu backup, nama berkas, ukuran, dan target. Urut terbaru dulu.

- **Filter tanggal** — pilih tanggal di kolom kiri atas untuk melihat backup hari itu saja.
- **Cari job** — kolom kanan atas menyaring per nama job.
- **Klik nama job** (di tab Arsip atau Job) membuka daftar arsip job itu, lengkap dengan
  pencarian berkas dan filter tanggal.
- Tiap baris punya empat aksi:
  - **Unduh** — simpan arsip ke komputer.
  - **Verifikasi** (ikon centang) — membaca ulang arsip untuk memastikan masih bisa
    dipulihkan: `tar -tf` sekali lintas untuk `.tar.zst` (sekaligus menguji aliran
    zstd-nya), atau `pg_restore -l` untuk `.dump`. Berjalan di latar server, dialog
    menampilkan detik berjalan dan boleh ditutup. Ini menjawab "backup hari itu
    benar-benar utuh?".
  - **Salin perintah restore** — menaruh perintah siap pakai di clipboard, mis.
    `./restore.sh pg db-produksi_20261018-020000.dump <tujuan>`.
  - **Cara buka** (ikon tanda tanya) — perintah ekstrak `.tar.zst` untuk Linux,
    macOS, dan Windows. Panduan yang sama juga ikut di dalam setiap arsip project
    sebagai `CARA-BUKA.txt`, jadi tetap ada walau arsipnya dipindah ke mesin lain.
  - **Hapus** — buang satu arsip (tidak menyentuh riwayat run).

Daftar ini dibaca langsung dari direktori target, bukan dari riwayat run, karena retention
menghapus berkas tanpa menyentuh riwayat — jadi arsip yang sudah hilang tidak akan muncul.

Contoh alur memulihkan backup tanggal tertentu:

1. Tab **Arsip** → isi filter tanggal, mis. `2026-10-18`.
2. **Verifikasi** arsip yang dituju; pastikan hasilnya "Arsip bisa dipulihkan".
3. **Unduh**, lalu jalankan perintah dari tombol **Salin perintah restore**:

```bash
./restore.sh pg db-produksi_20261018-020000.dump namadb    # PostgreSQL
./restore.sh my app-mysql_20261018-020000.sql.zst          # MySQL/MariaDB
./restore.sh tar webapp-files_20261018-030000.tar.zst /tmp/pulih   # project / file
```

Untuk server lain, siapkan dulu variabel koneksi (`PGHOST`, `PGUSER`, `PGPASSWORD`) atau
salin arsipnya ke server itu lalu jalankan `restore.sh` di sana.

---

## 15. Pindah ke server lain

Bungkus aplikasi (tanpa data dan tanpa kredensial):

```bash
cd /var/www
tar --exclude='arkivo/data' --exclude='arkivo/config.toml' \
    --exclude='__pycache__' -czf arkivo.tgz arkivo
```

Di server baru:

```bash
scp arkivo.tgz user@server-baru:
ssh user@server-baru 'tar xzf arkivo.tgz && cd arkivo && sudo ./install.sh'
```

Installer memasang sendiri semua yang kurang di server itu, membuat unit systemd
dengan jalur dan user yang benar, dan membuat token dashboard baru. Berkas
`config.toml` sengaja tidak dibawa karena memuat password — buat ulang dari
`config.example.toml` di server baru.

Untuk memindahkan riwayat sekaligus, salin `data/state.db` secara manual.

---

## 16. Penyelesaian masalah

**Job gagal: "password ... no password supplied"**
Isi `password` di `config.toml`, atau sediakan `~/.pgpass` / `~/.my.cnf` untuk
user yang menjalankan layanan.

**Job gagal: "ssh gagal: Permission denied"**
Kunci SSH belum bisa dipakai user layanan. Uji dengan user yang sama:
`sudo -u <user> ssh <host> true`. Isi kolom ssh kunci dengan jalur absolut.

**Job "partial": "N berkas tidak bisa dibaca dan dilewati"**
Tar menemukan berkas yang tidak boleh dibaca user SSH (misalnya folder `storage`
milik `www-data` dengan izin `600`). Arsip tetap dibuat dan dikirim dari semua
berkas yang bisa dibaca; job ditandai **partial** dan pesannya menyebut berkas
yang dilewati. Pilihan:

- biarkan — biasanya berkas sementara (cache, PDF sementara) tidak perlu ikut;
- isi kolom **Kecualikan** di form Edit job, mis. `storage/app/tmp, storage/logs`;
- kalau folder project adalah arkivo sendiri: `config.toml`, `data/secrets.json`, dan
  `data/state.db` **selalu dikecualikan otomatis** karena berisi password. Hanya yang di
  root project; `config.toml` milik aplikasi lain di subfolder tetap ikut.
- atau jalankan job sebagai user yang bisa membaca folder itu (ubah ssh user).

Galat nyata tetap menggagalkan job: disk penuh, `Cannot write`, path sumber hilang,
atau tar keluar tanpa alasan yang dikenal.

**"pg_restore: unsupported version"**
`pg_dump` dan `pg_restore` dari versi berbeda. Jalankan `python3 arkivo.py doctor`
— bagian ini akan ditandai. Perbaiki dengan menyamakan versi klien, atau paksa
`PGVERSION=<versi>`.

**"Access denied" padahal password benar (MySQL)**
Ada `~/.my.cnf` yang menimpa koneksi. arkivo sudah memakai `--no-defaults`
sehingga tidak terpengaruh; kalau kamu menjalankan `mysqldump` manual, tambahkan
`--no-defaults`.

**Sudah isi password di dashboard tapi backup masih gagal**
Cek dulu workspace job-nya: password yang benar-benar dipakai bisa dilihat dari
pesan `using password: YES` / `NO` di log run. Kalau tertulis `NO`, password itu
kosong — mungkin job ditandai dikelola dashboard tapi nilainya belum tersimpan.
Tombol **Password** menampilkan status; isi ulang lalu **Simpan**.

**Lupa password yang tersimpan**
Nilainya tidak bisa ditampilkan kembali (disengaja). Isi ulang lewat tombol
**Password**, atau kosongkan berkasnya:

```bash
cat data/secrets.json      # berisi nama job -> password
```

Berkas itu milik user yang menjalankan layanan, jadi aksesnya terbatas. Kalau
kamu ingin kembali memakai sandi dari `config.toml`, hapus nama job dari daftar
`_dashboard` di berkas itu.

**Job yang dihapus meninggalkan sisa**
Menghapus job dari dashboard tidak membersihkan entri passwordnya di
`data/secrets.json`, dan blok `[[job]]` di `config.toml` juga tidak ikut terhapus.
Bersihkan manual bila perlu — nama job adalah kuncinya di kedua tempat.

**Unggahan ke SMB/Google Drive gagal**
Pastikan `rclone` sehat: `rclone lsd <remote>:`. Tiap job mengirim satu berkas,
jadi kegagalan biasanya soal jaringan atau kredensial remote, bukan jumlah berkas.

**Job tidak jalan sesuai jadwal**
```bash
python3 arkivo.py list                  # apakah ditandai [nonaktif] / JATUH TEMPO?
systemctl status arkivo.timer        # apakah timer aktif?
journalctl -u arkivo.service -n 50   # apa kata tick terakhir?
```

**Lupa token dashboard**
```bash
grep dashboard_token config.toml
```

**Dashboard tidak bisa dibuka**
Pastikan `dashboard_host = "127.0.0.1"` dan akses lewat terowongan SSH. Jangan
membukanya ke internet tanpa reverse proxy ber-HTTPS.

**Ingin membatalkan job yang sedang berjalan**
Tombol **Batal** di dashboard. Berkas arsip separuh akan dibuang, dan statusnya
tercatat gagal dengan keterangan "dibatalkan dari dashboard".
