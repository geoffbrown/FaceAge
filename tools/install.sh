#!/usr/bin/env bash
# FaceAge — one-command install on macOS.
#
# Brings a clean Mac from "repo cloned" to a working `faceage` command:
# container runtime, VM, model weights (hash-checked), image build, PATH.
#
# Safe to re-run: every step is skipped if it is already done.
#
#   ./tools/install.sh              full install
#   ./tools/install.sh --rebuild    force the container image to rebuild
#   ./tools/install.sh --skip-build set everything up but don't build the image
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="$SCRIPT_DIR/faceage"

REBUILD=0; SKIP_BUILD=0
for a in "$@"; do
  case "$a" in
    --rebuild)    REBUILD=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    -h|--help)    sed -n '2,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) printf 'unknown option: %s\n' "$a" >&2; exit 1 ;;
  esac
done

die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# Read the canonical values out of the wrapper rather than restating them, so
# this installer cannot drift from what `faceage` actually expects.
# ---------------------------------------------------------------------------
[[ -f "$WRAPPER" ]] || die "cannot find the faceage wrapper at $WRAPPER"

WEIGHTS_URL="$(sed -n 's/^WEIGHTS_URL="\(.*\)"$/\1/p' "$WRAPPER")"
WEIGHTS_SHA="$(sed -n 's/^WEIGHTS_SHA="\(.*\)"$/\1/p' "$WRAPPER")"
IMAGE_DEFAULT="$(sed -n 's/^IMAGE="\${FACEAGE_IMAGE:-\([^}]*\)}".*/\1/p' "$WRAPPER")"
COLIMA_FLAGS="$(sed -n 's/^ *colima start \(.*\)$/\1/p' "$WRAPPER")"

[[ -n "$WEIGHTS_URL"   ]] || die "could not read WEIGHTS_URL from $WRAPPER"
[[ -n "$WEIGHTS_SHA"   ]] || die "could not read WEIGHTS_SHA from $WRAPPER"
[[ -n "$IMAGE_DEFAULT" ]] || die "could not read IMAGE from $WRAPPER"
[[ -n "$COLIMA_FLAGS"  ]] || die "could not read the colima start flags from $WRAPPER"

IMAGE="${FACEAGE_IMAGE:-$IMAGE_DEFAULT}"
MODEL="$REPO/models/faceage_model.h5"
DATA="${FACEAGE_DATA:-$HOME/FaceAgeData}"

# Disk the VM asks for, parsed from the same flag string.
VM_DISK_GB="$(printf '%s\n' "$COLIMA_FLAGS" | sed -n 's/.*--disk \([0-9]*\).*/\1/p')"
VM_DISK_GB="${VM_DISK_GB:-60}"

printf '\033[1mFaceAge installer\033[0m\n'
printf 'repo   : %s\n' "$REPO"
printf 'image  : %s\n' "$IMAGE"
printf 'data   : %s\n' "$DATA"

# ---------------------------------------------------------------------------
step "1/7  Checking this machine"
# ---------------------------------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || die "this installer is for macOS. Detected: $(uname -s)"

ARCH="$(uname -m)"
MACOS_VER="$(sw_vers -productVersion)"
MACOS_MAJOR="${MACOS_VER%%.*}"
ok "macOS $MACOS_VER on $ARCH"

APPLE_SILICON=0
[[ "$ARCH" == "arm64" ]] && APPLE_SILICON=1

if [[ "$COLIMA_FLAGS" == *"--vz-rosetta"* ]]; then
  if [[ "$APPLE_SILICON" -eq 1 ]]; then
    [[ "$MACOS_MAJOR" -ge 13 ]] \
      || die "Apple Virtualization + Rosetta needs macOS 13 or newer (found $MACOS_VER)."
  else
    warn "Intel Mac: linux/amd64 runs natively, so Rosetta emulation is not needed."
    warn "Colima may reject --vz-rosetta here. If step 4 fails, start the VM by hand:"
    warn "  colima start --cpu 4 --memory 8 --disk $VM_DISK_GB"
  fi
