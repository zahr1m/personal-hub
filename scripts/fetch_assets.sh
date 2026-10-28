#!/usr/bin/env bash
# Качает закреплённые бинарники и статику в .tools/, проверяет sha256 по assets.lock.
# Использование: bash scripts/fetch_assets.sh [VERSION_FILE]
set -euo pipefail
cd "$(dirname "$0")/.."
LOCK="${1:-scripts/assets.lock}"
mkdir -p .tools styles hub/web/static

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) CLI=tailwindcss-windows-x64.exe; OUT=.tools/tailwindcss.exe ;;
  *) CLI=tailwindcss-linux-x64; OUT=.tools/tailwindcss ;;
esac

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
while read -r name url want; do
  [[ -z "${name:-}" || "$name" == \#* ]] && continue
  # Tailwind CLI - только под текущую ОС
  [[ "$name" == tailwindcss-* && "$name" != "$CLI" ]] && continue
  dest=".tools/$name"
  if [[ ! -f "$dest" ]]; then
    curl --proto '=https' -fsSL --retry 3 -o "$tmp" "$url"
    mv "$tmp" "$dest"; tmp="$(mktemp)"
  fi
  got="$(sha256sum "$dest" | cut -d' ' -f1)"
  if [[ "$want" == "-" ]]; then
    sed -i "s|^$name \(.*\) -\$|$name \1 $got|" "$LOCK"
    echo "записан sha256 для $name: $got"
  elif [[ "$got" != "$want" ]]; then
    rm -f "$dest"
    echo "ОШИБКА: sha256 $name не совпал (ждали $want, получили $got)" >&2
    exit 1
  fi
done < "$LOCK"

# рабочие копии: CLI под текущую ОС, daisyUI рядом с app.css, htmx в static
cp ".tools/$CLI" "$OUT"; chmod +x "$OUT"
cp .tools/daisyui.mjs .tools/daisyui-theme.mjs styles/
cp .tools/htmx.min.js hub/web/static/htmx.min.js
echo "готово: $OUT"
