#!/usr/bin/env python3
"""arkivo - dump DB/project, kirim ke banyak target, retention, notifikasi.

CLI:
  ./arkivo.py doctor          cek prasyarat
  ./arkivo.py list            daftar job + jadwal berikutnya
  ./arkivo.py run <job>       jalankan satu job sekarang
  ./arkivo.py tick            jalankan job yang jatuh tempo (dipakai timer)
  ./arkivo.py serve           dashboard web

Pengaturan yang diubah dari dashboard disimpan di data/state.db, tidak pernah
ditulis balik ke config.toml, supaya komentar dan password di file itu tetap utuh.
"""
import contextlib
import html
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:  # Python 3.11+ punya tomllib; server lama pakai tomli
    import tomllib
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("butuh Python 3.11+ atau paket 'tomli'. Jalankan: sudo ./install.sh")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ARKIVO_CONFIG", ROOT / "config.toml"))


def load_config():
    with open(CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


def data_dir(cfg):
    d = Path(cfg.get("general", {}).get("data_dir", ROOT / "data")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def connect(cfg):
    db = data_dir(cfg) / "state.db"
    con = sqlite3.connect(db, timeout=30)
    con.execute(
        "CREATE TABLE IF NOT EXISTS runs("
        " id INTEGER PRIMARY KEY, job TEXT, started TEXT, finished TEXT,"
        " status TEXT, artifact TEXT, bytes INTEGER, error TEXT, log TEXT,"
        " heartbeat TEXT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS state(job TEXT PRIMARY KEY, last_run TEXT)")
    # pengaturan yang diubah dari dashboard. Disimpan di sini, BUKAN di config.toml,
    # supaya komentar dan password di file itu tidak pernah ditulis ulang.
    con.execute(
        "CREATE TABLE IF NOT EXISTS overrides("
        " kind TEXT, name TEXT, field TEXT, value TEXT,"
        " PRIMARY KEY(kind, name, field))"
    )
    con.commit()
    # migrasi: state.db dari versi sebelum ada kolom log
    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    # jobs baru yang ditambahkan dari dashboard (host/port/user/database/…).
    # Password tidak pernah di sini — tetap di config.toml, tidak lewat browser.
    con.execute(
        "CREATE TABLE IF NOT EXISTS jobs("
        " name TEXT PRIMARY KEY, data TEXT, added TEXT, deleted INTEGER DEFAULT 0)"
    )
    con.commit()
    if "log" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN log TEXT")
        con.commit()
    # heartbeat: proses yang masih hidup memperbarui kolom ini berkala. Run yang
    # lama tapi heartbeat-nya segar adalah upload lambat, bukan proses mati.
    if "heartbeat" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN heartbeat TEXT")
        con.commit()
    return con


# ---------------------------------------------------------------- job dinamis

# kunci untuk read-modify-write secrets.json; dashboard melayani request paralel
_secrets_lock = threading.Lock()


def secrets_path(cfg):
    return data_dir(cfg) / "secrets.json"


def ssh_dir(cfg):
    d = data_dir(cfg) / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def load_secrets(cfg):
    """Baca data/secrets.json. Berkas hilang/rusak dianggap kosong, bukan error fatal."""
    p = secrets_path(cfg)
    try:
        with open(p) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_secrets(cfg, data):
    """Tulis secrets.json dengan izin 600, atomik."""
    p = secrets_path(cfg)
    tmp = p.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _read_modify_write_secrets(cfg, change):
    """Ubah secrets.json dengan aman: read-modify-write di bawah satu kunci.

    Tanpa kunci, dua permintaan yang datang hampir bersamaan sama-sama membaca isi
    lama lalu menimpa satu sama lain, sehingga satu perubahan hilang.
    """
    with _secrets_lock:
        data = load_secrets(cfg)
        data = change(data) or data
        _write_secrets(cfg, data)
        return data


def save_secret(cfg, name, password):
    """Simpan/hapus password satu job, dan tandai job ini dikelola dashboard.

    Satu penulisan saja: kalau password disimpan lalu penanda ditulis terpisah,
    penulisan kedua membangun ulang isi berkas dari salinan lama dan bisa
    mengembalikan password yang baru saja dihapus.
    """
    def change(data):
        names = set(data.get("_dashboard", []) or [])
        if password:
            data[name] = password
        else:
            data.pop(name, None)
            # penanda DIPERTAHANKAN walau password dikosongkan: dashboard menyatakan
            # "job ini tanpa password". Kalau penanda dilepas, config.toml akan
            # diam-diam memasok sandi lama dan "hapus" tampak tidak berefek.
        names.add(name)
        data["_dashboard"] = sorted(names)
        return data

    _read_modify_write_secrets(cfg, change)


def password_overrides(cfg):
    """Nama job yang passwordnya diatur dari dashboard (bukan dari config.toml)."""
    return set(load_secrets(cfg).get("_dashboard", []) or [])


def set_password_override(cfg, name, on):
    """Tandai/lepas satu job sebagai dikelola dashboard (tanpa mengubah passwordnya)."""
    def change(data):
        names = set(data.get("_dashboard", []) or [])
        names.add(name) if on else names.discard(name)
        data["_dashboard"] = sorted(names)
        return data

    _read_modify_write_secrets(cfg, change)


def _slug(text, fallback="job"):
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(text).strip().lower()).strip("-")
    return s[:60] or fallback


def db_jobs(con, include_deleted=False):
    """Job yang ditambahkan dari dashboard. Bentuknya sama seperti [[job]] di config.

    `_key` adalah kunci baris (unik), `name` adalah nama tampilan yang dipakai
    untuk nama artefak dan jadwal. Keduanya bisa berbeda untuk job yang berasal
    dari config.toml lalu diedit di dashboard (barisnya diberi akhiran __db).
    """
    out = []
    for name, data, deleted in con.execute("SELECT name, data, deleted FROM jobs ORDER BY name"):
        if deleted and not include_deleted:
            continue
        try:
            job = json.loads(data)
        except (ValueError, TypeError):
            continue
        job["_key"] = name
        job["name"] = job.pop("display_name", None) or name
        job["_source"] = "dashboard"
        out.append(job)
    return out


def apply_password_overrides(cfg, con):
    """Terapkan password yang diatur dari dashboard.

    Untuk job yang ditandai di secrets.json, password SEPENUHNYA dari dashboard:
    kalau dashboard mengatakan kosong, hasilnya kosong. Kalau tidak, menghapus
    password di dashboard akan diam-diam memakai sandi lama dari config.toml —
    kamu pikir sudah dihapus, padahal masih aktif.

    Job yang tidak ditandai tidak tersentuh: passwordnya tetap dari config.toml
    atau berkas kredensial klien.
    """
    secrets = load_secrets(cfg)
    marked = set(secrets.get("_dashboard", []) or [])
    if not marked:
        return cfg
    for job in cfg.get("job", []):
        if job.get("name") in marked:
            job["password"] = secrets.get(job["name"], "")
    return cfg


def load_jobs(cfg, con, base=None):
    """SATU jalur penggabungan untuk seluruh aplikasi. Urutannya penting:

    1. job dari config.toml
    2. definisi job dari dashboard (config menang untuk field non-whitelist)
    3. pengaturan dashboard (jadwal, retensi, enabled, databases) — dashboard menang
    4. password dashboard — dashboard menang, hanya untuk job yang ditandai

    Semua pemanggil (tick, list, run, doctor, dashboard) harus lewat sini, supaya
    tidak ada perintah yang memakai gabungan berbeda.

    Tidak memutasi `cfg` yang diberikan: dict job/target disalin lebih dulu, supaya
    hasil satu panggilan tidak mencemari panggilan berikutnya.
    """
    job_dicts = {id(j): dict(j) for j in cfg.get("job", [])}
    target_dicts = {id(t): dict(t) for t in cfg.get("targets", [])}
    cfg = {
        **cfg,
        "job": [job_dicts[id(j)] for j in cfg.get("job", [])],
        "targets": [target_dicts[id(t)] for t in cfg.get("targets", [])],
    }
    cfg = merge_jobs(base if base is not None else cfg, con)
    cfg = apply_overrides(cfg, con)
    cfg = apply_telegram_overrides(cfg, con)
    return apply_password_overrides(cfg, con)


def apply_telegram_overrides(cfg, con):
    """Nilai telegram yang diisi dari dashboard menimpa config.toml."""
    over = {(f): v for k, n, f, v in
            con.execute("SELECT kind, name, field, value FROM overrides WHERE kind='telegram'")}
    if not over:
        return cfg
    tg = dict(cfg.get("telegram", {}))
    for field in EDITABLE_TELEGRAM_FIELDS:
        if field in over:
            tg[field] = over[field]
    cfg["telegram"] = tg
    return cfg


def merge_jobs(cfg, con):
    """Gabungkan job dari config.toml dengan job yang ditambahkan dari dashboard.

    Kalau namanya sama, isi config.toml MENANG per field. Itu jalur yang dipakai
    untuk mengisi password job buatan dashboard: identitasnya (type, host, user,
    database) dari dashboard, password dari config.toml. Tanpa aturan ini, blok
    config akan menimpa job dashboard bulat-bulat dan menyisakan job tanpa type.
    """
    jobs = list(cfg.get("job", []))
    index = {j.get("name"): i for i, j in enumerate(jobs)}
    for job in db_jobs(con):
        i = index.get(job["name"])
        if i is None:
            index[job["name"]] = len(jobs)
            jobs.append(job)
        elif job.get("_key", "").endswith("__db"):
            # job config.toml yang diedit di dashboard: definisi dashboard
            # MENGGANTIKAN seluruh job, bukan digabung. Kalau digabung, field lama
            # yang tidak diubah di form (mis. password) tetap menang dan hasil edit
            # terasa tidak berefek. Pengecualian: password tidak pernah lewat form
            # edit, jadi ambil dari config.toml (password dashboard diterapkan
            # belakangan oleh apply_password_overrides).
            shim = dict(job)
            if not shim.get("password"):
                shim["password"] = jobs[i].get("password", "")
            jobs[i] = shim
        else:
            merged = {**job, **jobs[i], "_source": "dashboard", "_key": job.get("_key")}
            jobs[i] = merged
    # job config yang dihapus dari dashboard: jangan dijalankan lagi
    deleted = {r[0] for r in con.execute(
        "SELECT name FROM overrides WHERE kind='job' AND field='_deleted' AND value='true'")}
    if deleted:
        jobs = [j for j in jobs if not (j.get("name") in deleted
                                        and j.get("_source") != "dashboard")]
    cfg["job"] = jobs
    return cfg


# field yang boleh diubah langsung (tanpa menambah/menghapus job)
EDITABLE_JOB_FIELDS = ("schedule", "retention_days", "enabled", "databases")
# target: nama adalah kunci override, jadi hanya jalur & saklar yang bisa diubah
EDITABLE_TARGET_FIELDS = ("enabled", "path")
# telegram: satu blok, disimpan di overrides dengan kind "telegram"
EDITABLE_TELEGRAM_FIELDS = ("bot_token", "chat_id", "enabled", "title")
# preferensi tampilan dashboard (bukan kredensial), disimpan di overrides juga
EDITABLE_GENERAL_FIELDS = ("lang",)
# field yang disimpan sebagai teks tapi harus dibaca sebagai bool. "false" itu truthy.
BOOL_FIELDS = ("enabled",)


def get_overrides(con):
    return {(k, n, f): v for k, n, f, v in
            con.execute("SELECT kind, name, field, value FROM overrides")}


def apply_overrides(cfg, con):
    """Terapkan pengaturan dashboard ke config. Tidak menyentuh config.toml."""
    over = get_overrides(con)
    if not over:
        return cfg
    for job in cfg.get("job", []):
        for field in EDITABLE_JOB_FIELDS:
            val = over.get(("job", job["name"], field))
            if val is None:
                continue
            if field == "retention_days":
                try:
                    job[field] = int(val)
                except ValueError:
                    continue
            elif field in BOOL_FIELDS:
                # WAJIB dikonversi: string "false" itu truthy, jadi tanpa ini
                # menonaktifkan job dari dashboard tidak akan berpengaruh
                job[field] = val.lower() in ("1", "true", "on", "yes")
            elif field == "databases":
                job[field] = [d.strip() for d in str(val).split(",") if d.strip()] or ["*"]
            else:
                job[field] = val
        if "enabled" not in job:
            job["enabled"] = True
    for target in cfg.get("targets", []):
        for field in EDITABLE_TARGET_FIELDS:
            val = over.get(("target", target.get("name", ""), field))
            if val is None:
                continue
            if field == "enabled":
                target["enabled"] = val == "true"
            else:
                target[field] = val
    return cfg


def save_override(con, kind, name, field, value):
    if kind == "job" and field not in EDITABLE_JOB_FIELDS:
        raise ValueError(f"field '{field}' tidak boleh diubah dari dashboard")
    if kind == "target" and field not in EDITABLE_TARGET_FIELDS:
        raise ValueError(f"field '{field}' tidak boleh diubah dari dashboard")
    if kind == "telegram" and field not in EDITABLE_TELEGRAM_FIELDS:
        raise ValueError(f"field '{field}' tidak boleh diubah dari dashboard")
    if kind == "general" and field not in EDITABLE_GENERAL_FIELDS:
        raise ValueError(f"field '{field}' tidak boleh diubah dari dashboard")
    if kind == "general" and field == "lang" and str(value) not in ("en", "id"):
        raise ValueError("bahasa harus 'en' atau 'id'")
    if kind == "job" and field == "schedule":
        # jadwal bisa diubah lewat /api/settings juga, bukan hanya form edit job
        value = str(value).strip().lower()
        if not SCHEDULE_RE.match(value):
            raise ValueError("jadwal tidak dikenal (contoh: daily 02:00, weekly 6 23:00)")
    if kind == "target" and field == "path":
        # jalur ini dipakai upload/retention: rclone butuh remote:path, lokal absolut
        value = str(value).strip()
        if not value or not re.match(r"^[A-Za-z0-9._~/:@+-]+$", value) \
                or ".." in value.split("/"):
            raise ValueError("jalur target tidak valid")
    if kind == "telegram" and field == "bot_token":
        # token masuk ke URL api.telegram.org — jangan biarkan karakter bebas
        value = str(value).strip()
        if value and not re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", value):
            raise ValueError("format bot token: <angka>:<huruf/angka> (dari @BotFather)")
    if kind == "telegram" and field == "chat_id":
        value = str(value).strip()
        if value and not re.match(r"^-?\d{1,20}$", value):
            raise ValueError("chat id harus angka (grup diawali tanda minus)")
    if kind == "telegram" and field == "title":
        # yang diedit pengguna hanya isi kurungnya; kurung siku tetap dipasang di
        # sini supaya format pesan konsisten walau API dipanggil langsung
        value = str(value).strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1].strip()
        value = value or "Arkivo"
        if len(value) > 58 or "\n" in value:
            raise ValueError("judul notifikasi maksimal 58 karakter, satu baris")
        value = f"[{value}]"
    if field in BOOL_FIELDS:
        value = "true" if str(value).lower() in ("1", "true", "on", "yes") else "false"
    con.execute("INSERT INTO overrides(kind,name,field,value) VALUES(?,?,?,?) "
                "ON CONFLICT(kind,name,field) DO UPDATE SET value=excluded.value",
                (kind, name, field, str(value)))
    con.commit()


# nama server: hanya untuk keterangan, tidak memengaruhi koneksi
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
HOST_RE = re.compile(r"^[a-zA-Z0-9._:-]+$")          # hostname / IP / alamat ssh
DB_ITEM_RE = re.compile(r"^[a-zA-Z0-9_$.*-]+$")
USER_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
# jam 0..23 dan menit 0..59: "daily 24:00" dulu lolos dan bikin /api/status error
# (ValueError dari datetime.replace), sehingga seluruh dashboard ikut mati.
SCHEDULE_RE = re.compile(
    r"^(?:manual|hourly|every[1-9]\d*[mh]|daily(?: (?:[01]?\d|2[0-3]):[0-5]\d)?"
    r"|weekly [0-6](?: (?:[01]?\d|2[0-3]):[0-5]\d)?)$")


def validate_job(form, name_override=None):
    """Periksa isi form server baru. Mengembalikan (job, error).

    Semua nilai yang masuk ke perintah shell diperiksa di sini — dashboard bisa
    diakses lewat jaringan, jadi jangan pernah percaya kiriman browser mentah.
    """
    tipe = (form.get("type") or "").strip().lower()
    if tipe not in ("postgres", "mysql", "mariadb", "path"):
        return None, "tipe harus salah satu: postgres, mysql, mariadb, path"

    nama = _slug(form.get("name") or name_override or "", "job")
    # nama dipakai sebagai nama berkas di semua target, jadi jangan sampai berisi
    # apa pun yang bermakna di shell atau filesystem. Diperiksa SETELAH _slug,
    # karena _slug mengubah huruf besar/tanda baca menjadi strip.
    if form.get("name") and not NAME_RE.match(nama):
        return None, "nama hanya boleh huruf kecil, angka, titik, garis bawah, dan strip"

    job = {"type": "mysql" if tipe == "mariadb" else tipe}

    # jadwal: pakai parser yang sudah ada, supaya format salah ketahuan sekarang
    sched = (form.get("schedule") or "manual").strip().lower()
    if not SCHEDULE_RE.match(sched):
        return None, ("jadwal tidak dikenal (contoh: manual, hourly, every30m, every6h, "
                      "daily 02:00, weekly 6 23:00)")
    job["schedule"] = sched

    try:
        job["retention_days"] = max(0, int(form.get("retention_days") or 14))
    except (TypeError, ValueError):
        return None, "retensi harus angka (hari)"

    # jalan lewat SSH: sumbernya di server lain
    if form.get("transport") == "ssh":
        host = (form.get("ssh_host") or "").strip()
        user = (form.get("ssh_user") or "").strip()
        if not host or not HOST_RE.match(host):
            return None, "host SSH tidak valid"
        if not user or not USER_RE.match(user):
            return None, "user SSH tidak valid"
        job["transport"] = "ssh"
        job["ssh_host"] = host
        if form.get("ssh_port"):
            try:
                job["ssh_port"] = int(form["ssh_port"])
            except (TypeError, ValueError):
                return None, "port SSH harus angka"
        job["ssh_user"] = user
        key = (form.get("ssh_key") or "").strip()
        if key:
            kp = Path(key).expanduser()
            if not key.startswith("/") or not kp.is_file():
                return None, f"file kunci SSH tidak ada atau bukan jalur absolut: {key}"
            job["ssh_key"] = str(kp)

    if tipe == "path":
        path = (form.get("path") or "").strip()
        if not path.startswith("/"):
            return None, "path harus absolut, diawali /"
        # jalur ini masuk ke perintah tar, termasuk lewat ssh. Tolak tegas apa pun
        # yang bisa menyisipkan perintah atau opsi.
        bad = [c for c in ";|&$`<>()\\'\"\n\r\x00" if c in path]
        if bad:
            return None, f"path tidak boleh memuat karakter ini: {' '.join(sorted(set(bad)))}"
        if not all(re.match(r"^[A-Za-z0-9._ -]+$", part) for part in path.split("/") if part):
            return None, "path hanya boleh huruf, angka, spasi, titik, garis bawah, dan strip"
        job["path"] = path
        if form.get("exclude"):
            pats = [p.strip() for p in str(form["exclude"]).split(",") if p.strip()]
            badpat = [p for p in pats if not re.match(r"^[A-Za-z0-9._/*?\[\]-]+$", p)]
            if badpat:
                return None, f"pola kecualikan tidak valid: {', '.join(badpat[:3])}"
            job["exclude"] = pats
    else:
        host = (form.get("host") or "127.0.0.1").strip()
        if not HOST_RE.match(host):
            return None, "host database tidak valid"
        job["host"] = host
        default_port = 5432 if tipe == "postgres" else 3306
        try:
            job["port"] = int(form.get("port") or default_port)
        except (TypeError, ValueError):
            return None, "port harus angka"
        user = (form.get("user") or ("postgres" if tipe == "postgres" else "root")).strip()
        if not user or not USER_RE.match(user):
            return None, "user database tidak valid"
        job["user"] = user
        raw_dbs = (form.get("databases") or "*").strip()
        if raw_dbs in ("", "*"):
            job["databases"] = ["*"]
        else:
            dbs = [d.strip() for d in raw_dbs.split(",") if d.strip()]
            # tidak boleh diawali '-' : bisa dibaca sebagai opsi oleh pg_dump/mysqldump
            bad = [d for d in dbs if not DB_ITEM_RE.match(d) or d.startswith("-")]
            if bad:
                return None, f"nama database tidak valid: {', '.join(bad[:3])}"
            job["databases"] = dbs
        # password tetap di config.toml: tidak pernah ditampung di sini
    return job, None


def edit_job(cfg, con, name, form):
    """Ubah definisi job. Mengembalikan (job, error).

    Job dashboard: ditimpa di tabel jobs.
    Job config.toml: config tidak pernah ditulis ulang, jadi dibuatkan job dashboard
    "shim" dengan nama sama. Kunci penyimpanannya `name__db` supaya nama job asli
    tidak berubah, dan `load_jobs` tidak pernah menghasilkan dua job bernama sama.
    """
    existing = None
    for job in load_jobs(cfg, con).get("job", []):
        if job["name"] == name:
            existing = job
            break
    if existing is None:
        return None, f"job '{name}' tidak ada"
    # kunci baris stabil: edit kedua harus menimpa baris yang sama, bukan membuat
    # baris baru (kalau tidak, job muncul dua kali setelah diedit).
    key = existing.get("_key") or (
        name if existing.get("_source") == "dashboard" else f"{name}__db")
    form = dict(form)
    form["name"] = form.get("name") or name
    job, err = validate_job(form, name_override=name)
    if err:
        return None, err
    # nama tampilan disimpan di data; upsert_db_job menaruhnya di display_name.
    # Job dari config.toml tidak boleh diganti nama: config tetap memakai nama lama,
    # sehingga hasilnya dua job berjalan (nama lama dari config + nama baru dari sini).
    if key.endswith("__db") and _slug(form["name"], name) != name:
        return None, "job dari config.toml tidak bisa diganti nama (ubah di config.toml)"
    job["name"] = _slug(form["name"], name)
    err = upsert_db_job(con, key, job, replace=True)
    return (job, None) if err is None else (None, err)


def remove_job(cfg, con, name):
    """Hapus job lewat nama tampilan. Job config.toml tidak bisa dihapus, hanya
    dinonaktifkan.

    Kunci baris di tabel jobs bisa beda dari nama tampilan: job config yang diedit
    memakai `<nama>__db`, dan job yang diganti nama tetap memakai kunci lamanya.
    Karena itu cari kuncinya dulu, jangan DELETE dengan nama tampilan.
    """
    job = next((j for j in load_jobs(cfg, con).get("job", []) if j.get("name") == name), None)
    if job is None:
        return False
    key = job.get("_key") or name
    if job.get("_source") == "dashboard":
        remove_db_job(con, key)
        if not key.endswith("__db"):
            return True
        # shim job config: barisnya dibuang, lalu config-nya ditandai terhapus
    con.execute("INSERT INTO overrides(kind,name,field,value) VALUES('job',?,?,?) "
                "ON CONFLICT(kind,name,field) DO UPDATE SET value=excluded.value",
                (name, "_deleted", "true"))
    con.commit()
    return True


# ---------------------------------------------------------------- tes koneksi

def test_connection(cfg, job):
    """Coba benar-benar tersambung ke sumber job, tanpa membuat backup.

    Tujuannya menangkap kesalahan yang mahal ditemukan belakangan: host salah,
    kredensial ditolak, database tidak ada, ssh tidak bisa masuk, path hilang.
    """
    tipe = job.get("type")
    try:
        if job.get("transport") == "ssh":
            ssh_check(job)
    except Exception as exc:
        return False, f"gagal ssh: {exc}"

    if tipe == "path":
        if job.get("transport") == "ssh":
            cmd = [*ssh_base(job), f"test -d {shlex.quote(job['path'])} && echo ADA || echo TIDAK"]
            res = subprocess.run(cmd, capture_output=True, timeout=60)
            out = res.stdout.decode(errors="replace").strip()
            if "ADA" in out:
                return True, f"direktori ada di {job.get('ssh_host')}: {job['path']}"
            return False, f"direktori tidak ada di server ssh: {job['path']}"
        p = Path(job.get("path", "")).expanduser()
        if not p.exists():
            return False, f"path tidak ada: {p}"
        if not os.access(p, os.R_OK):
            return False, f"path tidak bisa dibaca: {p}"
        return True, f"path bisa dibaca: {p}"

    host = str(job.get("host", "127.0.0.1"))
    port = int(job.get("port", 5432 if tipe == "postgres" else 3306))
    user = str(job.get("user", ""))
    pw = job.get("password", "")

    if tipe == "postgres":
        env = {**os.environ}
        if pw:
            env["PGPASSWORD"] = pw
        # lewat terowongan bila sumbernya di server ssh
        with tunnel_context(job) as j:
            major, why = server_major(j["host"], str(j["port"]), user, env)
            if major is None:
                return False, f"tidak bisa terhubung: {why or 'server tidak menjawab'}"
            psql = pg_bin("psql", major)
            base = [psql, "-h", j["host"], "-p", str(j["port"]), "-U", user,
                    "-d", "postgres", "-Atc"]
            ver = subprocess.run(base + ["SHOW server_version"], capture_output=True,
                                 env=env, timeout=30).stdout.decode().strip()
            dbases = subprocess.run(
                base + ["SELECT datname FROM pg_database WHERE datistemplate=false "
                        "AND datallowconn ORDER BY 1"], capture_output=True, env=env,
                timeout=30).stdout.decode().split()
            wanted = job.get("databases") or ["*"]
            if wanted != ["*"]:
                missing = [d for d in wanted if d not in dbases]
                if missing:
                    return False, (f"terhubung (server {ver}), tapi database tidak ada: "
                                   + ", ".join(missing))
            return True, (f"terhubung. PostgreSQL {ver}, {len(dbases)} database. "
                          f"Akan dibackup: {', '.join(wanted) if wanted != ['*'] else 'semua'}")

    if tipe in ("mysql", "mariadb"):
        with tunnel_context(job) as j:
            env = {**os.environ}
            if pw:
                env["MYSQL_PWD"] = pw
            base = [mysql_dump_bin(), "--no-defaults", "-h", j["host"], "-P", str(j["port"]),
                    "-u", user]
            if Path(mysql_dump_bin()).name.startswith("mysql"):
                base.append("--column-statistics=0")
            res = subprocess.run(base + ["--version"], capture_output=True, env=env, timeout=30)
            # --all-databases ke /dev/null: menyambung penuh dengan hak yang sama
            chance = subprocess.run(base + ["--all-databases", "--skip-lock-tables",
                                            "--skip-add-locks", "--no-data"],
                                    capture_output=True, env=env, timeout=120)
            err = chance.stderr.decode(errors="replace").strip()
            if chance.returncode != 0:
                return False, err[:400] or "tidak bisa terhubung"
            wanted = job.get("databases") or ["*"]
            dbs = re.findall(r"^CREATE DATABASE .*?`([^`]+)`", chance.stdout.decode(),
                             re.M) or re.findall(r"^-- Current Database: `([^`]+)`",
                                                 chance.stdout.decode(), re.M)
            if wanted != ["*"]:
                missing = [d for d in wanted if dbs and d not in dbs]
                if missing:
                    return False, "terhubung, tapi database tidak ada: " + ", ".join(missing)
            return True, (f"terhubung. {len(dbs)} database terlihat. "
                          f"Akan dibackup: {', '.join(wanted) if wanted != ['*'] else 'semua'}")

    return False, f"tipe job tidak dikenal: {tipe}"


def upsert_db_job(con, name, job, replace=False):
    """Simpan job dari dashboard. Menolak menimpa job yang sudah ada kecuali diminta."""
    row = con.execute("SELECT name FROM jobs WHERE name=?", (name,)).fetchone()
    if row and not replace:
        return f"job '{name}' sudah ada"
    fields = ("name", "transport", "ssh_host", "ssh_port", "ssh_user", "ssh_key", "path",
              "exclude", "host", "port", "user", "databases", "type", "schedule",
              "retention_days")
    clean = {k: v for k, v in job.items() if k in fields}
    # nama tampilan disimpan terpisah dari kunci baris
    if clean.get("name") and clean["name"] != name:
        clean["display_name"] = clean.pop("name")
    con.execute("INSERT INTO jobs(name, data, added, deleted) VALUES(?,?,?,0) "
                "ON CONFLICT(name) DO UPDATE SET data=excluded.data, deleted=0",
                (name, json.dumps(clean), datetime.now().isoformat()))
    con.commit()
    return None


