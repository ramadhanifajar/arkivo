#!/usr/bin/env python3
"""Self-check: python3 test_arkivo.py"""
import ast
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import arkivo  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


# --- jadwal ---------------------------------------------------------------
now = datetime(2026, 10, 18, 14, 0)
j = {"schedule": "every30m"}
check("every30m: belum pernah -> due", arkivo.due(j, None, now))
check("every30m: 29 menit -> belum", not arkivo.due(j, (now - timedelta(minutes=29)).isoformat(), now))
check("every30m: 31 menit -> due", arkivo.due(j, (now - timedelta(minutes=31)).isoformat(), now))

j = {"schedule": "daily 02:00"}
check("daily: lewat slot -> due", arkivo.due(j, (now - timedelta(days=1)).isoformat(), now))
check("daily: sudah jalan setelah slot -> tidak",
      not arkivo.due(j, now.replace(hour=2, minute=5).isoformat(), now))
check("daily: belum masuk slot -> tidak",
      not arkivo.due(j, now.replace(hour=14).isoformat(), datetime(2026, 10, 18, 1, 0)))

j = {"schedule": "weekly 6 23:00"}
sat_night = datetime(2026, 10, 17, 23, 30)  # Sabtu
check("weekly: sabtu lewat slot -> due", arkivo.due(j, (now - timedelta(days=8)).isoformat(), sat_night))
check("weekly: hari lain -> tidak", not arkivo.due(j, (now - timedelta(days=8)).isoformat(), now))

check("manual: tidak pernah due", not arkivo.due({"schedule": "manual"}, None, now))
check("next_run daily masa depan",
      arkivo.next_run({"schedule": "daily 02:00"}, None, now) == datetime(2026, 10, 19, 2, 0))

# every N jam
jh = {"schedule": "every6h"}
check("every6h: belum pernah -> due", arkivo.due(jh, None, now))
check("every6h: 5 jam -> belum", not arkivo.due(jh, (now - timedelta(hours=5)).isoformat(), now))
check("every6h: 7 jam -> due", arkivo.due(jh, (now - timedelta(hours=7)).isoformat(), now))
check("every6h next_run", arkivo.next_run(jh, now.isoformat(), now) == now + timedelta(hours=6))
check("every0m ditolak (bukan pola)", arkivo.every_delta("every0m") is None)
check("everyabcm ditolak", arkivo.every_delta("everyabc m") is None)

# --- helper ---------------------------------------------------------------
check("human 0", arkivo.human(0) == "0B")
check("human 1536", arkivo.human(1536) == "1.5KB")
check("mysql_dump_bin ketemu", arkivo.mysql_dump_bin() in ("mysqldump", "mariadb-dump"))

# --- invarian biner postgres ---------------------------------------------
# pg_dump dan pg_restore HARUS dari direktori versi yang sama, kalau tidak
# arsipnya bisa ditulis dalam format yang tidak bisa dibaca (bug nyata: PG18
# menulis 1.16, PG16 hanya mengerti 1.15).
if Path("/usr/lib/postgresql").is_dir():
    d = Path(arkivo.pg_bin("pg_dump")).parent
    r = Path(arkivo.pg_bin("pg_restore")).parent
    check("pg_dump & pg_restore dari versi sama", d == r, f"{d} vs {r}")
    check("pg_bin menghormati prefer_major",
          Path(arkivo.pg_bin("pg_dump", "16")).parent.parent.name == "16"
          if Path("/usr/lib/postgresql/16/bin/pg_dump").exists() else True)
else:
    check("pg_bin fallback ke PATH", arkivo.pg_bin("pg_dump") == "pg_dump")

# server yang tidak bisa dihubungi tidak boleh melempar, harus balik (None, alasan)
check("server_major mati -> None",
      arkivo.server_major("127.0.0.1", "1", "x", {})[0] is None)

# psql TANPA -d memakai nama user sebagai nama database. User backup yang tidak
# punya database senama dulu berakhir "kredensial ditolak" padahal password benar.
_sm_src = __import__("inspect").getsource(arkivo.server_major)
check("server_major selalu menyebut -d", '"-d"' in _sm_src, _sm_src[:120])
check("dump_postgres membaca database pemeliharaan",
      '"-d", "postgres"' in __import__("inspect").getsource(arkivo._orig_dump_postgres))
# mysqldump: -p = password, -P = port. Salah huruf = "Usage: mysqldump" tanpa stderr.
check("test koneksi mysql memakai -P untuk port",
      '"-P", str(j["port"])' in __import__("inspect").getsource(arkivo.test_connection))

# --- notifikasi telegram --------------------------------------------------
# notify menembak api.telegram.org; ganti urlopen supaya bisa diuji tanpa jaringan
_sent = {}


class _FakeResp:
    def read(self):
        return b"{}"


def _fake_urlopen(req, timeout=None):
    _sent["url"] = req.full_url
    _sent["data"] = req.data.decode()
    if "boom" in req.full_url:
        raise OSError("jaringan mati")
    return _FakeResp()


