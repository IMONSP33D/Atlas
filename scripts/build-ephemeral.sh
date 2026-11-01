#!/usr/bin/env bash
# Build the Atlas ephemeral image and run a per-language smoke matrix.
# Usage: scripts/build-ephemeral.sh [--tag atlas-ephemeral:dev] [--no-smoke]
# Needs a working Docker daemon. The smoke runs use the same hardening flags as Atlas.
set -euo pipefail

tag="atlas-ephemeral:dev"; smoke=1
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) tag="${2:?--tag needs a value}"; shift 2 ;;
    --tag=*) tag="${1#--tag=}"; shift ;;
    --no-smoke) smoke=0; shift ;;
    -h|--help) sed -n 2,4p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -t "$tag" "$here/../ephemeral"
[ "$smoke" -eq 1 ] || exit 0

stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"; mkdir -p "$stage/in"
w() { printf '%s\n' "$2" > "$stage/$1"; chmod 644 "$stage/$1"; }
w code.py  'print("hello")'
w code.js  'console.log("hello")'
w code.sh  'echo hello'
w code.ps1 'Write-Output "hello"'
w code.go  'package main
import "fmt"
func main() { fmt.Println("hello") }'
w code.rs  'fn main() { println!("hello"); }'
w code.c   '#include <stdio.h>
int main(void) { puts("hello"); return 0; }'
w code.cpp '#include <iostream>
int main() { std::cout << "hello\n"; }'
w code.cs  'System.Console.WriteLine("hello");'

fail=0
for pair in python:py node:js bash:sh powershell:ps1 go:go rust:rs c:c cpp:cpp csharp:cs; do
  lang="${pair%%:*}"; ext="${pair##*:}"
  out="$(docker run --rm --network none --memory 2g --cpus 2 --pids-limit 512 \
      --read-only --tmpfs /work:size=512m --tmpfs /tmp:size=256m \
      --security-opt no-new-privileges --cap-drop ALL \
      -v "$stage/in:/work/in:ro" -v "$stage/code.$ext:/work/code.$ext:ro" \
      "$tag" "$lang" "/work/code.$ext" 2>&1)" || true
  if [ "$(printf '%s' "$out" | tail -n1)" = "hello" ]; then
    printf '  ok    %s\n' "$lang"
  else
    printf '  FAIL  %s: %s\n' "$lang" "$out"; fail=1
  fi
done
if [ "$fail" -eq 0 ]; then echo "smoke matrix passed for $tag"; else echo "smoke matrix FAILED" >&2; exit 1; fi
