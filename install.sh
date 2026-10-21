#!/usr/bin/env bash
# Installer sekali-klik arkivo.
#   sudo ./install.sh              pasang dependensi + systemd
#   sudo ./install.sh --dry-run    hanya laporkan, tidak mengubah apa pun
#   sudo ./install.sh --user budi  pakai user lain untuk service
#
# Aman dijalankan berkali-kali: yang sudah ada dilewati.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=0
RUN_USER="${SUDO_USER:-$(id -un)}"
TEMPLATE_USER="$RUN_USER"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --help|-h) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "opsi tidak dikenal: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
skip() { printf '  \033[90m--\033[0m   %s\n' "$*"; }
miss() { printf '  \033[31m!!\033[0m   %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- distro
detect_pm() {
  for pm in apt-get dnf yum pacman zypper apk; do
    have "$pm" && { echo "$pm"; return; }
  done
  echo ""
}
PM="$(detect_pm)"
case "$PM" in
  apt-get) PMFAM=apt ;;
  dnf|yum) PMFAM=dnf ;;
  pacman)  PMFAM=pacman ;;
  zypper)  PMFAM=zypper ;;
  apk)     PMFAM=apk ;;
  *)       PMFAM="" ;;
esac
DISTRO="$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")"

# nama paket per distro. Tiap entri: <nama-logis> <paket-apt> <dnf> <pacman> <zypper> <apk>
pkg_for() { # $1 = nama logis. cetak nama paket, atau kosong kalau tidak ada padanannya
  local idx
  case "$1" in
    python3) idx=1 ;; rclone) idx=2 ;; zstd) idx=3 ;;
    pg_client) idx=4 ;; my_client) idx=5 ;; esac
  case "$PMFAM:$idx" in
    apt:1) echo python3 ;;    apt:2) echo rclone ;;        apt:3) echo zstd ;;
    apt:4) echo postgresql-client ;; apt:5) echo default-mysql-client ;;
    dnf:1) echo python3 ;;    dnf:2) echo rclone ;;        dnf:3) echo zstd ;;
    dnf:4) echo postgresql ;; dnf:5) echo mysql ;;
    pacman:1) echo python ;;  pacman:2) echo rclone ;;     pacman:3) echo zstd ;;
    pacman:4) echo postgresql ;; pacman:5) echo mariadb-clients ;;
    zypper:1) echo python3 ;; zypper:2) echo rclone ;;     zypper:3) echo zstd ;;
    zypper:4) echo postgresql ;; zypper:5) echo mariadb-client ;;
    apk:1) echo python3 ;;    apk:2) echo rclone ;;        apk:3) echo zstd ;;
    apk:4) echo postgresql16-client ;; apk:5) echo mariadb-client ;;
    *) echo "" ;;
  esac
}

install_pkgs() { # $@ = paket
  [[ $# -gt 0 ]] || return 0
  if [[ $DRY -eq 1 ]]; then
    ok "(dry-run) akan memasang: $*"
    return 0
  fi
  if [[ $EUID -ne 0 ]]; then
    miss "butuh root untuk memasang: $*"
    echo "     jalankan ulang: sudo $0"
    return 1
  fi
  case "$PMFAM" in
    apt)    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" ;;
    dnf)    dnf install -y "$@" ;;
    pacman) pacman -Sy --noconfirm --needed "$@" ;;
    zypper) zypper -n --no-recommends install "$@" ;;
    apk)    apk add --no-cache "$@" ;;
    *)      miss "tidak tahu cara memasang paket di distro ini: $*"; return 1 ;;
  esac
}

# pastikan tool tersedia; pasang kalau boleh
need() { # $1 = nama logis, $2 = argv[0] yang dicari, $3 = "wajib"/"opsional", $4 = alasan
  local logical=$1 bin=$2 level=$3 why=$4 pkg
  if have "$bin"; then ok "$bin ($(command -v "$bin"))"; return 0; fi
  pkg="$(pkg_for "$logical")"
  if [[ -z "$pkg" ]]; then
    miss "$bin tidak ada, dan tidak ada paket untuk distro ini — $why"
    [[ $level == wajib ]] && return 1 || return 0
  fi
  MISSING_PKGS+=("$pkg")
  miss "$bin tidak ada -> akan memasang paket '$pkg' — $why"
  return 0
}