_real_urlopen = arkivo.urllib.request.urlopen
arkivo.urllib.request.urlopen = _fake_urlopen
try:
    err = arkivo.notify({"telegram": {"bot_token": "tok", "chat_id": "42"}}, "halo <b>x</b>")
    check("telegram sukses -> None", err is None, str(err))
    check("kirim ke endpoint bot yang benar",
          "api.telegram.org/bottok/sendMessage" in _sent.get("url", ""), _sent.get("url", ""))
    check("chat_id & parse_mode ikut terkirim",
          "chat_id=42" in _sent.get("data", "") and "parse_mode=HTML" in _sent.get("data", ""))
    check("nisan bot_token tidak bocor ke stdout pesan",
          "tok" not in _sent.get("data", "").split("text=")[0].replace("bottok", ""))
    err = arkivo.notify({"telegram": {"bot_token": "boom", "chat_id": "42"}}, "x")
    check("telegram gagal -> pesan error", err is not None and "gagal" in err, str(err))
    err = arkivo.notify({"telegram": {}}, "x")
    check("telegram kosong -> pesan error", err is not None, str(err))
finally:
    arkivo.urllib.request.urlopen = _real_urlopen

# --- pengaturan dari dashboard -------------------------------------------
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src"
    src.mkdir()
    (src / "a.txt").write_text("x")
    cfg = {
        "general": {"data_dir": Path(td) / "data", "retention_days": 14},
        "targets": [{"name": "lokal", "type": "local", "path": str(Path(td) / "dest")}],
        "job": [{"name": "t", "type": "path", "path": str(src), "schedule": "daily 02:00"}],
    }
    con = arkivo.connect(cfg)
    check("tanpa override, jadwal dari config",
          arkivo.apply_overrides({"job": [{"name": "t", "schedule": "daily 02:00"}],
                                  "targets": [{"name": "lokal", "enabled": True}]}, con)
          ["job"][0]["schedule"] == "daily 02:00")
    arkivo.save_override(con, "job", "t", "schedule", "every5m")
    arkivo.save_override(con, "job", "t", "retention_days", "3")
    arkivo.save_override(con, "job", "t", "enabled", "false")
    arkivo.save_override(con, "target", "lokal", "enabled", "false")
    got = arkivo.apply_overrides(
        {"general": {}, "job": [{"name": "t", "schedule": "daily 02:00"}],
         "targets": [{"name": "lokal", "enabled": True}]}, con)
    check("override jadwal terpakai", got["job"][0]["schedule"] == "every5m")
    check("override retensi jadi angka", got["job"][0]["retention_days"] == 3)
    # ini bug nyata: "false" sebagai string itu truthy, job nonaktif tetap jalan
    check("override enabled jadi bool False", got["job"][0]["enabled"] is False)
    check("override target jadi bool False", got["targets"][0]["enabled"] is False)
    for kind, field in (("job", "password"), ("job", "path"), ("job", "type")):
        try:
            arkivo.save_override(con, kind, "t", field, "x")
            check(f"tolak ubah {kind}.{field}", False, "tidak ditolak")
        except ValueError:
            check(f"tolak ubah {kind}.{field}", True)
    # target.path memang boleh diubah, tapi hanya jalur yang aman untuk upload
    arkivo.save_override(con, "target", "lokal", "path", "/srv/backup")
    got = arkivo.apply_overrides(
        {"general": {}, "job": [], "targets": [{"name": "lokal", "path": "/lama"}]}, con)
    check("override target.path terpakai", got["targets"][0]["path"] == "/srv/backup",
          got["targets"][0]["path"])
    for bad in ("", "../etc", "/srv/back up;rm -rf", "gdrive:backup/$(id)"):
        try:
            arkivo.save_override(con, "target", "lokal", "path", bad)
            check(f"tolak target.path {bad!r}", False, "tidak ditolak")
        except ValueError:
            check(f"tolak target.path {bad!r}", True)

    # telegram: nilai dashboard menimpa config, token divalidasi formatnya
    arkivo.save_override(con, "telegram", "main", "bot_token", "123456789:AbCdEfGhIjKlMnOpQrStUv")
    arkivo.save_override(con, "telegram", "main", "chat_id", "-1009876543210")
    arkivo.save_override(con, "telegram", "main", "enabled", "false")
    got = arkivo.load_jobs({"general": {}, "job": [], "targets": [],
                            "telegram": {"bot_token": "1:dari-config", "chat_id": "1"}}, con)
    check("override telegram menimpa config",
          got["telegram"]["chat_id"] == "-1009876543210"
          and got["telegram"]["bot_token"].startswith("123456789:"),
          str(got["telegram"]))
    check("override telegram enabled jadi false",
          str(got["telegram"]["enabled"]).lower() == "false", got["telegram"].get("enabled"))
    for field, bad in (("bot_token", "bukan token"), ("bot_token", "123:abc def"),
                       ("chat_id", "dua belas"), ("chat_id", "12;rm -rf /")):
        try:
            arkivo.save_override(con, "telegram", "main", field, bad)
            check(f"tolak telegram.{field} {bad!r}", False, "tidak ditolak")
        except ValueError:
            check(f"tolak telegram.{field} {bad!r}", True)
    try:
        arkivo.save_override(con, "telegram", "main", "password", "x")
        check("tolak telegram.password", False, "tidak ditolak")
    except ValueError:
        check("tolak telegram.password", True)
    # notifikasi yang dimatikan tidak boleh mencoba mengirim
    check("telegram nonaktif -> notify diam",
          arkivo.notify({"telegram": {"enabled": False, "bot_token": "x", "chat_id": "1"}},
                        "tes") is None)

