from __future__ import annotations

import os
from pathlib import Path

import pytest

from atlas.backends.base import ExecSpec
from atlas.backends.docker import CommandTimeout, DockerBackend
from atlas.config import EphemeralConfig, ImageEntry, LimitsConfig
from atlas.errors import AtlasError
from atlas.validation import DecodedFile, NetworkPolicy

ENTRY = ImageEntry(runtime="ephemeral", id="default", build="atlas-ephemeral:test",
                   default=True, languages=["python", "bash"])
NO_NET = NetworkPolicy(enabled=False)
CFG = EphemeralConfig(image="atlas-ephemeral:test", memory_mb=2048, cpus=2, pids_limit=512, work_tmpfs_mb=512)


class FakeDocker:
    """Records argv; scripted per-subcommand replies."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None, float]] = []
        self.version_rc = 0
        self.image_rc = 0
        self.run_result: tuple[int, bytes, bytes] | CommandTimeout = (0, b"hello\n", b"")
        self.inspect: tuple[int, bytes, bytes] | None = None   # None -> derive from run result
        self.staged: dict[str, bytes] = {}

    def sub(self, name: str) -> list[list[str]]:
        return [c[0] for c in self.calls if len(c[0]) > 1 and c[0][1] == name]

    async def __call__(self, argv, stdin, timeout):
        argv = list(argv)
        self.calls.append((argv, stdin, timeout))
        cmd = argv[1]
        if cmd == "version":
            return self.version_rc, b"27.0.0\n", b""
        if cmd == "image":
            return self.image_rc, b"", b""
        if cmd == "run":
            self._snapshot(argv)
            if isinstance(self.run_result, CommandTimeout):
                raise self.run_result
            return self.run_result
        if cmd == "inspect":
            if self.inspect is not None:
                return self.inspect
            rc = self.run_result[0] if not isinstance(self.run_result, CommandTimeout) else 137
            return 0, f"false {rc}\n".encode(), b""
        return 0, b"", b""      # kill, rm

    def _snapshot(self, argv: list[str]) -> None:
        for i, a in enumerate(argv):
            if a == "-v":
                src = argv[i + 1].split(":")[0]
                p = Path(src)
                if p.is_dir():
                    for f in p.rglob("*"):
                        if f.is_file():
                            self.staged[str(f.relative_to(p))] = f.read_bytes()
                elif p.is_file():
                    self.staged[p.name] = p.read_bytes()


def spec(**kw) -> ExecSpec:
    base = dict(language="python", code="print('hello')", entrypoint=None, args=[], env={},
                stdin=None, timeout_sec=10, files=[])
    base.update(kw)
    return ExecSpec(**base)


def make(fd: FakeDocker, tmp_path: Path, **limits) -> DockerBackend:
    return DockerBackend(CFG, LimitsConfig(**limits), runner=fd, scratch_root=tmp_path / "scratch")


async def run(fd: FakeDocker, tmp_path: Path, s: ExecSpec, **limits):
    b = make(fd, tmp_path, **limits)
    h = await b.open(ENTRY, NO_NET)
    res = await b.execute(h, s)
    return b, h, res


def run_argv(fd: FakeDocker) -> list[str]:
    return fd.sub("run")[0]


# ------------------------------------------------------------------ argv

async def test_python_argv_exact_and_hardened(tmp_path):
    fd = FakeDocker()
    b, h, res = await run(fd, tmp_path, spec())
    argv = run_argv(fd)
    name = argv[argv.index("--name") + 1]
    assert name.startswith("atlas-")
    staging = h.ref
    assert argv == [
        "docker", "run", "--name", name, "--stop-timeout", "2",
        "--network", "none", "--memory", "2048m", "--cpus", "2", "--pids-limit", "512",
        "--read-only", "--tmpfs", "/work:size=512m", "--tmpfs", "/tmp:size=256m",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "-v", f"{staging}/in:/work/in:ro", "-v", f"{staging}/code.py:/work/code.py:ro",
        "atlas-ephemeral:test", "python", "/work/code.py",
    ]
    assert res.status == "ok" and res.stdout == "hello\n" and res.errors == []


async def test_each_hardening_flag_and_forbidden_flags(tmp_path):
    fd = FakeDocker()
    await run(fd, tmp_path, spec(stdin="x", env={"A": "b"}))
    a = run_argv(fd)

    def pair(flag, val):
        i = a.index(flag)
        assert a[i + 1] == val, (flag, a[i + 1])

    pair("--network", "none")
    pair("--security-opt", "no-new-privileges")
    pair("--cap-drop", "ALL")
    pair("--pids-limit", "512")
    pair("--memory", "2048m")
    pair("--cpus", "2")
    assert "--read-only" in a
    tmpfs = [a[i + 1] for i, x in enumerate(a) if x == "--tmpfs"]
    assert tmpfs == ["/work:size=512m", "/tmp:size=256m"]
    mounts = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
    assert len(mounts) == 2 and all(m.endswith(":ro") for m in mounts)
    assert any(m.endswith(":/work/in:ro") for m in mounts)
    assert any(m.endswith(":/work/code.py:ro") for m in mounts)
    assert "--privileged" not in a and "host" not in a
    assert not any(x.startswith("--network=") or x.startswith("--pid=") or x == "--pid" or x.startswith("--cap-add") for x in a)
    assert "--rm" not in a      # removed explicitly after inspect


async def test_custom_limits_flow_into_argv(tmp_path):
    fd = FakeDocker()
    cfg = EphemeralConfig(image="img:1", memory_mb=512, cpus=1, pids_limit=64, work_tmpfs_mb=32)
    b = DockerBackend(cfg, LimitsConfig(), runner=fd, scratch_root=tmp_path)
    h = await b.open(ENTRY, NO_NET)
    await b.execute(h, spec())
    a = run_argv(fd)
    assert "512m" in a and "/work:size=32m" in a and "64" in a and "img:1" in a


@pytest.mark.parametrize("lang,ext", [
    ("python", "py"), ("node", "js"), ("bash", "sh"), ("powershell", "ps1"), ("csharp", "cs"),
    ("dotnet", "cs"), ("rust", "rs"), ("c", "c"), ("cpp", "cpp"), ("go", "go"),
])
async def test_code_extension_per_language(tmp_path, lang, ext):
    fd = FakeDocker()
    await run(fd, tmp_path, spec(language=lang, code="SRC"))
    a = run_argv(fd)
    assert f"/work/code.{ext}" in a
    assert a[-3:-1] == [lang, f"/work/code.{ext}"] or a[-2:] == [lang, f"/work/code.{ext}"]
    assert fd.staged[f"code.{ext}"] == b"SRC"


async def test_files_staged_with_bytes_and_entrypoint(tmp_path):
    fd = FakeDocker()
    files = [DecodedFile("tool.bin", b"\x00\x01\xff", "x"), DecodedFile("data.txt", b"abc", "y")]
    b, h, res = await run(fd, tmp_path, spec(language="binary", code=None, entrypoint="tool.bin",
                                             args=["-v", "x y"], files=files))
    a = run_argv(fd)
    assert fd.staged["tool.bin"] == b"\x00\x01\xff" and fd.staged["data.txt"] == b"abc"
    assert a[-4:] == ["binary", "/work/in/tool.bin", "-v", "x y"]
    assert not any(":/work/code." in x for x in a)       # no code mount for entrypoint runs
    assert os.access(Path(h.ref) / "in" / "tool.bin", os.X_OK)


async def test_env_and_stdin_plumbing(tmp_path):
    fd = FakeDocker()
    await run(fd, tmp_path, spec(env={"FOO": "bar baz", "N": "1"}, stdin="héllo\n"))
    a = run_argv(fd)
    envs = [a[i + 1] for i, x in enumerate(a) if x == "-e"]
    assert envs == ["FOO=bar baz", "N=1"]
    assert "-i" in a and a.index("-i") < a.index("atlas-ephemeral:test")
    run_call = next(c for c in fd.calls if c[0][1] == "run")
    assert run_call[1] == "héllo\n".encode()


async def test_no_stdin_means_no_dash_i(tmp_path):
    fd = FakeDocker()
    await run(fd, tmp_path, spec())
    assert "-i" not in run_argv(fd)
    assert next(c for c in fd.calls if c[0][1] == "run")[1] is None


async def test_run_timeout_has_grace(tmp_path):
    fd = FakeDocker()
    await run(fd, tmp_path, spec(timeout_sec=20))
    assert next(c for c in fd.calls if c[0][1] == "run")[2] == 25


async def test_unsafe_file_names_refused(tmp_path):
    for bad in ("a/b", "..", "", "x\\y"):
        fd = FakeDocker()
        b = make(fd, tmp_path)
        h = await b.open(ENTRY, NO_NET)
        with pytest.raises(AtlasError) as ei:
            await b.execute(h, spec(files=[DecodedFile(bad, b"x", "h")]))
        assert ei.value.code == "FILE_TRANSFER_FAILED"
        assert not fd.sub("run")


# ------------------------------------------------------------------ outcomes

async def test_timeout_kills_container(tmp_path):
    fd = FakeDocker()
    fd.run_result = CommandTimeout(b"partial", b"warn")
    b, h, res = await run(fd, tmp_path, spec(timeout_sec=3))
    assert res.status == "timeout" and res.exit_code is None
    assert [e.code for e in res.errors] == ["EXEC_TIMEOUT"]
    assert res.stdout == "partial" and res.stderr == "warn"
    name = run_argv(fd)[run_argv(fd).index("--name") + 1]
    assert ["docker", "kill", name] in fd.sub("kill")
    assert ["docker", "rm", "-f", name] in fd.sub("rm")


async def test_oom_killed(tmp_path):
    fd = FakeDocker()
    fd.run_result = (137, b"", b"")
    fd.inspect = (0, b"true 137\n", b"")
    _, _, res = await run(fd, tmp_path, spec())
    assert res.status == "killed" and [e.code for e in res.errors] == ["EXEC_KILLED_OOM"]
    assert len(fd.sub("rm")) == 1


async def test_137_without_oom_is_plain_error(tmp_path):
    fd = FakeDocker()
    fd.run_result = (137, b"", b"")
    fd.inspect = (0, b"false 137\n", b"")
    _, _, res = await run(fd, tmp_path, spec())
    assert res.status == "error" and res.exit_code == 137
    assert [e.code for e in res.errors] == ["EXEC_NONZERO_EXIT"]


async def test_exit_125_start_failed(tmp_path):
    fd = FakeDocker()
    fd.run_result = (125, b"", b"docker: Error response from daemon: boom")
    fd.inspect = (1, b"", b"No such object")
    b = make(fd, tmp_path)
    h = await b.open(ENTRY, NO_NET)
    with pytest.raises(AtlasError) as ei:
        await b.execute(h, spec())
    assert ei.value.code == "EPHEMERAL_START_FAILED"
    assert "boom" in ei.value.notice.data["detail"]
    assert fd.sub("rm")        # cleanup still attempted


async def test_program_exit_125_with_existing_container_is_nonzero(tmp_path):
    fd = FakeDocker()
    fd.run_result = (125, b"", b"")
    fd.inspect = (0, b"false 125\n", b"")
    _, _, res = await run(fd, tmp_path, spec())
    assert res.status == "error" and res.errors[0].code == "EXEC_NONZERO_EXIT"


@pytest.mark.parametrize("lang", ["go", "rust", "c", "cpp", "csharp", "dotnet"])
async def test_compile_failure_convention(tmp_path, lang):
    fd = FakeDocker()
    fd.run_result = (101, b"", b"error: expected ;")
    _, _, res = await run(fd, tmp_path, spec(language=lang))
    assert res.status == "error" and res.exit_code == 101
    assert [e.code for e in res.errors] == ["EXEC_COMPILE_FAILED"]
    assert res.stderr == "error: expected ;"


async def test_101_from_interpreted_language_is_nonzero_exit(tmp_path):
    fd = FakeDocker()
    fd.run_result = (101, b"", b"")
    _, _, res = await run(fd, tmp_path, spec(language="python"))
    assert res.errors[0].code == "EXEC_NONZERO_EXIT"


async def test_nonzero_exit(tmp_path):
    fd = FakeDocker()
    fd.run_result = (3, b"", b"bad")
    _, _, res = await run(fd, tmp_path, spec())
    assert res.status == "error" and res.exit_code == 3
    assert res.errors[0].code == "EXEC_NONZERO_EXIT" and res.errors[0].data["exit_code"] == 3


async def test_output_truncation(tmp_path):
    fd = FakeDocker()
    fd.run_result = (0, b"a" * 100, b"b" * 10)
    _, _, res = await run(fd, tmp_path, spec(), max_output_bytes=50)
    assert len(res.stdout) == 50 and res.stdout_truncated and not res.stderr_truncated
    assert res.status == "ok"
    assert [w.code for w in res.warnings] == ["EXEC_OUTPUT_TRUNCATED"]
    assert res.warnings[0].data["streams"] == ["stdout"]


async def test_invalid_utf8_replaced(tmp_path):
    fd = FakeDocker()
    fd.run_result = (0, b"ok\xff\xfe", b"")
    _, _, res = await run(fd, tmp_path, spec())
    assert res.stdout == "ok��"


async def test_wall_time_recorded(tmp_path):
    t = iter([0.0, 0.0, 1.5, 1.5, 1.5, 1.5, 1.5])
    fd = FakeDocker()
    b = DockerBackend(CFG, LimitsConfig(), runner=fd, scratch_root=tmp_path, clock=lambda: next(t, 9.0))
    h = await b.open(ENTRY, NO_NET)
    res = await b.execute(h, spec())
    assert res.wall_time_ms == 1500 and res.cpu_time_ms is None and res.peak_memory_bytes is None


# ------------------------------------------------------------------ open / state / close

async def test_network_refused_without_running_anything(tmp_path):
    fd = FakeDocker()
    b = make(fd, tmp_path)
    with pytest.raises(AtlasError) as ei:
        await b.open(ENTRY, NetworkPolicy(enabled=True, allow=("example.com",)))
    assert ei.value.code == "NETWORK_UNAVAILABLE"
    assert "egress bridge not implemented" in ei.value.notice.message


async def test_daemon_down(tmp_path):
    fd = FakeDocker(); fd.version_rc = 1
    b = make(fd, tmp_path)
    with pytest.raises(AtlasError) as ei:
        await b.open(ENTRY, NO_NET)
    assert ei.value.code == "DOCKER_UNAVAILABLE"
    assert await b.entry_state(ENTRY) == "unavailable"


async def test_docker_binary_missing(tmp_path):
    async def boom(argv, stdin, timeout):
        raise FileNotFoundError("docker")
    b = DockerBackend(CFG, LimitsConfig(), runner=boom, scratch_root=tmp_path)
    with pytest.raises(AtlasError) as ei:
        await b.open(ENTRY, NO_NET)
    assert ei.value.code == "DOCKER_UNAVAILABLE"


async def test_image_missing(tmp_path):
    fd = FakeDocker(); fd.image_rc = 1
    b = make(fd, tmp_path)
    with pytest.raises(AtlasError) as ei:
        await b.open(ENTRY, NO_NET)
    assert ei.value.code == "EPHEMERAL_IMAGE_MISSING"
    assert ["docker", "image", "inspect", "atlas-ephemeral:test"] in fd.sub("image")
    assert await b.entry_state(ENTRY) == "unavailable"


async def test_entry_state_ready_and_cached_10s(tmp_path):
    now = [100.0]
    fd = FakeDocker()
    b = DockerBackend(CFG, LimitsConfig(), runner=fd, scratch_root=tmp_path, clock=lambda: now[0])
    assert await b.entry_state(ENTRY) == "ready"
    assert await b.entry_state(ENTRY) == "ready"
    assert len(fd.sub("version")) == 1
    fd.version_rc = 1
    now[0] += 9
    assert await b.entry_state(ENTRY) == "ready"        # still cached
    now[0] += 2
    assert await b.entry_state(ENTRY) == "unavailable"  # expired, re-probed
    assert len(fd.sub("version")) == 2
    assert fd.sub("version")[0] == ["docker", "version", "--format", "{{.Server.Version}}"]


async def test_staging_dir_is_private_and_close_removes_it(tmp_path):
    fd = FakeDocker()
    b = make(fd, tmp_path)
    h = await b.open(ENTRY, NO_NET)
    p = Path(h.ref)
    assert p.is_dir() and (p.stat().st_mode & 0o777) == 0o700 and p.parent == tmp_path / "scratch"
    await b.execute(h, spec())
    assert await b.close(h) is False
    assert not p.exists()
    assert await b.close(h) is False        # idempotent


async def test_second_execute_restages_cleanly(tmp_path):
    fd = FakeDocker()
    b = make(fd, tmp_path)
    h = await b.open(ENTRY, NO_NET)
    await b.execute(h, spec(files=[DecodedFile("old.txt", b"1", "h")]))
    await b.execute(h, spec(language="bash", code="echo", files=[]))
    assert not (Path(h.ref) / "in" / "old.txt").exists()
    assert not (Path(h.ref) / "code.py").exists() and (Path(h.ref) / "code.sh").exists()


async def test_screenshot_unsupported(tmp_path):
    b = make(FakeDocker(), tmp_path)
    h = await b.open(ENTRY, NO_NET)
    with pytest.raises(AtlasError) as ei:
        await b.screenshot(h, "auto", None, None)
    assert ei.value.code == "SCREENSHOT_UNSUPPORTED"


async def test_env_key_validation(tmp_path):
    """Env var names that could confuse argv are rejected before docker runs."""
    fd = FakeDocker()
    with pytest.raises(AtlasError) as ei:
        await run(fd, tmp_path, spec(env={"BAD=KEY": "x"}))
    assert ei.value.code == "SCHEMA_INVALID"
    assert fd.sub("run") == []