def remove_db_job(con, name):
    """Hapus job dashboard. Job dari config.toml tidak bisa dihapus dari sini."""
    cur = con.execute("DELETE FROM jobs WHERE name=?", (name,))
    con.commit()
    return cur.rowcount > 0


def remove_runs(con, rid=None, status=None):
    """Hapus baris riwayat. Run yang masih berjalan tidak pernah dihapus."""
    if rid is not None:
        cur = con.execute("DELETE FROM runs WHERE id=? AND status!='running'", (int(rid),))
    elif status == "failed":
        cur = con.execute("DELETE FROM runs WHERE status='failed'")
    else:
        raise ValueError("pilih id atau status='failed'")
    con.commit()
    return cur.rowcount


# ---------------------------------------------------------------- notifikasi

def cfg_lang(con):
    """Bahasa yang dipakai log & notifikasi: sama dengan pengaturan dashboard."""
    try:
        row = con.execute("SELECT value FROM overrides "
                          "WHERE kind='general' AND name='' AND field='lang'").fetchone()
    except sqlite3.Error:
        return "en"
    return row[0] if row and row[0] in ("en", "id") else "en"


def tg_title(cfg):
    """Awalan pesan Telegram. Kurung siku dipasang otomatis kalau belum ada."""
    t = str(cfg.get("telegram", {}).get("title") or "").strip()
    if t.startswith("[") and t.endswith("]"):
        return t
    return f"[{t or 'Arkivo'}]"


def notify(cfg, text):
    """Kirim ke Telegram. Mengembalikan None kalau berhasil, pesan error kalau gagal."""
    tg = cfg.get("telegram", {})
    if str(tg.get("enabled", True)).lower() in ("false", "0", "off", "no"):
        return None
    token, chat = tg.get("bot_token", ""), tg.get("chat_id", "")
    if not token or not chat:
        return "bot_token/chat_id belum diisi"
    payload = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=20).read()
        return None
    except Exception as exc:  # notifikasi gagal tidak boleh membatalkan backup
        msg = f"telegram gagal: {exc}"
        print(f"[warn] {msg}", file=sys.stderr)
        return msg


# ---------------------------------------------------------------- utilitas

_log_ctx = threading.local()
CANCEL = {}  # nama job -> {"event": Event, "procs": [Popen]}

# Progres realtime per job yang sedang berjalan. Hanya untuk ditampilkan; tidak
# pernah dibaca oleh logika backup. `buf` menunjuk ke buffer log milik thread run
# supaya dashboard bisa menampilkan log yang tumbuh tanpa menunggu run selesai.
PROGRESS = {}
_progress_lock = threading.Lock()


def progress_start(job_name, est=None):
    with _progress_lock:
        PROGRESS[job_name] = {"phase": "menyiapkan", "bytes": 0, "est": est,
                              "rate": 0.0, "started": datetime.now().timestamp(),
                              "buf": []}


def progress_update(job_name, **fields):
    with _progress_lock:
        p = PROGRESS.get(job_name)
        if p is not None:
            p.update(fields)


def progress_done(job_name):
    with _progress_lock:
        PROGRESS.pop(job_name, None)


def progress_snapshot():
    """Salinan progres semua job (aman dibaca dari thread HTTP)."""
    now = datetime.now().timestamp()
    with _progress_lock:
        out = {}
        for name, p in PROGRESS.items():
            out[name] = {"phase": p["phase"], "bytes": p["bytes"], "est": p["est"],
                         "rate": round(p["rate"], 1),
                         "elapsed": round(now - p["started"], 1),
                         "log": "\n".join(list(p["buf"])[-200:])}
        return out


def _sample_growth(job_name, path, stop, interval=0.5):
    """Catat ukuran berkas tampungan tiap `interval` detik selama dump berjalan."""
    last, last_t = 0, time.time()
    while True:
        try:
            n = path.stat().st_size
        except OSError:
            n = last
        now = time.time()
        dt = max(now - last_t, 0.001)
        progress_update(job_name, bytes=n, rate=(n - last) / dt)
        last, last_t = n, now
        if stop.wait(interval):
            return


# Baris log dibuat di server, jadi terjemahannya juga di sini (bukan di browser).
# Hanya pola pesan milik arkivo; keluaran tool (pg_dump, tar, rclone) dibiarkan apa
# adanya. Urutan penting: "GAGAL target" sebelum "GAGAL:".
LOG_EN = [
    (re.compile(r"^mulai (.+) \((.+)\) tipe=(.+)$"), r"start \1 (\2) type=\3"),
    (re.compile(r"^dump selesai: (.+)$"), r"dump finished: \1"),
    (re.compile(r"^target (.+) dimatikan, dilewati$"), r"target \1 disabled, skipped"),
    (re.compile(r"^terkirim ke (.+): (.+)$"), r"uploaded to \1: \2"),
    (re.compile(r"^storage (.+): sisa (.+)%$"), r"storage \1: \2% free"),
    (re.compile(r"^GAGAL target (.+): (.+)$"), r"FAILED target \1: \2"),
    (re.compile(r"^GAGAL: (.+)$"), r"FAILED: \1"),
    (re.compile(r"^selesai: (.+) \((.+)s\)$"), r"finished: \1 (\2s)"),
    (re.compile(r"^reconcile: (\d+) run yatim ditandai gagal$"),
     r"reconcile: \1 orphan runs marked failed"),
    (re.compile(r"^password job (.+): diisi$"), r"password job \1: set"),
    (re.compile(r"^password job (.+): dikosongkan$"), r"password job \1: cleared"),
    (re.compile(r"^terowongan ssh: (.+)$"), r"ssh tunnel: \1"),
]


def translate_log(line):
    # Galat internal bisa terselip di dalam baris "GAGAL target ...: ...".
    # Terjemahkan potongannya lebih dulu, baru awalan barisnya.
    line = re.sub(
        r"rclone melebihi batas waktu (\d+)s(?: \(([^)]*)\))?",
        lambda m: (f"rclone exceeded timeout {m.group(1)}s"
                   + (f" ({m.group(2)})" if m.group(2) else "")),
        line)
    for rx, rep in LOG_EN:
        if rx.match(line):
            return rx.sub(rep, line)
    return line


def _msg(id_text, en_text):
    """Pilih pesan internal sesuai bahasa run yang sedang berjalan."""
    return en_text if getattr(_log_ctx, "lang", "id") == "en" else id_text


def log_line(text):
    """Catat satu baris log untuk run yang sedang berjalan (kalau ada).

    Setiap baris diberi cap waktu lengkap supaya durasi tiap fase bisa dibaca
    langsung dari log, bukan hanya total dari kolom riwayat. Bahasanya mengikuti
    pengaturan dashboard (`_log_ctx.lang`).
    """
    buf = getattr(_log_ctx, "buf", None)
    if buf is not None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lang = getattr(_log_ctx, "lang", "id")
        for line in str(text).splitlines() or [""]:
            if lang == "en":
                line = translate_log(line)
            buf.append(f"[{stamp}] {line}")


def request_cancel(job_name):
    """Minta job berhenti. Mengembalikan True kalau job itu sedang berjalan."""
    entry = CANCEL.get(job_name)
    if not entry:
        return False
    entry["event"].set()
    for proc in list(entry["procs"]):
        try:
            proc.terminate()
        except Exception:
            pass
    return True


def check_cancelled(job_name):
    entry = CANCEL.get(job_name)
    if entry and entry["event"].is_set():
        raise RuntimeError("dibatalkan dari dashboard")


def check_cancelled_quietly(job_name):
    entry = CANCEL.get(job_name)
    return bool(entry and entry["event"].is_set())


def run_pipe(job_name, producer, consumer, out_path, env, partial_ok=False):
    """Sambung stdout producer -> stdin consumer -> file. Satu file, satu stream.

    `partial_ok` dipakai job path: tar keluar dengan status 2 saat sebagian berkas
    tidak terbaca (mis. 'Cannot open: Permission denied' pada folder milik user
    lain), padahal arsip sisanya sah dan bisa dipulihkan. Tanpa ini seluruh
    backup dibuang hanya karena beberapa file sementara. Galat fatal (disk penuh,
    I/O, path hilang) tetap menggagalkan job.
    """
    log_line("$ " + " ".join(producer))
    log_line("| " + " ".join(consumer))
    with open(out_path, "wb") as fh:
        p1 = subprocess.Popen(producer, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        entry = CANCEL.get(job_name)
        if entry:
            entry["procs"].append(p1)
        p2 = subprocess.Popen(consumer, stdin=p1.stdout, stdout=fh, stderr=subprocess.PIPE, env=env)
        if entry:
            entry["procs"].append(p2)
        p1.stdout.close()
        _, err2 = p2.communicate()
        _, err1 = p1.communicate()
        if entry:
            for proc in (p1, p2):
                if proc in entry["procs"]:
                    entry["procs"].remove(proc)
    for chunk in (err1, err2):
        text = chunk.decode(errors="replace").strip()
        if text:
            log_line(text)
    cancelled = check_cancelled_quietly(job_name)
    if cancelled:
        # job dihentikan: jangan laporkan galat mentah dari proses yang di-terminate
        raise RuntimeError("dibatalkan dari dashboard")
    producer_failed = p1.returncode != 0
    if producer_failed and partial_ok and out_path.stat().st_size > 0:
        fatal, warn = _classify_partial(err1)
        if not fatal:
            warnings = getattr(_log_ctx, "warnings", None)
            if warnings is not None:
                warnings.append(warn)
            else:
                log_line(f"[warn] {warn}")
            producer_failed = False
    if producer_failed:
        raise RuntimeError(f"{producer[0]} gagal: {err1.decode(errors='replace')[:1500]}")
    if p2.returncode != 0:
        raise RuntimeError(f"{consumer[0]} gagal: {err2.decode(errors='replace')[:1500]}")


# galat tar yang membatalkan seluruh arsip; sisanya dianggap berkas terlewat
_FATAL_TAR = ("No space left", "Cannot write", "Cannot chdir", "Cannot connect",
              "Unexpected EOF", "write error", "Read error", "Error is not recoverable",
              "You must specify", "Try 'tar --help'")


def _classify_partial(stderr):
    """Pisahkan 'berkas tidak terbaca' dari kegagalan nyata. (fatal, pesan)."""
    lines = [l.strip() for l in stderr.decode(errors="replace").splitlines() if l.strip()]
    if any(mark in l for l in lines for mark in _FATAL_TAR):
        return True, ""
    hits = [l.replace("tar: ", "") for l in lines
            if "Cannot open" in l or "Cannot stat" in l or "Permission denied" in l]
    if not hits:
        return True, ""   # exit bukan nol tanpa alasan yang dikenal: jangan tebak
    shown = "; ".join(hits[:5])
    more = f" (+{len(hits) - 5} lagi)" if len(hits) > 5 else ""
    return False, (f"{len(hits)} berkas tidak bisa dibaca dan dilewati: {shown}{more}. "
                   f"Arsip tetap dibuat; kecualikan folder itu bila tidak diperlukan.")


def mysql_dump_bin():
    for name in ("mariadb-dump", "mysqldump"):
        if shutil.which(name):
            return name
    raise RuntimeError("mariadb-dump/mysqldump tidak ada di PATH")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def human_delta(seconds):
    """Durasi singkat untuk 'berapa lama lagi' (jam/menit/detik)."""
    sec = max(0, int(seconds))
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}j {m}m" if m else f"{h}j"


def cleanup(path):
    """Hapus artefak parsial kalau job gagal di tengah jalan."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- dump

def pg_bin(name, prefer_major=None):
    """Pilih biner PostgreSQL, utamakan versi yang cocok dengan server.

    Di Ubuntu/Debian, /usr/bin/pg_dump dan /usr/bin/pg_restore adalah pg_wrapper,
    yang boleh memilih versi berbeda untuk tiap tool. Akibatnya pg_dump bisa
    menulis arsip format 1.16 (dari PG18) sementara pg_restore hanya mengerti
    1.15 (dari PG16) — arsipnya tidak bisa dipulihkan sama sekali.
    Selalu ambil dump dan restore dari direktori versi yang sama, dan
    utamakan versi yang sama dengan server supaya tidak ada peringatan
    'unrecognized configuration parameter' saat restore.
    """
    forced = os.environ.get("PGVERSION")
    if forced:
        cand = Path(f"/usr/lib/postgresql/{forced}/bin/{name}")
        if cand.exists():
            return str(cand)
    bin_dirs = [p for p in Path("/usr/lib/postgresql").glob("*/bin") if p.is_dir()]

    def major(d):
        return int(d.parent.name) if d.parent.name.isdigit() else 0

    if prefer_major:
        for d in bin_dirs:
            if major(d) == int(prefer_major) and (d / name).exists():
                return str(d / name)
    for d in sorted(bin_dirs, key=major, reverse=True):
        if (d / name).exists():
            return str(d / name)
    return name  # tidak ada direktorat versi: serahkan ke PATH


def server_major(host, port, user, env):
    """Versi mayor server; (None, pesan_error) kalau tidak bisa dipastikan.

    Selalu sambung ke database pemeliharaan (`postgres`, lalu `template1`).
    Tanpa `-d`, psql memakai nama user sebagai nama database; untuk user backup
    yang tidak punya database senama, server menolak dengan 'database "app"
    does not exist' — dan itu dulu salah dilaporkan sebagai kredensial ditolak.
    """
    err = ""
    for db in ("postgres", "template1"):
        try:
            res = subprocess.run(
                [pg_bin("psql"), "-h", host, "-p", port, "-U", user, "-d", db,
                 "-Atc", "SHOW server_version_num"],
                capture_output=True, env=env, timeout=30)
        except Exception as exc:
            return None, str(exc)[:300]
        if res.returncode == 0:
            raw = res.stdout.decode().strip()
            if raw.isdigit():
                # server_version_num: 160013 -> 16, 100023 -> 10, 90624 -> 9
                return int(raw) // 10000, ""
        err = (res.stderr.decode(errors="replace").strip() or err).replace("\n", " ")
    return None, err[:400]


def dump_postgres(job, out, env):
    host = job.get("host", "127.0.0.1")
    port = str(job.get("port", 5432))
    user = job.get("user", "postgres")
    dbs = job.get("databases", ["*"])
    pw = job.get("password", "")
    if pw:
        env = {**env, "PGPASSWORD": pw}
    major, why = server_major(host, port, user, env)
    if major is None:
        raise RuntimeError(f"tidak bisa terhubung ke postgres: {why or 'server tidak menjawab'}")
    psql, pgdump = pg_bin("psql", major), pg_bin("pg_dump", major)

    if dbs == ["*"]:
        cmd = [psql, "-h", host, "-p", port, "-U", user, "-d", "postgres", "-Atc",
               "SELECT datname FROM pg_database WHERE datistemplate=false AND datallowconn"]
        listing = subprocess.run(cmd, capture_output=True, env=env, timeout=60)
        if listing.returncode != 0:
            raise RuntimeError(f"psql gagal: {listing.stderr.decode(errors='replace')[:1000]}")
        dbs = [d for d in listing.stdout.decode().split() if d and d != "postgres"] or ["postgres"]

    produced = []
    for db in dbs:
        if len(dbs) == 1:
            target = out
        else:
            target = out.with_name(f"{out.stem}__{db}{out.suffix}")
        cmd = [pgdump, "-h", host, "-p", port, "-U", user, "-Fc", "-f", str(target), db]
        res = subprocess.run(cmd, capture_output=True, env=env, timeout=job.get("timeout", 3600))
        if res.returncode != 0:
            cleanup(target)
            raise RuntimeError(f"pg_dump {db} gagal: {res.stderr.decode(errors='replace')[:1200]}")
        produced.append(target)
    return produced


def dump_mysql(job, out, env):
    host = job.get("host", "127.0.0.1")
    port = str(job.get("port", 3306))
    user = job.get("user", "root")
    pw = job.get("password", "")
    binary = mysql_dump_bin()
    # --no-defaults: ~/.my.cnf dan /etc/mysql/my.cnf operator bisa menimpa -u/-h,
    # bikin backup menembak DB yang salah. Semua parameter dikirim eksplisit.
    # --skip-lock-tables: wajib untuk --all-databases (tabel log tidak bisa di-LOCK).
    producer = [binary, "--no-defaults", "-h", host, "-P", port, "-u", user,
                "--single-transaction", "--quick", "--skip-lock-tables",
                "--skip-add-locks", "--routines", "--triggers", "--events"]
    if Path(binary).name.startswith("mysql"):
        # klien MySQL 8 menanyakan COLUMN_STATISTICS yang tidak ada di server MariaDB.
        # mariadb-dump tidak mengenal flag ini, jadi hanya dikirim ke mysqldump.
        producer.append("--column-statistics=0")
    if job.get("databases") and job["databases"] != ["*"]:
        producer += ["--databases", *job["databases"]]
    else:
        producer += ["--all-databases"]
    # sandi lewat env, bukan argumen: argumen bocor lewat ps ke semua user
    if pw:
        env = {**env, "MYSQL_PWD": pw}
    run_pipe(job["name"], producer, ["zstd", "-q", "-3", "-T0"], out, env)
    return [out]


def ssh_base(job):
    """Argumen dasar ssh untuk job yang sumbernya di server lain."""
    host = job.get("ssh_host")
    user = job.get("ssh_user") or "root"
    if not host:
        raise RuntimeError("job bertransport ssh tapi ssh_host kosong")
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=accept-new"]
    if job.get("ssh_port"):
        args += ["-p", str(job["ssh_port"])]
    if job.get("ssh_key"):
        args += ["-i", str(Path(job["ssh_key"]).expanduser())]
    args.append(f"{user}@{host}")
    return args


def ssh_check(job):
    """Pastikan bisa masuk ke server lain. Pesannya dibuat jelas kalau gagal."""
    cmd = [*ssh_base(job)[:-1], ssh_base(job)[-1], "true"]
    res = subprocess.run(cmd, capture_output=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError("ssh gagal: " + res.stderr.decode(errors="replace").strip()[:400])


def panduan_ekstrak(nama_arsip):
    """Teks panduan yang ditaruh DI DALAM arsip project.

    Arsip .tar.zst tidak bisa dibuka dengan klik dua kali di Windows/macOS. Tanpa
    panduan, orang yang memulihkan di mesin non-Linux akan mengira berkasnya rusak.
    """
    return f"""CARA MEMBUKA ARSIP INI
======================

Berkas: {nama_arsip}
Format: tar yang dikompresi zstd (.tar.zst)

Linux (biasanya sudah tersedia)
-------------------------------
  tar --zstd -xf {nama_arsip}
  # kalau tar lama:  zstd -dc {nama_arsip} | tar -xf -

macOS
-----
  brew install zstd
  zstd -dc {nama_arsip} | tar -xf -

Windows
-------
  Cara termudah: pasang 7-Zip versi 21.07 atau lebih baru, klik dua kali
  berkas ini, lalu ekstrak (7-Zip membuka .zst menjadi .tar, ekstrak sekali lagi).

  Lewat PowerShell + zstd:
    winget install zstd
    zstd -d {nama_arsip}          # menghasilkan .tar
    tar -xf {nama_arsip.replace(".zst", "")}

  Atau pakai WSL: tar --zstd -xf {nama_arsip}

Memeriksa arsip rusak atau tidak
--------------------------------
  Linux/macOS: zstd -t {nama_arsip}
  Windows   : 7-Zip akan melaporkan "Data Error" bila rusak.

Catatan
-------
- Arsip ini berisi isi folder project apa adanya, termasuk path relatif (./...).
- File rahasia (config.toml, data/secrets.json, data/state.db) sengaja TIDAK
  disertakan.
- Untuk database, gunakan pg_restore (Postgres) atau mysql (MySQL/MariaDB),
  bukan tar; lihat ./restore.sh di mesin server.
"""


def dump_path(job, out, env):
    """Repo/project: tar satu stream, jangan pernah pohon file."""
    guard = _secrets_guard()
    panduan = out.with_name("CARA-BUKA.txt")
    panduan.write_text(panduan_ekstrak(out.name))
    try:
        if job.get("transport") == "ssh":
            ssh_check(job)
            # panduan ikut dikirim ke server yang membuat arsip, lalu dibuang dari
            # sana. Satu tar tetap: panduan lebih dulu, lalu isi project.
            tmp_remote = f"/tmp/arkivo-cara-buka-{os.getpid()}.txt"
            subprocess.run([*ssh_base(job), f"cat > {shlex.quote(tmp_remote)}"],
                           input=panduan.read_bytes(), check=True, timeout=60)
            remote = (f"tar -cf - -C /tmp {shlex.quote(Path(tmp_remote).name)} "
                      f"{' '.join(shlex.quote(e) for e in guard + _excludes(job))} "
                      f"-C {shlex.quote(job['path'])} . ; "
                      f"rm -f {shlex.quote(tmp_remote)}")
            log_line(f"ssh {job.get('ssh_user')}@{job.get('ssh_host')} {remote}")
            run_pipe(job["name"], [*ssh_base(job), remote],
                     ["zstd", "-q", "-3", "-T0"], out, env, partial_ok=True)
            panduan.unlink(missing_ok=True)
            return [out]

        src = Path(job["path"]).expanduser()
        if not src.exists():
            raise RuntimeError(f"path tidak ada: {src}")
        producer = ["tar", "-cf", "-", "-C", str(panduan.parent),
                    Path(panduan.name).name, *guard, *_excludes(job),
                    "-C", str(src), "."]
        run_pipe(job["name"], producer, ["zstd", "-q", "-3", "-T0"], out, env, partial_ok=True)
        panduan.unlink(missing_ok=True)
        return [out]
    finally:
        panduan.unlink(missing_ok=True)


def _secrets_guard():
    """File rahasia arkivo yang tidak boleh ikut ter-backup lalu terunggah.

    config.toml memuat password DB, data/secrets.json password yang diisi dari
    dashboard, dan data/state.db memuat log run (pesan error bisa memuat
    kredensial). Pola diawali ./ supaya hanya file di ROOT project yang kena:
    tar menganggap pola tanpa ./ sebagai nama dasar, sehingga "config.toml" akan
    ikut membuang config.toml milik aplikasi lain di kedalaman mana pun.
    """
    return ["--exclude", "./config.toml",
            "--exclude", "./data/secrets.json",
            "--exclude", "./data/state.db"]


def _excludes(job):
    if job.get("exclude"):
        out = []
        for pat in job["exclude"]:
            out += ["--exclude", pat]
        return out
    return ["--exclude", ".git/objects", "--exclude", "node_modules", "--exclude", "vendor"]


@contextlib.contextmanager
def tunnel_context(job):
    """Buka terowongan ssh ke port database server lain, kalau job minta.

    Dengan ini, dump_postgres/dump_mysql bekerja tanpa perubahan: keduanya
    menyambung ke 127.0.0.1:<port lokal> milik terowongan, bukan ke kode baru.
    """
    if job.get("transport") != "ssh":
        # fungsi ini sebuah @contextmanager, jadi harus YIELD. Mengembalikan
        # nullcontext dari sini membuat generator tidak menghasilkan apa pun
        # ("generator didn't yield") dan merusak semua backup non-SSH.
        yield job
        return
    remote_host = job.get("host", "127.0.0.1")
    remote_port = int(job.get("port", 5432 if job["type"] == "postgres" else 3306))
    local_port = free_port()
    args = [*ssh_base(job)[:-1], "-N", "-L",
            f"127.0.0.1:{local_port}:{remote_host}:{remote_port}", ssh_base(job)[-1]]
    log_line(f"terowongan ssh: 127.0.0.1:{local_port} -> {remote_host}:{remote_port}")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    entry = CANCEL.get(job["name"])
    if entry:
        entry["procs"].append(proc)
    try:
        wait_port(local_port, proc)
        yield {**job, "host": "127.0.0.1", "port": local_port}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, proc, timeout=20):
    """Tunggu terowongan siap; laporkan kalau ssh mati duluan."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode(errors="replace").strip()
            raise RuntimeError(f"ssh terowongan mati: {err[:400] or 'keluar tanpa pesan'}")
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"terowongan ssh tidak siap dalam {timeout}s")


_orig_dump_postgres = dump_postgres
_orig_dump_mysql = dump_mysql


def dump_postgres(job, out, env):
    with tunnel_context(job) as j:
        return _orig_dump_postgres(j, out, env)


def dump_mysql(job, out, env):
    with tunnel_context(job) as j:
        return _orig_dump_mysql(j, out, env)


DUMPERS = {"postgres": dump_postgres, "mysql": dump_mysql, "mariadb": dump_mysql, "path": dump_path}



def artifact_name(job):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = {"postgres": ".dump", "path": ".tar.zst"}.get(job["type"], ".sql.zst")
    return f"{job['name']}_{stamp}{ext}"


# ---------------------------------------------------------------- target

def target_freespace(cfg, target):
    kind, loc = target.get("type"), target.get("path", "")
    if kind == "local":
        try:
            u = shutil.disk_usage(Path(loc).expanduser())
            return (u.free / u.total) * 100
        except OSError:
            return None
    if kind == "rclone":
        remote = loc.split(":", 1)[0]
        try:
            res = subprocess.run(["rclone", "about", f"{remote}:", "--json"],
                                 capture_output=True, timeout=60)
            if res.returncode == 0:
                import json
                info = json.loads(res.stdout or b"{}")
                if info.get("total"):
                    return (info["free"] / info["total"]) * 100
        except Exception:
            return None
    return None


# rclone about bisa lambat / menggantung; dashboard memanggil status tiap 30 detik,
# jadi hasilnya disimpan sebentar dan probe remote dijalankan di latar belakang.
# Percobaan yang sedang jalan (atau baru gagal) tidak diulang sampai cache kedaluwarsa.
_disk_cache = {}
_disk_pending = set()


def _probe_disk(key, target):
    try:
        remote = str(target.get("path", "")).split(":", 1)[0]
        res = subprocess.run(["rclone", "about", f"{remote}:", "--json"],
                             capture_output=True, timeout=20)
        u = json.loads(res.stdout or b"{}") if res.returncode == 0 else {}
        info = ({"total": u["total"], "free": u.get("free", 0),
                 "used": u["total"] - u.get("free", 0)} if u.get("total") else None)
    except Exception:
        info = None
    _disk_cache[key] = (datetime.now().timestamp(), info)
    _disk_pending.discard(key)


def target_disk(target):
    """Total/sisa ruang target. None kalau tidak terbaca (belum / sedang diperiksa)."""
    kind, loc = target.get("type"), str(target.get("path", ""))
    key = (kind, loc.split(":", 1)[0] if kind == "rclone" else loc)
    hit = _disk_cache.get(key)
    if hit:
        if datetime.now().timestamp() - hit[0] < 300:
            return hit[1]
    if kind == "local":
        try:
            p = Path(loc).expanduser()
            while not p.exists() and p != p.parent:
                p = p.parent   # target belum dibuat: ruang induknya yang relevan
            u = shutil.disk_usage(p)
            info = {"total": u.total, "free": u.free, "used": u.used}
        except Exception:
            info = None
        _disk_cache[key] = (datetime.now().timestamp(), info)
        return info
    if key not in _disk_pending:
        _disk_pending.add(key)
        # rclone bisa butuh detik-detikan (SMB/SFTP): jangan tahan respons dashboard
        threading.Thread(target=_probe_disk, args=(key, target), daemon=True).start()
    return hit[1] if hit else None


DEFAULT_UPLOAD_TIMEOUT = 6 * 3600


def upload_timeout(cfg, target):
    """Batas total upload satu berkas ke satu target, dalam detik.

    Terpisah dari `timeout` job (batas dump): target cloud bisa jauh lebih lambat
    daripada dump database. Nilai per target menang atas nilai di [general].
    """
    for raw in (target.get("upload_timeout"),
                cfg.get("general", {}).get("upload_timeout")):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return DEFAULT_UPLOAD_TIMEOUT


def upload(cfg, target, local_file, job):
    kind = target.get("type")
    dest = target.get("path", "")
    if kind == "local":
        d = Path(dest).expanduser() / job["name"]
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_file, d / local_file.name)
        return f"{d / local_file.name}"
    if kind == "rclone":
        remote_path = f"{dest.rstrip('/')}/{job['name']}"
        cmd = ["rclone", "copyto", str(local_file), f"{remote_path}/{local_file.name}",
               "--retries", "5", "--low-level-retries", "10", "--contimeout", "60s",
               # statistik per detik: satu-satunya sumber progres upload yang jujur
               "--stats", "1s", "--stats-one-line", "--stats-log-level", "NOTICE"]
        if target.get("chunker"):
            cmd += ["--transfers", "1"]
        rc, err = _run_rclone(cmd, job["name"], upload_timeout(cfg, target))
        if rc != 0:
            raise RuntimeError(f"rclone gagal: {err[:1200]}")
        return f"{remote_path}/{local_file.name}"
    raise RuntimeError(f"target type tidak dikenal: {kind}")