# --- resolusi jalur arsip (anti path traversal) ---------------------------
with tempfile.TemporaryDirectory() as td:
    base = Path(td) / "dest"
    (base / "job1").mkdir(parents=True)
    art = base / "job1" / "a.tar.zst"
    art.write_text("arsip")
    cfg = {"general": {"data_dir": Path(td) / "data"},
           "targets": [{"name": "lokal", "type": "local", "path": str(base)}]}
    check("arsip di dalam target ditemukan",
          arkivo.resolve_artifact(cfg, str(art)) == art.resolve())
    check("jalur relatif juga jalan",
          arkivo.resolve_artifact(cfg, "job1/a.tar.zst") == art.resolve())
    check("traversal ditolak", arkivo.resolve_artifact(cfg, "../../etc/passwd") is None)
    check("absolut di luar target ditolak",
          arkivo.resolve_artifact(cfg, "/etc/passwd") is None)
    check("direktori bukan file ditolak",
          arkivo.resolve_artifact(cfg, str(base)) is None)

# --- validasi job dari dashboard ------------------------------------------
# nilai ini masuk ke perintah shell (tar, pg_dump, mysqldump), jadi harus ketat
for label, form in (
        ("path berisi ;", {"name": "a", "type": "path", "path": "/tmp; rm -rf /"}),
        ("path berisi backtick", {"name": "a", "type": "path", "path": "/tmp/`id`"}),
        ("path berisi $()", {"name": "a", "type": "path", "path": "/tmp/$(id)"}),
        ("path berisi |", {"name": "a", "type": "path", "path": "/tmp|x"}),
        ("path relatif", {"name": "a", "type": "path", "path": "tmp/x"}),
        ("db diawali -", {"name": "a", "type": "postgres", "databases": "--all-databases"}),
        ("db berisi ;", {"name": "a", "type": "postgres", "databases": "app; DROP--"}),
        ("host berisi spasi", {"name": "a", "type": "postgres", "host": "a b;id"}),
        ("user berisi ;", {"name": "a", "type": "mysql", "user": "root;id"}),
        ("tipe tak dikenal", {"name": "a", "type": "sqlite", "path": "/tmp"}),
        ("port bukan angka", {"name": "a", "type": "postgres", "port": "abc"}),
        ("jadwal ngawur", {"name": "a", "type": "path", "path": "/tmp", "schedule": "tiap hari"}),
        ("ssh host kosong", {"name": "a", "type": "path", "path": "/tmp",
                             "transport": "ssh", "ssh_user": "root"}),
):
    _, err = arkivo.validate_job(form)
    check(f"tolak: {label}", err is not None, "diterima padahal berbahaya")

for label, form, want in (
        ("path wajar", {"name": "web-app", "type": "path", "path": "/srv/www/my app"},
         {"path": "/srv/www/my app", "schedule": "manual"}),
        ("pg multi database", {"name": "d1", "type": "postgres", "databases": "app, crm"},
         {"databases": ["app", "crm"], "port": 5432, "user": "postgres"}),
        ("mysql semua db", {"name": "d2", "type": "mysql", "databases": "*"},
         {"databases": ["*"], "port": 3306, "user": "root"}),
        ("mariadb jadi mysql", {"name": "d3", "type": "mariadb"}, {"type": "mysql"}),
        ("ssh + jadwal jam", {"name": "d4", "type": "postgres", "transport": "ssh",
                              "ssh_host": "10.0.0.5", "ssh_user": "root",
                              "schedule": "every6h"},
         {"transport": "ssh", "ssh_host": "10.0.0.5", "schedule": "every6h"}),
):
    job, err = arkivo.validate_job(form)
    check(f"terima: {label}", err is None and all(job.get(k) == v for k, v in want.items()),
          err or str(job))

job, err = arkivo.validate_job({"name": "root", "type": "path", "path": "/"})
check("terima: path /", err is None and job["path"] == "/", err or "")
check("nama berbahaya di-slug aman",
      re.fullmatch(r"[a-z0-9._-]+", arkivo._slug("x; rm -rf /", "")) is not None)

