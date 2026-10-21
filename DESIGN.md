# DESIGN.md — arkivo dashboard

Satu-satunya permukaan visual: dashboard `arkivo.py serve`, ditulis sebagai string `PAGE`
di dalam `arkivo.py`. Tidak ada build step, tidak ada file aset, tidak ada CDN — halaman
harus tetap utuh di server tanpa internet.

Mode: **Operate**. Pengguna datang untuk satu tugas (memastikan backup jalan dan bisa
dipulihkan), sering dibuka sambil memeriksa hal lain, dan tidak boleh dipaksa belajar
antarmuka baru.

## Dunia visual

Meja kerja terang di kantor berpenerangan normal: panel putih di atas latar abu sejuk,
sudut membulat besar, garis rambut tipis. Tenang dan lapang, bukan gelap-teknis. Satu
aksen indigo untuk aksi dan seleksi; hijau/kuning/merah hanya untuk status.

Mode gelap mengikuti `prefers-color-scheme`; tombol tema di topbar memaksa
terang/gelap/sistem (disimpan di `localStorage`). Setiap warna yang berubah antar tema
adalah token di `:root`, didefinisikan ulang di `@media (prefers-color-scheme:dark)`
dan `[data-theme=dark]` — tidak ada warna mentah di aturan komponen.

| Token | Nilai (terang / gelap) | Peran |
|---|---|---|
| `--page` | `#eceff3` / `#0f1216` | latar halaman |
| `--card` / `--card-2` | `#fff`/`#f6f7f9` / `#171b21`/`#1d222a` | panel / header-tabel, hover, nav |
| `--line` / `--line-2` | `#e5e8ee`/`#d7dbe3` / `#262c35`/`#39414d` | pemisah / garis kontrol |
| `--ink` / `--ink-2` / `--ink-3` | `#111318`/`#4b5563`/`#6b7280` / `#e7eaef`/`#b7bec9`/`#8b93a1` | teks utama / sekunder / meta |
| `--accent` / `--accent-2` / `--accent-soft` | `#4f46e5`/`#4338ca`/`#eef0ff` / `#818cf8`/`#a5b4fc`/`#232746` | tombol utama, seleksi, avatar |
| `--onaccent` | `#fff` / `#10131a` | teks di atas aksen |
| `--ok` / `--warn` / `--bad` / `--info` | `#15803d`/`#a16207`/`#b42318`/`#1d4ed8` / `#4ade80`/`#fbbf24`/`#f87171`/`#93b4ff` | status |

Kontras teks terendah (`--ink-3` pada `--card-2`) 4.6:1 di kedua tema; teks utama 17:1.
Setiap warna status selalu disertai label teks, tidak pernah warna saja.

Tipografi: satu keluarga sans sistem (`--sans`) untuk UI; `--mono` hanya untuk data mesin
(nama berkas, jalur, log, perintah restore). Ukuran tetap, bukan fluid: judul halaman 26px,
judul panel 14.5px, tabel 13px, label tabel 11px huruf besar berkurir `.06em`.

## Tata letak

- **Topbar kartu** melayang (`sticky`), berisi merek, navigasi pil (Ikhtisar, Job, Arsip,
  Riwayat), dan aksi global (tema, Diagnostik, Tes Telegram, Panduan).
- **Identitas halaman** mengikuti halaman aktif: judul tab (`Job · arkivo`), judul `h1`
  (Ringkasan backup / Job backup / Arsip tersimpan / Riwayat run), dan tombol utama di
  kanan (Tambah job pada Job, Muat ulang pada Arsip & Riwayat). Ikhtisar tidak punya
  tombol utama — halaman itu hanya membaca. Sumbernya satu peta `VIEWS` di `setView()`.
- **Ikhtisar**: banner kapasitas bila ada target di bawah `min_free_percent`, empat kartu
  KPI (job aktif, backup terakhir, arsip, keberhasilan dengan bar komposisi), lalu grid
  dua kolom: kiri **Job** dan **Kapasitas penyimpanan** bertumpuk dengan lebar sama
  (2fr), kanan **Aktivitas** membentang setinggi keduanya (`grid-row:1 / span 2`,
  `align-self:stretch`). Di bawah 1000px semuanya jadi satu kolom. Ikhtisar hanya
  informasi: tidak ada tombol backup, arsip, edit job, atau tambah job di sini.
