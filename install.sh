#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir="$HOME/.local/bin"

mkdir -p "$install_dir"
cp "$script_dir/cresume" "$install_dir/cresume"
chmod 755 "$install_dir/cresume"
printf 'Installed cresume to %s/cresume\n' "$install_dir"

case ":${PATH:-}:" in
    *":$install_dir:"*) ;;
    *)
        printf '\nAdd ~/.local/bin to PATH in your shell configuration, then reload it:\n'
        printf '  export PATH="$HOME/.local/bin:$PATH"\n'
        ;;
esac

if command -v fzf >/dev/null 2>&1; then
    printf '\nfzf detected; cresume will use the searchable picker.\n'
else
    printf '\nfzf is optional but recommended. Without it, cresume uses a numbered menu.\n'
fi