# --- job dari dashboard: simpan, gabung, hapus ----------------------------
with tempfile.TemporaryDirectory() as td:
    cfg = {"general": {"data_dir": Path(td) / "data"},
           "job": [{"name": "dari-config", "type": "path", "path": td}]}
    con = arkivo.connect(cfg)
    job, err = arkivo.validate_job({"name": "dari-dashboard", "type": "path", "path": td})
    check("validasi job baru", err is None, err or "")
    check("simpan job baru", arkivo.upsert_db_job(con, "dari-dashboard", job) is None)
    check("tolak nama bentrok", arkivo.upsert_db_job(con, "dari-dashboard", job) is not None)
    merged = arkivo.merge_jobs({"job": [{"name": "dari-config"}]}, con)
    check("job config + dashboard tergabung",
          [j["name"] for j in merged["job"]] == ["dari-config", "dari-dashboard"],
          str([j["name"] for j in merged["job"]]))
    check("job dashboard ditandai asalnya", merged["job"][1].get("_source") == "dashboard")

    # jalur mengisi password: job dibuat dari dashboard, password ditulis di
    # config.toml. Identitas dari dashboard TIDAK boleh hilang tertimpa blok config.
    j2, _ = arkivo.validate_job({"name": "db-x", "type": "postgres", "host": "10.0.0.5",
                                 "user": "bk", "databases": "app, crm"})
    arkivo.upsert_db_job(con, "db-x", j2)
    merged = arkivo.merge_jobs(
        {"job": [{"name": "db-x", "password": "rahasia"}]}, con)
    got = merged["job"][0]
    check("password dari config sampai ke job dashboard", got.get("password") == "rahasia")
    check("type/host job dashboard tidak hilang", got.get("type") == "postgres"
          and got.get("host") == "10.0.0.5", str(got))
    check("daftar database job dashboard tetap utuh", got.get("databases") == ["app", "crm"],
          str(got.get("databases")))
    # kalau keduanya mengisi field yang sama, config.toml yang menang
    merged = arkivo.merge_jobs({"job": [{"name": "db-x", "host": "dari-config"}]}, con)
    check("field sama: config menang", merged["job"][0]["host"] == "dari-config")

    # edit job config.toml: harus menimpa satu baris, bukan menambah job kedua
    cfg_pw = {"general": {"data_dir": Path(td) / "data"},
              "job": [{"name": "konf", "type": "postgres", "host": "1.2.3.4",
                       "user": "bk", "databases": ["app"], "password": "rahasia"}]}
    form = {"name": "konf", "type": "postgres", "host": "10.0.0.9", "user": "bk",
            "databases": "app, crm", "schedule": "daily 02:00"}
    check("edit: tidak error", arkivo.edit_job(cfg_pw, con, "konf", form)[1] is None)
    got = arkivo.load_jobs(cfg_pw, con)["job"]
    check("edit: tetap satu job", [j["name"] for j in got if j["name"].startswith("konf")]
          == ["konf"], str([j["name"] for j in got]))
    check("edit: perubahan dari form terpakai",
          got[0]["host"] == "10.0.0.9" and got[0]["databases"] == ["app", "crm"], str(got[0]))
    check("edit: password config.toml tidak hilang", got[0].get("password") == "rahasia",
          str(got[0].get("password")))
    check("edit kedua menimpa baris yang sama",
          arkivo.edit_job(cfg_pw, con, "konf", dict(form, host="10.0.0.10"))[1] is None
          and len(arkivo.load_jobs(cfg_pw, con)["job"]) == 3,
          str([j["name"] for j in arkivo.load_jobs(cfg_pw, con)["job"]]))
    check("edit: job config tidak bisa diganti nama",
          arkivo.edit_job(cfg_pw, con, "konf", dict(form, name="nama-baru"))[1] is not None)
    con.execute("DELETE FROM jobs WHERE name='konf__db'")
    con.commit()

    check("hapus job dashboard", arkivo.remove_db_job(con, "dari-dashboard") is True)
    check("hapus job yang tak ada -> False", arkivo.remove_db_job(con, "dari-config") is False)
    merged = arkivo.merge_jobs({"job": [{"name": "dari-config"}]}, con)
    check("job config tetap ada setelah hapus",
          [j["name"] for j in merged["job"]] == ["dari-config", "db-x"],
          str([j["name"] for j in merged["job"]]))

# --- diagnostik -----------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src"
    src.mkdir()
    (Path(td) / "dest").mkdir()
    cfg = {"general": {"data_dir": Path(td) / "data", "min_free_percent": 0},
           "targets": [{"name": "lokal", "type": "local", "path": str(Path(td) / "dest")}],
           "job": [{"name": "p", "type": "path", "path": str(src)},
                   {"name": "hilang", "type": "path", "path": str(Path(td) / "tidak-ada")}]}
    items = arkivo.checkup(cfg)
    by = {i["title"]: i["level"] for i in items}
    check("diagnostik: sumber ada -> ok", by.get("job p: sumber") == "ok", str(by.get("job p: sumber")))
    check("diagnostik: sumber hilang -> bad", by.get("job hilang: sumber") == "bad")
    check("diagnostik: state.db belum ada tidak dianggap rusak",
          any(i["level"] == "ok" and i["title"] == "state.db belum dibuat" for i in items),
          str([i["title"] for i in items if "state.db" in i["title"]]))
    cfg2 = dict(cfg, targets=[{"name": "ghost", "type": "local",
                               "path": str(Path(td) / "no" / "deeper")}])
    items2 = arkivo.checkup(cfg2)
    check("diagnostik: target dengan induk hilang -> bad",
          any(i["level"] == "bad" and "ghost" in i["title"] for i in items2),
          str([i["title"] for i in items2]))

    # target lokal yang belum ada tapi induknya bisa ditulis: arkivo membuatnya
    # saat run pertama, jadi ini bukan kegagalan
    cfg3 = dict(cfg, targets=[{"name": "baru", "type": "local",
                               "path": str(Path(td) / "belum-dibuat")}])
    items3 = arkivo.checkup(cfg3)
    check("diagnostik: target baru dibuat-otomatis -> bukan bad",
          not any(i["level"] == "bad" and "baru" in i["title"] for i in items3),
          str([(i["level"], i["title"]) for i in items3 if "baru" in i["title"]]))
    check("diagnostik: target baru dijelaskan sebagai belum ada",
          any(i["title"] == "target baru: direktori belum ada (dibuat saat run pertama)"
              for i in items3),
          str([i["title"] for i in items3 if "baru" in i["title"]]))