- **Job**: tabel penuh (Database/sumber, Terakhir, Berikutnya, Jadwal/retensi sebagai teks,
  saklar aktif, aksi) + panel Target penyimpanan: jalur sebagai teks (bukan input di
  tabel), kapasitas per target, dan tombol **Edit** yang membuka dialog konfirmasi.
  Jalur tidak pernah tersimpan hanya karena terketik — harus lewat Simpan di dialog.
- **Arsip**: satu tabel semua arsip, dikelompokkan per hari (header lengket per tanggal,
  jumlah berkas dan total ukuran), filter tanggal (`<input type=date>`) dan pencarian nama
  job di kotak pencarian berikon. Klik nama job → dialog arsip job itu, dengan baris filter
  berpadding sendiri, jumlah hasil/ukuran di baris terpisah di bawahnya, pengelompokan per
  hari, unduh, verifikasi, salin perintah restore, hapus.
- **Riwayat**: 60 run terakhir dengan ukuran, durasi, log, unduh, hapus.

Jam dan retensi tidak lagi diedit langsung di tabel Job — semuanya lewat dialog edit.
Navigasi memakai hash (`#jobs`, `#archives`, `#history`) supaya tautan bisa dibagikan dan
halaman kembali ke tampilan yang sama setelah muat ulang.

## Ikon merek

Favicon adalah `data:` SVG inline di `<head>` (tanpa berkas aset): kotak indigo bersudut
bulat berisi panah masuk ke baki. Mark di topbar memakai bentuk yang sama.

## Bahasa komponen

- **Kartu** — radius 20px (panel) / 16px (KPI), garis 1px, tanpa bayangan. Elevasi
  dinyatakan sekali lewat garis.
- **Status chip** — titik 8px + label (`berhasil`, `sebagian`, `gagal`, `berjalan`,
  `belum pernah`).
- **Tombol** — tinggi 36px (30px untuk aksi baris), radius 11px. Varian: default (garis),
  `primary` (indigo, satu per area), `ghost`, `sq` (ikon 30px), `danger` (hover merah).
  Label teks tombol baris disembunyikan di ponsel; `title` dan `aria-label` tetap ada.
- **Kotak hasil** (`resnote`) — dipakai untuk hasil uji koneksi (dialog job dan dari baris
  Job) dan verifikasi arsip: titik status + judul tebal + detail, latar `--ok-soft`/
  `--bad-soft`/`--warn-soft`. Bukan baris teks kecil yang terselip di antara tombol.
  Selama menunggu, kotak yang sama berisi "Menguji koneksi…" dengan titik berdenyut.
- **Kapasitas** — bar 8px dengan ambang yang sama dengan `min_free_percent` di server:
  hijau (aman), kuning (di bawah 2× batas), merah (di bawah batas). Angka selalu menyertai
  warna (`80,6 GB bebas dari 227,1 GB`, `35% sisa`).
- **Pengaturan Telegram** — dialog dari ikon pesawat di topbar: bot token, chat id,
  saklar notifikasi otomatis, tombol Simpan dan Kirim tes. Token tidak pernah dikirim
  balik ke browser; field kosong berarti "pertahankan yang tersimpan".
- **Kapasitas tak terbaca** — bila `rclone about` belum/gagal dijawab, `disk` bernilai
  `null` dan semua helper (`diskState`, `bar`, `diskCell`, `capNote`) diperiksa dulu;
  tidak boleh ada akses `d.used` tanpa cek. Probe rclone berjalan di thread latar dengan
  cache 5 menit supaya `/api/status` tidak pernah tertahan.
- **Progres realtime** — server mencatat fase, byte, laju, dan daftar log per job
  yang sedang jalan (`PROGRESS`, dibaca `/api/progress`). Dump diukur dengan
  mengintip ukuran file tampungan tiap 0,5 s; upload rclone memakai baris
  `--stats 1s` miliknya sendiri. Baris job menampilkan chip
  `dump · 871 MB · 155 MB/s · ±73% · 4,0s`, tombol **Proses** membuka log yang
  tumbuh. Tidak ada persentase "pasti": angka `±` memakai ukuran run sebelumnya.
- **Panduan ekstrak** — setiap arsip project memuat `CARA-BUKA.txt` di root-nya
  (dibuat saat backup, bukan disalin dari repo), berisi perintah Linux/macOS/Windows
  dan cara memeriksa arsip rusak. Tombol tanda tanya di tiap baris arsip menampilkan
  isi yang sama di dialog, supaya bisa dibaca tanpa mengunduh.