fi

FREE_GB="$(df -g "$HOME" | awk 'NR==2 {print $4}')"
if [[ -n "$FREE_GB" && "$FREE_GB" -lt $(( VM_DISK_GB + 5 )) ]]; then
  warn "only ${FREE_GB}GB free; the VM is provisioned for ${VM_DISK_GB}GB (sparse, so it"
  warn "won't consume that immediately, but the build needs several GB of headroom)."
else
  ok "${FREE_GB}GB free on $HOME"
fi

# ---------------------------------------------------------------------------
step "2/7  Container runtime (colima + docker CLI)"
# ---------------------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  die "Homebrew is not installed, and it is how colima and docker are obtained.
Install it first:
  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
then re-run this script."
fi
ok "homebrew $(brew --version | head -1 | awk '{print $2}')"

for pkg in colima docker; do
  if command -v "$pkg" >/dev/null 2>&1; then
    ok "$pkg already installed"
  else
    info "Installing $pkg..."
    brew install "$pkg"
  fi
done

# ---------------------------------------------------------------------------
step "3/7  Rosetta 2"
# ---------------------------------------------------------------------------
if [[ "$APPLE_SILICON" -eq 1 && "$COLIMA_FLAGS" == *"--vz-rosetta"* ]]; then
  # The VM's x86-64 translation is backed by Rosetta on the host.
  if /usr/bin/pgrep -q oahd 2>/dev/null || [[ -d /Library/Apple/usr/libexec/oah ]]; then
    ok "Rosetta 2 present"
  else
    info "Installing Rosetta 2 (needed to run the linux/amd64 image)..."
    softwareupdate --install-rosetta --agree-to-license
  fi
else
  ok "not needed on this machine"
fi

# ---------------------------------------------------------------------------
step "4/7  Colima VM"
# ---------------------------------------------------------------------------
if colima status >/dev/null 2>&1; then
  ok "VM already running"
  # A VM created without vz+rosetta will fall back to slow QEMU emulation, or
  # fail outright on the amd64 build. Surface that now rather than 10 minutes in.
  if [[ "$APPLE_SILICON" -eq 1 ]] && ! colima status 2>&1 | grep -qi 'virtualization\|vz'; then
    warn "this VM may not be using Apple Virtualization + Rosetta."
    warn "If the build is very slow or fails, recreate it:"
    warn "  colima delete && colima start $COLIMA_FLAGS"
  fi
else
  info "Starting the VM (first start takes ~30s)..."
  # shellcheck disable=SC2086
  colima start $COLIMA_FLAGS
  ok "VM started"
fi

docker info >/dev/null 2>&1 || die "docker cannot reach the VM. Try: colima restart"
ok "docker talking to the VM"

# ---------------------------------------------------------------------------
step "5/7  Model weights (92 MB)"
# ---------------------------------------------------------------------------
verify_weights() {
  [[ -f "$MODEL" ]] || return 1
  [[ "$(shasum -a 256 "$MODEL" | cut -d' ' -f1)" == "$WEIGHTS_SHA" ]]
}

mkdir -p "$REPO/models"
if verify_weights; then
  ok "already present, sha256 matches release v1"
else
  if [[ -f "$MODEL" ]]; then
    warn "existing weights failed the hash check — re-downloading"
    mv "$MODEL" "$MODEL.bad.$(date +%s)"
  fi
  info "Downloading from AIM-Harvard release v1..."
  curl -fL --progress-bar -o "$MODEL.part" "$WEIGHTS_URL" \
    || die "download failed. Check your connection and re-run."
  mv "$MODEL.part" "$MODEL"
  verify_weights || die "downloaded weights do not match the expected sha256.
  expected: $WEIGHTS_SHA
  got     : $(shasum -a 256 "$MODEL" | cut -d' ' -f1)
Refusing to continue with unverified weights."
  ok "downloaded and hash verified"
fi

# ---------------------------------------------------------------------------
step "6/7  Container image"
# ---------------------------------------------------------------------------
if [[ "$SKIP_BUILD" -eq 1 ]]; then
  warn "skipped (--skip-build). Build it later with: faceage build"