_RCLONE_RATE = re.compile(r"([\d.]+)\s*([KMGTP]?)i?B\s*/\s*s")
_RCLONE_SIZE = re.compile(r"(?:Transferred|Copied):\s*([\d.]+)\s*([KMGTP]?)i?B")
_UNIT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
         "T": 1024 ** 4, "P": 1024 ** 5}


def _run_rclone(cmd, job_name, timeout):
    """Jalankan rclone sambil membaca statistik per detiknya untuk progres.

    stderr rclone dibaca per baris; baris statistik diterjemahkan jadi byte dan
    laju. Baris lain disimpan untuk pesan galat. Mengembalikan (returncode, stderr).
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    entry = CANCEL.get(job_name)
    if entry:
        entry["procs"].append(proc)
    deadline = time.time() + timeout
    lines = []
    sent, rate = 0, 0.0
    try:
        for line in proc.stderr:
            lines.append(line.rstrip())
            if len(lines) > 200:
                del lines[:100]
            if "/s" in line:
                m = _RCLONE_RATE.search(line)
                if m:
                    rate = float(m.group(1)) * _UNIT[m.group(2)]
                    sent = _bytes_of(line) or sent
                    progress_update(job_name, rate=rate, bytes=sent)
            if time.time() > deadline:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                detail = f" ({human(sent)}, {human(rate)}/s)"
                raise RuntimeError(_msg(
                    f"rclone melebihi batas waktu {timeout}s" + detail,
                    f"rclone exceeded timeout {timeout}s" + detail))
        proc.wait(timeout=30)
    finally:
        if entry and proc in entry["procs"]:
            entry["procs"].remove(proc)
    return proc.returncode, "\n".join(lines)


def _bytes_of(line):
    """Byte yang sudah terkirim menurut satu baris statistik rclone.

    Barisnya bisa `Transferred: 5.6 MiB / 10 MiB, 57%, …` atau bentuk satu baris
    `1.2 MiB / 10 MiB, 12%, …` — ambil angka sebelum tanda `/`.
    """
    if "/" not in line.split(",")[0]:   # baris per-berkas: hanya nama + laju
        return 0
    m = _RCLONE_SIZE.search(line) or re.search(r"([\d.]+)\s*([KMGTP]?)i?B\s*/", line)
    return int(float(m.group(1)) * _UNIT[m.group(2)]) if m else 0


def apply_retention(cfg, target, job):
    days = job.get("retention_days", cfg.get("general", {}).get("retention_days", 14))
    if not days:
        return 0
    kind, dest = target.get("type"), target.get("path", "")
    removed = 0
    if kind == "local":
        d = Path(dest).expanduser() / job["name"]
        if not d.is_dir():
            return 0
        cutoff = datetime.now().timestamp() - days * 86400
        for f in d.glob(f"{job['name']}_*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
    elif kind == "rclone":
        res = subprocess.run(
            ["rclone", "delete", f"{dest.rstrip('/')}/{job['name']}",
             "--min-age", f"{days}d", "--include", f"{job['name']}_*"],
            capture_output=True, timeout=1800)
        if res.returncode != 0:
            print(f"[warn] retention remote gagal: {res.stderr.decode(errors='replace')[:300]}",
                  file=sys.stderr)
    return removed


# ---------------------------------------------------------------- jadwal

def every_delta(sched):
    """Ubah 'every30m' / 'every6h' jadi timedelta. None kalau bukan pola itu."""
    m = re.match(r"^every(\d+)([mh])$", sched or "")
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    return timedelta(minutes=n) if m.group(2) == "m" else timedelta(hours=n)


def due(job, last_run, now):
    sched = (job.get("schedule") or "").strip().lower()
    if not sched or sched == "manual":
        return False
    if last_run is None:
        return True
    last = datetime.fromisoformat(last_run)
    if sched == "hourly":
        return now - last >= timedelta(hours=1)
    delta = every_delta(sched)
    if delta:
        return now - last >= delta
    if sched.startswith("daily"):
        try:
            hh, mm = (sched.split()[1] if len(sched.split()) > 1 else "02:00").split(":")
            slot = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ValueError, IndexError):
            return False        # jadwal rusak di config/database lama: jangan crash
        return now >= slot and last < slot
    if sched.startswith("weekly"):
        try:
            parts = sched.split()
            # gaya crontab: 0=Minggu .. 6=Sabtu. Python: 0=Senin .. 6=Minggu.
            dow = (int(parts[1]) - 1) % 7
            hh, mm = (parts[2] if len(parts) > 2 else "02:00").split(":")
            slot = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ValueError, IndexError):
            return False
        slot -= timedelta(days=(now.weekday() - dow) % 7)
        return now.weekday() == dow and now >= slot and last < slot
    return False


def next_run(job, last_run, now):
    sched = (job.get("schedule") or "manual").strip().lower()
    if sched == "hourly":
        base = datetime.fromisoformat(last_run) if last_run else now
        return base + timedelta(hours=1)
    delta = every_delta(sched)
    if delta:
        base = datetime.fromisoformat(last_run) if last_run else now
        return base + delta
    if sched.startswith("daily"):
        try:
            hh, mm = (sched.split()[1] if len(sched.split()) > 1 else "02:00").split(":")
            slot = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ValueError, IndexError):
            return None
        return slot if slot > now else slot + timedelta(days=1)
    return None


# ---------------------------------------------------------------- eksekusi

ORPHAN_AFTER = 600      # detik tanpa heartbeat sebelum run dianggap mati
HEARTBEAT_INTERVAL = 60


def _heartbeat(cfg, run_id, stop):
    """Beri tanda hidup berkala selama run berjalan.

    Dibuka koneksi sendiri: koneksi sqlite tidak boleh dipakai lintas thread.
    Update hanya menyentuh kolom heartbeat; kalau run sudah ditutup proses lain
    (`finished` terisi), UPDATE tidak melakukan apa pun.
    """
    con = None
    try:
        con = connect(cfg)
        while not stop.wait(HEARTBEAT_INTERVAL):
            try:
                con.execute("UPDATE runs SET heartbeat=? WHERE id=? AND finished IS NULL",
                            (datetime.now().isoformat(), run_id))
                con.commit()
            except sqlite3.Error:
                pass
    except Exception:
        # heartbeat hanya penjaga; kegagalannya tidak boleh menghentikan backup
        pass
    finally:
        if con is not None:
            con.close()


def claim_run(con, job, started):
    """Daftarkan run baru sebagai 'running', atau tolak kalau job masih berjalan.

    Antip dobel lintas PROSES: timer (arkivo.py tick) dan dashboard adalah proses
    berbeda, jadi penjaga di memori (CANCEL) tidak saling melihat. BEGIN IMMEDIATE
    membuat cek-dan-insert atomik antar proses.

    Baris yatim (proses mati tanpa menutup run) dikenali dari heartbeat yang basi,
    BUKAN dari umur run. Upload ke cloud bisa lebih lama dari timeout dump; dulu
    run panjang dianggap mati sehingga job kedua bisa berjalan bersamaan.

    Mengembalikan run_id, atau None kalau job masih berjalan.
    """
    name = job["name"]
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return None     # database sedang dikunci proses lain yang memulai run
    row = con.execute("SELECT id, COALESCE(heartbeat, started) FROM runs "
                      "WHERE job=? AND finished IS NULL ORDER BY id DESC LIMIT 1",
                      (name,)).fetchone()
    if row:
        try:
            age = (datetime.now() - datetime.fromisoformat(row[1])).total_seconds()
        except (TypeError, ValueError):
            age = ORPHAN_AFTER
        if age < ORPHAN_AFTER:
            con.rollback()
            return None
        con.execute("UPDATE runs SET finished=?, status='failed', error=?, "
                    "log=COALESCE(log,'') || ? WHERE id=?",
                    (started.isoformat(), "terputus: proses sebelumnya tidak menutup run",
                     f"\n[{started:%Y-%m-%d %H:%M:%S}] GAGAL: run sebelumnya putus, "
                     f"ditandai gagal sebelum run baru dimulai", row[0]))
    run_id = con.execute("INSERT INTO runs(job, started, heartbeat, status) "
                         "VALUES(?,?,?,?)",
                         (name, started.isoformat(), started.isoformat(),
                          "running")).lastrowid
    con.commit()
    return run_id


def run_job(cfg, job, con, trigger="schedule"):
    name = job["name"]
    started = datetime.now()
    run_id = claim_run(con, job, started)
    if run_id is None:
        # klaim gagal: entri CANCEL didaftarkan pemanggil sebelum thread mulai,
        # jadi harus dilepas di sini. Kalau tidak, job ini dianggap berjalan
        # selamanya oleh proses ini dan tombol Backup terus ditolak.
        CANCEL.pop(name, None)
        return None     # job ini masih berjalan (proses lain atau thread lain)

    tmp_dir = data_dir(cfg) / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    total_bytes, errors, uploaded = 0, [], []
    read_warnings = []  # berkas sumber yang terlewat (dilaporkan sebagai partial)
    produced = []
    out = None  # jalur artefak; # diketahui sebelum dump agar bisa dibersihkan saat gagal
    buf = []
    lang = cfg_lang(con)
    _log_ctx.buf = buf
    _log_ctx.lang = lang
    _log_ctx.warnings = []
    progress_start(name)
    progress_update(name, buf=buf, phase="menyiapkan")
    # pakai entri yang sudah ada (didaftarkan pemanggil sebelum thread mulai)
    # supaya klik ganda tidak bisa menyelinap masuk
    if CANCEL.get(name) is None:
        CANCEL[name] = {"event": threading.Event(), "procs": []}
    check_cancelled(name)
    sampler_stop = threading.Event()
    sampler = None
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat, args=(cfg, run_id, heartbeat_stop), daemon=True)
    heartbeat_thread.start()

    try:
        log_line(f"mulai {name} ({trigger}) tipe={job['type']}")
        check_cancelled(name)
        fname = artifact_name(job)
        out = tmp_dir / fname
        dumper = DUMPERS.get(job["type"])
        if not dumper:
            raise RuntimeError(f"tipe job tidak dikenal: {job['type']}")
        # ukuran arsip sebelumnya = perkiraan kasar untuk fase dump
        row = con.execute(
            "SELECT bytes FROM runs WHERE job=? AND bytes > 0 ORDER BY id DESC LIMIT 1",
            (name,)).fetchone()
        progress_update(name, phase="dump", est=(row[0] if row else None))
        sampler = threading.Thread(target=_sample_growth, args=(name, out, sampler_stop),
                                   daemon=True)
        sampler.start()
        produced = dumper(job, out, dict(os.environ))
        sampler_stop.set()
        total_bytes = sum(p.stat().st_size for p in produced)
        progress_update(name, phase="menunggu", bytes=total_bytes, rate=0)
        read_warnings = list(getattr(_log_ctx, "warnings", []))
        log_line(f"dump selesai: {human(total_bytes)}")

        for target in cfg.get("targets", []):
            if not target.get("enabled", True):
                log_line(f"target {target.get('name')} dimatikan, dilewati")
                continue
            try:
                check_cancelled(name)
                progress_update(name, phase="unggah ke " + str(target.get("name")),
                                bytes=0, rate=0, est=total_bytes)
                for art in produced:
                    uploaded.append(upload(cfg, target, art, job))
                    log_line(f"terkirim ke {target.get('name')}: {art.name}")
                apply_retention(cfg, target, job)
                free = target_freespace(cfg, target)
                floor = cfg.get("general", {}).get("min_free_percent", 15)
                if free is not None:
                    log_line(f"storage {target.get('name')}: sisa {free:.1f}%")
                    if free < floor:
                        if lang == "id":
                            txt = (f"{html.escape(tg_title(cfg))} ⚠️ <b>{name}</b>: "
                                   f"storage {target.get('name')} sisa {free:.1f}% "
                                   f"(batas {floor}%)")
                        else:
                            txt = (f"{html.escape(tg_title(cfg))} ⚠️ <b>{name}</b>: "
                                   f"storage {target.get('name')} has {free:.1f}% free "
                                   f"(limit {floor}%)")
                        notify(cfg, txt)
            except Exception as exc:
                errors.append(f"{target.get('name', '?')}: {exc}")
                log_line(f"GAGAL target {target.get('name')}: {exc}")

        status = "success" if not errors and not read_warnings else ("partial" if uploaded else "failed")
        err_text = "; ".join(errors + read_warnings)
    except Exception as exc:
        status, err_text = "failed", str(exc)
        log_line(f"GAGAL: {exc}")
    finally:
        sampler_stop.set()
        if sampler:
            sampler.join(timeout=2)
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        # job gagal/dibatalkan bisa meninggalkan arsip separuh. Buang, supaya tidak
        # ada yang mengira itu backup yang sah. Yang sudah terkirim tidak disentuh.
        for p in [*produced, out]:
            if p:
                p.unlink(missing_ok=True)
        _log_ctx.buf = None
        _log_ctx.lang = None
        _log_ctx.warnings = None
        progress_done(name)
        CANCEL.pop(name, None)

    finished = datetime.now()
    dur = (finished - started).total_seconds()
    # ditulis langsung: _log_ctx.buf sudah dilepas di finally
    tail = f"selesai: {status} ({dur:.0f}s)"
    if lang == "en":
        tail = translate_log(tail)
    buf.append(f"[{finished:%Y-%m-%d %H:%M:%S}] {tail}")
    con.execute("UPDATE runs SET finished=?, status=?, artifact=?, bytes=?, error=?, log=? WHERE id=?",
                (finished.isoformat(), status, ", ".join(uploaded), total_bytes, err_text,
                 "\n".join(buf)[-200_000:], run_id))
    con.execute("INSERT INTO state(job,last_run) VALUES(?,?) "
                "ON CONFLICT(job) DO UPDATE SET last_run=excluded.last_run",
                (name, finished.isoformat()))
    con.commit()

    icon = {"success": "✅", "partial": "⚠️", "failed": "❌"}[status]
    head = f"{html.escape(tg_title(cfg))} {icon} <b>{name}</b> ({trigger})\n"
    if lang == "id":
        st = {"success": "berhasil", "partial": "sebagian", "failed": "gagal"}[status]
        msg = head + f"status: {st}\nukuran: {human(total_bytes)}\ndurasi: {dur:.0f}s"
    else:
        msg = head + f"status: {status}\nsize: {human(total_bytes)}\nduration: {dur:.0f}s"
    if errors:
        msg += "\n\n" + "\n".join(f"• {e[:300]}" for e in errors)
    notify(cfg, msg)
    return status, err_text


# ---------------------------------------------------------------- CLI


# Diagnostik disusun di server, jadi terjemahannya juga di sini (bukan lewat
# kamus DOM di browser). Hanya dipakai untuk dashboard; CLI tetap Indonesia.
DIAG_EN = {
    "tersedia": "available", "tidak ada": "missing",
    "klien mysql": "mysql client", "klien mysql tidak ada": "mysql client missing",
    "pg_dump & pg_restore beda versi": "pg_dump & pg_restore version mismatch",
    "klien postgres dari PATH": "postgres client from PATH",
    "remote dipakai config tapi tidak ada": "remote used by config is missing",
    "rclone tidak bisa dijalankan": "rclone cannot run",
    "tidak ada file sementara tertinggal": "no leftover temporary files",
    "belum ada job": "no jobs yet",
    "state.db tidak bisa dibaca": "state.db unreadable",
    "belum ada riwayat untuk diuji": "no history to verify",
    "diagnostik gagal": "diagnostics failed",
    "belum ada remote (jalankan: rclone config)": "no remotes yet (run: rclone config)",
}
DIAG_PAT = [
    (r"^(\S+) tersedia$", lambda m: f"{m[1]} available"),
    (r"^(\S+) tidak ada$", lambda m: f"{m[1]} missing"),
    (r"^(\d+) remote rclone$", lambda m: f"{m[1]} rclone remotes"),
    (r"^versi klien postgres konsisten \((.*)\)$",
     lambda m: f"postgres client versions consistent ({m[1]})"),
    (r"^target (.*): direktori belum ada \(dibuat saat run pertama\)$",
     lambda m: f"target {m[1]}: directory missing (created on first run)"),
    (r"^target (.*): direktori induk tidak ada / tak bisa ditulis$",
     lambda m: f"target {m[1]}: parent directory missing / not writable"),
    (r"^target (.*): disk sisa ([\d.]+)%$", lambda m: f"target {m[1]}: {m[2]}% disk free"),
    (r"^target (.*): tidak bisa ditulis$", lambda m: f"target {m[1]}: not writable"),
    (r"^target (.*): gagal baca disk$", lambda m: f"target {m[1]}: cannot read disk"),
    (r"^state\.db (utuh|RUSAK)$", lambda m: f"state.db {'intact' if m[1]=='utuh' else 'CORRUPT'}"),
    (r"^state\.db belum dibuat$", lambda m: "state.db not created yet"),
    (r"^(\d+) file sementara tertinggal$", lambda m: f"{m[1]} leftover temporary files"),
    (r"^job (.*): ssh ke (.*) berhasil$", lambda m: f"job {m[1]}: ssh to {m[2]} works"),
    (r"^job (.*): tidak bisa ssh$", lambda m: f"job {m[1]}: cannot ssh"),
    (r"^job (.*): sumber$", lambda m: f"job {m[1]}: source"),
    (r"^job (.*): tanpa password$", lambda m: f"job {m[1]}: no password"),
    (r"^(.*): arsip hilang$", lambda m: f"{m[1]}: archive missing"),
    (r"^(.*): arsip (terbaca|TIDAK terbaca)$",
     lambda m: f"{m[1]}: archive {'readable' if m[2]=='terbaca' else 'UNREADABLE'}"),
]
DIAG_DETAIL_EN = {
    "membuat arsip": "creating archives", "kompresi arsip": "archive compression",
    "job postgres": "postgres jobs", "memulihkan arsip postgres": "restoring postgres archives",
    "daftar database postgres": "listing postgres databases",
    "target gdrive/SMB/S3": "gdrive/SMB/S3 targets",
    "tambahkan [[job]] di config.toml": "add [[job]] to config.toml",
    "(akan dibuat saat run pertama)": "(created on the first run)",
    "isi `password` di config.toml, atau sediakan file kredensial klien":
        "set `password` in config.toml, or provide a client credential file",
}


def diag_localize(items, lang):
    """Terjemahkan judul/detail diagnostik ke bahasa dashboard."""
    if lang != "en":
        return items
    out = []
    for it in items:
        title = DIAG_EN.get(it["title"], it["title"])
        if title == it["title"]:
            for pat, fn in DIAG_PAT:
                m = re.match(pat, it["title"])
                if m:
                    title = fn(m)
                    break
        detail = it.get("detail") or ""
        detail = DIAG_DETAIL_EN.get(detail, detail)
        out.append({**it, "title": title, "detail": detail})
    return out


def checkup(cfg, deep=False):
    """Diagnostik: apa saja yang kurang/bisa membuat backup gagal.

    Dipakai `doctor` di terminal dan panel Diagnostik di dashboard.
    deep=True membaca ulang arsip terbaru (lebih lambat, lebih meyakinkan).
    """
    items = []

    def add(level, title, detail=""):
        items.append({"level": level, "title": title, "detail": detail})

    for tool, why in (("tar", "membuat arsip"), ("zstd", "kompresi arsip"),
                      ("pg_dump", "job postgres"), ("pg_restore", "memulihkan arsip postgres"),
                      ("psql", "daftar database postgres"), ("rclone", "target gdrive/SMB/S3")):
        found = shutil.which(tool)
        if found:
            add("ok", f"{tool} tersedia", found)
        else:
            add("bad" if tool in ("tar", "zstd") else "warn", f"{tool} tidak ada", why)

    try:
        add("ok", "klien mysql", mysql_dump_bin())
    except RuntimeError as exc:
        add("warn", "klien mysql tidak ada", str(exc))

    # beda versi pg_dump vs pg_restore = arsip tidak bisa dipulihkan sama sekali
    if Path("/usr/lib/postgresql").is_dir():
        dump, rest = pg_bin("pg_dump"), pg_bin("pg_restore")
        if Path(dump).parent != Path(rest).parent:
            add("bad", "pg_dump & pg_restore beda versi",
                f"{dump} vs {rest} — arsip bisa tidak terbaca saat restore")
        else:
            add("ok", f"versi klien postgres konsisten ({Path(dump).parent.parent.name})")
    else:
        add("ok", "klien postgres dari PATH")

    # rclone remote
    if shutil.which("rclone"):
        try:
            out = subprocess.run(["rclone", "listremotes"], capture_output=True,
                                 timeout=30).stdout.decode().split()
            names = [r.strip().rstrip(":") for r in out if r.strip()]
            add("ok" if names else "warn", f"{len(names)} remote rclone",
                ", ".join(names) or "belum ada remote (jalankan: rclone config)")
            used = {t.get("path", "").split(":", 1)[0] for t in cfg.get("targets", [])
                    if t.get("type") == "rclone" and t.get("enabled", True)}
            missing = sorted(u for u in used if u and u not in names)
            if missing:
                add("bad", "remote dipakai config tapi tidak ada",
                    ", ".join(missing) + " — cek `rclone config`")
        except Exception as exc:
            add("warn", "rclone tidak bisa dijalankan", str(exc)[:200])

    # target: jalur lokal & sisa disk
    floor = cfg.get("general", {}).get("min_free_percent", 15)
    for t in cfg.get("targets", []):
        nama = t.get("name", "?")
        if not t.get("enabled", True):
            continue
        if t.get("type") == "local":
            p = Path(t.get("path", "")).expanduser()
            if not p.is_dir():
                # arkivo membuat direktori ini saat run pertama, jadi belum ada
                # bukan kegagalan — kecuali induknya juga tidak ada / tak bisa ditulis
                parent = p.parent
                if parent.is_dir() and os.access(parent, os.W_OK):
                    add("ok", f"target {nama}: direktori belum ada (dibuat saat run pertama)",
                        str(p))
                else:
                    add("bad", f"target {nama}: direktori induk tidak ada / tak bisa ditulis",
                        str(parent))
                continue
            try:
                u = shutil.disk_usage(p)
                pct = u.free / u.total * 100
                add("warn" if pct < floor else "ok", f"target {nama}: disk sisa {pct:.1f}%",
                    f"batas {floor}% · {human(u.free)} bebas")
                if not os.access(p, os.W_OK):
                    add("bad", f"target {nama}: tidak bisa ditulis", str(p))
            except OSError as exc:
                add("warn", f"target {nama}: gagal baca disk", str(exc))

    # state.db sehat?
    db = data_dir(cfg) / "state.db"
    if db.exists():
        try:
            con = sqlite3.connect(db, timeout=10)
            res = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            add("ok" if res == "ok" else "bad", "state.db " + ("utuh" if res == "ok" else "RUSAK"),
                res if res != "ok" else str(db))
        except Exception as exc:
            add("bad", "state.db tidak bisa dibaca", str(exc)[:200])
    else:
        add("ok", "state.db belum dibuat", "(akan dibuat saat run pertama)")

    # sisa file tampungan dari job yang gagal/dibatalkan
    tmp = data_dir(cfg) / "tmp"
    leftovers = [f for f in tmp.glob("*") if f.is_file()] if tmp.is_dir() else []
    if leftovers:
        total = sum(f.stat().st_size for f in leftovers)
        add("warn", f"{len(leftovers)} file sementara tertinggal", human(total))
    else:
        add("ok", "tidak ada file sementara tertinggal")

    # job
    for job in cfg.get("job", []):
        nama, tipe = job.get("name", "?"), job.get("type", "?")
        transport = job.get("transport")
        if transport == "ssh":
            try:
                ssh_check(job)
                add("ok", f"job {nama}: ssh ke {job.get('ssh_host')} berhasil")
            except Exception as exc:
                add("bad", f"job {nama}: tidak bisa ssh", str(exc)[:300])
        if tipe == "path":
            if transport == "ssh":
                continue
            p = Path(job.get("path", "")).expanduser()
            add("ok" if p.exists() else "bad", f"job {nama}: sumber",
                str(p) if p.exists() else f"path tidak ada: {p}")
        elif not job.get("password") and not _has_credentials(job):
            add("warn", f"job {nama}: tanpa password",
                "isi `password` di config.toml, atau sediakan file kredensial klien")
    if not items or all(i["level"] != "ok" for i in items):
        add("warn", "belum ada job", "tambahkan [[job]] di config.toml")

    if deep:
        items += deep_check(cfg)
    return items


def _has_credentials(job):
    """Apakah klien bisa menemukan sandi tanpa `password` di config.

    Sumbernya beda per klien: libpq membaca ~/.pgpass, klien mysql membaca
    ~/.my.cnf. Menyamakan keduanya membuat peringatan yang salah.
    """
    candidates = []
    if job.get("type") == "postgres":
        candidates.append(Path(os.environ.get("PGPASSFILE", "~/.pgpass")).expanduser())
    else:
        candidates += [Path("~/.my.cnf").expanduser(),
                       Path(os.environ.get("MYSQL_TEST_LOGIN_FILE", "/nonexistent")).expanduser()]
    for p in candidates:
        try:
            if p.is_file():
                return True
        except OSError:
            continue
    return False


def deep_check(cfg):
    """Baca ulang arsip terbaru tiap job untuk memastikan benar-benar bisa dipulihkan."""
    out = []
    db = data_dir(cfg) / "state.db"
    if not db.exists():
        return [{"level": "warn", "title": "belum ada riwayat untuk diuji", "detail": ""}]
    con = sqlite3.connect(db, timeout=10)
    try:
        for job in cfg.get("job", []):
            row = con.execute(
                "SELECT artifact FROM runs WHERE job=? AND status IN ('success','partial') "
                "AND artifact IS NOT NULL AND artifact != '' ORDER BY id DESC LIMIT 1",
                (job["name"],)).fetchone()
            if not row:
                continue
            for path in [a for a in row[0].split(", ") if a.startswith("/")]:
                f = Path(path)
                if not f.is_file():
                    out.append({"level": "bad", "title": f"{job['name']}: arsip hilang",
                                "detail": path})
                    continue
                ok, msg = verify_archive(f, job["type"])
                out.append({"level": "ok" if ok else "bad",
                            "title": f"{job['name']}: arsip {'terbaca' if ok else 'TIDAK terbaca'}",
                            "detail": msg or path})
    finally:
        con.close()
    return out


def verify_archive(path, tipe):
    """Cek isi arsip benar-benar bisa dibaca ulang, bukan sekadar ada filenya.

    Satu lintasan saja. Versi lama menjalankan `zstd -t` lalu `tar -tf`, yang
    artinya mendekompresi dua kali — untuk arsip 1 GB itu ~5 detik kerja disk
    yang sia-sia. `tar` sudah gagal sendiri kalau aliran zstd-nya rusak
    ("Unexpected EOF in archive", "Read error (39)"), jadi dekompresinya cukup
    sekali lewat tar.
    """
    try:
        if tipe == "path" or path.name.endswith(".zst"):
            # -T0: dekompresi memakai semua core, bukan satu
            r = subprocess.run(["tar", "-I", "zstd -d -T0", "-tf", str(path)],
                               capture_output=True, timeout=1800)
            n = len(r.stdout.splitlines())
            return (r.returncode == 0), f"{n} berkas di dalam arsip" if r.returncode == 0 \
                else r.stderr.decode(errors="replace")[:300]
        # .dump postgres: `-l` hanya membaca daftar isi, tidak mendekompresi seluruh
        # arsip — 175 MB selesai dalam ~0,05 detik.
        r = subprocess.run([pg_bin("pg_restore"), "-l", str(path)], capture_output=True,
                           timeout=1800)
        n = len([l for l in r.stdout.decode().splitlines() if "; " in l])
        return (r.returncode == 0), f"{n} objek di dalam arsip" if r.returncode == 0 \
            else r.stderr.decode(errors="replace")[:300]
    except Exception as exc:
        return False, str(exc)[:300]


_verify_lock = threading.Lock()
_verify_jobs = {}   # path -> {"state": "running"|"done", "started", "ok", "message"}


def _verify_bg(path, tipe):
    ok, msg = verify_archive(Path(path), tipe)
    with _verify_lock:
        _verify_jobs[path] = {"state": "done", "ok": ok, "message": msg,
                              "elapsed": round(datetime.now().timestamp()
                                               - _verify_jobs.get(path, {}).get(
                                                   "started", datetime.now().timestamp()), 1)}


def cmd_doctor(cfg):
    items = checkup(cfg)
    icons = {"ok": "OK  ", "warn": "WARN", "bad": "FAIL"}
    for it in items:
        print(f"{icons.get(it['level'], '?')}  {it['title']}")
        if it["detail"]:
            print(f"      {it['detail']}")
    bad = [i for i in items if i["level"] == "bad"]
    print(f"\n{len(items)} pemeriksaan, {len(bad)} bermasalah")
    return 1 if bad else 0



def cmd_list(cfg, con):
    now = datetime.now()
    for job in cfg.get("job", []):
        row = con.execute("SELECT last_run FROM state WHERE job=?", (job["name"],)).fetchone()
        last = row[0] if row else None
        nxt = next_run(job, last, now)
        last_s = datetime.fromisoformat(last).strftime("%Y-%m-%d %H:%M") if last else "belum pernah"
        off = "" if job.get("enabled", True) else "  [nonaktif]"
        sisa = f"dalam {human_delta((nxt - now).total_seconds())}" if nxt else "-"
        print(f"{job['name']:20} {job['type']:9} terakhir={last_s:17} berikutnya="
              f"{nxt.strftime('%Y-%m-%d %H:%M') if nxt else '-':17} {sisa:12} "
              f"retensi={job.get('retention_days', 14)}h{off}")
        if job.get("enabled", True) and due(job, last, now):
            print(f"{'':20} ^ JATUH TEMPO")
    return 0


def cmd_tick(cfg, con):
    now = datetime.now()
    ran = 0
    for job in cfg.get("job", []):
        if not job.get("enabled", True):
            continue
        row = con.execute("SELECT last_run FROM state WHERE job=?", (job["name"],)).fetchone()
        if due(job, row[0] if row else None, now):
            print(f"[{now:%H:%M:%S}] jalan: {job['name']}", flush=True)
            if run_job(cfg, job, con) is None:
                print(f"[{now:%H:%M:%S}] lewati {job['name']}: masih berjalan "
                      f"di proses lain")
                continue
            ran += 1
    if not ran:
        print(f"[{now:%H:%M:%S}] tidak ada job jatuh tempo")
    return 0


def cmd_run(cfg, con, name):
    for job in cfg.get("job", []):
        if job["name"] == name:
            res = run_job(cfg, job, con, trigger="manual")
            if res is None:
                print(f"{name}: sedang berjalan di proses lain", file=sys.stderr)
                return 1
            status, err = res
            print(f"{name}: {status}" + (f" — {err}" if err else ""))
            return 0 if status == "success" else 1
    print(f"job tidak ditemukan: {name}", file=sys.stderr)
    return 2


def run_job_bg(cfg, job, trigger="dashboard"):
    """Jalankan job di thread sendiri, dengan koneksi database miliknya sendiri.

    Koneksi sqlite tidak boleh dipakai lintas thread, jadi thread pekerja harus
    membuka koneksinya sendiri — kalau tidak, job gagal diam-diam.
    """
    con = connect(cfg)
    try:
        return run_job(cfg, job, con, trigger=trigger)
    finally:
        con.close()


def start_job_thread(cfg, job, trigger="dashboard"):
    """Daftarkan job sebagai berjalan LALU mulai threadnya.

    Pendaftaran dilakukan di thread pemanggil supaya permintaan kedua yang datang
    saat job baru mulai tidak ikut lolos (antip klik ganda).
    """
    if job["name"] in CANCEL:
        return False
    CANCEL[job["name"]] = {"event": threading.Event(), "procs": []}
    threading.Thread(target=run_job_bg, args=(cfg, job, trigger), daemon=True).start()
    return True


def local_target_bases(cfg):
    """Direktori akar semua target lokal yang dikonfigurasi.

    Dashboard tidak boleh mengasumsikan arsip selalu di <data_dir>/local —
    jalur target lokal bisa apa saja, dan salah tebak berarti arsip tidak
    bisa diunduh atau dihapus.
    """
    bases = []
    for t in cfg.get("targets", []):
        if t.get("type") == "local" and t.get("path"):
            try:
                bases.append(Path(t["path"]).expanduser().resolve())
            except OSError:
                continue
    return bases


def resolve_artifact(cfg, rel):
    """Ubah jalur arsip dari riwayat menjadi jalur aman di dalam target lokal.

    Mengembalikan None kalau di luar semua target — mencegah path traversal.
    """
    cand = Path(rel)
    bases = local_target_bases(cfg)
    for base in bases:
        try:
            target = (cand if cand.is_absolute() else base / rel).resolve()
        except OSError:
            continue
        if base in target.parents and target.is_file():
            return target
    return None


def delete_artifact(cfg, rel):
    """Hapus satu arsip hasil backup. Tidak bisa keluar dari direktori target lokal."""
    target = resolve_artifact(cfg, rel)
    if target is None:
        return "file tidak ditemukan"
    try:
        target.unlink()
    except OSError as exc:
        return str(exc)
    return None


def list_archives(cfg, job_name):
    """Semua arsip job ini yang masih ada di target lokal, terbaru dulu.

    Dibaca dari filesystem, bukan dari riwayat: retention menghapus berkas tanpa
    menyentuh tabel runs, jadi riwayat bisa menyebut arsip yang sudah tidak ada.
    Nama berkas memuat stempel waktu (`<job>_YYYYmmdd-HHMMSS[_...].<ext>`), dan
    itulah yang dipakai untuk memilih backup tanggal tertentu.
    """
    out = []
    for base in local_target_bases(cfg):
        d = base / job_name
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file() or not f.name.startswith(job_name + "_"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({"path": str(f), "name": f.name, "target": base.name,
                        "bytes": st.st_size, "mtime": st.st_mtime,
                        "stamp": _stamp_of(f.name, job_name)})
    # stempel di nama adalah waktu backup sebenarnya; mtime hanya cadangan
    out.sort(key=lambda a: (a["stamp"], a["mtime"]), reverse=True)
    return out


def _stamp_of(filename, job_name):
    """ISO waktu dari nama arsip; kosong kalau nama tidak berpola arkivo."""
    m = re.match(rf"^{re.escape(job_name)}_(\d{{8}})-(\d{{6}})", filename)
    if not m:
        return ""
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").isoformat()
    except ValueError:
        return ""


def reconcile_runs(con):
    """Tandai run 'running' yang prosesnya sudah tidak ada sebagai gagal.

    Dashboard di-restart saat backup berjalan -> barisnya tertinggal 'running'
    selamanya. Dulu SEMUA baris unfinished dimatikan saat start, padahal run bisa
    dimiliki proses lain yang masih hidup (timer `tick`, `run` dari CLI). Sekarang
    hanya run tanpa heartbeat segar yang dianggap yatim, aturan yang sama dengan
    claim_run. Dipanggil sekali saat dashboard start.
    """
    rows = con.execute("SELECT id, job, COALESCE(heartbeat, started) FROM runs "
                       "WHERE finished IS NULL").fetchall()
    if not rows:
        return 0
    now = datetime.now()
    stale = []
    for run_id, job, beat in rows:
        try:
            age = (now - datetime.fromisoformat(beat)).total_seconds()
        except (TypeError, ValueError):
            age = ORPHAN_AFTER
        if age >= ORPHAN_AFTER:
            stale.append((run_id, job))
    if not stale:
        return 0
    stamp = now.isoformat()
    for run_id, job in stale:
        con.execute("UPDATE runs SET finished=?, status='failed', "
                    "error='terputus: proses pemilik run sudah berhenti', "
                    "log=COALESCE(log,'') || ? WHERE id=?",
                    (stamp, f"\n[{stamp[:19].replace('T', ' ')}] GAGAL: run yatim — proses "
                            f"pemiliknya sudah berhenti (tidak ada heartbeat)", run_id))
        con.execute("DELETE FROM state WHERE job=?", (job,))
    con.commit()
    log_line(f"reconcile: {len(stale)} run yatim ditandai gagal")
    return len(stale)


def cleanup_orphan_tmp(cfg):
    """Hapus arsip separuh di data/tmp yang ditinggalkan run yang terputus."""
    d = data_dir(cfg) / "tmp"
    if not d.is_dir():
        return 0
    keep = {"CARA-BUKA.txt"}
    n = 0
    for f in d.iterdir():
        if f.is_file() and f.name not in keep:
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def cmd_serve(cfg, con):
    """Dashboard tiny: daftar job, riwayat, aksi backup manual, unduh artefak."""
    import http.server
    import json
    import secrets

    n = reconcile_runs(con)
    orphan = cleanup_orphan_tmp(cfg)
    if n or orphan:
        print(f"[info] {n} run yatim ditandai gagal, {orphan} berkas sementara dibersihkan")

    cfg = load_jobs(cfg, con)
    port = cfg.get("general", {}).get("dashboard_port", 8787)
    token = cfg.get("general", {}).get("dashboard_token", "")
    if not token:
        token = secrets.token_urlsafe(24)
        print(f"[info] dashboard_token belum diisi, pakai token sesi ini: {token}")

    PAGE = """<!doctype html><html lang=id><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Ikhtisar · arkivo</title>