- **Pengelompokan hari** — header lebar penuh berlatar `--card-2` (Hari ini / Kemarin /
  tanggal panjang `id-ID`) dengan jumlah berkas dan total ukuran; dipakai di halaman
  Arsip dan dialog arsip.
- **Ikon** — SVG digambar sendiri di `IC`, satu gaya: 24x24, `fill:none`, stroke 1.7,
  ujung bulat. Atribut SVG selalu dikutip (bug nyata: `r=4/` merusak nilai atribut).
- **Panel bawaan browser** — seleksi teks, `caret-color:var(--accent)`, scrollbar tipis,
  `:focus-visible` cincin indigo, tombol `×` pada dialog. Bagian dari desain, bukan
  bawaan yang dibiarkan.
- **Skeleton** shimmer saat pemuatan pertama; refresh 30 detik tidak mengosongkan tabel.
- **Toast** (`role=status`) untuk hasil aksi.
- **Konfirmasi** (`confirmBox()`, `<dialog id=askbox>`) menggantikan `confirm()`
  bawaan: kartu 440px, ikon lingkaran (info indigo / hapus merah), judul, satu
  kalimat akibat, detail monospace untuk path/nama, dua tombol (Batal + aksi).
  Fokus awal di **Batal**, Esc dan klik backdrop = batal. Dipakai untuk jalankan
  backup, hapus job, hapus password, hapus arsip.
  Jangan menamai variabel global sama dengan `id` elemen — `ask` bentrok dengan
  `window.ask` dan mematikan fungsinya.
- **Dialog** — `<dialog>` bawaan, 600px (880px untuk form job dan daftar arsip). Isi teks
  (panduan/log) memakai `<pre>`; isi panel memakai `#dlgr` — jangan menaruh HTML di `<pre>`.

## Aturan yang tidak boleh dilanggar

1. **Tanpa aset eksternal.** Tidak ada font, skrip, atau gambar dari jaringan.
2. **Nilai pengguna selalu di-escape** lewat `esc()` sebelum masuk `innerHTML`.
3. **Path arsip tidak pernah ditaruh mentah di atribut `onclick`** — pakai indeks ke
   `ARCHIVES` (`dlJob(job, i)`) supaya tanda kutip di nama berkas tidak merusak markup.
4. **Aksesibilitas:** `aria-label` di setiap kontrol, `title` di tombol ikon,
   `:focus-visible` terlihat, target sentuh ≥30px.
5. **Ponsel:** kolom sekunder `.hide-sm`, tabel tetap satu baris dengan scroll horizontal
   hanya bila perlu; filter dan pencarian melebar penuh.
6. **Angka:** ukuran lewat `human()` di server, durasi lewat `fmtDur()`, tanggal lewat
   `toLocaleDateString('id-ID')`.

## Verifikasi

Diverifikasi dengan merender halaman sungguhan (`google-chrome --headless --screenshot`)
pada 1440px dan 390px: Ikhtisar, Job, Arsip (desktop + ponsel), dialog arsip per job, form
tambah/edit termasuk mode SSH, diagnostik, log, password, uji koneksi, dan keadaan kosong.
`test_arkivo.py` memeriksa string `PAGE` yang benar-benar dikirim ke browser (escape kutip,
sintaks JS lewat `node --check`) dan `list_archives()`.

## Bahasa

Dashboard dua bahasa: Inggris (default) dan Indonesia, dipilih dari tombol
`EN/ID` di topbar, disimpan sebagai override `kind='general', field='lang'`.

Teks sumber di `PAGE` tetap bahasa Indonesia. Terjemahan Inggris dilakukan di
browser oleh `tr()`: kamus `ID2EN` (exact match) lalu `DYN` (pola untuk teks
ber-interpolasi). Teks yang tidak ada di kamus lolos apa adanya — itu yang
membuat nama job, path, dan pesan error tool tidak ikut diterjemahkan.
Elemen yang perlu terjemahan utuh memakai `data-i18n`, placeholder memakai
`data-i18n-ph`. Hasil terjemahan ditandai per node (`EN_DONE`) supaya tidak
diterjemahkan dua kali — beberapa nilai Inggris juga kunci Indonesia yang sah.

Diagnostik disusun di server, jadi terjemahannya di `diag_localize()` (Python),
bukan lewat kamus DOM.