elif docker image inspect "$IMAGE" >/dev/null 2>&1 && [[ "$REBUILD" -eq 0 ]]; then
  ok "$IMAGE already built (use --rebuild to force)"
else
  info "Building $IMAGE for linux/amd64 — this takes ~10 minutes under emulation."
  docker build --platform linux/amd64 -f "$REPO/docker/Dockerfile" -t "$IMAGE" "$REPO"
  ok "image built"
fi

# ---------------------------------------------------------------------------
step "7/7  The faceage command"
# ---------------------------------------------------------------------------
chmod +x "$WRAPPER" "$SCRIPT_DIR/install.sh"

case "${SHELL:-}" in
  */bash) RC="$HOME/.bash_profile" ;;
  */fish) RC="" ;;
  *)      RC="$HOME/.zshrc" ;;
esac

if [[ -z "$RC" ]]; then
  warn "fish shell detected — add this to ~/.config/fish/config.fish yourself:"
  warn "  set -gx FACEAGE_REPO \"$REPO\""
  warn "  fish_add_path \"$SCRIPT_DIR\""
elif grep -q '# >>> faceage >>>' "$RC" 2>/dev/null; then
  ok "$(basename "$RC") already configured"
else
  # FACEAGE_REPO is pinned because the wrapper otherwise assumes
  # ~/Documents/GitHub/FaceAge, which is wrong for any other clone location.
  cat >> "$RC" <<EOF

# >>> faceage >>>
export FACEAGE_REPO="$REPO"
export PATH="$SCRIPT_DIR:\$PATH"
# <<< faceage <<<
EOF
  ok "added FACEAGE_REPO and PATH to $(basename "$RC")"
fi

mkdir -p "$DATA/subjects/${FACEAGE_SUBJECT:-me}/sessions"

# ---------------------------------------------------------------------------
step "Result"
# ---------------------------------------------------------------------------
FACEAGE_REPO="$REPO" "$WRAPPER" doctor || true

# Moving from another Mac: if that Mac backed up to iCloud Drive, the copy is
# already here. Say so, with the one command that brings it back.
ICLOUD_BACKUP="$HOME/Library/Mobile Documents/com~apple~CloudDocs/FaceAge Backup"
RESTORE_HINT=""
if [[ -f "$ICLOUD_BACKUP/manifest.json" ]] \
   && [[ -z "$(find "$DATA/subjects" -type f -not -name '.DS_Store' 2>/dev/null | head -1)" ]]; then
  WRITTEN="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("%s on %s, %d files" % (m.get("written_at","?"), (m.get("wrote") or {}).get("host","?"), len(m.get("files",{}))))' "$ICLOUD_BACKUP/manifest.json" 2>/dev/null || true)"
  RESTORE_HINT="
$(printf '\033[1mA FaceAge backup is in your iCloud Drive\033[0m') ($WRITTEN).
This Mac has no sessions yet. Bring everything over with:

  faceage restore \"$ICLOUD_BACKUP\"
"
fi

cat <<EOF

$(printf '\033[32mInstall complete.\033[0m')
$RESTORE_HINT
Open a new terminal (or run: source ${RC:-your shell rc}) so \`faceage\` is on PATH.

Then confirm the environment reproduces the authors' published numbers:

  # one-time: put a few UTK images in validation/images/ (see README section 4)
  faceage validate

And to score a session:

  mkdir -p "$DATA/subjects/me/sessions/\$(date +%F)"
  # copy 8-12 .jpg/.png photos in, then:
  faceage run
  faceage chart

Read README section 3 before your first capture — lighting is the dominant
confound, and an inconsistent setup will manufacture change that isn't real.

Moving from another Mac? Restore its backup (a folder or a .zip):

  faceage restore "/path/to/FaceAge Backup"

And set this Mac up to keep backing up, so the next move is one command too:

  faceage backup            # iCloud Drive by default; or: faceage backup /Volumes/Disk/FaceAge
EOF