<link rel=icon href=/logo.svg type=image/svg+xml>
<style>
:root{--page:#eceff3;--card:#fff;--card-2:#f6f7f9;--line:#e5e8ee;--line-2:#d7dbe3;
--ink:#111318;--ink-2:#4b5563;--ink-3:#6b7280;--accent:#4f46e5;--accent-2:#4338ca;
--accent-soft:#eef0ff;--ok:#15803d;--ok-soft:#e9f7ee;--warn:#a16207;--warn-soft:#fdf5e3;
--bad:#b42318;--bad-soft:#fdecea;--info:#1d4ed8;--info-soft:#eaf1fd;--lime:#a3e635;
--hover:#fafbfc;--switch:#d7dbe3;--onbg:#15171c;--onink:#fff;--input-hover:#c3c9d4;
--ok-line:#c7ecd3;--warn-line:#f2e3bb;--bad-line:#f6cfcb;--info-line:#cddcfa;
--skel1:#f2f4f7;--skel2:#e9ecf1;--toastbg:#15171c;--toastink:#fff;--backdrop:rgba(17,19,24,.42);
--onaccent:#fff;
--r:20px;--r-sm:12px;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
/* gelap mengikuti sistem; tombol tema hanya untuk memaksa salah satu */
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--page:#0f1216;--card:#171b21;--card-2:#1d222a;--line:#262c35;--line-2:#39414d;
--ink:#e7eaef;--ink-2:#b7bec9;--ink-3:#8b93a1;--accent:#818cf8;--accent-2:#a5b4fc;
--accent-soft:#232746;--ok:#4ade80;--ok-soft:#12261a;--warn:#fbbf24;--warn-soft:#2a2110;
--bad:#f87171;--bad-soft:#2b1517;--info:#93b4ff;--info-soft:#131d31;
--hover:#1c222a;--switch:#39414d;--onbg:#e7eaef;--onink:#0f1216;--input-hover:#4a5361;
--ok-line:#1f4a2e;--warn-line:#4a3a12;--bad-line:#4d2326;--info-line:#243a5e;
--skel1:#1c222a;--skel2:#242b34;--toastbg:#2b333e;--toastink:#f2f4f7;
--backdrop:rgba(0,0,0,.62);--onaccent:#10131a}}
:root[data-theme=dark]{
--page:#0f1216;--card:#171b21;--card-2:#1d222a;--line:#262c35;--line-2:#39414d;
--ink:#e7eaef;--ink-2:#b7bec9;--ink-3:#8b93a1;--accent:#818cf8;--accent-2:#a5b4fc;
--accent-soft:#232746;--ok:#4ade80;--ok-soft:#12261a;--warn:#fbbf24;--warn-soft:#2a2110;
--bad:#f87171;--bad-soft:#2b1517;--info:#93b4ff;--info-soft:#131d31;
--hover:#1c222a;--switch:#39414d;--onbg:#e7eaef;--onink:#0f1216;--input-hover:#4a5361;
--ok-line:#1f4a2e;--warn-line:#4a3a12;--bad-line:#4d2326;--info-line:#243a5e;
--skel1:#1c222a;--skel2:#242b34;--toastbg:#2b333e;--toastink:#f2f4f7;
--backdrop:rgba(0,0,0,.62);--onaccent:#10131a}
:root{color-scheme:light}
:root[data-theme=dark]{color-scheme:dark}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark}}
*{box-sizing:border-box}
body{font:14px/1.5 var(--sans);margin:0;background:var(--page);color:var(--ink);
-webkit-font-smoothing:antialiased}
::selection{background:var(--accent-soft);color:var(--ink)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
*{scrollbar-width:thin;scrollbar-color:var(--line-2) transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:8px;border:3px solid var(--page)}
[hidden]{display:none!important}
.shell{max-width:1240px;margin:0 auto;padding:18px 20px 40px}
.topbar{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
display:flex;align-items:center;gap:12px;padding:10px 14px;position:sticky;top:12px;z-index:6}
.brand{display:flex;align-items:center;gap:10px;font-weight:680;font-size:15px;
letter-spacing:-.02em;padding-right:6px}
.mark{width:26px;height:26px;display:grid;place-items:center;border-radius:8px;
background:var(--accent);color:var(--onaccent);flex:0 0 auto}
.mark svg{width:17px;height:17px}
.nav{display:flex;gap:2px;background:var(--card-2);padding:3px;border-radius:12px}
.nav button{border:0;background:transparent;color:var(--ink-2);font:inherit;font-size:13px;
font-weight:560;padding:7px 14px;border-radius:9px;cursor:pointer;white-space:nowrap}
.nav button:hover{color:var(--ink)}
.nav button.on{background:var(--onbg);color:var(--onink)}
.spacer{flex:1}
.iconbtn{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;
border-radius:10px;border:1px solid var(--line-2);background:var(--card);color:var(--ink-2);
cursor:pointer}
.iconbtn:hover{background:var(--card-2);color:var(--ink)}
#langtag{font-size:10.5px;font-weight:700;letter-spacing:.03em}
.ic{width:16px;height:16px;flex:0 0 auto}
.hello{display:flex;align-items:flex-end;gap:16px;margin:30px 2px 18px;flex-wrap:wrap}
.hello h1{font-size:26px;letter-spacing:-.03em;margin:0;font-weight:700}
.hello p{margin:3px 0 0;color:var(--ink-3);font-size:13px}
.btn{display:inline-flex;align-items:center;gap:7px;height:36px;padding:0 14px;
border-radius:11px;border:1px solid var(--line-2);background:var(--card);color:var(--ink);
font:inherit;font-size:13px;font-weight:560;cursor:pointer;white-space:nowrap;
transition:background .15s,border-color .15s,color .15s}
.btn:hover{background:var(--card-2)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--onaccent)}
.btn.primary:hover{background:var(--accent-2);border-color:var(--accent-2)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--ink-2)}
.btn.ghost:hover{background:var(--card-2);color:var(--ink)}
.btn.sec{background:var(--info-soft);border-color:var(--info-line);color:var(--info)}
.btn.sec:hover{background:var(--info-line);border-color:var(--info-line)}
.btn.sm{height:30px;padding:0 10px;font-size:12.5px;border-radius:9px}
.btn.sq{width:30px;padding:0;justify-content:center}
.btn.danger:hover{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.grid{display:grid;gap:16px}
.kpis{grid-template-columns:repeat(4,1fr);margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
.kpi .k{display:flex;justify-content:space-between;align-items:center;color:var(--ink-3);
font-size:12.5px;font-weight:560}
.kpi .v{font-size:30px;font-weight:700;letter-spacing:-.04em;margin-top:10px;line-height:1.1}
.kpi .v small{font-size:14px;color:var(--ink-3);font-weight:500;letter-spacing:0}
.kpi .f{color:var(--ink-3);font-size:12px;margin-top:8px}
.mixwrap{margin-top:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ink-3)}
.dot.ok{background:#22c55e}.dot.warn{background:#eab308}.dot.bad{background:#ef4444}
.dot.run{background:var(--info);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r)}
.phead{display:flex;align-items:center;gap:10px;padding:16px 20px}
.phead h2{font-size:14.5px;margin:0;font-weight:660;letter-spacing:-.01em}
.phead .sub{color:var(--ink-3);font-size:12.5px}
.pbody{padding:0 20px 16px}
.stack{display:grid;grid-template-columns:2fr 1fr;grid-template-rows:auto auto;
gap:16px;align-items:start}
/* Job dan Kapasitas selebar sama (2fr); Aktivitas mengisi sampai bawah */
.stack>.jobs{grid-column:1}
.stack>.caps{grid-column:1;grid-row:2;margin-top:0}
.stack>.feedpanel{grid-column:2;grid-row:1 / span 2;align-self:stretch;
display:flex;flex-direction:column}
.stack>.feedpanel .feed{flex:1}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);
font-weight:640;text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:11px 10px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--hover)}
.num{font-variant-numeric:tabular-nums}
.jobname{font-weight:620;letter-spacing:-.01em;background:none;border:0;padding:0;
font:inherit;color:var(--ink);cursor:pointer;text-align:left;white-space:nowrap}
.jobname:hover{color:var(--accent);text-decoration:underline;text-underline-offset:3px}
.meta{color:var(--ink-3);font-size:12px;margin-top:2px;white-space:nowrap}
.meta.err{color:var(--warn);max-width:210px;overflow:hidden;text-overflow:ellipsis;
 cursor:help}