# ---------------------------------------------------------------- mulia
say "arkivo installer"
echo "  direktori : $APP_DIR"
echo "  distro    : $DISTRO"
echo "  paket mgr : ${PM:-tidak terdeteksi}"
echo "  service   : user=$RUN_USER"
[[ $DRY -eq 1 ]] && echo "  mode      : DRY RUN (tidak ada yang diubah)"
echo

# --- config
say "1. Konfigurasi"
if [[ ! -f "$APP_DIR/config.toml" ]]; then
  if [[ -f "$APP_DIR/config.example.toml" ]]; then
    if [[ $DRY -eq 1 ]]; then
      ok "(dry-run) akan menyalin config.example.toml -> config.toml"
      ok "(dry-run) akan menyesuaikan data_dir & target lokal ke $APP_DIR"
    else
      cp "$APP_DIR/config.example.toml" "$APP_DIR/config.toml"
      # contoh memakai jalur /var/www/arkivo. Kalau dipasang di tempat lain,
      # arsip akan ditulis ke lokasi yang salah — betulkan sekarang.
      sed -i -e "s|/var/www/arkivo|$APP_DIR|g" "$APP_DIR/config.toml"
      ok "config.toml dibuat dari contoh"
      [[ "$APP_DIR" != "/var/www/arkivo" ]] && \
        ok "jalur disesuaikan ke $APP_DIR"
    fi
  else
    miss "config.example.toml tidak ada"; exit 1
  fi
else
  skip "config.toml sudah ada (tidak ditimpa)"
fi
CONFIG="$APP_DIR/config.toml"

# --- python
say "2. Python"
PYTHON=""
for cand in python3.13 python3.12 python3.11 python3; do
  have "$cand" && { PYTHON="$(command -v "$cand")"; break; }
done
if [[ -z "$PYTHON" ]]; then
  miss "python3 tidak ada -> akan memasang paket '$(pkg_for python3)'"
  install_pkgs "$(pkg_for python3)"
  PYTHON="$(command -v python3 || true)"
  [[ -n "$PYTHON" ]] || { miss "python3 tetap tidak ada, menyerah"; exit 1; }
fi
PYVER="$("$PYTHON" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
ok "$PYTHON (versi $PYVER)"

# config dibaca pakai python; tentukan dumper yang benar-benar dibutuhkan
read -r NEED_PG NEED_MY NEED_RCLONE < <("$PYTHON" - "$CONFIG" <<'PY'
import sys
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        # tidak bisa baca config: amankan dengan menganggap semua tool dibutuhkan
        print("1 1 1")
        raise SystemExit
with open(sys.argv[1], "rb") as fh:
    cfg = tomllib.load(fh)
types = {j.get("type") for j in cfg.get("job", [])}
remote = any(t.get("type") == "rclone" and t.get("enabled", True) for t in cfg.get("targets", []))
print("1" if types & {"postgres"} else "0",
      "1" if types & {"mysql", "mariadb"} else "0",
      "1" if remote else "0")
PY
)
if [[ "$PYVER" == 3.9 || "$PYVER" == 3.10 ]]; then
  miss "python $PYVER tidak punya modul tomllib -> akan memasang 'tomli'"
  if [[ $DRY -eq 0 ]]; then "$PYTHON" -m pip install --quiet tomli || miss "gagal pasang tomli lewat pip"; fi
fi

# --- tool dasar
say "3. Alat dasar"
MISSING_PKGS=()
need zstd zstd wajib "kompresi arsip"
need rclone rclone opsional "upload ke gdrive/SMB/S3/SSH"
have tar || { miss "tar tidak ada -> akan memasang paket 'tar'"; MISSING_PKGS+=(tar); }

# --- dumper sesuai isi config
say "4. Alat dump (sesuai isi config.toml)"
if [[ "$NEED_PG" == 1 ]]; then
  need pg_client pg_dump wajib "job postgres ada di config"
else
  skip "tidak ada job postgres, pg_dump tidak diperlukan"
fi
if [[ "$NEED_MY" == 1 ]]; then
  if have mariadb-dump || have mysqldump; then
    ok "$(command -v mariadb-dump 2>/dev/null || command -v mysqldump)"
  else
    need my_client mysqldump wajib "job mysql/mariadb ada di config"
  fi
