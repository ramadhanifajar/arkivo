#!/usr/bin/env bash
# Restore manual. Jalankan: ./restore.sh
#   postgres : ./restore.sh pg   <file.dump> <dbname>
#   mysql    : ./restore.sh my   <file.sql.zst> [dbname]
#   files    : ./restore.sh tar  <file.tar.zst> <dir-tujuan>
#
# PGVERSION=18 ./restore.sh pg ... memaksa satu versi klien PostgreSQL.
set -euo pipefail

# semua direktori biner pg, versi terbaru dulu
pg_dirs() { ls -1d /usr/lib/postgresql/*/bin 2>/dev/null | sort -t/ -k4 -nr; }

# versi mayor pg_dump pembuat arsip, dibaca dari header arsip
archive_major() {
  local list ver
  for d in $(pg_dirs); do
    list=$("$d/pg_restore" -l "$1" 2>/dev/null) && break
  done
  [[ -n "${list:-}" ]] || return 1
  ver=$(sed -n 's/.*Dumped by pg_dump version: \([0-9]\+\).*/\1/p' <<<"$list" | head -1)
  [[ -n "$ver" ]] && echo "$ver"
}

pg_bin() { # $1 = nama biner. utamakan PGVERSION, lalu versi arsip, lalu terbaru
  local name=$1 ver=${PGVERSION:-}
  if [[ -z "$ver" ]]; then ver=$(archive_major "${3:-}" 2>/dev/null || true); fi
  if [[ -n "$ver" && -x "/usr/lib/postgresql/$ver/bin/$name" ]]; then
    echo "/usr/lib/postgresql/$ver/bin/$name"; return
  fi
  local d
  for d in $(pg_dirs); do
    [[ -x "$d/$name" ]] && { echo "$d/$name"; return; }
  done
  command -v "$name" || { echo "$name tidak ditemukan" >&2; exit 1; }
}

kind=${1:?pakai: pg|my|tar}
src=${2:?file arsip}
case "$kind" in
  pg|postgres)
    [[ -n "${3:-}" ]] || { echo "butuh nama database tujuan" >&2; exit 2; }
    bin=$(pg_bin pg_restore "" "$src")
    echo "memakai: $bin ($("$bin" --version))"
    # pg_restore mengembalikan 1 walau restore berhasil sebagian (mis. objek sudah ada).
    # Jangan sampai kode keluar membohongi pemanggil: laporkan dan jelaskan.
    set +e
    "$bin" -d "$3" --clean --if-exists --no-owner -j4 "$src"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
      echo "PERINGATAN: pg_restore keluar dengan kode $rc — periksa pesan di atas." >&2
      echo "Verifikasi hasilnya:  psql -d $3 -c '\\dt'" >&2
    fi
    ;;
  my|mysql)
    # dump --all-databases sudah memuat CREATE DATABASE; tanpa nama db berarti pulihkan semuanya
    # --no-defaults WAJIB jadi argumen pertama, kalau tidak mysql menolaknya
    zstd -dc -- "$src" | mysql --no-defaults ${MYSQL_HOST:+--host="$MYSQL_HOST"} \
      ${MYSQL_PORT:+--port="$MYSQL_PORT"} ${MYSQL_USER:+--user="$MYSQL_USER"} ${3:-}
    ;;
  tar)
    mkdir -p "${3:?dir tujuan}"
    tar --zstd -xf "$src" -C "$3"
    ;;
  *) echo "jenis tidak dikenal: $kind" >&2; exit 2 ;;
esac
echo "restore selesai: $src"