.dim{color:var(--ink-3)}
.mono{font-family:var(--mono);font-size:11.5px}
.tag{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 8px;
border-radius:7px;font-size:11.5px;font-weight:560;background:var(--card-2);
border:1px solid var(--line);color:var(--ink-2);white-space:nowrap}
.tag.ok{background:var(--ok-soft);border-color:var(--ok-line);color:var(--ok)}
.tag.warn{background:var(--warn-soft);border-color:var(--warn-line);color:var(--warn)}
.tag.bad{background:var(--bad-soft);border-color:var(--bad-line);color:var(--bad)}
.tag.info{background:var(--info-soft);border-color:var(--info-line);color:var(--info)}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;white-space:nowrap;
color:var(--ink-2)}
.chip.ok{color:var(--ok)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}
.chip.run{color:var(--info)}.chip.idle{color:var(--ink-3)}
.chipbtn{background:none;border:0;padding:0;font:inherit;font-size:12.5px;cursor:pointer}
.chipbtn .ic{width:14px;height:14px;opacity:.75}
.chipbtn:hover{color:var(--accent)}.chipbtn:hover .ic{opacity:1}
input,select{font:inherit;font-size:13px;height:34px;padding:0 10px;border-radius:9px;
border:1px solid var(--line-2);background:var(--card);color:var(--ink);caret-color:var(--accent)}
input:hover,select:hover{border-color:var(--input-hover)}
input:focus,select:focus{outline:none;border-color:var(--accent);
box-shadow:0 0 0 3px var(--accent-soft)}
input[type=number]{width:78px;font-variant-numeric:tabular-nums}
input[type=date]{width:150px}
.switch{position:relative;display:inline-flex;width:36px;height:21px;vertical-align:middle}
.switch input{position:absolute;inset:0;margin:0;opacity:0;cursor:pointer;z-index:1;height:auto}
.switch i{position:absolute;inset:0;border-radius:999px;background:var(--switch);transition:background .15s}
.switch i::after{content:"";position:absolute;width:15px;height:15px;border-radius:50%;
background:var(--card);box-shadow:0 1px 2px rgba(16,24,40,.2);left:3px;top:3px;
transition:transform .15s}
.switch input:checked+i{background:var(--accent)}
.switch input:checked+i::after{transform:translateX(15px)}
.switch input:focus-visible+i{outline:2px solid var(--accent);outline-offset:2px}
.actions{display:flex;gap:6px;justify-content:flex-end}
.mix{display:flex;height:8px;border-radius:999px;overflow:hidden;background:var(--card-2);
margin-top:10px}
.mix i{display:block;height:100%}
.mix .s{background:#22c55e}.mix .p{background:#eab308}.mix .f{background:#ef4444}
.legend{display:flex;gap:12px;margin-top:8px;color:var(--ink-3);font-size:11.5px;
flex-wrap:wrap}
.legend b{color:var(--ink-2);font-weight:620}
.feed{display:flex;flex-direction:column;gap:2px}
.feedrow{display:grid;grid-template-columns:26px 1fr auto;gap:10px;align-items:center;
padding:9px 8px;border-radius:10px}
.feedrow:hover{background:var(--card-2)}
.feedrow .t{font-size:12.5px}
.feedrow .m{color:var(--ink-3);font-size:11.5px}
.avatar{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;
font-size:10.5px;font-weight:700;background:var(--accent-soft);color:var(--accent)}
.empty{padding:34px 12px;text-align:center;color:var(--ink-3)}
.empty strong{display:block;color:var(--ink-2);font-weight:640;margin-bottom:4px;font-size:13.5px}
.empty .btn{margin-top:14px}
.skeleton{height:44px;margin:8px 0;border-radius:10px;
background:linear-gradient(90deg,var(--skel1),var(--skel2),var(--skel1));background-size:200% 100%;
animation:shimmer 1.2s linear infinite}
@keyframes shimmer{to{background-position:-200% 0}}
.diagrow{display:grid;grid-template-columns:14px 1fr;gap:10px;padding:10px 0;
border-bottom:1px solid var(--line)}
.diagrow:last-child{border-bottom:0}
.diagrow .dot{margin-top:6px}
.diagtitle{font-size:13px}
.diagdetail{color:var(--ink-3);font-size:11.5px;margin-top:2px;word-break:break-word;
font-family:var(--mono)}
dialog{border:0;border-radius:18px;background:var(--card);color:var(--ink);padding:0;
width:min(600px,calc(100vw - 28px));box-shadow:0 24px 64px -16px rgba(16,24,40,.32)}
dialog.wide{width:min(880px,calc(100vw - 28px))}
/* dialog konfirmasi: kartu kecil, ikon, dua tombol */
dialog.mc{width:min(440px,calc(100vw - 32px));border-radius:20px;
 box-shadow:0 32px 80px -20px rgba(16,24,40,.45)}
dialog.mc::backdrop{backdrop-filter:blur(2px)}
.askbody{display:flex;gap:14px;padding:22px 22px 6px;align-items:flex-start}
.askicon{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;
 flex:0 0 auto}
.askicon svg{width:20px;height:20px}
.askicon.info{background:var(--accent-soft);color:var(--accent)}
.askicon.danger{background:var(--bad-soft);color:var(--bad)}
.asktext h3{font-size:15.5px;margin:2px 0 6px;font-weight:660;letter-spacing:-.01em}
.asktext p{margin:0;font-size:13px;color:var(--ink-2);line-height:1.55}
.askdetail{margin-top:8px!important;font-family:var(--mono);font-size:11.5px!important;
 color:var(--ink-3)!important;word-break:break-all;background:var(--card-2);
 border:1px solid var(--line);border-radius:9px;padding:8px 10px}
.askfoot{display:flex;justify-content:flex-end;gap:8px;padding:16px 22px 20px}
.askfoot .main{display:flex;gap:8px}
dialog.mc .btn.danger{color:var(--bad);border-color:var(--bad-line);
 background:var(--bad-soft)}
dialog.mc .btn.danger:hover{background:var(--bad);color:#fff;border-color:var(--bad)}
dialog::backdrop{background:var(--backdrop)}
.sheethead{display:flex;align-items:center;gap:12px;padding:16px 20px;
border-bottom:1px solid var(--line)}
.sheethead strong,.sheethead h3{font-size:15px;font-weight:660;margin:0;flex:1;
letter-spacing:-.01em}
.sheetbody{padding:18px 20px}
.sheetfoot{display:flex;align-items:center;gap:8px;margin-top:18px;flex-wrap:wrap;
 justify-content:flex-end}
.sheetfoot .main{display:flex;gap:8px}
.sheetfoot .note{margin-right:auto}
fieldset{border:0;padding:0;margin:0 0 16px}
legend{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);
font-weight:640;padding:0;margin-bottom:8px}
.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:11px 14px}
.fgrid label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--ink-2)}
.fgrid input,.fgrid select{width:100%}
pre{font-family:var(--mono);font-size:12px;line-height:1.6;margin:0;padding:16px 20px;
max-height:60vh;overflow:auto;white-space:pre-wrap;word-break:break-word;color:var(--ink-2)}
.pane{padding:18px 20px}
.scrolllist{max-height:min(52vh,420px);overflow:auto}
.res{margin:12px 0 0;font-size:12.5px;min-height:18px}
.res.ok{color:var(--ok)}.res.bad{color:var(--bad)}
/* hasil uji koneksi: kotak status, bukan baris teks kecil di antara tombol */
.resnote{display:flex;gap:10px;align-items:flex-start;margin-top:14px;padding:11px 13px;
border-radius:12px;border:1px solid var(--line);background:var(--card-2);font-size:12.5px}
.resnote.ok{border-color:var(--ok-line);background:var(--ok-soft)}
.resnote.bad{border-color:var(--bad-line);background:var(--bad-soft)}
.resnote.warn{border-color:var(--warn-line);background:var(--warn-soft)}
.resnote .dot{margin-top:5px;flex:0 0 auto}
.resnote.ok .dot{background:#22c55e}.resnote.bad .dot{background:#ef4444}
.resnote.warn .dot{background:#eab308}.resnote .dot.run{background:var(--info)}
.resnote b{display:block;margin-bottom:2px;font-weight:640}
.resnote .detail{color:var(--ink-2);word-break:break-word}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--ink-3)}
.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,12px);opacity:0;
background:var(--toastbg);color:var(--toastink);padding:10px 16px;border-radius:11px;font-size:12.5px;
pointer-events:none;z-index:30;transition:opacity .18s,transform .18s}
.toast.show{opacity:1;transform:translate(-50%,0)}
.archbar{display:flex;gap:8px;align-items:center;padding:14px 20px 12px;flex-wrap:wrap}
.archbar .note{color:var(--ink-3);font-size:12px}
.archmeta{padding:0 20px 10px}
.archmeta .note{color:var(--ink-3);font-size:12px}
.searchbox{position:relative;display:flex;align-items:center;flex:1;min-width:170px;max-width:280px}
.searchbox svg{position:absolute;left:10px;color:var(--ink-3);pointer-events:none;width:14px;height:14px}
.searchbox input{padding-left:31px;width:100%}
.archlist{max-height:52vh;overflow:auto;padding:0 20px 16px}
.archrow{display:grid;grid-template-columns:1fr auto;gap:10px;padding:11px 10px;
border-bottom:1px solid var(--line);align-items:center}
.archrow:last-child{border-bottom:0}
.archrow:hover{background:var(--hover)}
.archrow .nm{font-size:12.5px;font-weight:560;word-break:break-all}
.archrow .mt{color:var(--ink-3);font-size:11.5px;margin-top:2px}
.archrow .btns{display:flex;gap:6px}
.liveline{padding:0 20px 12px}
.liveline .chip{font-size:12px}
#livefoot{gap:8px}
.howto h4{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3);
margin:16px 0 6px;font-weight:640}
.howto p{margin:0 0 6px;font-size:13px;color:var(--ink-2)}
pre.cmd{font-family:var(--mono);font-size:12px;line-height:1.6;background:var(--card-2);
border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0;
white-space:pre-wrap;word-break:break-all;color:var(--ink);max-height:none}
pre.cmd.alt{margin-top:6px;color:var(--ink-3)}
.capbar{height:8px;border-radius:999px;background:var(--card-2);overflow:hidden}
.capbar i{display:block;height:100%;border-radius:999px;background:var(--accent)}
.capbar i.ok{background:#22c55e}.capbar i.warn{background:#eab308}.capbar i.bad{background:#ef4444}
.capbar.empty-bar{background:var(--card-2)}
.caprow{padding:12px 0;border-bottom:1px solid var(--line)}
.caprow:last-child{border-bottom:0}
.caphead{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;font-size:13px;margin-bottom:8px}
.caphead b{font-weight:620}
.capfoot{color:var(--ink-3);font-size:12px;margin-top:6px}
td .capbar{width:110px;max-width:100%;margin-bottom:4px}
td.path{word-break:break-all;max-width:340px}
.themebtn svg{transition:transform .2s}
.archday{position:sticky;top:0;background:var(--card-2);padding:7px 10px;font-size:11px;
letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);font-weight:640;
border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
.archday span{font-weight:500;letter-spacing:0;text-transform:none;font-size:11.5px}
@media (max-width:1000px){
 .kpis{grid-template-columns:repeat(2,1fr)}
 .stack{grid-template-columns:1fr;grid-template-rows:none}
 .stack>.jobs,.stack>.caps,.stack>.feedpanel{grid-column:1;grid-row:auto}
}
@media (max-width:720px){
 .shell{padding:12px}
 .topbar{flex-wrap:wrap;position:static}
 .nav{order:3;width:100%;justify-content:space-between}
 .nav button{flex:1;padding:7px 6px;text-align:center}
 .hello{margin:20px 2px 14px}
 .hello h1{font-size:22px}
 .kpis{grid-template-columns:1fr 1fr;gap:10px}
 .kpi{padding:13px 14px}.kpi .v{font-size:24px}
 .hide-sm{display:none}
 .pbody{padding:0 12px 12px}
 .phead{padding:14px 12px;flex-wrap:wrap}
 .phead .sub{order:3;width:100%}
 .phead input{order:4;width:100%!important;min-width:0}
 td,th{padding:9px 8px}
 .fgrid{grid-template-columns:1fr}
 .archbar{padding:12px 12px 10px}
 .archmeta{padding:0 12px 8px}
 .archbar input{width:100%!important;min-width:0}
 .searchbox{flex:1 1 100%;max-width:none;width:100%}
 .archbar .note{width:100%}
 .archlist{padding:0 12px 14px}
 .archrow{grid-template-columns:1fr}
 .archrow .btns{justify-content:flex-start}
 .actions{flex-wrap:nowrap}
 .actions .btn.sm{padding:0 7px}
 .scroll{overflow-x:auto}
 dialog .actions{justify-content:flex-start}
}
</style>
<div class=shell>
<header class=topbar>
<span class=brand><span class=mark><svg viewBox="0 0 24 24" fill=none stroke=currentColor
 stroke-width=1.9 stroke-linecap=round stroke-linejoin=round aria-hidden=true>
<path d="M12 3.2v6.4"/><path d="m9.6 7.6 2.4 2.4 2.4-2.4"/>
<path d="M4.5 11.6h15"/>
<path d="M6 11.6h12v6.5a2.2 2.2 0 0 1-2.2 2.2H8.2A2.2 2.2 0 0 1 6 18.1z"/>
<path d="M10 15h4"/></svg></span>arkivo</span>
<nav class=nav aria-label=Bagian>
<button data-view=overview class=on onclick="setView('overview')">Ikhtisar</button>
<button data-view=jobs onclick="setView('jobs')">Job</button>
<button data-view=archives onclick="setView('archives')">Arsip</button>
<button data-view=history onclick="setView('history')">Riwayat</button>
</nav>
<span class=spacer></span>
<button class=iconbtn id=langbtn onclick="toggleLang()" aria-label="Ganti bahasa"
 title="Bahasa / Language"><span id=langtag>EN</span></button>
<button class=iconbtn id=themebtn onclick="cycleTheme()" aria-label="Ganti tema"></button>
<button class="iconbtn" onclick="showDiag()" title="Diagnostik" aria-label="Diagnostik">
<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.8
 stroke-linecap=round stroke-linejoin=round><path d="M3 12h4l2.5-7 4 14 2.5-7h5"/></svg></button>
<button class="iconbtn" id=tgbtn onclick="openTelegram()" title="Pengaturan Telegram"
 aria-label="Pengaturan Telegram">
<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.8
 stroke-linecap=round stroke-linejoin=round><path d="m21 3-9.5 18-2.4-7-6.1-2.6z"/>
<path d="m9.1 14 4.6-4.5"/></svg></button>
<button class=iconbtn onclick="showHelp()" title="Panduan" aria-label="Panduan">
<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.8
 stroke-linecap=round stroke-linejoin=round><circle cx="12" cy="12" r="8"/>
<path d="M9.6 9.4a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .9-1 1.6v.4"/><path d="M12 17h.01"/></svg></button>
</header>

<section class=hello>
<div style="flex:1">
<h1 id=pagetitle>Ringkasan backup</h1>
<p id=subtitle></p>
</div>
<span id=tgmsg class=dim style="font-size:12px" hidden></span>
<button class="btn primary" id=pageaction onclick="openAdd()">
<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2
 stroke-linecap=round><path d="M12 5v14M5 12h14"/></svg>Tambah job</button>
</section>

<div id=capbox hidden></div>

<section id=v-overview>
<div class="grid kpis" id=kpis></div>
<div class=stack>
<section class="panel jobs">
<div class=phead><h2>Job</h2><span class=sub id=jobsub></span><span class=spacer></span>
<button class="btn ghost sm" onclick="setView('jobs')">Lihat semua</button></div>
<div class=pbody id=jobsshort><div class=skeleton></div></div>
</section>
<section class="panel feedpanel">
<div class=phead><h2>Aktivitas</h2><span class=sub>run terakhir</span></div>
<div class="pbody feed" id=feed><div class=skeleton></div></div>
</section>
<section class="panel caps">
<div class=phead><h2>Kapasitas penyimpanan</h2><span class=sub>sisa ruang target</span>
<span class=spacer></span><span class=sub id=capsub></span></div>
<div class=pbody id=caps><div class=skeleton></div></div>
</section>
</div>
</section>

<section id=v-jobs hidden>
<section class=panel>
<div class=phead><h2>Job</h2><span class=sub id=jobcount></span></div>
<div class=pbody id=jobs><div class=skeleton></div></div>
</section>
<section class=panel style="margin-top:16px">
<div class=phead><h2>Target penyimpanan</h2><span class=sub id=targetcount></span></div>
<div class=pbody id=targets><div class=skeleton></div></div>
</section>
</section>

<section id=v-archives hidden>
<section class=panel>
<div class=phead><h2>Arsip tersimpan</h2><span class=sub id=archsub></span>
<span class=spacer></span>
<input type=date id=archdate onchange="renderArchives()" aria-label="Filter tanggal">
<div class=searchbox>
<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.8
 stroke-linecap=round aria-hidden=true><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>
<input type=search id=archq oninput="renderArchives()" placeholder="cari nama job" data-i18n-ph="Search jobs"
 aria-label="Cari job">
</div>
</div>
<div class=pbody id=archiveall><div class=skeleton></div></div>
</section>
</section>

<section id=v-history hidden>
<section class=panel>
<div class=phead><h2>Riwayat</h2><span class=sub>60 run terakhir</span>
<button class="btn sm danger" id=clearfailed hidden onclick="delFailed()">Hapus yang gagal</button></div>
<div class=pbody id=runs><div class=skeleton></div></div>
</section>
</section>
</div>

<div id=toast class=toast role=status aria-live=polite></div>
<dialog id=askbox class=mc>
<div class=askbody>
<div class=askicon id=askicon><svg viewBox="0 0 24 24" fill=none stroke=currentColor
 stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>
<path d="M12 8v5"/><path d="M12 16.5h.01"/></svg></div>
<div class=asktext><h3 id=asktitle>Konfirmasi</h3>
<p id=askmsg></p><p class=askdetail id=askdetail hidden></p></div>
</div>
<div class=askfoot>
<button class="btn" id=askno>Batal</button>
<button class="btn primary" id=askyes>Ya, lanjutkan</button>
</div></dialog>
<dialog id=dlg>
<div class=sheethead><strong id=dlgt></strong>
<button class=btn sq onclick="stopLive();dlg.close()" aria-label=Tutup>&times;</button></div>
<div class=liveline id=liveline hidden><span class="chip run"><i class="dot run"></i>
  <span id=dlglive></span></span></div>
<pre id=dlgb></pre>
<div id=dlgr class=pane hidden></div>
<div class=sheetfoot id=livefoot hidden style="padding:0 20px 16px">
<button class="btn sm" onclick=openEndedLog()>Log penuh</button></div></dialog>

<dialog id=arch class=wide>
<div class=sheethead><h3 id=archtitle>Arsip</h3>
<button class=btn sq onclick="arch.close()" aria-label=Tutup>&times;</button></div>
<div class=archbar>
<input type=date id=archjobdate onchange="loadArchives(CURRENT)" aria-label="Tanggal arsip">
<div class=searchbox>
<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.8
 stroke-linecap=round aria-hidden=true><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>
<input type=search id=archjobq oninput="filterArchiveRows()" placeholder="cari berkas" data-i18n-ph="Search files"
 aria-label="Cari berkas">
</div>
<button class="btn sm ghost" id=archclear onclick="clearArchFilter()" hidden>Hapus filter</button>
</div>
<div class=archmeta><span class=note id=archnote></span></div>
<div class=archlist id=archrows></div>
</dialog>

<dialog id=add class=wide><form method=dialog onsubmit="event.preventDefault();saveJob()">
<div class=sheethead><h3 id=add_title>Tambah job</h3>
<button type=button class=btn sq onclick="add.close()" aria-label=Tutup>&times;</button></div>
<div class=sheetbody>
<fieldset><legend>Identitas</legend><div class=fgrid>
<label>Nama job<input type=text id=f_name placeholder="db-prod-01" autocomplete=off></label>
<label>Tipe<select id=f_type onchange="fType()">
  <option value=postgres>PostgreSQL
  <option value=mysql>MySQL
  <option value=mariadb>MariaDB
  <option value=path>Project / file
</select></label></div></fieldset>
<fieldset><legend>Sumber</legend><div class=fgrid>
<label>Sumber<select id=f_transport onchange="fType()">
  <option value=local>Mesin ini
  <option value=ssh>Server lain (SSH)
</select></label>
<label class=r_ssh>ssh host<input type=text id=f_ssh_host placeholder="10.0.0.5" autocomplete=off></label>
<label class=r_ssh>ssh user<input type=text id=f_ssh_user placeholder=root autocomplete=off></label>
<label class=r_ssh>ssh port<input type=number id=f_ssh_port placeholder=22></label>
<label class="r_ssh">ssh kunci<input type=text id=f_ssh_key
  placeholder="/root/.ssh/id_ed25519 (opsional)" autocomplete=off></label>
<label id=r_db>Host DB<input type=text id=f_host placeholder="127.0.0.1" autocomplete=off></label>
<label id=r_port>Port DB<input type=number id=f_port placeholder=5432></label>
<label id=r_user>User DB<input type=text id=f_user placeholder=postgres autocomplete=off></label>
<label id=r_dbs>Database<input type=text id=f_databases placeholder="* atau app, crm" autocomplete=off></label>
<label id=r_path hidden>Path<input type=text id=f_path placeholder="/var/www/app" autocomplete=off></label>
<label id=r_exc hidden>Kecualikan<input type=text id=f_exclude
  placeholder="node_modules, storage/app/tmp" autocomplete=off></label></div></fieldset>
<fieldset><legend>Jadwal &amp; retensi</legend><div class=fgrid>
<label>Jadwal<input type=text id=f_schedule value="daily 02:00" autocomplete=off></label>
<label>Retensi (hari)<input type=number id=f_retention value=14 min=0></label></div></fieldset>
<p class=dim style="font-size:12.5px" data-i18n="Passwords are set via the key button on the job row; they are never sent back to the browser.">Password diisi lewat tombol kunci di baris job;
tidak pernah dikirim balik ke browser.</p>
<div class=sheetfoot>
<span id=addmsg class="dim note" style="font-size:12.5px"></span>
<button class=btn type=button onclick="add.close()">Batal</button>
<button class="btn sec" type=button id=test_btn onclick="testCurrent()">Uji koneksi</button>
<button class="btn primary" type=submit id=save_btn>Simpan</button></div>
<div id=addres class=resnote hidden></div>
</div></form></dialog>

<dialog id=tdlg><form method=dialog onsubmit="event.preventDefault();saveTarget()">
<div class=sheethead><h3 id=t_title>Edit target</h3>
<button type=button class=btn sq onclick="tdlg.close()" aria-label=Tutup>&times;</button></div>
<div class=sheetbody>
<label style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--ink-2)">
Jalur target<input type=text id=t_path autocomplete=off spellcheck=false
 style="width:100%;font-family:var(--mono);font-size:12px"></label>
<p class=dim style="font-size:12px;margin:8px 0 0" id=t_hint></p>
<div class=sheetfoot>
<button class=btn type=button onclick="tdlg.close()">Batal</button>
<button class="btn primary" type=submit id=t_save>Simpan</button></div>
<div id=t_res class=resnote hidden></div>
</div></form></dialog>

<dialog id=tgdlg><form method=dialog onsubmit="event.preventDefault();saveTelegram()">
<div class=sheethead><h3>Notifikasi Telegram</h3>
<button type=button class=btn sq onclick="tgdlg.close()" aria-label=Tutup>&times;</button></div>
<div class=sheetbody>
<div class=fgrid style="margin-bottom:12px">
<label>Bot token<input type=text id=g_token autocomplete=off spellcheck=false
  placeholder="123456:ABC-DEF…"></label>
<label>Chat ID<input type=text id=g_chat placeholder="-1001234567890" autocomplete=off></label>
<label>Judul notifikasi<input type=text id=g_title value="Arkivo" maxlength=58 autocomplete=off></label>
</div>
<label style="display:flex;align-items:center;gap:10px;font-size:12.5px;color:var(--ink-2)">
<label class=switch><input type=checkbox id=g_enabled aria-label="Kirim notifikasi"><i></i></label>
Kirim notifikasi otomatis setiap selesai backup</label>
<p class=dim style="font-size:12px;margin:12px 0 0" id=g_hint></p>
<div class=sheetfoot>
<button class=btn type=button onclick="tgdlg.close()">Tutup</button>
<button class="btn sec" type=button id=g_test onclick="testTg()">Kirim tes</button>
<button class="btn primary" type=submit id=g_save>Simpan</button></div>
<div id=g_res class=resnote hidden></div>
</div></form></dialog>

<script>

// ---- i18n: teks sumber Indonesia, terjemahan Inggris lewat kamus ----------
// Teks pengguna (nama job, path, pesan error tool) tidak ada di kamus, jadi
// lolos apa adanya. Penerjemah DOM dipakai supaya tidak perlu membungkus tiap
// literal dengan t(). ponytail: exact-match; kalau nanti ada nama job yang
// kebetulan sama dengan label UI, beri elemennya data-no-i18n.
const LANG = LANG_INJECT;
const ID2EN = {
  // --- navigasi & judul
  'Ikhtisar': 'Overview',
  'Job': 'Jobs',
  'Arsip': 'Archives',
  'Riwayat': 'History',
  'Ringkasan backup': 'Backup summary',
  'Job backup': 'Backup jobs',
  'Riwayat run': 'Run history',
  'Target penyimpanan': 'Storage targets',
  'Kapasitas penyimpanan': 'Storage capacity',
  'Kapasitas menipis': 'Low capacity',
  'Diagnostik': 'Diagnostics',
  'Panduan singkat': 'Quick guide',

  // --- aksi
  'Tambah job': 'Add job',
  'Simpan': 'Save',
  'Batal': 'Cancel',
  'Tutup': 'Close',
  'Hapus': 'Delete',
  'Muat ulang': 'Reload',
  'Uji koneksi': 'Test connection',
  'Kirim tes': 'Send test',
  'Uji arsip terbaru': 'Verify latest archive',
  'Jalankan sekarang': 'Run now',
  'Hentikan': 'Stop',
  'Lihat arsip': 'View archives',
  'Lihat arsip job ini': 'View this job\u2019s archives',
  'Lihat log': 'View log',
  'Lihat proses berjalan': 'View running process',
  'Lihat semua': 'View all',
  'Log penuh': 'Full log',
  'Unduh arsip': 'Download archive',
  'Hapus arsip': 'Delete archive',
  'Hapus job': 'Delete job',
  'Hapus password': 'Remove password',
  'Edit job': 'Edit job',
  'Edit target': 'Edit target',
  'Edit jalur target': 'Edit target path',
  'Kelola target': 'Manage targets',
  'Ganti tema': 'Change theme',
  'Hapus filter': 'Clear filter',
  'Periksa rusak/tidak': 'Check integrity',
  'Salin perintah restore': 'Copy restore command',
  'Ya, lanjutkan': 'Yes, continue',

  // --- label kolom
  'Database / sumber': 'Database / source',
  'Terakhir': 'Last',
  'Berikutnya': 'Next',
  'Jadwal / retensi': 'Schedule / retention',
  'Aktif': 'Enabled',
  'Aksi': 'Actions',
  'Status': 'Status',
  'Ukuran': 'Size',
  'Durasi': 'Duration',
  'Mulai': 'Started',
  'Target': 'Target',
  'Jalur': 'Path',
  'Kapasitas': 'Capacity',
  'Berkas': 'Files',
  'Waktu': 'Time',
  'Tanggal arsip': 'Archive date',
  'Filter tanggal': 'Date filter',
  'Cari berkas': 'Search files',
  'Cari job': 'Search jobs',

  // --- status
  'berhasil': 'success',
  'sebagian': 'partial',
  'gagal': 'failed',
  'berjalan': 'running',
  'belum pernah': 'never',
  'dalam': 'in',
  'belum ada': 'none yet',
  'belum ada run': 'no runs yet',
  'belum ada password': 'no password yet',
  'job sedang berjalan': 'job is running',
  'perlu password': 'password needed',
  'sudah ada': 'set',
  'tidak terbaca': 'unreadable',
  'tidak diketahui': 'unknown',
  'nonaktif': 'disabled',
  'target dimatikan': 'target disabled',
  'kapasitas tidak terbaca': 'capacity unreadable',
  'tersedia': 'available',
  'menipis': 'low',
  'penuh': 'full',
  'di bawah batas': 'below threshold',
  'semua baik': 'all good',
  'semua job terjadwal': 'all jobs scheduled',
  'Arsip TIDAK terbaca': 'Archive UNREADABLE',
  'Arsip utuh dan bisa dipulihkan': 'Archive intact and restorable',
  'Memeriksa arsip…': 'Checking archive…',
  'Memeriksa…': 'Checking…',
  'Menguji koneksi…': 'Testing connection…',
  'Mengirim tes…': 'Sending test…',
  'Menyimpan…': 'Saving…',
  'Menjalankan diagnostik.': 'Running diagnostics.',
  'Koneksi berhasil': 'Connection succeeded',
  'Koneksi gagal': 'Connection failed',
  'Pesan tes terkirim': 'Test message sent',
  'Gagal mengirim': 'Failed to send',
  'Gagal menyalin.': 'Failed to copy.',
  'Gagal menyimpan': 'Failed to save',
  'tidak ada keterangan': 'no details',
  'tidak bisa menghubungi dashboard': 'cannot reach dashboard',

  // --- empty state
  'Belum ada job': 'No jobs yet',
  'Belum ada target': 'No targets yet',
  'Belum ada riwayat': 'No history yet',
  'Belum ada aktivitas': 'No activity yet',
  'Tidak ada arsip': 'No archives',
  'Tambah job dulu untuk melihat arsipnya.': 'Add a job first to see its archives.',
  'Tambahkan job dari halaman Job.': 'Add a job from the Jobs page.',
  'Tambahkan sumber backup pertama: database atau folder project.':
    'Add your first backup source: a database or a project folder.',
  'Tambahkan [[targets]] di config.toml.': 'Add [[targets]] to config.toml.',
  'Run akan muncul setelah job pertama berjalan.':
    'Runs appear after the first job runs.',
  'Riwayat run akan muncul setelah job pertama berjalan.':
    'Run history appears after the first job runs.',
  'Arsip muncul setelah job berjalan dan target lokal aktif.':
    'Archives appear once a job runs and a local target is enabled.',
  'Job ini belum punya arsip di target lokal, atau semua sudah dihapus retention.':
    'This job has no archives in the local target, or retention removed them.',
  'Tidak ada arsip pada tanggal itu. Coba hapus filter tanggal.':
    'No archives on that date. Try clearing the date filter.',
  'Tidak ada yang cocok dengan filter. Hapus filter untuk melihat semua.':
    'Nothing matches the filter. Clear it to see everything.',
  'tidak ada arsip yang cocok': 'no matching archives',

  // --- dialog
  'Identitas': 'Identity',
  'Nama job': 'Job name',
  'Tipe': 'Type',
  'Sumber': 'Source',
  'Mesin ini': 'This machine',
  'Server lain (SSH)': 'Another server (SSH)',
  'Project / file': 'Project / files',
  'Host DB': 'DB host',
  'Port DB': 'DB port',
  'User DB': 'DB user',
  'Database': 'Database',
  'Path': 'Path',
  'Kecualikan': 'Exclude',
  'Jadwal & retensi': 'Schedule & retention',
  'Jadwal': 'Schedule',
  'Retensi (hari)': 'Retention (days)',
  'ssh host': 'ssh host',
  'ssh user': 'ssh user',
  'ssh port': 'ssh port',
  'ssh kunci': 'ssh key',
  'Jalur target': 'Target path',
  'Bot token': 'Bot token',
  'Chat ID': 'Chat ID',
  'Kirim notifikasi otomatis setiap selesai backup':
    'Send a notification after each backup',
  'Notifikasi Telegram': 'Telegram notifications',
  'Pengaturan Telegram': 'Telegram settings',
  'Hapus arsip?': 'Delete archive?',
  'Hapus job?': 'Delete job?',
  'Hapus password?': 'Remove password?',
  'Hapus riwayat?': 'Delete history entry?',
  'Hapus riwayat gagal?': 'Delete failed history?',
  'Hapus riwayat': 'Delete entry',
  'Hapus yang gagal': 'Delete failed',
  'Jalankan backup?': 'Run backup?',
  'Satu baris riwayat run akan dihapus.':
    'One run history entry will be deleted.',
  'Arsip yang sudah tersimpan tidak ikut terhapus. Log run ini juga hilang.':
    'Existing archives are kept. This run log is lost too.',
  'Arsip yang sudah tersimpan tidak ikut terhapus.':
    'Existing archives are kept.',
  'Baris riwayat berstatus gagal akan dihapus.':
    'Failed history entries will be deleted.',

  // --- pesan
  'Arsip dihapus.': 'Archive deleted.',
  'Password dihapus.': 'Password removed.',
  'Job tersimpan.': 'Job saved.',
  'Job dihapus.': 'Job deleted.',
  'Riwayat dihapus.': 'History deleted.',
  'Tersimpan.': 'Saved.',
  'Tersimpan': 'Saved',
  'Perintah restore disalin.': 'Restore command copied.',
  'Jalankan backup': 'Run backup',
  'Job tidak ditemukan.': 'Job not found.',
  'Target tidak ditemukan.': 'Target not found.',
  'Perubahan tersimpan.': 'Changes saved.',
  'Pengaturan Telegram tersimpan.': 'Telegram settings saved.',
  'Jalur target tersimpan.': 'Target path saved.',
  'Notifikasi mengikuti pengaturan baru.':
    'Notifications follow the new settings.',
  'Daftar arsip berubah — memuat ulang.': 'Archive list changed — reloading.',
  'Arsip ini akan dihapus permanen dari target penyimpanan.':
    'This archive will be permanently deleted from the storage target.',
  'Arsip yang sudah tersimpan tidak ikut terhapus. Job dari config.toml ':
    'Existing archives are kept. Jobs from config.toml ',
  'hanya dinonaktifkan, bukan dihapus dari berkas config.':
    'are only disabled, not removed from the config file.',
  'Nilai token tidak pernah ditampilkan kembali.':
    'The token is never shown again.',
  'Nilai tersimpan tidak pernah ditampilkan kembali. Mengosongkan lalu simpan akan menghapus password.':
    'Stored values are never shown again. Clearing and saving removes the password.',
  'Memakai pengaturan yang sudah tersimpan.':
    'Using the saved settings.',
  'Menghubungi sumber backup dengan pengaturan tersimpan.':
    'Contacting the backup source with the saved settings.',
  'Uji koneksi butuh job yang sudah tersimpan. Klik Simpan, lalu uji lagi dari halaman Job.':
    'Test connection needs a saved job. Save it, then test again from the Jobs page.',
  'Simpan job dulu': 'Save the job first',
  'Password diisi lewat tombol kunci di baris job; tidak pernah dikirim balik ke browser.':
    'Passwords are set via the key button on the job row; they are never sent back to the browser.',
  'Buat bot lewat @BotFather, lalu kirim pesan ke bot itu dan ambil chat id-nya.':
    'Create a bot via @BotFather, message it, then read its chat id.',
  'Cek chat Telegram-mu.': 'Check your Telegram chat.',
  'Jalur absolut di mesin ini. Direktori dibuat otomatis saat run pertama.':
    'Absolute path on this machine. The directory is created on the first run.',
  'jendela ini boleh ditutup, verifikasi tetap berjalan di server.':
    'this window can be closed, verification keeps running on the server.',

  // --- waktu relatif
  'baru saja': 'just now',
  'kemarin': 'yesterday',
  ' hari lalu': ' days ago',
  ' jam lalu': ' hours ago',
  ' menit lalu': ' minutes ago',
  'Hari ini · ': 'Today · ',
  'Kemarin · ': 'Yesterday · ',

  // --- tema
  'Tema terang.': 'Light theme.',
  'Tema gelap.': 'Dark theme.',
  'Tema mengikuti sistem.': 'Theme follows the system.',

  // --- misc
  'Arsip tersimpan': 'Stored archives',
  'Keberhasilan': 'Success rate',
  'Aktivitas': 'Activity',
  'berkas': 'files',
  'Arsip ini': 'This archive',
  'Arsip di riwayat': 'Archives in history',
  'Backup terakhir': 'Last backup',
  'Cara buka': 'How to open',
  'Cara membuka arsip .tar.zst': 'How to open a .tar.zst archive',
  'Termudah: 7-Zip': 'Easiest: 7-Zip',
  'Arsip besar butuh waktu —': 'Large archives take time —',
  'Password · ': 'Password · ',
  'Cara membuka · ': 'How to open · ',
  'Verifikasi arsip · ': 'Archive verification · ',
  'Uji koneksi · ': 'Test connection · ',
  'Verifikasi arsip: periksa isinya masih bisa dipulihkan':
    'Archive verification: check the contents are still restorable',
  'Proses · ': 'Process · ',
  'Log · ': 'Log · ',
  'Arsip · ': 'Archive · ',
  'Edit job · ': 'Edit job · ',
  'Edit target · ': 'Edit target · ',
  'Diagnostik · ': 'Diagnostics · ',
  'Backup dimulai: ': 'Backup started: ',
  'Gagal: ': 'Failed: ',
  'Gagal menghapus: ': 'Failed to delete: ',
  'Gagal menyimpan: ': 'Failed to save: ',
  'Job "': 'Job "',
  'Backup "': 'Backup "',
  ' akan dihapus.': ' will be deleted.',
  ' akan dihapus dari dashboard.': ' will be deleted from the dashboard.',
  ' akan dijalankan sekarang di server.': ' will run now on the server.',
  ' dihentikan.': ' stopped.',
  ' sedang tidak berjalan.': ' is not running.',
  'Password untuk job "': 'Password for job "',
  'sisa ruang target': 'target free space',
  'run terakhir': 'latest runs',
  '60 run terakhir': 'last 60 runs',
  'run gagal terakhir': 'latest failed runs',
  'job aktif': 'active jobs',
  'bebas dari': 'free of',
  'bebas total': 'free in total',
  'tar dikompresi zstd': 'zstd-compressed tar',
  'rclone config': 'rclone config',
  'config.toml + dashboard': 'config.toml + dashboard',
  'password dari dashboard': 'password from dashboard',
  'password dari config.toml / berkas kredensial klien':
    'password from config.toml / client credential file',
  'password baru': 'new password',
  'tersimpan — kosongkan untuk mempertahankan': 'stored — leave blank to keep',
  'Untuk folder. Pilih ': 'for folders. Pick ',
  '* atau app, crm': '* or app, crm',
  'node_modules, storage/app/tmp': 'node_modules, storage/app/tmp',
  'melebihi 30 menit, dihentikan': 'exceeded 30 minutes, stopped',
  'mengambil progres…': 'fetching progress…',
  'menghubungi server…': 'contacting server…',
  'selesai · ': 'finished · ',
  'Judul notifikasi': 'Notification title',
  '(log kosong)': '(empty log)',
  '(menunggu keluaran…)': '(waiting for output…)',
  'menyiapkan': 'preparing',
  'menunggu': 'waiting',
};