# arsip yang benar-benar bisa dipulihkan
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src"
    src.mkdir()
    (src / "a.txt").write_text("halo")
    good = Path(td) / "a.tar.zst"
    with open(good, "wb") as fh:
        p1 = subprocess.Popen(["tar", "-cf", "-", "-C", str(src), "."], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["zstd", "-q", "-3"], stdin=p1.stdout, stdout=fh)
        p1.stdout.close()
        p2.communicate()
    ok, msg = arkivo.verify_archive(good, "path")
    check("verify_archive: arsip baik -> True", ok, msg)
    bad = Path(td) / "rusak.tar.zst"
    bad.write_bytes(b"bukan arsip sama sekali")
    ok, msg = arkivo.verify_archive(bad, "path")
    check("verify_archive: arsip rusak -> False", not ok, msg)
    # arsip terpotong: header valid, isi hilang. zstd -t dulu menangkapnya, jadi
    # memastikan tar sendiri (satu lintasan) tetap melaporkannya.
    trunc = Path(td) / "potong.tar.zst"
    data = good.read_bytes()
    trunc.write_bytes(data[: max(10, len(data) // 2)])
    ok, msg = arkivo.verify_archive(trunc, "path")
    check("verify_archive: arsip terpotong -> False", not ok, msg)

# --- run yatim setelah dashboard restart -----------------------------------
# Baris 'running' tanpa thread = pekerjaan hilang; kalau dibiarkan, dashboard
# selamanya menampilkan "berjalan" dan tombol Backup ditolak.
with tempfile.TemporaryDirectory() as td:
    cfg = {"general": {"data_dir": Path(td)}, "targets": [], "job": [], "telegram": {}}
    con = arkivo.connect(cfg)
    con.execute("INSERT INTO runs(job, started, status) VALUES('jx', ?, 'running')",
                (datetime.now().isoformat(),))
    con.execute("INSERT INTO state(job, last_run) VALUES('jx', ?)",
                (datetime.now().isoformat(),))
    con.commit()
    n = arkivo.reconcile_runs(con)
    row = con.execute("SELECT status, finished, error, log FROM runs WHERE job='jx'").fetchone()
    check("reconcile: run yatim ditandai gagal", n == 1 and row[0] == "failed", str(row))
    check("reconcile: finished diisi", row[1] is not None)
    check("reconcile: error menjelaskan sebab", "restart" in (row[2] or ""), str(row[2]))
    check("reconcile: log menyebut run yatim", "yatim" in (row[3] or ""), str(row[3]))
    check("reconcile: state tidak diklaim berjalan",
          con.execute("SELECT COUNT(*) FROM state WHERE job='jx'").fetchone()[0] == 0)
    check("reconcile: idempoten", arkivo.reconcile_runs(con) == 0)
    # arsip separuh dibersihkan, panduan tidak
    (Path(td) / "tmp").mkdir()
    (Path(td) / "tmp" / "half.tar.zst").write_bytes(b"x")
    (Path(td) / "tmp" / "CARA-BUKA.txt").write_text("panduan")
    check("cleanup: arsip separuh dibuang", arkivo.cleanup_orphan_tmp(cfg) == 1)
    check("cleanup: panduan dipertahankan",
          (Path(td) / "tmp" / "CARA-BUKA.txt").exists())
    con.close()

# --- progres realtime ------------------------------------------------------
# Progres hanya untuk tampilan; yang diuji adalah bentuk datanya, bukan timing.
arkivo.progress_start("job-p", est=1000)
arkivo.progress_update("job-p", phase="dump", bytes=500, rate=250.0)
snap = arkivo.progress_snapshot()["job-p"]
check("progres: fase & byte tercatat", snap["phase"] == "dump" and snap["bytes"] == 500, str(snap))
check("progres: laju dibulatkan", snap["rate"] == 250.0, str(snap["rate"]))
check("progres: elapsed ada", snap["elapsed"] >= 0, str(snap.get("elapsed")))
arkivo.progress_done("job-p")
check("progres: hilang setelah selesai", "job-p" not in arkivo.progress_snapshot())
# log live memakai buffer run yang sama
arkivo.progress_start("job-l")
buf = ["baris 1", "baris 2"]
arkivo.progress_update("job-l", buf=buf)
check("progres: log live ikut terbaca",
      arkivo.progress_snapshot()["job-l"]["log"] == "baris 1\nbaris 2",
      arkivo.progress_snapshot()["job-l"]["log"])
arkivo.progress_done("job-l")
# parsing statistik rclone
for line, want in (("  1.234 MiB / 10 MiB, 12%, 1.5 MiB/s, ETA 5s", 1293942),
                   ("Transferred: 5.678 MiB / 10 MiB, 57%, 2.0 MiB/s", 5953814),
                   ("* file.dump: 100% done, 1.0 MiB/s", 0)):
    got = arkivo._bytes_of(line)
    check(f"rclone stats: {line.strip()[:28]}", abs(got - want) <= 1, f"{got} != {want}")

# --- rahasia arkivo tidak boleh ikut ter-backup -------------------------
# config.toml / data/secrets.json / data/state.db memuat password. Kalau ikut
# ter-arsip, password terunggah ke GDrive/SMB bersama arkivo.
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src"
    (src / "data").mkdir(parents=True)
    (src / "sub" / "data").mkdir(parents=True)
    (src / "config.toml").write_text('password = "rahasia"\n')
    (src / "data" / "secrets.json").write_text('{"job": "pw"}\n')
    (src / "data" / "state.db").write_text("db\n")
    (src / "README.md").write_text("halo\n")
    # file bernama sama milik aplikasi lain di dalam project harus TETAP ikut
    (src / "sub" / "config.toml").write_text("app config\n")
    cfg = {"general": {"data_dir": Path(td) / "out"},
           "targets": [{"name": "lokal", "type": "local", "path": str(Path(td) / "dest"),
                        "enabled": True}],
           "telegram": {}, "job": [{"name": "proj", "type": "path", "path": str(src),
                                    "exclude": [], "schedule": "manual",
                                    "retention_days": 0, "enabled": True}]}
    con = arkivo.connect(cfg)
    status, err = arkivo.run_job(cfg, cfg["job"][0], con, trigger="test")
    check("job path dengan rahasia tetap sukses", status == "success", str(err))
    art = next((Path(td) / "dest" / "proj").glob("*.tar.zst"))
    names = subprocess.run(["tar", "-I", "zstd -d", "-tf", str(art)],
                           capture_output=True, text=True).stdout.split()
    for leak in ("./config.toml", "./data/secrets.json", "./data/state.db"):
        check(f"rahasia {leak} tidak masuk arsip", leak not in names, str(names))
    check("config.toml milik app lain tetap ikut", "./sub/config.toml" in names, str(names))
    check("berkas biasa tetap ikut", "./README.md" in names, str(names))
    # panduan ekstrak ikut di dalam arsip, untuk pemulihan di Windows/macOS
    check("arsip project memuat CARA-BUKA.txt", "CARA-BUKA.txt" in names, str(names))
    isi = subprocess.run(["tar", "-I", "zstd -d", "-xOf", str(art), "CARA-BUKA.txt"],
                         capture_output=True, text=True).stdout
    check("CARA-BUKA.txt menyebut nama arsip", art.name in isi)
    check("CARA-BUKA.txt punya perintah per OS",
          "Linux" in isi and "macOS" in isi and "Windows" in isi)
    check("file panduan sementara tidak tertinggal",
          not list((Path(td) / "out" / "tmp").glob("CARA-BUKA*")))

# --- halaman dashboard harus benar-benar bisa dijalankan browser -----------
# Menguji teks sumber arkivo.py TIDAK cukup: escape seperti \n di dalam string
# Python berubah saat di-parse, dan bisa menghasilkan baris baru di tengah string
# JS berkutip tunggal — itu SyntaxError yang mematikan SELURUH script dashboard.
# Jadi yang diperiksa adalah string SUNGGUHAN yang dikirim ke browser.
def _page_html():
    tree = ast.parse((Path(__file__).parent / "arkivo.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PAGE" for t in node.targets):
            return ast.literal_eval(node.value)
    return None


page = _page_html()
check("PAGE bisa diambil dari arkivo.py", page is not None)
if page:
    check("PAGE sudah di-escape (tidak ada baris baru liar di string JS)",
          not any(l.count("'") % 2 == 1 for l in
                  re.search(r"<script>(.*?)</script>", page, re.S).group(1).splitlines()),
          "ada kutip tunggal tak berpasangan")
    # token harus benar-benar disuntik, bukan placeholder
    check("placeholder token diganti saat disajikan", "TOKEN_INJECT" in page)

    if shutil.which("node"):
        js = re.search(r"<script>(.*?)</script>", page, re.S).group(1).replace(
            "TOKEN_INJECT", '""')
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "page.js"
            p.write_text(js)
            r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
            check("syntax JS dashboard valid (node --check)", r.returncode == 0,
                  r.stderr.strip().splitlines()[0] if r.stderr else "")
    else:
        check("syntax JS dashboard (node tidak ada, dilewati)", True)

# --- password dari dashboard ----------------------------------------------
# Password dashboard MENANG atas config.toml, tapi hanya untuk job yang ditandai
# di secrets.json. Kalau aturan ini salah, mengedit password di dashboard akan
# tampak tidak berefek — bug yang mahal karena backup diam-diam pakai sandi lama.
with tempfile.TemporaryDirectory() as td:
    cfg = {"general": {"data_dir": Path(td) / "data"},
           "targets": [], "job": [{"name": "db-x", "type": "postgres", "host": "h",
                                   "user": "u", "databases": ["*"], "password": "dari-config"}]}
    con = arkivo.connect(cfg)

    check("tanpa secrets: config tetap dipakai",
          arkivo.load_jobs(cfg, con)["job"][0]["password"] == "dari-config")

    arkivo.save_secret(cfg, "db-x", "dari-dashboard")
    got = arkivo.load_jobs(cfg, con)["job"][0]
    check("job disimpan dari dashboard: password dashboard menang",
          got["password"] == "dari-dashboard", str(got.get("password")))
    check("identitas job tetap dari config", got.get("type") == "postgres"
          and got.get("host") == "h", str(got))

    # simpan lalu kosongkan HARUS meninggalkan penanda. Bug nyata: handler API
    # memanggil save_secret lalu set_password_override(False) setelahnya, sehingga
    # penanda langsung hilang dan config.toml kembali memasok sandi — "hapus"
    # tampak berefek padahal password lama masih dipakai.
    arkivo.save_secret(cfg, "db-x", "dari-dashboard")
    arkivo.save_secret(cfg, "db-x", "")
    check("penanda bertahan setelah dikosongkan",
          "db-x" in arkivo.password_overrides(cfg),
          str(arkivo.password_overrides(cfg)))
    got = arkivo.load_jobs(cfg, con)["job"][0]
    check("dihapus: password benar-benar kosong", not got.get("password"),
          str(got.get("password")))

    # melepas penanda mengembalikan kendali ke config.toml (mis. ingin memakai
    # ~/.pgpass lagi)
    arkivo.set_password_override(cfg, "db-x", False)
    check("penanda benar-benar lepas",
          "db-x" not in arkivo.password_overrides(cfg))
    got = arkivo.load_jobs(cfg, con)["job"][0]
    check("penanda dilepas: config dipakai lagi", got["password"] == "dari-config",
          str(got.get("password")))

    # load_jobs tidak boleh memutasi config masukan
    arkivo.save_secret(cfg, "db-x", "lain")
    arkivo.load_jobs(cfg, con)
    check("load_jobs tidak mencemari cfg masukan",
          cfg["job"][0]["password"] == "dari-config", str(cfg["job"][0]["password"]))

    # izin berkas harus ketat
    mode = oct(arkivo.secrets_path(cfg).stat().st_mode & 0o777)
    check("secrets.json izin 600", mode == "0o600", mode)

# kunci penanda di secrets.json tidak boleh dianggap sebagai nama job
with tempfile.TemporaryDirectory() as td:
    cfg = {"general": {"data_dir": Path(td) / "data"}, "job": [], "targets": []}
    con = arkivo.connect(cfg)
    arkivo.save_secret(cfg, "job-asli", "sandix")
    arkivo.set_password_override(cfg, "job-asli", True)
    names = list(arkivo.load_secrets(cfg))
    check("secrets.json menyimpan nama job dan daftar penanda",
          "job-asli" in names and "_dashboard" in names, str(names))
    got = arkivo.load_jobs(cfg, con)["job"]
    check("_dashboard tidak jadi job palsu", [j["name"] for j in got] == [],
          str([j["name"] for j in got]))

# --- end-to-end job path -> target lokal ----------------------------------
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src"
    src.mkdir()
    (src / "a.txt").write_text("halo")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("dunia")

    cfg = {
        "general": {"data_dir": Path(td) / "data", "retention_days": 14, "min_free_percent": 0},
        "targets": [{"name": "lokal", "type": "local", "path": str(Path(td) / "dest")}],
        "job": [{"name": "t", "type": "path", "path": str(src), "schedule": "manual"}],
    }
    con = arkivo.connect(cfg)
    status, err = arkivo.run_job(cfg, cfg["job"][0], con)

    check("job path sukses", status == "success", err)
    arts = list((Path(td) / "dest" / "t").glob("t_*.tar.zst"))
    check("artefak tersalin ke target", len(arts) == 1)
    if arts:
        listing = subprocess.run(["tar", "--zstd", "-tf", str(arts[0])], capture_output=True)
        check("tar terbaca & berisi file", listing.returncode == 0 and b"./a.txt" in listing.stdout,
              listing.stderr.decode()[:200])

    row = con.execute("SELECT status, bytes FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    check("riwayat tercatat sukses", row[0] == "success" and row[1] > 0)
    check("tmp dibersihkan", not list((Path(td) / "data" / "tmp").glob("*")))

# --- job gagal: path tidak ada -------------------------------------------
with tempfile.TemporaryDirectory() as td:
    cfg = {
        "general": {"data_dir": Path(td) / "data"},
        "targets": [{"name": "lokal", "type": "local", "path": str(Path(td) / "dest")}],
        "job": [{"name": "bad", "type": "path", "path": str(Path(td) / "tidak-ada")}],
    }
    con = arkivo.connect(cfg)
    status, err = arkivo.run_job(cfg, cfg["job"][0], con)
    check("path hilang -> failed", status == "failed", status)
    check("error menyebut path", "tidak ada" in err, err)

# --- job path: sebagian berkas tak terbaca -> partial, bukan failed ---------
# tar keluar 2 saat folder tidak bisa dibuka (mis. milik user lain di server ssh).
# Dulu seluruh arsip dibuang; sekarang arsip sah tetap dikirim dan job "partial".
with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src"
    src.mkdir()
    (src / "bisa.txt").write_text("halo")
    locked = src / "terkunci"
    locked.mkdir()
    (locked / "rahasia.txt").write_text("x")
    os.chmod(locked, 0o000)
    try:
        cfg = {
            "general": {"data_dir": Path(td) / "data"},
            "targets": [{"name": "lokal", "type": "local", "path": str(Path(td) / "dest")}],
            "job": [{"name": "p", "type": "path", "path": str(src)}],
        }
        con = arkivo.connect(cfg)
        status, err = arkivo.run_job(cfg, cfg["job"][0], con)
        arts = list((Path(td) / "dest" / "p").glob("p_*.tar.zst"))
        check("berkas tak terbaca -> partial", status == "partial", f"{status}: {err[:160]}")
        check("arsip tetap terkirim walau sebagian dilewati", len(arts) == 1)
        check("pesan menyebut berkas yang dilewati", "tidak bisa dibaca" in err, err[:200])
        if arts:
            listing = subprocess.run(["tar", "--zstd", "-tf", str(arts[0])], capture_output=True)
            check("isi arsip memuat berkas yang bisa dibaca",
                  b"./bisa.txt" in listing.stdout, listing.stderr.decode()[:200])
    finally:
        os.chmod(locked, 0o700)

# --- daftar arsip per job -------------------------------------------------
# Dashboard memakai filesystem, bukan tabel runs: retention menghapus berkas
# tanpa menyentuh riwayat, jadi riwayat bisa menyebut arsip yang sudah tidak ada.
with tempfile.TemporaryDirectory() as td:
    dest = Path(td) / "dest"
    d = dest / "job-a"
    d.mkdir(parents=True)
    names = ["job-a_20261018-020000.tar.zst", "job-a_20261020-020000.tar.zst",
             "job-a_20261019-020000.tar.zst", "job-b_20261020-020000.tar.zst",
             "catatan.txt"]
    for n in names:
        (d / n).write_text("x")
    cfg = {"general": {}, "targets": [{"name": "lokal", "type": "local", "path": str(dest)}]}
    got = arkivo.list_archives(cfg, "job-a")
    check("arsip: hanya milik job itu", len(got) == 3, str([a["name"] for a in got]))
    check("arsip: terbaru dulu",
          [a["name"] for a in got] == sorted([n for n in names if n.startswith("job-a_")],
                                             reverse=True),
          str([a["name"] for a in got]))
    check("arsip: stempel waktu terbaca", got[0]["stamp"] == "2026-10-20T02:00:00",
          got[0]["stamp"])
    check("arsip: nama di luar pola -> stempel kosong",
          arkivo._stamp_of("job-a_rusak.tar.zst", "job-a") == "")
    check("arsip: job tanpa arsip -> kosong", arkivo.list_archives(cfg, "job-x") == [])

# --- retention ------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    dest = Path(td) / "dest" / "t"
    dest.mkdir(parents=True)
    old, fresh = dest / "t_old.tar.zst", dest / "t_new.tar.zst"
    old.write_text("x")
    fresh.write_text("y")
    two_weeks = (datetime.now() - timedelta(days=20)).timestamp()
    os.utime(old, (two_weeks, two_weeks))

    cfg = {"general": {}}
    n = arkivo.apply_retention(cfg, {"type": "local", "path": str(Path(td) / "dest")},
                               {"name": "t", "retention_days": 7})
    check("retention hapus 1 file lama", n == 1, f"n={n}")
    check("file baru tetap ada", fresh.exists())

# --- config contoh valid --------------------------------------------------
ex = Path(__file__).parent / "config.example.toml"
if ex.exists():
    with open(ex, "rb") as fh:
        parsed = tomllib.load(fh)
    check("config.example.toml punya job", len(parsed.get("job", [])) >= 3)
    check("semua tipe job didukung",
          all(jobbl["type"] in arkivo.DUMPERS for jobbl in parsed["job"]),
          str([j["type"] for j in parsed["job"]]))

print()
if FAILED:
    print(f"{len(FAILED)} gagal: {', '.join(FAILED)}")
    sys.exit(1)
print("semua lolos")
