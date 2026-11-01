#!/usr/bin/env bash
# Atlas ephemeral runner. Usage: /runner <language> <file>
# <file> is either the agent's code written to /work/code.<ext> by Atlas, or an
# uploaded entrypoint under /work/in/. Compiled languages build into /work/build.
# Exit code is the program's exit code; compile failures exit 101 (EXEC_COMPILE_FAILED).
set -uo pipefail
lang="$1"; src="$2"; shift 2
mkdir -p /work/build; cd /work

compile_or_die() { "$@" || exit 101; }

case "$lang" in
  python)     exec python3 -I "$src" "$@" ;;
  node)       exec node "$src" "$@" ;;
  bash)       exec bash "$src" "$@" ;;
  powershell) exec pwsh -NoLogo -NonInteractive -File "$src" "$@" ;;
  go)         compile_or_die go build -o /work/build/a.out "$src"; exec /work/build/a.out "$@" ;;
  rust)       compile_or_die rustc -O -o /work/build/a.out "$src"; exec /work/build/a.out "$@" ;;
  c)          compile_or_die gcc -O2 -o /work/build/a.out "$src" -lm; exec /work/build/a.out "$@" ;;
  cpp)        compile_or_die g++ -O2 -std=c++20 -o /work/build/a.out "$src"; exec /work/build/a.out "$@" ;;
  csharp|dotnet)
              # Single-file C#: wrap in a console project. If an uploaded .csproj exists, build that instead.
              if ls /work/in/*.csproj >/dev/null 2>&1; then
                compile_or_die dotnet build /work/in -c Release -o /work/build --nologo -v q
              else
                mkdir -p /work/build/proj && cp "$src" /work/build/proj/Program.cs
                cat > /work/build/proj/proj.csproj <<'EOF'
<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><Nullable>enable</Nullable><ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>
EOF
                compile_or_die dotnet build /work/build/proj -c Release -o /work/build --nologo -v q
              fi
              dll=$(ls /work/build/*.dll | head -1); exec dotnet "$dll" "$@" ;;
  binary)     chmod +x "$src" 2>/dev/null; exec "$src" "$@" ;;
  *)          echo "unsupported language: $lang" >&2; exit 102 ;;
esac