// teks ber-interpolasi tidak bisa exact-match: pola + fungsi
const DYN = [
  [/^(\\d+) job$/, m => m[1] + (m[1] === '1' ? ' job' : ' jobs')],
  [/^(\\d+) tujuan$/, m => m[1] + (m[1] === '1' ? ' target' : ' targets')],
  [/^(\\d+) total$/, m => m[1] + ' total'],
  [/^(\\d+) dari (\\d+) job aktif$/, m => m[1] + ' of ' + m[2] + ' jobs active'],
  [/^(\\d+) berhasil · (\\d+) sebagian · (\\d+) gagal$/,
    m => m[1] + ' success · ' + m[2] + ' partial · ' + m[3] + ' failed'],
  [/^(\\d+) run gagal terakhir$/, m => m[1] + ' recent failed runs'],
  [/^simpan (\\d+) hari$/, m => 'keep ' + m[1] + ' days'],
  [/^unggah ke (.+)$/, m => 'upload to ' + m[1]],
  [/^([\\d.,]+ \\w+) bebas dari ([\\d.,]+ \\w+)$/, m => m[1] + ' free of ' + m[2]],
  [/^([\\d.,]+ \\w+) bebas · (tersedia|menipis|penuh)$/,
    m => m[1] + ' free · ' + trLabel(m[2])],
  [/^([\\d.,]+ \\w+) bebas total$/, m => m[1] + ' free in total'],
  [/^(\\d+)% sisa$/, m => m[1] + '% left'],
  [/^(\\d+) dari (\\d+) arsip · total ([\\d.,]+ \\w+)$/,
    m => m[1] + ' of ' + m[2] + ' archives · ' + m[3] + ' total'],
  [/^(\\d+) arsip · total ([\\d.,]+ \\w+)$/, m => m[1] + ' archives · ' + m[2] + ' total'],
  [/^(\\d+) arsip · ([\\d.,]+ \\w+)$/, m => m[1] + ' archives · ' + m[2]],
  [/^· (\\d+) berkas · ([\\d.,]+ \\w+)$/, m => '· ' + m[1] + ' files · ' + m[2]],
  [/^(\\d+) berkas tidak bisa dibaca dan dilewati: (.*)$/,
    m => m[1] + ' unreadable files skipped: ' + m[2]],
  [/^(.+): rclone melebihi batas waktu (\\d+)s$/,
    m => m[1] + ': rclone exceeded timeout ' + m[2] + 's'],
  [/^(\\d+) hari lalu$/, m => m[1] + ' days ago'],
  [/^(\\d+) jam lalu$/, m => m[1] + ' hours ago'],
  [/^(\\d+) menit lalu$/, m => m[1] + ' minutes ago'],
  [/^Hari ini · (.*)$/, m => 'Today · ' + m[1]],
  [/^Kemarin · (.*)$/, m => 'Yesterday · ' + m[1]],
  [/^selesai · (.*)$/, m => 'finished · ' + m[1]],
  [/^menyiapkan$/, () => 'preparing'],
  [/^menunggu$/, () => 'waiting'],
  [/^unggah ke (.*)$/, m => 'uploading to ' + m[1]],
  [/^(\\w+) · (\\d+)% · (.*)$/, (m, s) => trWord(m[1]) + ' · ' + m[2] + '% · ' + m[3]],
  [/^(\\w+) · ([\\d.,]+\\w*)$/, (m, s) => trWord(m[1]) + ' · ' + m[2]],
  [/^(\\d+) dari (\\d+) job aktif$/, m => m[1] + ' of ' + m[2] + ' jobs active'],
  [/^Aktifkan target (.+)$/, m => 'Enable target ' + m[1]],
  [/^Aktifkan (.+)$/, m => 'Enable ' + m[1]],
  [/^Target (.+)$/, m => 'Target ' + m[1]],
  [/^Job aktif$/, () => 'Active jobs'],
  [/^Kirim notifikasi$/, () => 'Send notifications'],
  [/^Token tersimpan \\((.*)\\)\\. Nilai token tidak pernah ditampilkan kembali\\.$/,
    m => 'Token stored (' + m[1] + '). The token is never shown again.'],
  // pesan ber-interpolasi (nama job, jumlah baris)
  [/^Job "(.*)" akan dihapus dari dashboard\\.$/, m => 'Job "' + m[1] + '" will be deleted from the dashboard.'],
  [/^Backup "(.*)" akan dijalankan sekarang di server\\.$/, m => 'Backup "' + m[1] + '" will run now on the server.'],
  [/^Password untuk job "(.*)" akan dihapus\\.$/, m => 'Password for job "' + m[1] + '" will be removed.'],
  [/^Job "(.*)" dihentikan\\.$/, m => 'Job "' + m[1] + '" stopped.'],
  [/^Job "(.*)" sedang tidak berjalan\\.$/, m => 'Job "' + m[1] + '" is not running.'],
  [/^(\\d+) baris riwayat berstatus gagal akan dihapus\\.$/, m => m[1] + ' failed history entries will be deleted.'],
  [/^Hapus (\\d+) run$/, m => 'Delete ' + m[1] + ' runs'],
  [/^(\\d+) riwayat dihapus\\.$/, m => m[1] + ' history entries deleted.'],
  [/^Backup dimulai: (.*)$/, m => 'Backup started: ' + m[1]],
  [/^Gagal: (.*)$/, m => 'Failed: ' + m[1]],
  [/^Gagal menghapus: (.*)$/, m => 'Failed to delete: ' + m[1]],
  [/^Gagal menyimpan: (.*)$/, m => 'Failed to save: ' + m[1]],
  [/^Gagal mengirim$/, () => 'Failed to send'],
  [/^Mengunduh (.*)…$/, m => 'Downloading ' + m[1] + '…'],
];
const PHASE_EN = {menyiapkan: 'preparing', menunggu: 'waiting', dump: 'dump',
                  unggah: 'uploading', verifikasi: 'verifying', selesai: 'done'};