else
  skip "tidak ada job mysql/mariadb, mysqldump tidak diperlukan"
fi
if [[ "$NEED_RCLONE" == 0 ]]; then
  skip "tidak ada target rclone aktif, rclone opsional"
fi

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  say "5. Memasang paket yang kurang"
  echo "  -> ${MISSING_PKGS[*]}"
  install_pkgs "${MISSING_PKGS[@]}"
  if [[ $DRY -eq 0 ]]; then
    echo
    say "   verifikasi ulang"
    for b in pg_dump mysqldump rclone zstd tar; do
      have "$b" && ok "$b" || miss "$b masih tidak ada"
    done
  fi
else
  say "5. Tidak ada paket yang perlu dipasang"
fi

# --- systemd
say "6. Layanan background (systemd)"
if [[ $EUID -ne 0 && $DRY -eq 0 ]]; then
  miss "langkah ini butuh root. Jalankan ulang: sudo $0"
  echo "     (dependensi dan config di atas sudah beres; hanya unit systemd yang belum)"
  exit 1
fi
if ! have systemctl; then
  miss "systemd tidak ada. Tambahkan ke cron user $RUN_USER:"
  echo "     */5 * * * * ARKIVO_CONFIG=$CONFIG $PYTHON $APP_DIR/arkivo.py tick >> /var/log/arkivo.log 2>&1"
elif [[ $DRY -eq 1 ]]; then
  ok "(dry-run) akan menulis unit systemd + enable timer & dashboard"
else
  grep -q "^dashboard_token = \"\"" "$CONFIG" 2>/dev/null && \
    sed -i "s|^dashboard_token = .*|dashboard_token = \"$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)\"|" "$CONFIG" && \
    ok "dashboard_token dibuat acak"

  UNIT_TMP="$(mktemp -d)"
  trap 'rm -rf "$UNIT_TMP"' EXIT
  for unit in arkivo.service arkivo.timer arkivo-dashboard.service; do
    sed -e "s|^User=.*|User=$RUN_USER|" \
        -e "s|^Group=.*|Group=$(id -gn "$RUN_USER")|" \
        -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
        -e "s|ARKIVO_CONFIG=.*|ARKIVO_CONFIG=$CONFIG|" \
        -e "s|ExecStart=/usr/bin/python3|ExecStart=$PYTHON|" \
        -e "s|/var/www/arkivo/arkivo.py|$APP_DIR/arkivo.py|g" \
        "$APP_DIR/systemd/$unit" > "$UNIT_TMP/$unit"
    install -m 644 "$UNIT_TMP/$unit" "/etc/systemd/system/$unit"
    ok "unit dipasang: $unit"
  done
  systemctl daemon-reload
  systemctl enable --now arkivo.timer arkivo-dashboard.service >/dev/null 2>&1
  ok "timer + dashboard aktif"
fi

# --- ringkasan
echo
say "Selesai."
if [[ $DRY -eq 0 ]]; then
  echo "  cek status : systemctl status arkivo.timer arkivo-dashboard.service"
  echo "  jalankan   : $PYTHON $APP_DIR/arkivo.py run <job>"
  echo "  jadwal     : $PYTHON $APP_DIR/arkivo.py list"
  echo "  tes         : $PYTHON $APP_DIR/test_arkivo.py"
  echo "  log        : journalctl -u arkivo.service -n 50"
  echo "  dashboard  : http://127.0.0.1:$(grep -E '^dashboard_port' "$CONFIG" | grep -oE '[0-9]+')/?token=$(grep -E '^dashboard_token' "$CONFIG" | cut -d'"' -f2)"
  echo
  echo "  rclone remote yang terdeteksi:"
  rclone listremotes 2>/dev/null | sed 's/^/    /' || echo "    (belum ada — jalankan: rclone config)"
  echo
  echo "  pindah ke server lain:"
  echo "    tar -C \"$(dirname "$APP_DIR")\" --exclude='$(basename "$APP_DIR")/data' \\"
  echo "        --exclude='$(basename "$APP_DIR")/config.toml' --exclude='__pycache__' \\"
  echo "        -czf arkivo.tgz \"$(basename "$APP_DIR")\""
  echo "    scp arkivo.tgz user@server:  &&  ssh user@server 'tar xzf arkivo.tgz && sudo arkivo/install.sh'"
fi