function trWord(w){ return PHASE_EN[w] || w; }
const LABEL_EN = {tersedia:'available', menipis:'low', penuh:'full'};
function trLabel(w){ return LABEL_EN[w] || w; }
// hasil terjemahan ditandai supaya tidak diterjemahkan dua kali: beberapa nilai
// Inggris ("Edit job") juga merupakan kunci Indonesia yang sah.
const EN_DONE = new WeakSet();
function tr(s, obj){
  if(LANG !== 'en') return s;
  if(obj && EN_DONE.has(obj)) return s;
  let out = s;
  if(ID2EN[s]) out = ID2EN[s];
  else for(const [re, fn] of DYN){
    const m = s.match(re);
    if(m){ out = fn(m, s); break; }
  }
  if(out !== s && obj) EN_DONE.add(obj);
  return out;
}
const SKIP_TAGS = new Set(['SCRIPT','STYLE','PRE','CODE','SVG','PATH']);
function trText(node){
  if(!node || !node.nodeValue) return;
  const raw = node.nodeValue, key = raw.trim();
  if(!key) return;
  const out = tr(key, node);
  if(out && out !== key) node.nodeValue = raw.replace(key, out);
}
function trAttrs(el){
  const ph = el.getAttribute && el.getAttribute('data-i18n-ph');
  if(ph && LANG === 'en' && el.placeholder !== ph) el.placeholder = ph;
  // teks panjang/berbaris yang tidak bisa dicocokkan lewat kamus
  const full = el.getAttribute && el.getAttribute('data-i18n');
  if(full && LANG === 'en' && el.textContent.trim() !== full){
    el.textContent = full; return;
  }
  for(const a of ['title','placeholder','aria-label']){
    if(a === 'placeholder' && ph) continue;
    const v = el.getAttribute && el.getAttribute(a);
    if(!v) continue;
    const out = tr(v.trim(), el);
    if(out !== v.trim()) el.setAttribute(a, v.replace(v.trim(), out));
  }
}
function trTree(root){
  if(!root) return;
  if(root.nodeType === 3){ trText(root); return; }
  if(root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n; while((n = w.nextNode())){
    const p = n.parentElement;
    if(p && (SKIP_TAGS.has(p.tagName) || p.closest('[data-no-i18n]'))) continue;
    trText(n);
  }
  for(const el of root.querySelectorAll('[title],[placeholder],[aria-label],[data-i18n]')){
    if(!el.closest('[data-no-i18n]')) trAttrs(el);
  }
}
if(LANG === 'en'){
  document.documentElement.lang = 'en';
  new MutationObserver(muts => {
    for(const m of muts){
      if(m.type === 'characterData') trText(m.target);
      else if(m.type === 'attributes') trAttrs(m.target);
      else for(const n of m.addedNodes) trTree(n);
    }
  }).observe(document.body, {childList:true, subtree:true,
    characterData:true, attributes:true,
    attributeFilter:['title','placeholder','aria-label','data-i18n']});
  // judul & isi awal
  const bootI18n = () => { trTree(document.body); document.title = tr(document.title); };
  bootI18n();
  setTimeout(bootI18n, 60);
  setInterval(() => { document.title = tr(document.title); }, 1500);
}
async function setLang(l){
  await fetch('api/settings',{method:'POST',headers:H(),
    body:JSON.stringify({kind:'general', name:'', field:'lang', value:l})});
  location.reload();
}
function toggleLang(){ setLang(LANG === 'en' ? 'id' : 'en'); }

const TOKEN = new URLSearchParams(location.search).get('token') || TOKEN_INJECT;
const H = () => ({'X-Token': TOKEN, 'Content-Type': 'application/json'});
const esc = s => String(s ?? '').replace(/[&<>"\\u0027]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\\u0027':'&#39;'}[c]));
const IC = {
  run:'<path d="M7 4.5v15l13-7.5z"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',
  edit:'<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m14 6 4 4"/>',
  test:'<path d="M9 3h6M10 3v6l-6 9a2 2 0 0 0 1.7 3h12.6a2 2 0 0 0 1.7-3l-6-9V3"/>',
  key:'<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3"/>',
  trash:'<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/>',
  log:'<path d="M5 4h14v16H5z"/><path d="M9 9h6M9 13h6M9 17h3"/>',
  download:'<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
  check:'<path d="m5 12 5 5L19 7"/>',
  copy:'<path d="M9 9h11v11H9z"/><path d="M5 15V4h11"/>',
  shield:'<path d="M12 3 5 6v6c0 4.4 3 7.6 7 9 4-1.4 7-4.6 7-9V6z"/>',
  add:'<path d="M12 5v14M5 12h14"/>',
  refresh:'<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  pulse:'<path d="M3 12h3.5l2-5 3.5 10 2.5-6h6.5"/>',
  help:'<circle cx="12" cy="12" r="8"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .9-1 1.6v.4"/><path d="M12 17h.01"/>'
};
const icon = (n) => `<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor
 stroke-width=1.7 stroke-linecap=round stroke-linejoin=round aria-hidden=true>${IC[n]}</svg>`;
let toastT;
function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastT); toastT = setTimeout(()=>t.classList.remove('show'), 2600);
}
// Konfirmasi menggantikan confirm() bawaan: tampilannya mengikuti tema dan tidak
// bisa dimatikan browser. Mengembalikan Promise<boolean>.
let askResolve = null;
function confirmBox({title, msg, detail, ok, danger, icon}){
  asktitle.textContent = tr(title);
  askmsg.textContent = tr(msg || '');
  askdetail.textContent = tr(detail || '');
  askdetail.hidden = !detail;
  askyes.textContent = tr(ok || 'Ya, lanjutkan');
  askyes.className = 'btn ' + (danger ? 'danger' : 'primary');
  askicon.className = 'askicon ' + (danger ? 'danger' : 'info');
  askicon.innerHTML = icon === 'trash'
    ? `<svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2
        stroke-linecap=round stroke-linejoin=round aria-hidden=true>
        <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>`
    : `<svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2
        stroke-linecap=round stroke-linejoin=round aria-hidden=true>
        <path d="M12 8v5"/><path d="M12 16.5h.01"/></svg>`;
  askbox.showModal();
  askno.focus();                       // pilihan aman jadi fokus awal
  return new Promise(resolve => { askResolve = resolve; });
}
function askClose(v){
  if(!askbox.open) return;
  askbox.close();
  if(askResolve){ const r = askResolve; askResolve = null; r(v); }
}
askno.onclick = () => askClose(false);
askyes.onclick = () => askClose(true);
// Esc dan klik backdrop = batal
askbox.addEventListener('cancel', e => { e.preventDefault(); askClose(false); });
askbox.addEventListener('click', e => { if(e.target === askbox) askClose(false); });
const THEMES = ['system','light','dark'];
const SUN = '<circle cx="12" cy="12" r="4.2"/><path d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"/>';
const MOON = '<path d="M20 14.5A8 8 0 0 1 9.5 4a8.2 8.2 0 1 0 10.5 10.5z"/>';
const AUTO = '<circle cx="12" cy="12" r="8"/><path d="M12 4v16" /><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/>';
function theme(){ try{ return localStorage.getItem('theme') || 'system'; }catch(e){ return 'system'; } }
function applyTheme(t){
  const root = document.documentElement;
  if(t === 'system') delete root.dataset.theme; else root.dataset.theme = t;
  const btn = document.getElementById('themebtn');
  btn.innerHTML = `<svg class=ic viewBox="0 0 24 24" fill=none stroke=currentColor
    stroke-width=1.8 stroke-linecap=round stroke-linejoin=round aria-hidden=true>${
    t === 'dark' ? MOON : t === 'light' ? SUN : AUTO}</svg>`;
  const names = {system:'tema mengikuti sistem', light:'tema terang', dark:'tema gelap'};
  btn.title = names[t] + ' — klik untuk ganti';
  btn.setAttribute('aria-label', btn.title);
  const tag = document.getElementById('langtag');
  if(tag) tag.textContent = LANG.toUpperCase();
}
function cycleTheme(){
  const next = THEMES[(THEMES.indexOf(theme()) + 1) % THEMES.length];
  try{ localStorage.setItem('theme', next); }catch(e){}
  applyTheme(next);
  toast(next === 'system' ? 'Tema mengikuti sistem.' : next === 'dark' ? 'Tema gelap.' : 'Tema terang.');
}
applyTheme(theme());
let VIEW = 'overview';
// judul tab + judul halaman + tombol utama mengikuti halaman yang dibuka
const VIEWS = {
  overview: {title:'Ringkasan backup', tab:'Ikhtisar'},
  jobs:     {title:'Job backup', tab:'Job', action:'Tambah job', ic:'add'},
  archives: {title:'Arsip tersimpan', tab:'Arsip', action:'Muat ulang', ic:'refresh'},
  history:  {title:'Riwayat run', tab:'Riwayat', action:'Muat ulang', ic:'refresh'}
};
const ACTIONS = {jobs:()=>openAdd(), archives:()=>reloadArchives(), history:()=>tick()};
function setView(v){
  if(!VIEWS[v]) v = 'overview';
  VIEW = v;
  for(const id of ['overview','jobs','archives','history']){
    document.getElementById('v-'+id).hidden = (id !== v);
  }
  for(const b of document.querySelectorAll('.nav button'))
    b.classList.toggle('on', b.dataset.view === v);
  const meta = VIEWS[v];
  document.title = tr(meta.tab) + ' · arkivo';
  pagetitle.textContent = meta.title;
  // Ikhtisar hanya membaca: tidak ada aksi tambah job di sini
  pageaction.hidden = !meta.action;
  if(meta.action){
    pageaction.innerHTML = icon(meta.ic) + meta.action;
    pageaction.onclick = ACTIONS[v];
  }
  if(v === 'archives') renderArchives();
  history.replaceState(null, '', '#' + v);
  window.scrollTo({top:0, behavior:'smooth'});
}
function reloadArchives(){ ARCHIVES = {}; renderArchives(); }
function blankState(title, hint, action){
  return `<div class=empty><strong>${esc(title)}</strong>${esc(hint)}
    ${action ? `<div>${action}</div>` : ''}</div>`;
}
function statusChip(r){
  const running = r.status === 'running';
  const cls = running ? 'run' : r.status === 'success' ? 'ok'
    : r.status === 'partial' ? 'warn' : 'bad';
  const label = running ? 'berjalan' : r.status === 'success' ? 'berhasil'
    : r.status === 'partial' ? 'sebagian' : r.status === 'failed' ? 'gagal' : r.status;
  return `<span class="chip ${cls}"><i class="dot ${cls}"></i>${label}</span>`;
}
function fmtDur(s){
  if(s == null) return '—';
  if(s < 10) return s.toFixed(1).replace('.',',') + 's';
  if(s < 60) return Math.round(s) + 's';
  const m = Math.floor(s/60);
  return s < 3600 ? `${m}m ${Math.round(s%60)}s` : `${Math.floor(m/60)}j ${m%60}m`;
}
function fmtBytes(n){
  const u = ['B','KB','MB','GB','TB']; let i = 0; n = n || 0;
  while(n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
  return (i === 0 ? Math.round(n) : n.toFixed(1).replace('.',',')) + ' ' + u[i];
}
let FLOOR = FLOOR_INJECT;   // min_free_percent dari config
function diskState(d){
  if(!d || !d.total) return 'ok';
  const pct = d.used / d.total * 100;
  return pct >= 100 - FLOOR ? 'bad' : pct >= 100 - FLOOR * 2 ? 'warn' : 'ok';
}
function bar(d, cls){
  if(!d || !d.total) return '<div class="capbar empty-bar" aria-hidden=true></div>';
  const pct = Math.min(100, d.used / d.total * 100);
  return `<div class="capbar ${cls || ''}" title="terpakai ${pct.toFixed(1).replace('.',',')}%">
    <i class="${diskState(d)}" style="width:${pct}%"></i></div>`;
}
function diskCell(t){
  const d = t.disk;
  if(!d || !d.total) return '<span class=dim>tidak terbaca</span>';
  const st = diskState(d);
  const label = st === 'bad' ? 'penuh' : st === 'warn' ? 'menipis' : 'tersedia';
  // satu string utuh (bukan dua potongan) supaya penerjemah bisa mencocokkan pola
  return bar(d) + '<div class="meta">' + tr(`${fmtBytes(d.free)} bebas · ${label}`)
    .replace(label, `<span class="${st}">${label}</span>`) + '</div>';
}
function capNote(t){
  const d = t.disk;
  if(!d || !d.total) return '<span class="tag">tidak terbaca</span>';
  const st = diskState(d), pct = (d.free / d.total * 100);
  const tag = st === 'bad' ? `<span class="tag bad">${pct.toFixed(0)}% sisa</span>`
    : st === 'warn' ? `<span class="tag warn">${pct.toFixed(0)}% sisa</span>`
    : `<span class="tag ok">${pct.toFixed(0)}% sisa</span>`;
  return tag + (100 - pct <= FLOOR ? ' <span class="dim">di bawah batas</span>' : '');
}
function ago(iso){
  if(!iso) return 'belum pernah';
  const t = new Date(iso).getTime();
  if(isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if(s < 90) return 'baru saja';
  if(s < 3600) return Math.round(s/60) + ' menit lalu';
  if(s < 86400) return Math.round(s/3600) + ' jam lalu';
  const d = Math.round(s/86400);
  return d === 1 ? 'kemarin' : d + ' hari lalu';
}
function lastRunByJob(runs){
  const m = {};
  for(const r of runs) if(!m[r.job]) m[r.job] = r;
  return m;
}
let STATE = {jobs:[], targets:[], runs:[]};
let ARCHIVES = {};
async function tick(){
  const d = await (await fetch('api/status',{headers:H()})).json();
  STATE = d;
  const last = lastRunByJob(d.runs);
  const active = d.jobs.filter(j=>j.enabled).length;
  const recent = d.runs.filter(r => r.status !== 'running').slice(0, 30);
  const okN = recent.filter(r=>r.status==='success').length;
  const partN = recent.filter(r=>r.status==='partial').length;
  const badN = recent.filter(r=>r.status!=='success' && r.status!=='partial').length;
  const newest = d.runs.find(r=>r.status !== 'running');
  const totalSize = d.runs.reduce((a,r)=>a + (r.bytes || 0), 0);
  const archN = d.runs.reduce((a,r)=>a + (r.artifacts ? r.artifacts.length : 0), 0);
  const rate = recent.length ? Math.round(((okN + partN) / recent.length) * 100) : 0;
  subtitle.textContent = new Date().toLocaleDateString(LANG === 'en' ? 'en-GB' : 'id-ID',
    {weekday:'long', day:'numeric', month:'long', year:'numeric'})
    + (LANG === 'en' ? ' · ' + active + ' of ' + d.jobs.length + ' active jobs'
                     : ' · ' + active + ' dari ' + d.jobs.length + ' job aktif');
  kpis.innerHTML = [
    kpi('Job aktif', active + '<small>/' + d.jobs.length + '</small>',
        badN ? badN + ' run gagal terakhir' : 'semua job terjadwal'),
    kpi('Backup terakhir', newest ? ago(newest.started) : '—',
        newest ? esc(newest.job) + ' · ' + statusLabel(newest.status) : 'belum ada run'),
    kpi('Arsip di riwayat', String(archN), fmtBytes(totalSize) + ' total'),
    kpi('Keberhasilan', recent.length ? rate + '<small>%</small>' : '—',
        `${okN} berhasil · ${partN} sebagian · ${badN} gagal`)
  ].join('');
  if(recent.length){
    const mix = `<div class=mix>
      <i class=s style="width:${okN/recent.length*100}%"></i>
      <i class=p style="width:${partN/recent.length*100}%"></i>
      <i class=f style="width:${badN/recent.length*100}%"></i></div>
      <div class=legend><span><b>${okN}</b> berhasil</span>
      <span><b>${partN}</b> sebagian</span><span><b>${badN}</b> gagal</span></div>`;
    kpis.lastElementChild.insertAdjacentHTML('beforeend',
      '<div class=mixwrap>' + mix + '</div>');
  }

  jobsub.textContent = d.jobs.length + ' job';
  jobcount.textContent = d.jobs.length + ' total';
  targetcount.textContent = d.targets.length + ' tujuan';
  // JANGAN kosongkan ARCHIVES di sini: dialog arsip menyimpan indeks ke cache ini,
  // dan tick() jalan tiap 30 detik — mengosongkannya membuat tombol unduh/verifikasi
  // diam saja. Cache dimuat ulang saat dialog dibuka dan setelah hapus arsip.

  const rows = (d.jobs.length ? '<div class=scroll><table><thead><tr><th>Job'
    + '<th class=hide-sm>Database / sumber</th><th>Terakhir<th>Berikutnya'
    + '<th>Jadwal / retensi<th>Aktif<th style="text-align:right">Aksi'
    + '<tbody>' + d.jobs.map(j=>{
      const r = last[j.name];
      return `<tr>
      <td><button class=jobname onclick="openArchives('${esc(j.name)}')"
            title="Lihat arsip job ini">${esc(j.name)}</button>
        ${j.needs_password ? '<div class="tag warn" style="margin-top:5px">perlu password</div>' : ''}
        <div class=meta>${esc(j.type)} · ${esc(j.source)}</div>
      <td class="dim hide-sm">${esc(j.databases) || '*'}
      <td>${r && r.status==='running'
        ? `<button class="chip run chipbtn" onclick="liveLog('${esc(j.name)}')"
             title="Lihat proses berjalan"><i class="dot run"></i>berjalan${icon('pulse')}</button>
           <div class="meta prog" data-job="${esc(j.name)}"></div>`
        : `${r ? statusChip(r) : `<span class="chip idle"><i class="dot"></i>belum pernah</span>`}
           <div class="meta prog" data-job="${esc(j.name)}">${r ? esc(r.started) : ''}</div>`}
      <td class=dim>${esc(j.next) || '—'}
        ${j.enabled && j.next_in != null
          ? `<div class=meta>${tr('dalam')} ${fmtDur(j.next_in)}</div>` : ''}
      <td><div>${esc(j.schedule)}</div>
        <div class="meta">${tr(`simpan ${j.retention_days} hari`)}</div>
      <td><label class=switch><input type=checkbox ${j.enabled?'checked':''}
           aria-label="Aktifkan ${esc(j.name)}"
           onchange="save('job','${esc(j.name)}','enabled',this.checked)"><i></i></label>
      <td><div class=actions>
        ${r && r.status==='running'
          ? `<button class="btn sm" onclick="cancelIt('${esc(j.name)}')" title="Hentikan">${icon('stop')}</button>`
          : `<button class="btn sm" onclick="runIt('${esc(j.name)}')" title="Jalankan sekarang">${icon('run')}</button>`}
        <button class="btn sm" onclick="openArchives('${esc(j.name)}')" title="Lihat arsip">${icon('log')}</button>
        <button class="btn sm" onclick="openEdit('${esc(j.name)}')" title="Edit job">${icon('edit')}</button>
        <button class="btn sm" onclick="testJob('${esc(j.name)}')" title="Uji koneksi">${icon('test')}</button>
        ${j.type != 'path' ? `<button class="btn sm" onclick="pwDialog('${esc(j.name)}',${j.has_password})"
           title="${esc(j.password_from_dashboard ? 'password dari dashboard' : (j.has_password ? 'password dari config.toml / berkas kredensial klien' : 'belum ada password'))}"
           >${icon('key')}</button>` : ''}
        <button class="btn sm danger" onclick="delJob('${esc(j.name)}')" title="Hapus job">${icon('trash')}</button>
        </div></tr>`;
    }).join('') + '</table></div>'
    : blankState('Belum ada job',
        'Tambahkan sumber backup pertama: database atau folder project.',
        `<button class="btn primary" onclick="openAdd()">Tambah job</button>`));
  jobs.innerHTML = rows;
  // Ikhtisar hanya informasi: aksi backup & arsip ada di halaman Job.
  jobsshort.innerHTML = d.jobs.length
    ? '<div class=scroll><table><tbody>' + d.jobs.slice(0,5).map(j=>{
        const r = last[j.name];
        return `<tr><td><div class=jobname style="cursor:default">${esc(j.name)}</div>
          <div class=meta>${esc(j.type)} · ${esc(j.schedule)} · ${tr(`simpan ${j.retention_days} hari`)}</div>
          <td>${r ? statusChip(r) : '<span class="chip idle"><i class="dot"></i>belum pernah</span>'}
          <td class=dim>${esc(j.next) || '—'}
        ${j.enabled && j.next_in != null
          ? `<div class=meta>${tr('dalam')} ${fmtDur(j.next_in)}</div>` : ''}</tr>`;
      }).join('') + '</tbody></table></div>'
    : blankState('Belum ada job', 'Tambahkan job dari halaman Job.');

  targets.innerHTML = d.targets.length ? '<table><thead><tr><th>Target<th>Jalur'
    + '<th class=hide-sm>Kapasitas<th>Aktif<th style="text-align:right">Aksi'
    + '<tbody>' + d.targets.map(t=>`<tr><td>${esc(t.name)}<div class=meta>${esc(t.type)}</div>
      <td class="mono path" title="${esc(t.path)}">${esc(t.path)}
      <td class="hide-sm">${t.enabled ? diskCell(t) : '<span class=dim>nonaktif</span>'}
      <td><label class=switch><input type=checkbox ${t.enabled?'checked':''}
           aria-label="Aktifkan target ${esc(t.name)}"
           onchange="save('target','${esc(t.name)}','enabled',this.checked)"><i></i></label>
      <td><div class=actions>
        <button class="btn sm" onclick="openTarget('${esc(t.name)}')"
          title="Edit jalur target">${icon('edit')}</button>
        </div>`).join('')
    + '</table>' : blankState('Belum ada target', 'Tambahkan [[targets]] di config.toml.');

  caps.innerHTML = d.targets.length ? d.targets.map(t=>`<div class=caprow>
    <div class=caphead><b>${esc(t.name)}</b>
      <span class=dim>${esc(t.type)} · ${esc(t.path)}</span>
      <span class=spacer></span>${t.enabled ? capNote(t) : '<span class=dim>nonaktif</span>'}</div>
    ${t.disk ? bar(t.disk) : '<div class="capbar empty-bar" aria-hidden=true></div>'}
    <div class=capfoot>${!t.enabled ? '<span class=dim>target dimatikan</span>'
      : t.disk ? `${fmtBytes(t.disk.free)} bebas dari ${fmtBytes(t.disk.total)}`
      : '<span class=dim>kapasitas tidak terbaca</span>'}</div></div>`).join('')
    : blankState('Belum ada target', 'Tambahkan [[targets]] di config.toml.');
  const fn = d.targets.filter(t=>t.disk);
  capsub.textContent = fn.length
    ? fmtBytes(fn.reduce((a,t)=>a + t.disk.free, 0)) + ' bebas total' : '';

  // kotak kapasitas memperingatkan sebelum disk penuh, bukan setelah upload gagal
  const low = d.targets.filter(t=>t.enabled && t.disk && diskState(t.disk) !== 'ok');
  capbox.hidden = !low.length;
  capbox.innerHTML = low.length ? `<section class=panel style="margin-bottom:16px">
    <div class=phead><h2>Kapasitas menipis</h2>
    <span class="tag ${low.some(t=>diskState(t.disk)==='bad')?'bad':'warn'}">
      ${low.length} target</span><span class=spacer></span>
    <button class="btn ghost sm" onclick="setView('jobs')">Kelola target</button></div>
    <div class=pbody>` + low.map(t=>`<div class=diagrow>
      <i class="dot ${diskState(t.disk)}"></i><div>
      <div class=diagtitle>${esc(t.name)}: ${fmtBytes(t.disk.free)} bebas</div>
      <div class=diagdetail>${(t.disk.free / t.disk.total * 100).toFixed(1)}% sisa dari
        ${fmtBytes(t.disk.total)} · batas ${FLOOR}% (min_free_percent)</div></div></div>`
      ).join('') + '</div></section>' : '';

  feed.innerHTML = d.runs.length ? d.runs.slice(0,7).map(r=>`<div class=feedrow>
    <span class=avatar>${esc((r.job || '?').slice(0,2).toUpperCase())}</span>
    <div><div class=t>${esc(r.job)}</div><div class=m>${esc(r.started)} · ${esc(r.size)}</div></div>
    ${statusChip(r)}</div>`).join('')
    : blankState('Belum ada aktivitas', 'Run akan muncul setelah job pertama berjalan.');

  runs.innerHTML = d.runs.length ? '<div class=scroll><table>'
    + '<thead><tr><th>Job<th>Mulai<th class=hide-sm>Status<th>Ukuran<th class="hide-sm">Durasi'
    + '<th style="text-align:right">Aksi'
    + '<tbody>' + d.runs.map(r=>`<tr>
      <td>${esc(r.job)}
      <td class="dim num">${esc(r.started)}
      <td class=hide-sm>${statusChip(r)}
        ${r.error ? `<div class="meta err" title="${esc(r.error)}">${esc(errLine(r.error))}</div>` : ''}
      <td class=num>${esc(r.size)}
      <td class="dim num hide-sm">${fmtDur(r.duration)}
      <td><div class=actions>
        <button class="btn sm" title="Lihat log"
          onclick="${r.running ? `liveLog('${esc(r.job)}')` : `showLog(${r.id},'${esc(r.job)}')`}">${icon('log')}</button>
        ${r.artifacts.length ? `<button class="btn sm" onclick="dl('${esc(r.artifacts[0])}')" title="Unduh arsip">${icon('download')}</button>
         <button class="btn sm danger" onclick="delArt('${esc(r.artifacts[0])}')" title="Hapus arsip">${icon('trash')}</button>` : ''}
        ${r.running ? '' : `<button class="btn sm danger" onclick="delRun(${r.id})"
          title="Hapus riwayat">${icon('trash')}</button>`}
        </div></tr>`).join('') + '</table></div>'
    : blankState('Belum ada riwayat', 'Riwayat run akan muncul setelah job pertama berjalan.');
  clearfailed.hidden = !d.runs.some(r=>r.status === 'failed');
  if(VIEW === 'archives' && !archiveall.querySelector('table')) renderArchives();
  paintProgress();   // tick() menggambar ulang baris: pasang kembali badge proses
}
function statusLabel(s){
  const id = s === 'success' ? 'berhasil' : s === 'partial' ? 'sebagian'
    : s === 'failed' ? 'gagal' : s;
  return tr(id);
}
// penyebab kegagalan sering multiline (stderr tar/ssh); ambil baris pertama saja
function errLine(e){
  const first = String(e || '').split('\\n')[0].replace(/^\\w+ gagal:\\s*/, '').trim();
  return first.length > 68 ? first.slice(0, 67) + '…' : first;
}
function kpi(k, v, foot){
  return `<div class=kpi><div class=k><span>${k}</span></div>
    <div class=v>${v}</div><div class=f>${foot}</div></div>`;
}
function loadingBlock(label){
  dlgt.textContent = label;
  showPane(`<div class=resnote run><i class="dot run"></i><div>
    <b>Memeriksa…</b><div class=detail>Menjalankan diagnostik.</div></div></div>`);
}
async function showDiag(deep){
  if(!dlg.open) dlg.showModal();
  loadingBlock('Diagnostik');
  let d;
  try{
    d = await (await fetch('api/diagnose' + (deep ? '?deep=1' : ''),
                          {headers:H()})).json();
  }catch(e){
    d = {items:[{level:'bad', title:'tidak bisa menghubungi dashboard',
                 detail:String(e)}]};
  }
  const bad = d.items.filter(i=>i.level==='bad').length;
  const warn = d.items.filter(i=>i.level==='warn').length;
  const en = LANG === 'en';
  dlgt.textContent = (en ? 'Diagnostics · ' : 'Diagnostik · ') + (bad
    ? bad + (en ? ' issues' : ' masalah')
    : warn ? warn + (en ? ' warnings' : ' peringatan')
    : (en ? 'all good' : 'semua baik'));
  showPane(`<div class=scrolllist>` + d.items.map(i=>`<div class=diagrow>
      <i class="dot ${i.level}"></i><div><div class=diagtitle>${esc(i.title)}</div>
      ${i.detail ? `<div class=diagdetail>${esc(i.detail)}</div>` : ''}</div></div>`).join('')
    + `</div><div class=sheetfoot style="margin-top:10px">
      <button class=btn onclick="showDiag(true)">${icon('shield')}Uji arsip terbaru</button>
      <button class=btn onclick="dlg.close()">Tutup</button></div>`);
}
function fType(){
  const isPath = (f_type.value == 'path');
  const isSsh  = (f_transport.value == 'ssh');
  for(const id of ['r_db','r_port','r_user','r_dbs'])
    document.getElementById(id).hidden = isPath;
  document.getElementById('r_path').hidden = !isPath;
  document.getElementById('r_exc').hidden  = !isPath;
  for(const el of document.querySelectorAll('.r_ssh')) el.hidden = !isSsh;
  f_port.placeholder = f_type.value == 'postgres' ? '5432' : '3306';
  f_user.placeholder = f_type.value == 'postgres' ? 'postgres' : 'root';
}
let EDIT = null;   // null = tambah baru; nama job = sedang mengedit

function formBody(){
  return {name:f_name.value, type:f_type.value, transport:f_transport.value,
    ssh_host:f_ssh_host.value, ssh_user:f_ssh_user.value, ssh_port:f_ssh_port.value,
    ssh_key:f_ssh_key.value, host:f_host.value, port:f_port.value, user:f_user.value,
    databases:f_databases.value, path:f_path.value, exclude:f_exclude.value,
    schedule:f_schedule.value, retention_days:f_retention.value};
}
function fillForm(j){
  f_name.value=j.name||''; f_type.value=j.type||'postgres';
  f_transport.value=j.ssh_host ? 'ssh' : 'local';
  f_ssh_host.value=j.ssh_host||''; f_ssh_user.value=j.ssh_user||'';
  f_ssh_port.value=j.ssh_port||''; f_ssh_key.value=j.ssh_key||'';
  f_host.value=j.host||''; f_port.value=j.port||''; f_user.value=j.user||'';
  f_databases.value=j.databases==='' ? '' : (j.databases||'*');
  f_path.value=j.path||''; f_exclude.value=(j.exclude||[]).join(', ');
  f_schedule.value=j.schedule||'manual';
  f_retention.value=j.retention_days||14;
  fType();
}
function openAdd(){
  EDIT=null; add_title.textContent='Tambah job';
  ['f_name','f_ssh_host','f_ssh_user','f_ssh_port','f_ssh_key','f_host','f_port',
   'f_user','f_databases','f_path','f_exclude'].forEach(i=>document.getElementById(i).value='');
  f_type.value='postgres'; f_transport.value='local';
  f_schedule.value='daily 02:00'; f_retention.value=14;
  clearNote(); save_btn.disabled=false; fType(); add.showModal(); f_name.focus();
}
async function openEdit(n){
  const d = await (await fetch('api/status',{headers:H()})).json();
  const j = d.jobs.find(x=>x.name===n);
  if(!j){ toast('Job tidak ditemukan.'); return; }
  EDIT = j;
  add_title.textContent = 'Edit job · ' + n;
  clearNote(); fillForm(j); save_btn.disabled=false; add.showModal();
}
async function saveJob(){
  const body = formBody();
  const url = EDIT ? 'api/jobs/edit' : 'api/jobs';
  if(EDIT) body.orig = EDIT.name;
  save_btn.disabled = true; clearNote();
  let d;
  try{
    d = await (await fetch(url,{method:'POST',headers:H(),
      body:JSON.stringify(body)})).json();
  } finally { save_btn.disabled = false; }
  if(!d.ok){ noteBlock('bad', 'Gagal menyimpan', d.error || ''); return; }
  add.close(); toast(EDIT ? 'Perubahan tersimpan.' : 'Job tersimpan.'); EDIT=null;
  tick();
}
function noteBlock(kind, title, detail){
  addres.hidden = false;
  addres.className = 'resnote ' + kind;
  addres.innerHTML = `<i class="dot ${kind === 'ok' ? 'ok' : kind === 'bad' ? 'bad'
    : kind === 'warn' ? 'warn' : 'run'}"></i><div><b>${esc(title)}</b>
    ${detail ? `<div class=detail>${esc(detail)}</div>` : ''}</div>`;
}
function clearNote(){ addres.hidden = true; addres.innerHTML = ''; }
let TARGET = null;
function openTarget(name){
  const t = STATE.targets.find(x=>x.name === name);
  if(!t){ toast('Target tidak ditemukan.'); return; }
  TARGET = t;
  t_title.textContent = 'Edit target · ' + name;
  t_path.value = t.path;
  t_hint.textContent = t.type === 'local'
    ? 'Jalur absolut di mesin ini. Direktori dibuat otomatis saat run pertama.'
    : 'Remote rclone, format remote:tujuan (diatur di `rclone config`).';
  t_res.hidden = true; t_res.innerHTML = ''; t_save.disabled = false;
  tdlg.showModal(); t_path.focus(); t_path.select();
}
async function saveTarget(){
  if(!TARGET) return;
  const val = t_path.value.trim();
  if(val === TARGET.path){ tdlg.close(); return; }
  t_save.disabled = true;
  t_res.hidden = false; t_res.className = 'resnote run';
  t_res.innerHTML = '<i class="dot run"></i><div><b>Menyimpan…</b></div>';
  const d = await (await fetch('api/settings',{method:'POST',headers:H(),
    body:JSON.stringify({kind:'target',name:TARGET.name,field:'path',value:val})})).json();
  t_save.disabled = false;
  if(!d.ok){
    t_res.className = 'resnote bad';
    t_res.innerHTML = `<i class="dot bad"></i><div><b>Gagal menyimpan</b>
      <div class=detail>${esc(d.error || 'tidak diketahui')}</div></div>`;
    return;
  }
  tdlg.close(); toast('Jalur target tersimpan.');
  TARGET = null; tick();
}
async function testCurrent(){
  if(!EDIT){
    noteBlock('warn', 'Simpan job dulu',
      'Uji koneksi butuh job yang sudah tersimpan. Klik Simpan, lalu uji lagi dari halaman Job.');
    return;
  }
  test_btn.disabled = true;
  noteBlock('run', 'Menguji koneksi…', 'Menghubungi ' + EDIT.name + ' dengan pengaturan tersimpan.');
  let d;
  try{
    d = await (await fetch('api/test',{method:'POST',headers:H(),
      body:JSON.stringify({name:EDIT.name})})).json();
  }catch(e){
    d = {ok:false, error:'tidak bisa menghubungi dashboard'};
  }finally{ test_btn.disabled = false; }
  if(d.ok) noteBlock('ok', 'Koneksi berhasil', d.message || '');
  else noteBlock('bad', 'Koneksi gagal', d.message || d.error || 'tidak ada keterangan');
}
function resultBlock(ok, title, body){
  return `<div class="resnote ${ok?'ok':'bad'}"><i class="dot ${ok?'ok':'bad'}"></i><div>
    <b>${esc(title)}</b>${body ? `<div class=detail>${esc(body)}</div>` : ''}</div></div>`;
}
function showPane(html){
  liveline.hidden = true; livefoot.hidden = true;
  dlgb.hidden = true; dlgb.innerHTML = '';
  dlgr.hidden = false; dlgr.innerHTML = html;
}
function showText(text){
  liveline.hidden = true; livefoot.hidden = true;
  dlgr.hidden = true; dlgr.innerHTML = '';
  dlgb.hidden = false; dlgb.textContent = text;
}
async function testJob(n){
  dlgt.textContent = tr('Uji koneksi · ') + n;
  showPane('<div class="resnote run"><i class="dot run"></i><div><b>Menguji koneksi…</b>' +
    '<div class=detail>Menghubungi sumber backup dengan pengaturan tersimpan.</div></div></div>');
  dlg.showModal();
  let d;
  try{
    d = await (await fetch('api/test',{method:'POST',headers:H(),
      body:JSON.stringify({name:n})})).json();
  }catch(e){
    d = {ok:false, error:'tidak bisa menghubungi dashboard'};
  }
  showPane(resultBlock(d.ok, d.ok ? 'Koneksi berhasil' : 'Koneksi gagal',
    d.message || d.error || 'tidak ada keterangan'));
}
async function delJob(n){
  const ok = await confirmBox({title:'Hapus job?', danger:true, icon:'trash',
    msg:`Job "${n}" akan dihapus dari dashboard.`,
    detail:'Arsip yang sudah tersimpan tidak ikut terhapus. Job dari config.toml '
         + 'hanya dinonaktifkan, bukan dihapus dari berkas config.',
    ok:'Hapus job'});
  if(!ok) return;
  const d = await (await fetch('api/jobs/delete',{method:'POST',headers:H(),
    body:JSON.stringify({name:n})})).json();
  if(!d.ok) toast('Gagal menghapus: '+(d.error||'')); else { toast('Job dihapus.'); tick(); }
}
const HELP = {
id:`TAMBAH JOB
  Tombol "Tambah job". PostgreSQL/MySQL/MariaDB = backup database; "Project /
  file" untuk folder. Pilih "Server lain (SSH)" bila sumbernya di mesin lain,
  lalu isi ssh host/user (kunci SSH opsional).
  Password database diisi lewat tombol kunci di baris job.

ARSIP & RESTORE
  Klik nama job (atau menu Arsip) untuk melihat semua arsip yang tersimpan,
  urut terbaru. Filter dengan tanggal bila mencari backup hari tertentu.
  Unduh arsipnya, lalu pulihkan:
  Postgres: ./restore.sh pg <file> <db>
  MySQL:    ./restore.sh my <file> [db]
  File:     ./restore.sh tar <file> <dir>
  Tombol centang memeriksa arsip masih bisa dibaca (zstd -t / pg_restore -l).

BEBERAPA DATABASE / BEBERAPA SERVER
  Tiap job berdiri sendiri: satu job bisa berisi beberapa database (isi kolom
  Database: "app, crm"), dan beberapa job bisa menunjuk server berbeda.

JADWAL
  manual | hourly | every30m | every6h | daily 02:00 | weekly 6 23:00  (6 = Sabtu)

DIAGNOSTIK
  Memeriksa tool, versi pg_dump vs pg_restore, remote rclone, sisa disk,
  keutuhan state.db, dan sisa file sementara. "Uji arsip terbaru" membaca ulang
  arsip untuk memastikan benar-benar bisa dipulihkan.`,
en:`ADD A JOB
  "Add job". PostgreSQL/MySQL/MariaDB backs up a database; "Project / file"
  backs up a folder. Pick "Other server (SSH)" when the source lives on another
  machine, then fill in the ssh host/user (SSH key optional).
  Database passwords are set with the key button on the job row.

ARCHIVES & RESTORE
  Click a job name (or the Archives tab) to list every stored archive, newest
  first. Filter by date when you need a specific day.
  Download the archive, then restore:
  Postgres: ./restore.sh pg <file> <db>
  MySQL:    ./restore.sh my <file> [db]
  File:     ./restore.sh tar <file> <dir>
  The check button verifies the archive is still readable (zstd -t / pg_restore -l).

SEVERAL DATABASES / SEVERAL SERVERS
  Each job stands alone: one job can cover several databases (fill the Database
  field with "app, crm"), and several jobs can point at different servers.

SCHEDULE
  manual | hourly | every30m | every6h | daily 02:00 | weekly 6 23:00  (6 = Saturday)

DIAGNOSTICS
  Checks tools, pg_dump vs pg_restore versions, rclone remotes, free disk,
  state.db integrity, and leftover temporary files. "Verify latest archive"
  reads the archive back to prove it can actually be restored.`
};
async function showHelp(){
  dlgt.textContent = LANG === 'en' ? 'Quick guide' : 'Panduan singkat';
  showText(HELP[LANG === 'en' ? 'en' : 'id']);
  dlg.showModal();
}
async function pwDialog(n, has){
  dlgt.textContent = tr('Password · ') + n;
  showPane(`<p class=dim style="margin:0 0 12px;font-size:12.5px">Nilai tersimpan tidak
     pernah ditampilkan kembali. Mengosongkan lalu simpan akan menghapus password.</p>
     <p style="margin:0 0 14px;font-size:13px">Status:
     ${has ? '<span class="tag ok">sudah ada</span>' : '<span class="tag warn">belum ada</span>'}</p>
     <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
     <input type=password id=pw_in placeholder="password baru" autocomplete=new-password
       style="flex:1;min-width:180px">
     <button class="btn primary" id=pw_btn>Simpan</button>
     ${has ? '<button class="btn danger" id=pw_del>Hapus password</button>' : ''}</div>
     <p id=pw_msg class=res></p>`);
  const send = async (pw) => {
    const d = await (await fetch('api/password', {method:'POST', headers:H(),
      body: JSON.stringify({name:n, password:pw})})).json();
    const m = document.getElementById('pw_msg');
    m.className = 'res ' + (d.ok ? 'ok' : 'bad');
    m.textContent = d.ok
      ? (pw ? 'Tersimpan di data/secrets.json (izin 600).' : 'Password dihapus.')
      : ('Gagal: ' + (d.error||''));
    if(d.ok){ setTimeout(()=>{dlg.close(); tick();}, 700); }
  };
  document.getElementById('pw_btn').onclick = () => send(document.getElementById('pw_in').value);
  const del = document.getElementById('pw_del');
  if(del) del.onclick = async () => {
    const ok = await confirmBox({title:'Hapus password?', danger:true, icon:'trash', ok:'Hapus password',
      msg:`Password untuk job "${n}" akan dihapus.`,
      detail:'Backup berikutnya akan memakai kredensial dari config.toml atau berkas '
           + 'kredensial klien (.pgpass / .my.cnf).'});
    if(ok) send('');
  };
  document.getElementById('pw_in').onkeydown = e => { if(e.key==='Enter') send(e.target.value); };
  dlg.showModal();
  document.getElementById('pw_in').focus();
}
async function save(kind,name,field,value){
  const r = await fetch('api/settings',{method:'POST',headers:H(),
    body:JSON.stringify({kind,name,field,value:String(value)})});
  const d = await r.json();
  if(!d.ok) toast('Gagal menyimpan: '+(d.error||'')); else { toast('Tersimpan.'); tick(); }
}
async function runIt(n){
  const ok = await confirmBox({title:'Jalankan backup?', ok:'Jalankan backup',
    msg:`Backup "${n}" akan dijalankan sekarang di server.`});
  if(!ok) return;
  const r = await fetch('api/run?job='+encodeURIComponent(n),{method:'POST',headers:H()});
  const d = await r.json();
  toast(d.started ? 'Backup dimulai: '+n : 'Gagal: '+(d.error||'job sedang berjalan'));
  setTimeout(tick,1500); setTimeout(tick,6000);
}
async function cancelIt(n){
  const r = await fetch('api/cancel?job='+encodeURIComponent(n),{method:'POST',headers:H()});
  const d = await r.json();
  toast(d.cancelled ? 'Job "'+n+'" dihentikan.' : 'Job "'+n+'" sedang tidak berjalan.');
}
async function dl(f){
  // jangan lewat blob: file arsip bisa berukuran GB, dan revokeObjectURL tepat
  // setelah click membatalkan unduhan di sebagian browser. Server sudah mengirim
  // Content-Disposition, jadi tautan biasa cukup.
  const a = document.createElement('a');
  a.href = 'api/download?file=' + encodeURIComponent(f)
         + '&token=' + encodeURIComponent(TOKEN);
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('Mengunduh ' + f.split('/').pop() + '…');
}
async function delArt(f){
  const ok = await confirmBox({title:'Hapus arsip?', danger:true, icon:'trash', ok:'Hapus arsip',
    msg:'Arsip ini akan dihapus permanen dari target penyimpanan.',
    detail:f});
  if(!ok) return;
  const r = await fetch('api/delete',{method:'POST',headers:H(),body:JSON.stringify({file:f})});
  const d = await r.json();
  if(!d.ok) toast('Gagal menghapus: '+(d.error||''));
  else { toast('Arsip dihapus.'); if(CURRENT) loadArchives(CURRENT); tick(); }
}
async function delRun(id){
  const ok = await confirmBox({title:'Hapus riwayat?', danger:true, icon:'trash',
    ok:'Hapus riwayat', msg:'Satu baris riwayat run akan dihapus.',
    detail:'Arsip yang sudah tersimpan tidak ikut terhapus. Log run ini juga hilang.'});
  if(!ok) return;
  const r = await fetch('api/runs/delete',{method:'POST',headers:H(),
    body:JSON.stringify({id})});
  const d = await r.json();
  if(!d.ok) toast('Gagal menghapus: '+(d.error||''));
  else { toast('Riwayat dihapus.'); tick(); }
}
async function delFailed(){
  const n = STATE.runs.filter(r=>r.status === 'failed').length;
  const ok = await confirmBox({title:'Hapus riwayat gagal?', danger:true, icon:'trash',
    ok:'Hapus '+n+' run', msg:`${n} baris riwayat berstatus gagal akan dihapus.`,
    detail:'Arsip yang sudah tersimpan tidak ikut terhapus.'});
  if(!ok) return;
  const r = await fetch('api/runs/delete',{method:'POST',headers:H(),
    body:JSON.stringify({status:'failed'})});
  const d = await r.json();
  if(!d.ok) toast('Gagal menghapus: '+(d.error||''));
  else { toast(`${d.deleted||0} riwayat dihapus.`); tick(); }
}
async function verifyArt(f, bytes){
  const name = f.split('/').pop();
  dlgt.textContent = tr('Verifikasi arsip · ') + name;
  const prefix = f.endsWith('.dump')
    ? `${fmtBytes(bytes)} — pg_restore -l hanya membaca daftar isi, biasanya di bawah 1 detik.`
    : `${fmtBytes(bytes)} — .tar.zst harus didekompresi penuh, jadi waktunya sebanding ukuran.`;
  showPane(`<div class="resnote run" id=vbox><i class="dot run"></i><div>
    <b>Memeriksa arsip…</b><div class=detail id=vdet>${esc(prefix)}</div></div></div>`);
  dlg.showModal();
  let d;
  try{
    const s = await (await fetch('api/verify/start',{method:'POST',headers:H(),
      body:JSON.stringify({file:f})})).json();
    if(!s.ok){ d = s; }
    else d = await pollVerify(f, Date.now());
  }catch(e){ d = {ok:false, error:'tidak bisa menghubungi dashboard'}; }
  showPane(resultBlock(d.ok, d.ok ? 'Arsip utuh dan bisa dipulihkan' : 'Arsip TIDAK terbaca',
    (d.message || d.error || '') +
    (d.ok ? ' — berkasnya tidak rusak dan perintah restore di daftar akan berhasil.'
          : ' — jangan andalkan arsip ini; jalankan backup ulang.')));
}
// verifikasi arsip besar berjalan di server; di sini hanya memantau + menampilkan
// detik berjalan supaya tidak terasa menggantung
async function pollVerify(f, t0){
  for(;;){
    await new Promise(r=>setTimeout(r, 700));
    let st;
    try{
      st = await (await fetch('api/verify/status?file='+encodeURIComponent(f),
                              {headers:H()})).json();
    }catch(e){ continue; }
    if(st.state === 'done') return st;
    if(st.state === 'idle' || st.state === 'missing')
      return {ok:false, error:'verifikasi tidak berjalan (server di-restart?)'};
    if(st.state === 'running'){
      const el = document.getElementById('vdet');
      if(el) el.textContent = `Sudah ${fmtDur(st.elapsed || 0)}. Arsip besar butuh waktu — `
        + 'jendela ini boleh ditutup, verifikasi tetap berjalan di server.';
      if(Date.now() - t0 > 1800000)
        return {ok:false, error:'melebihi 30 menit, dihentikan'};
    }
  }
}
function copyText(text){
  navigator.clipboard.writeText(text).then(()=>toast('Perintah restore disalin.'))
    .catch(()=>toast('Gagal menyalin.'));
}
const RESTORE = {'.dump':'./restore.sh pg', '.tar.zst':'./restore.sh tar',
                 '.sql.zst':'./restore.sh my'};
function restoreCmd(name){
  const ext = Object.keys(RESTORE).find(e => name.endsWith(e)) || '';
  // argumen ketiga berbeda per jenis: pg butuh nama database, my opsional,
  // tar butuh direktori tujuan — sebut yang benar supaya perintahnya bisa langsung dipakai
  const arg = ext === '.dump' ? '<namadb>' : ext === '.tar.zst' ? '<dir-tujuan>' : '';
  return ((RESTORE[ext] || './restore.sh') + ' ' + name + (arg ? ' ' + arg : ''));
}
let CURRENT = null;
// path arsip tidak pernah ditaruh mentah di atribut onclick: tanda kutip atau
// karakter khusus di nama berkas bisa merusak HTML/JS. Index ke ARCHIVES aman,
// TAPI index bisa basi (daftar dimuat ulang setelah hapus / retention). Karena itu
// kegagalan dicari ulang dari nama berkas, dan kalau tetap tidak ada, pengguna
// diberi tahu — bukan tombol yang diam saja.
function archAt(job, i){
  const list = ARCHIVES[job] || [];
  const a = list[i];
  if(a) return a;
  toast('Daftar arsip berubah — memuat ulang.');
  loadArchives(job);
  return null;
}
async function openArchives(job){
  CURRENT = job;
  archtitle.textContent = 'Arsip · ' + job;
  archnote.textContent = '';
  archrows.innerHTML = '<div class=skeleton></div><div class=skeleton></div>';
  archjobdate.value = '';
  archjobq.value = '';
  arch.showModal();
  loadArchives(job);
}
async function loadArchives(job){
  if(!job) return;
  archrows.innerHTML = '<div class=skeleton></div><div class=skeleton></div>';
  const d = await (await fetch('api/archives?job='+encodeURIComponent(job),
    {headers:H()})).json();
  ARCHIVES[job] = d.archives || [];
  renderArchiveRows();
}
function renderArchiveRows(){
  const job = CURRENT;
  const all = ARCHIVES[job] || [];
  const want = archjobdate.value;
  const q = archjobq.value.trim().toLowerCase();
  const list = all.filter(a => {
    if(want && !(a.stamp || '').startsWith(want)) return false;
    if(q && !a.name.toLowerCase().includes(q)) return false;
    return true;
  });
  const total = list.reduce((x,a)=>x + a.bytes, 0);
  archnote.textContent = list.length
    ? `${list.length} dari ${all.length} arsip · total ${fmtBytes(total)}`
    : 'tidak ada arsip yang cocok';
  archclear.hidden = !(want || q);
  if(!list.length){
    archrows.innerHTML = blankState('Tidak ada arsip',
      all.length ? 'Tidak ada yang cocok dengan filter. Hapus filter untuk melihat semua.'
                 : 'Job ini belum punya arsip di target lokal, atau semua sudah dihapus retention.');
    return;
  }
  // kelompokkan per hari: satu kali backup menghasilkan beberapa berkas (per database)
  const days = [];
  for(const a of list){
    const day = (a.stamp || new Date((a.mtime || 0) * 1000).toISOString()).slice(0,10);
    let g = days[days.length - 1];
    if(!g || g.day !== day) days.push(g = {day, items:[], bytes:0});
    g.items.push(a); g.bytes += a.bytes;
  }
  archrows.innerHTML = days.map(g=>`<div class=archday>${dayLabel(g.day)}
    <span>· ${g.items.length} berkas · ${fmtBytes(g.bytes)}</span></div>
    ${g.items.map(a=>{
      const idx = all.indexOf(a);
      return `<div class=archrow>
      <div><div class=nm>${esc(a.name)}</div>
        <div class=mt>${esc(a.stamp ? a.stamp.replace('T', ' ').slice(11,16) : '—')}
          · ${fmtBytes(a.bytes)} · target ${esc(a.target)}</div></div>
      <div class=btns>
        <button class="btn sm" onclick="dlJob('${esc(job)}',${idx})" title="Unduh">${icon('download')}</button>
        <button class="btn sm" onclick="verifyJob('${esc(job)}',${idx})" title="Verifikasi arsip: periksa isinya masih bisa dipulihkan">${icon('check')}</button>
        <button class="btn sm" onclick="copyCmd('${esc(job)}',${idx})"
          title="Salin perintah restore">${icon('copy')}</button>
        <button class="btn sm" onclick="openHow('${esc(a.name)}')"
          title="Cara membuka arsip .tar.zst">${icon('help')}</button>
        <button class="btn sm danger" onclick="delJobArt('${esc(job)}',${idx})" title="Hapus">${icon('trash')}</button>
      </div></div>`;
    }).join('')}`).join('');
}
function dayLabel(iso){
  const d = new Date(iso + 'T00:00:00');
  if(isNaN(d)) return iso;
  const today = new Date(); today.setHours(0,0,0,0);
  const diff = Math.round((today - d) / 86400000);
  const long = d.toLocaleDateString(LANG === 'en' ? 'en-GB' : 'id-ID',
    {weekday:'long', day:'numeric', month:'long', year:'numeric'});
  if(diff === 0) return 'Hari ini · ' + long;
  if(diff === 1) return 'Kemarin · ' + long;
  return long;
}
function clearArchFilter(){
  archjobdate.value = ''; archjobq.value = '';
  renderArchiveRows();
}
function dlJob(job, i){ const a = archAt(job, i); if(a) dl(a.path); }
function delJobArt(job, i){ const a = archAt(job, i); if(a) delArt(a.path); }
function verifyJob(job, i){ const a = archAt(job, i); if(a) verifyArt(a.path, a.bytes); }
function copyCmd(job, i){
  const a = archAt(job, i);
  if(!a) return;
  // perintah diambil dari nama berkas asli; panduan lengkap ada di tombol "Cara buka"
  copyText(restoreCmd(a.name));
}
function openHow(name){
  dlgt.textContent = tr('Cara membuka · ') + name;
  const base = name.replace('.zst', '');
  showPane(`<div class=howto>
    <p>Arsip ini <b>tar dikompresi zstd</b>. Klik dua kali tidak cukup di
    Windows/macOS — begini caranya:</p>
    <h4>Linux</h4>
    <pre class=cmd>tar --zstd -xf ${esc(name)}</pre>
    <pre class="cmd alt">zstd -dc ${esc(name)} | tar -xf -   # tar lama</pre>
    <h4>macOS</h4>
    <pre class=cmd>brew install zstd
zstd -dc ${esc(name)} | tar -xf -</pre>
    <h4>Windows</h4>
    <p class=dim style="font-size:12.5px">Termudah: 7-Zip <b>21.07+</b>, klik dua kali
    berkasnya lalu ekstrak (7-Zip membuat .tar, ekstrak sekali lagi).</p>
    <pre class=cmd>winget install zstd
zstd -d ${esc(name)}
tar -xf ${esc(base)}</pre>
    <h4>Periksa rusak/tidak</h4>
    <pre class=cmd>zstd -t ${esc(name)}</pre>
    <p class=dim style="font-size:12.5px">Panduan yang sama juga ikut di dalam arsip
    sebagai <code>CARA-BUKA.txt</code>. File rahasia (config.toml,
    data/secrets.json) sengaja tidak disertakan.</p>
    </div>`);
  dlg.showModal();
}
function filterArchiveRows(){ renderArchiveRows(); }
async function renderArchives(){
  const date = archdate.value;
  const q = archq.value.trim().toLowerCase();
  if(!STATE.jobs.length){
    archiveall.innerHTML = blankState('Belum ada job', 'Tambahkan job dulu untuk melihat arsipnya.');
    return;
  }
  archiveall.innerHTML = '<div class=skeleton></div><div class=skeleton></div>';
  const jobs = STATE.jobs.filter(j => !q || j.name.toLowerCase().includes(q));
  for(const j of jobs){
    if(!ARCHIVES[j.name]){
      const d = await (await fetch('api/archives?job='+encodeURIComponent(j.name),
        {headers:H()})).json();
      ARCHIVES[j.name] = d.archives || [];
    }
  }
  const rows = [];
  for(const j of jobs)
    for(const a of ARCHIVES[j.name])
      if(!date || (a.stamp || '').startsWith(date)) rows.push({a, job:j.name});
  rows.sort((x,y)=>y.a.mtime - x.a.mtime);
  archsub.textContent = rows.length
    ? `${rows.length} arsip · ${fmtBytes(rows.reduce((a,r)=>a + r.a.bytes, 0))}` : '';
  if(!rows.length){
    archiveall.innerHTML = blankState('Tidak ada arsip',
      date ? 'Tidak ada arsip pada tanggal itu. Coba hapus filter tanggal.'
           : 'Arsip muncul setelah job berjalan dan target lokal aktif.');
    return;
  }
  // kelompok per hari — satu run menghasilkan beberapa berkas
  const days = [];
  for(const r of rows){
    const day = (r.a.stamp || new Date((r.a.mtime || 0) * 1000).toISOString()).slice(0,10);
    let g = days[days.length - 1];
    if(!g || g.day !== day) days.push(g = {day, items:[], bytes:0});
    g.items.push(r); g.bytes += r.a.bytes;
  }
  archiveall.innerHTML = '<div class=scroll><table><thead><tr><th>Job'
    + '<th class=hide-sm>Waktu<th class=hide-sm>Berkas<th class=hide-sm>Ukuran'
    + '<th class=hide-sm>Target<th style="text-align:right">Aksi<tbody>'
    + days.map(g=>`<tr><td colspan=6 style="background:var(--card-2);padding:7px 10px;
        font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);
        font-weight:640">${esc(dayLabel(g.day))}
        <span style="letter-spacing:0;text-transform:none;font-weight:500">
        · ${g.items.length} berkas · ${fmtBytes(g.bytes)}</span></td></tr>`
      + g.items.map(({a,job})=>{
        const idx = ARCHIVES[job].indexOf(a);
        return `<tr>
        <td><button class=jobname onclick="openArchives('${esc(job)}')">${esc(job)}</button>
        <td class="dim num hide-sm">${esc(a.stamp ? a.stamp.replace('T',' ').slice(0,16) : '')}
        <td class="mono hide-sm">${esc(a.name)}
        <td class="num hide-sm">${fmtBytes(a.bytes)}
        <td class="hide-sm dim">${esc(a.target)}
        <td><div class=actions>
          <button class="btn sm" onclick="dlJob('${esc(job)}',${idx})" title="Unduh">${icon('download')}</button>
          <button class="btn sm" onclick="verifyJob('${esc(job)}',${idx})" title="Verifikasi arsip: periksa isinya masih bisa dipulihkan">${icon('check')}</button>
          <button class="btn sm" onclick="copyCmd('${esc(job)}',${idx})"
            title="Salin perintah restore">${icon('copy')}</button>
          <button class="btn sm" onclick="openHow('${esc(a.name)}')"
            title="Cara membuka arsip .tar.zst">${icon('help')}</button>
          <button class="btn sm danger" onclick="delJobArt('${esc(job)}',${idx})" title="Hapus">${icon('trash')}</button>
          </div></tr>`;
      }).join('')).join('') + '</table></div>';
}
async function showLog(id,job){
  const d = await (await fetch('api/log?id='+id,{headers:H()})).json();
  dlgt.textContent = tr('Log · ')+job; showText(d.log || tr('(log kosong)'));
  dlg.showModal();
}
// ---- progres realtime -------------------------------------------------------
// Server mencatat fase, byte, dan laju per job. Browser hanya membaca; tidak ada
// persentase karena ukuran akhir arsip belum diketahui selama dump berjalan.
let LIVE = null;        // nama job yang dialog log-nya sedang terbuka
let PROG = {};          // snapshot terakhir dari /api/progress
let progTimer = null;

function progLine(p){
  const parts = [tr(p.phase)];
  if(p.bytes) parts.push(fmtBytes(p.bytes));
  if(p.rate) parts.push(fmtBytes(p.rate) + '/s');
  if(p.est && p.bytes) parts.push('±' + Math.min(99, Math.round(p.bytes / p.est * 100)) + '%');
  parts.push(fmtDur(p.elapsed));
  return parts.join(' · ');
}
// versi ringkas untuk kolom tabel: fase + persen + waktu
function progShort(p){
  const parts = [tr(p.phase)];
  if(p.est && p.bytes) parts.push(Math.min(99, Math.round(p.bytes / p.est * 100)) + '%');
  parts.push(fmtDur(p.elapsed));
  return parts.join(' · ');
}
async function pollProgress(){
  let d;
  try{ d = await (await fetch('api/progress',{headers:H()})).json(); }
  catch(e){ return; }
  PROG = d.jobs || {};
  paintProgress();
  if(!LIVE) return;
  const p = PROG[LIVE];
  if(p && dlg.open){
    dlglive.textContent = progLine(p);
    dlgb.textContent = p.log || tr('(menunggu keluaran…)');
    scrollLog();
    return;
  }
  if(p) return;                       // dialog tertutup: progres tetap dicatat
  // job baru saja selesai: ambil log final dari riwayat
  const fresh = STATE.runs.find(r=>r.job === LIVE && r.status !== 'running');
  if(!fresh){ await tick(); return; }
  const full = await (await fetch('api/log?id='+fresh.id,{headers:H()})).json();
  if(LIVE && dlg.open){
    liveline.hidden = false; livefoot.hidden = false;
    dlglive.textContent = tr('selesai · ') + statusLabel(fresh.status) + ' · '
      + fresh.size + ' · ' + fmtDur(fresh.duration);
    document.querySelector('#liveline .dot').classList.remove('run');
    dlgb.textContent = full.log || tr('(log kosong)');
    scrollLog();
  }
  stopLive();
}
function paintProgress(){
  for(const el of document.querySelectorAll('.prog[data-job]')){
    const p = PROG[el.dataset.job];
    if(p) el.innerHTML = `<span class="chip run" title="${esc(progLine(p))}"><i class="dot run"></i>${esc(progShort(p))}</span>`;
  }
}
function scrollLog(){
  const pre = document.getElementById('dlgb');
  if(pre) pre.scrollTop = pre.scrollHeight;
}
function startProgress(){
  if(progTimer) return;
  pollProgress();
  progTimer = setInterval(() => {
    // polling hanya saat perlu: ada job berjalan, atau dialog proses terbuka
    if(Object.keys(PROG).length || STATE.runs.some(r=>r.status==='running') || LIVE)
      pollProgress();
  }, 1000);
}
function stopLive(){
  LIVE = null;
  liveline.hidden = true; livefoot.hidden = true;
  if(progTimer){ clearInterval(progTimer); progTimer = null; }
}
async function liveLog(job){
  if(dlg.open) dlg.close();
  LIVE = job;
  dlgt.textContent = tr('Proses · ') + job;
  dlgr.hidden = true; dlgr.innerHTML = '';
  dlgb.hidden = false; dlgb.textContent = tr('menghubungi server…');
  liveline.hidden = false; livefoot.hidden = true;   // Log penuh baru perlu setelah selesai
  document.querySelector('#liveline .dot').classList.add('run');
  dlglive.textContent = tr('mengambil progres…');
  dlg.showModal();
  startProgress();
  await pollProgress();
}
function openEndedLog(){
  const job = LIVE;
  const last = STATE.runs.find(r=>r.job === job);
  stopLive();
  if(last) showLog(last.id, job); else dlg.close();
}
const gNote = (kind, title, detail) => {
  g_res.hidden = false; g_res.className = 'resnote ' + kind;
  g_res.innerHTML = `<i class="dot ${kind === 'ok' ? 'ok' : kind === 'bad' ? 'bad'
    : kind === 'warn' ? 'warn' : 'run'}"></i><div><b>${esc(title)}</b>
    ${detail ? `<div class=detail>${esc(detail)}</div>` : ''}</div>`;
};
function openTelegram(){
  const t = STATE.telegram || {};
  g_token.value = '';
  g_token.placeholder = t.has_token ? 'tersimpan — kosongkan untuk mempertahankan'
                                     : '123456:ABC-DEF…';
  g_chat.value = t.chat_id || '';
  g_title.value = (t.title || '[Arkivo]').replace(/^\\[|\\]$/g, '');
  g_enabled.checked = t.enabled !== false;
  g_hint.textContent = t.has_token
    ? `Token tersimpan (${t.from_config ? 'config.toml + dashboard' : 'dashboard'}). `
      + 'Nilai token tidak pernah ditampilkan kembali.'
    : 'Buat bot lewat @BotFather, lalu kirim pesan ke bot itu dan ambil chat id-nya.';
  g_res.hidden = true; g_res.innerHTML = ''; g_save.disabled = false;
  tgdlg.showModal(); (t.has_token ? g_chat : g_token).focus();
}
async function saveTelegram(){
  const body = {kind:'telegram', name:'main'};
  const token = g_token.value.trim();
  g_save.disabled = true;
  const put = (field, value) => fetch('api/settings',{method:'POST',headers:H(),
    body:JSON.stringify({...body, field, value})}).then(r=>r.json());
  const jobs = [];
  if(token) jobs.push(put('bot_token', token));
  jobs.push(put('chat_id', g_chat.value.trim()));
  jobs.push(put('title', g_title.value.trim()));
  jobs.push(put('enabled', String(g_enabled.checked)));
  let err = null;
  for(const d of await Promise.all(jobs)) if(!d.ok && !err) err = d.error;
  g_save.disabled = false;
  if(err){ gNote('bad', 'Gagal menyimpan', err); return; }
  gNote('ok', 'Tersimpan', 'Notifikasi mengikuti pengaturan baru.');
  toast('Pengaturan Telegram tersimpan.');
  g_token.value = '';
  tick();
  setTimeout(()=>{ if(tgdlg.open) tgdlg.close(); }, 900);
}
async function testTg(){
  const btn = document.getElementById('g_test');
  if(btn) btn.disabled = true;
  gNote('run', 'Mengirim tes…', 'Memakai pengaturan yang sudah tersimpan.');
  let d;
  try{
    d = await (await fetch('api/test-telegram',{method:'POST',headers:H()})).json();
  }catch(e){ d = {ok:false, error:'tidak bisa menghubungi dashboard'}; }
  finally{ if(btn) btn.disabled = false; }
  if(d.ok) gNote('ok', 'Pesan tes terkirim', 'Cek chat Telegram-mu.');
  else gNote('bad', 'Gagal mengirim', d.error || 'tidak ada keterangan');
}
tick(); setInterval(tick, 30000);
startProgress();
setView(location.hash.replace('#',''));
// preferensi tema sistem berubah saat halaman terbuka: ikuti tanpa reload
matchMedia('(prefers-color-scheme:dark)').addEventListener('change', () => {
  if(theme() === 'system') applyTheme('system');
});
</script>"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _db(self):
            # tiap request buka koneksi sendiri: sqlite tidak boleh lintas thread
            return connect(cfg)

        def _cfg(self):
            """Config segar + job dashboard + pengaturan, supaya perubahan langsung terpakai."""
            db = self._db()
            try:
                return load_jobs(load_config(), db)
            finally:
                db.close()

        def _lang(self):
            """Bahasa dashboard: default Inggris, bisa diganti dari dashboard."""
            db = self._db()
            try:
                row = db.execute("SELECT value FROM overrides "
                                 "WHERE kind='general' AND name='' AND field='lang'").fetchone()
                return row[0] if row and row[0] in ("en", "id") else "en"
            finally:
                db.close()

        def _auth(self):
            supplied = self.headers.get("X-Token") or urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("token", [""])[0]
            return token and secrets.compare_digest(supplied, token)

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def _find_job(self, name, c):
            for job in c.get("job", []):
                if job["name"] == name:
                    return job
            return None

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/logo.svg":
                # logo publik: tanpa token, supaya favicon tampil di tab
                body = (ROOT / "logo.svg").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/":
                body = (PAGE.replace("TOKEN_INJECT", json.dumps(token))
                        .replace("LANG_INJECT", json.dumps(self._lang()))
                        .replace("FLOOR_INJECT", json.dumps(
                            cfg.get("general", {}).get("min_free_percent", 15)))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._auth():
                self._json({"error": "unauthorized"}, 401)
                return
            if path == "/api/status":
                now = datetime.now()
                c = self._cfg()
                db = self._db()
                jobs = []
                for job in c.get("job", []):
                    row = db.execute("SELECT last_run FROM state WHERE job=?",
                                     (job["name"],)).fetchone()
                    last = row[0] if row else None
                    nxt = next_run(job, last, now)
                    jobs.append({"name": job["name"], "type": job["type"],
                                 "source": ("ssh " + job["ssh_host"]) if job.get("transport") == "ssh"
                                           else job.get("_source", "config"),
                                 "databases": ", ".join(job.get("databases", []) or ["*"])
                                              if job["type"] != "path" else job.get("path", ""),
                                 # field mentah untuk form edit: tanpa ini, mengedit job
                                 # SSH/path akan mengirim nilai kosong dan ditolak validasi
                                 "path": job.get("path", ""),
                                 "exclude": job.get("exclude", []),
                                 "ssh_host": job.get("ssh_host", ""),
                                 "ssh_user": job.get("ssh_user", ""),
                                 "ssh_port": job.get("ssh_port", ""),
                                 "ssh_key": job.get("ssh_key", ""),
                                 "host": job.get("host", ""),
                                 "port": job.get("port", ""),
                                 "user": job.get("user", ""),
                                 "needs_password": bool(
                                     job["type"] != "path" and not job.get("password")
                                     and not _has_credentials(job)),
                                 "schedule": job.get("schedule", "manual"),
                                 "retention_days": job.get("retention_days",
                                                           c.get("general", {}).get("retention_days", 14)),
                                 "enabled": bool(job.get("enabled", True)),
                                 "last": last[:16].replace("T", " ") if last else None,
                                 "next": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
                                 "next_in": max(0, round((nxt - now).total_seconds()))
                                            if nxt else None})
                targets = []
                for t in c.get("targets", []):
                    disk = target_disk(t) if t.get("enabled", True) else None
                    targets.append({"name": t.get("name", "?"), "path": t.get("path", ""),
                                    "type": t.get("type", ""),
                                    "enabled": bool(t.get("enabled", True)),
                                    "disk": disk})
                tg = c.get("telegram", {})
                telegram = {
                    "chat_id": tg.get("chat_id", ""),
                    "title": tg_title({"telegram": tg}),
                    # token tidak pernah dikirim ke browser; cukup statusnya
                    "has_token": bool(tg.get("bot_token")),
                    "enabled": str(tg.get("enabled", True)).lower()
                               not in ("false", "0", "off", "no"),
                    # dari config.toml atau diisi lewat dashboard
                    "from_config": bool(load_config().get("telegram", {}).get("bot_token")),
                }
                secrets = load_secrets(c)
                for j in jobs:
                    # hanya "ada/tidak", nilainya tidak pernah dikirim ke browser
                    j["has_password"] = bool(secrets.get(j["name"]))
                    j["password_from_dashboard"] = j["name"] in (
                        secrets.get("_dashboard") or [])
                rows = db.execute("SELECT id,job,started,finished,status,artifact,bytes,error "
                                  "FROM runs ORDER BY id DESC LIMIT 60").fetchall()
                runs = [{"id": r[0], "job": r[1], "started": r[2][:16].replace("T", " "),
                         "status": r[4], "size": human(r[6] or 0), "bytes": r[6] or 0,
                         "duration": (datetime.fromisoformat(r[3]) - datetime.fromisoformat(r[2])
                                      ).total_seconds() if r[3] else 0,
                         "artifacts": r[5].split(", ") if r[5] else [], "error": r[7],
                         "running": r[3] is None} for r in rows]
                self._json({"jobs": jobs, "targets": targets, "runs": runs,
                            "telegram": telegram,
                            "lang": self._lang()})
                return
            if path == "/api/archives":
                nama = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("job", [""])[0]
                c = self._cfg()
                if not any(j.get("name") == nama for j in c.get("job", [])):
                    self._json({"error": "job tidak ada"}, 404)
                    return
                self._json({"job": nama, "archives": list_archives(c, nama)})
                return
            if path == "/api/diagnose":
                deep = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("deep", ["0"])[0] in ("1", "true")
                try:
                    items = checkup(self._cfg(), deep=deep)
                except Exception as exc:
                    items = [{"level": "bad", "title": "diagnostik gagal", "detail": str(exc)[:300]}]
                self._json({"items": diag_localize(items, self._lang())})
                return
            if path == "/api/verify/status":
                f = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("file", [""])[0]
                target = resolve_artifact(self._cfg(), f)
                if target is None:
                    self._json({"state": "missing"}, 404)
                    return
                with _verify_lock:
                    st = dict(_verify_jobs.get(str(target)) or {"state": "idle"})
                if st.get("state") == "running":
                    st["elapsed"] = round(datetime.now().timestamp() - st["started"], 1)
                self._json(st)
                return
            if path == "/api/progress":
                self._json({"jobs": progress_snapshot()})
                return
            if path == "/api/log":
                rid = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("id", ["0"])[0]
                row = self._db().execute("SELECT log FROM runs WHERE id=?", (rid,)).fetchone()
                self._json({"log": (row[0] if row else "") or ""})
                return
            if path == "/api/download":
                name = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("file", [""])[0]
                target = resolve_artifact(self._cfg(), name)
                if target is None:
                    self._json({"error": "not found"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                self.send_header("Content-Length", str(target.stat().st_size))
                self.end_headers()
                with open(target, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._auth():
                self._json({"error": "unauthorized"}, 401)
                return
            url = urllib.parse.urlparse(self.path)
            c = self._cfg()
            if url.path == "/api/run":
                name = urllib.parse.parse_qs(url.query).get("job", [""])[0]
                job = self._find_job(name, c)
                if not job:
                    self._json({"error": "job tidak ada"}, 404)
                    return
                if not start_job_thread(c, job, "dashboard"):
                    self._json({"error": "job ini sedang berjalan"}, 409)
                    return
                self._json({"ok": True, "started": True})
                return
            if url.path == "/api/cancel":
                name = urllib.parse.parse_qs(url.query).get("job", [""])[0]
                self._json({"cancelled": request_cancel(name)})
                return
            if url.path == "/api/password":
                b = self._body()
                nama = _slug(b.get("name") or "", "job")
                c = self._cfg()
                if not self._find_job(nama, c):
                    self._json({"ok": False, "error": "job tidak ada"}, 404)
                    return
                # batas panjang saja: password bisa memuat karakter apa pun, dan
                # TIDAK pernah masuk ke perintah shell (dikirim lewat variabel
                # lingkungan / terowongan), jadi tidak perlu disaring ketat
                pw = str(b.get("password") or "")
                if len(pw) > 500:
                    self._json({"ok": False, "error": "password terlalu panjang"}, 400)
                    return
                log_line(f"password job {nama}: {'diisi' if pw else 'dikosongkan'}")
                # save_secret sudah menyimpan password SEKALIGUS menandai job ini
                # dikelola dashboard. Memanggil set_password_override lagi setelahnya
                # akan melepas penanda itu dan mengembalikan kendali ke config.toml.
                save_secret(c, nama, pw)
                self._json({"ok": True, "has_password": bool(pw)})
                return
            if url.path == "/api/jobs":
                b = self._body()
                job, err = validate_job(b)
                if err:
                    self._json({"ok": False, "error": err}, 400)
                    return
                nama = _slug(b.get("name") or "", "job")
                err = upsert_db_job(self._db(), nama, job)
                self._json({"ok": err is None, "error": err, "name": nama},
                           200 if err is None else 409)
                return
            if url.path == "/api/jobs/edit":
                b = self._body()
                c = self._cfg()
                job, err = edit_job(c, self._db(), str(b.get("orig") or b.get("name") or ""), b)
                if err:
                    self._json({"ok": False, "error": err}, 400)
                    return
                self._json({"ok": True, "name": job.get("name")})
                return
            if url.path == "/api/test":
                b = self._body()
                c = self._cfg()
                target = str(b.get("name") or "")
                cari = next((j for j in c.get("job", []) if j.get("name") == target), None)
                if cari is None:
                    self._json({"ok": False, "error": "job tidak ada"}, 404)
                    return
                ok, msg = test_connection(c, cari)
                self._json({"ok": ok, "message": msg})
                return
            if url.path == "/api/jobs/delete":
                nama = self._body().get("name", "")
                if not remove_job(self._cfg(), self._db(), nama):
                    self._json({"ok": False, "error": "job tidak ditemukan"})
                    return
                self._json({"ok": True})
                return
            if url.path == "/api/runs/delete":
                b = self._body()
                rid = b.get("id")
                db = self._db()
                try:
                    deleted = remove_runs(db, rid, b.get("status"))
                except (TypeError, ValueError):
                    self._json({"ok": False, "error": "pilih id atau status='failed'"}, 400)
                    return
                finally:
                    db.close()
                self._json({"ok": True, "deleted": deleted})
                return
            if url.path == "/api/settings":
                b = self._body()
                try:
                    save_override(self._db(), b.get("kind", ""), b.get("name", ""),
                                  b.get("field", ""), b.get("value", ""))
                except ValueError as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                    return
                self._json({"ok": True})
                return
            if url.path == "/api/verify":
                b = self._body()
                c = self._cfg()
                target = resolve_artifact(c, str(b.get("file") or ""))
                if target is None:
                    self._json({"ok": False, "error": "file tidak ditemukan"}, 404)
                    return
                tipe = "postgres" if target.name.endswith(".dump") else "path"
                ok, msg = verify_archive(target, tipe)
                self._json({"ok": ok, "message": msg})
                return
            if url.path == "/api/verify/start":
                # arsip besar butuh detik-menit; jangan tahan request. Jalankan di
                # thread, simpan hasilnya, browser memantau lewat /api/verify/status.
                b = self._body()
                target = resolve_artifact(self._cfg(), str(b.get("file") or ""))
                if target is None:
                    self._json({"ok": False, "error": "file tidak ditemukan"}, 404)
                    return
                tipe = "postgres" if target.name.endswith(".dump") else "path"
                with _verify_lock:
                    _verify_jobs.pop(str(target), None)
                    _verify_jobs[str(target)] = {"state": "running", "started":
                                                 datetime.now().timestamp()}
                threading.Thread(target=_verify_bg,
                                 args=(str(target), tipe), daemon=True).start()
                self._json({"ok": True, "started": True})
                return
            if url.path == "/api/delete":
                path = self._body().get("file", "")
                err = delete_artifact(c, path)
                self._json({"ok": err is None, "error": err})
                return
            if url.path == "/api/test-telegram":
                tg = c.get("telegram", {})
                if not tg.get("bot_token") or not tg.get("chat_id"):
                    self._json({"ok": False,
                                "error": "bot token / chat id belum diisi"})
                    return
                # tes harus tetap jalan walau notifikasi otomatis dimatikan
                head = html.escape(tg.get("title") or "[Arkivo]")
                text = (f"{head} ✅ Tes dari dashboard arkivo — notifikasi berfungsi."
                        if self._lang() == "id" else
                        f"{head} ✅ Test from the arkivo dashboard — notifications work.")
                err = notify({**c, "telegram": {**tg, "enabled": True}}, text)
                self._json({"ok": err is None, "error": err})
                return
            self._json({"error": "not found"}, 404)

    print(f"dashboard: http://127.0.0.1:{port}/?token={token}")
    if cfg.get("general", {}).get("dashboard_host", "127.0.0.1") != "127.0.0.1":
        print("[warn] dashboard_host bukan localhost — pakai reverse proxy + HTTPS")
    http.server.ThreadingHTTPServer(
        (cfg.get("general", {}).get("dashboard_host", "127.0.0.1"), port), Handler
    ).serve_forever()
    return 0


def main(argv):
    if not CONFIG_PATH.exists():
        print(f"config tidak ada: {CONFIG_PATH}", file=sys.stderr)
        return 2
    cfg = load_config()
    con = connect(cfg)
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "doctor":
        return cmd_doctor(load_jobs(cfg, con))
    if cmd == "list":
        return cmd_list(load_jobs(cfg, con), con)
    if cmd == "tick":
        return cmd_tick(load_jobs(cfg, con), con)
    if cmd == "serve":
        return cmd_serve(cfg, con)
    if cmd == "run":
        if len(argv) < 3:
            print("pakai: arkivo.py run <job>", file=sys.stderr)
            return 2
        return cmd_run(load_jobs(cfg, con), con, argv[2])
    print(f"perintah tidak dikenal: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
