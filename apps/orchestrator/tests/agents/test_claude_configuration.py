"""Claude's verified configuration directory is explicit and isolated per launch."""

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents.capability_verification import capability_scope
from forge.agents.claude_gateway import ClaudeGateway, ClaudeInstallation
from forge.agents.client_process import ClientProcessSupervisor
from forge.application.ports.subscription_gateway import SubscriptionFailure
from forge.domain.tool import ToolName
from test_claude_supervised import (
    _EXECUTABLE_DIGEST,
    _anthropic_request,
    _Broker,
    _gateway,
    _report,
    _Verifier,
)


@pytest.fixture(autouse=True)
def _supported_isolation_platform(monkeypatch):
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported", lambda: True
    )


@pytest.mark.parametrize(
    (
        "linux",
        "uid_map_ok",
        "gid_map_ok",
        "creds_ok",
        "fs_root_protected",
        "etc_protected",
        "policy_absent",
        "policy_protected",
        "surfaces_absent",
    ),
    [
        (False, True, True, True, True, True, True, True, True),
        (True, False, True, True, True, True, True, True, True),
        (True, True, False, True, True, True, True, True, True),
        (True, True, True, False, True, True, True, True, True),
        (True, True, True, True, False, True, True, True, True),
        (True, True, True, True, True, False, True, True, True),
        (True, True, True, True, True, True, False, False, True),
        (True, True, True, True, True, True, False, True, False),
    ],
)
def test_claude_platform_rejects_unsupported_matrix(
    monkeypatch,
    linux,
    uid_map_ok,
    gid_map_ok,
    creds_ok,
    fs_root_protected,
    etc_protected,
    policy_absent,
    policy_protected,
    surfaces_absent,
):
    from forge.agents import claude_gateway as gateway

    monkeypatch.setattr(gateway, "_linux_memfd_proc_available", lambda: linux)
    monkeypatch.setattr(
        gateway,
        "_linux_initial_id_map",
        lambda path: uid_map_ok if path == gateway._CLAUDE_PROC_UID_MAP else gid_map_ok,
    )
    monkeypatch.setattr(
        gateway, "_linux_credentials_and_capabilities_supported", lambda *args: creds_ok
    )
    monkeypatch.setattr(
        gateway,
        "_protected_root_directory",
        lambda path: (
            fs_root_protected
            if path == gateway._CLAUDE_FS_ROOT
            else (etc_protected if path == gateway._CLAUDE_ETC_ROOT else policy_protected)
        ),
    )
    monkeypatch.setattr(
        gateway,
        "_path_strictly_absent",
        lambda path: policy_absent if path == gateway._CLAUDE_SYSTEM_ROOT else surfaces_absent,
    )

    assert not gateway._claude_isolation_platform_supported()


def test_claude_platform_admits_protected_absent_fixed_policy(monkeypatch):
    from forge.agents import claude_gateway as gateway

    monkeypatch.setattr(gateway, "_linux_memfd_proc_available", lambda: True)
    monkeypatch.setattr(gateway, "_linux_initial_id_map", lambda _path: True)
    monkeypatch.setattr(
        gateway, "_linux_credentials_and_capabilities_supported", lambda *args: True
    )
    monkeypatch.setattr(gateway, "_protected_root_directory", lambda _path: True)
    monkeypatch.setattr(gateway, "_path_strictly_absent", lambda _path: True)

    assert gateway._claude_isolation_platform_supported()


def test_linux_memfd_proc_available_delegates_to_operational_probe(monkeypatch):
    from forge.agents import claude_gateway as gateway

    monkeypatch.setattr(gateway, "linux_operational_pinning_supported", lambda: True)
    assert gateway._linux_memfd_proc_available() is True

    monkeypatch.setattr(gateway, "linux_operational_pinning_supported", lambda: False)
    assert gateway._linux_memfd_proc_available() is False


def test_path_strictly_absent_semantics(tmp_path: Path):
    from forge.agents import claude_gateway as gateway

    absent = tmp_path / "nonexistent.json"
    assert gateway._path_strictly_absent(absent)

    present = tmp_path / "present.txt"
    present.write_text("ok", encoding="utf-8")
    assert not gateway._path_strictly_absent(present)

    dir_path = tmp_path / "dir"
    dir_path.mkdir()
    assert not gateway._path_strictly_absent(dir_path)

    try:
        symlink_target = tmp_path / "target.txt"
        symlink_target.write_text("target", encoding="utf-8")
        symlink_path = tmp_path / "symlink.txt"
        symlink_path.symlink_to(symlink_target)
        assert not gateway._path_strictly_absent(symlink_path)

        broken_symlink = tmp_path / "broken_symlink.txt"
        broken_symlink.symlink_to(tmp_path / "no-such-target")
        assert not gateway._path_strictly_absent(broken_symlink)
    except OSError, NotImplementedError:
        pass


def test_path_strictly_absent_fails_closed_on_lookup_errors(monkeypatch, tmp_path: Path):
    from forge.agents import claude_gateway as gateway

    def raise_permission_error(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "lstat", raise_permission_error)
    assert not gateway._path_strictly_absent(tmp_path / "file.json")


def test_protected_root_directory_semantics(tmp_path: Path, monkeypatch):
    import stat

    from forge.agents import claude_gateway as gateway

    root_dir = tmp_path / "root_dir"
    root_dir.mkdir(mode=0o755)

    class MockStat:
        st_mode = stat.S_IFDIR | 0o755
        st_uid = 0

    monkeypatch.setattr(os, "lstat", lambda _path: MockStat())
    monkeypatch.setattr(os, "access", lambda _path, _mode, **_kwargs: False)
    assert gateway._protected_root_directory(root_dir)

    class NonRootStat:
        st_mode = stat.S_IFDIR | 0o755
        st_uid = 1000

    monkeypatch.setattr(os, "lstat", lambda _path: NonRootStat())
    assert not gateway._protected_root_directory(root_dir)

    class GroupWritableStat:
        st_mode = stat.S_IFDIR | 0o775
        st_uid = 0

    monkeypatch.setattr(os, "lstat", lambda _path: GroupWritableStat())
    assert not gateway._protected_root_directory(root_dir)

    class OtherWritableStat:
        st_mode = stat.S_IFDIR | 0o757
        st_uid = 0

    monkeypatch.setattr(os, "lstat", lambda _path: OtherWritableStat())
    assert not gateway._protected_root_directory(root_dir)

    class SymlinkStat:
        st_mode = stat.S_IFLNK | 0o755
        st_uid = 0

    monkeypatch.setattr(os, "lstat", lambda _path: SymlinkStat())
    assert not gateway._protected_root_directory(root_dir)

    class FileStat:
        st_mode = stat.S_IFREG | 0o755
        st_uid = 0

    monkeypatch.setattr(os, "lstat", lambda _path: FileStat())
    assert not gateway._protected_root_directory(root_dir)

    monkeypatch.setattr(os, "lstat", lambda _path: MockStat())
    monkeypatch.setattr(os, "access", lambda _path, _mode, **_kwargs: True)
    assert not gateway._protected_root_directory(root_dir)

    def raise_oserror(_path):
        raise OSError("disk error")

    monkeypatch.setattr(os, "lstat", raise_oserror)
    assert not gateway._protected_root_directory(root_dir)


def test_linux_initial_id_map_semantics(tmp_path: Path):
    from forge.agents import claude_gateway as gateway

    valid_map = tmp_path / "valid_uid_map"
    valid_map.write_text("         0          0 4294967295\n", encoding="utf-8")
    assert gateway._linux_initial_id_map(valid_map)

    remapped_in = tmp_path / "remapped_in"
    remapped_in.write_text("         0       1000          1\n", encoding="utf-8")
    assert not gateway._linux_initial_id_map(remapped_in)

    remapped_out = tmp_path / "remapped_out"
    remapped_out.write_text("      1000          0          1\n", encoding="utf-8")
    assert not gateway._linux_initial_id_map(remapped_out)

    partial_map = tmp_path / "partial_map"
    partial_map.write_text("         0          0      65536\n", encoding="utf-8")
    assert not gateway._linux_initial_id_map(partial_map)

    multi_map = tmp_path / "multi_map"
    multi_map.write_text(
        "         0          0       1000\n      1000       1000      65536\n",
        encoding="utf-8",
    )
    assert not gateway._linux_initial_id_map(multi_map)

    malformed_map = tmp_path / "malformed"
    malformed_map.write_text("0 0\n", encoding="utf-8")
    assert not gateway._linux_initial_id_map(malformed_map)

    empty_map = tmp_path / "empty"
    empty_map.write_text("", encoding="utf-8")
    assert not gateway._linux_initial_id_map(empty_map)

    assert not gateway._linux_initial_id_map(tmp_path / "missing")


def test_linux_status_credentials_and_capabilities(tmp_path: Path):
    from forge.agents import claude_gateway as gateway

    def make_status(
        uid="10001 10001 10001 10001",
        gid="10001 10001 10001 10001",
        cap_inh="0000000000000000",
        cap_prm="0000000000000000",
        cap_eff="0000000000000000",
        cap_bnd="000001ffffffffff",
        cap_amb="0000000000000000",
    ) -> Path:
        status = tmp_path / f"status_{uid.replace(' ', '_')}_{cap_eff}_{cap_inh}"
        status.write_text(
            f"Uid:\t{uid}\n"
            f"Gid:\t{gid}\n"
            f"CapInh:\t{cap_inh}\n"
            f"CapPrm:\t{cap_prm}\n"
            f"CapEff:\t{cap_eff}\n"
            f"CapBnd:\t{cap_bnd}\n"
            f"CapAmb:\t{cap_amb}\n",
            encoding="utf-8",
        )
        return status

    assert gateway._linux_status_credentials_and_capabilities(make_status())
    assert not gateway._linux_status_credentials_and_capabilities(make_status(uid="0 0 0 0"))
    assert not gateway._linux_status_credentials_and_capabilities(
        make_status(uid="10001 0 10001 10001")
    )
    assert not gateway._linux_status_credentials_and_capabilities(make_status(gid="0 0 0 0"))
    assert not gateway._linux_status_credentials_and_capabilities(
        make_status(gid="10001 0 10001 10001")
    )
    assert not gateway._linux_status_credentials_and_capabilities(
        make_status(cap_eff="0000000000000001")
    )
    assert not gateway._linux_status_credentials_and_capabilities(
        make_status(cap_inh="0000000000000001")
    )
    assert not gateway._linux_status_credentials_and_capabilities(
        make_status(cap_prm="0000000000000001")
    )
    assert not gateway._linux_status_credentials_and_capabilities(
        make_status(cap_amb="0000000000000001")
    )

    bad_cap = tmp_path / "bad_cap"
    bad_cap.write_text(
        "Uid:\t10001 10001 10001 10001\nGid:\t10001 10001 10001 10001\nCapEff:\tnothex\n",
        encoding="utf-8",
    )
    assert not gateway._linux_status_credentials_and_capabilities(bad_cap)

    duplicate_cap = tmp_path / "duplicate_cap"
    duplicate_cap.write_text(
        "Uid:\t10001 10001 10001 10001\n"
        "Gid:\t10001 10001 10001 10001\n"
        "CapInh:\t0000000000000000\n"
        "CapInh:\t0000000000000000\n"
        "CapPrm:\t0000000000000000\n"
        "CapEff:\t0000000000000000\n"
        "CapAmb:\t0000000000000000\n",
        encoding="utf-8",
    )
    assert not gateway._linux_status_credentials_and_capabilities(duplicate_cap)

    missing = tmp_path / "missing_fields"
    missing.write_text("Uid:\t10001 10001 10001 10001\n", encoding="utf-8")
    assert not gateway._linux_status_credentials_and_capabilities(missing)


async def test_claude_launch_uses_only_the_verified_client_home(tmp_path, monkeypatch):
    home = tmp_path / "verified-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient-home"))
    monkeypatch.setenv(
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH", str(tmp_path / "ambient-managed-policy")
    )
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "ambient-entrypoint-must-not-be-used")
    monkeypatch.setenv("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "ambient-must-not-override")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-ambient-value")
    base = _gateway("success")
    installation = replace(base._installation, client_home=str(home))
    report = _report(client_home=str(home.resolve()))
    broker = _Broker()
    launches = []

    class Capture:
        async def start(self, spec, **kwargs):
            launches.append(spec)
            return await ClientProcessSupervisor().start(spec, **kwargs)

    result = await ClaudeGateway(
        installation, _Verifier(report), broker=broker, supervisor=Capture()
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert result.failure is None and result.launch_proof.stop_confirmed
    assert broker.revoked and len(broker.calls) == 1
    assert len(launches) == 1
    environment = launches[0].environment
    assert environment["CLAUDE_CONFIG_DIR"] == str(home.resolve())
    assert environment["CLAUDE_CODE_ENTRYPOINT"] == "local-agent"
    assert environment["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] == "1"
    assert "CLAUDE_CODE_MANAGED_SETTINGS_PATH" not in environment
    assert launches[0].executable_digest == installation.executable_digest
    assert set(environment) == {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    } | ({"SystemRoot"} if os.name == "nt" else set())


async def test_claude_home_proof_mismatch_rejects_before_launch(tmp_path):
    home = tmp_path / "verified-home"
    home.mkdir()
    broker = _Broker()
    installation = ClaudeInstallation(
        executable=sys.executable,
        cwd=str(tmp_path),
        model="claude-test",
        effort="medium",
        client_home=str(home),
        account="test-account",
        executable_digest=_EXECUTABLE_DIGEST,
    )

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("mismatched home must not launch")

    result = await ClaudeGateway(
        installation,
        _Verifier(_report(client_home=str(tmp_path))),
        broker=broker,
        supervisor=NoLaunch(),
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))
    assert result.failure is SubscriptionFailure.UNAVAILABLE and result.launch_proof is None
    assert result.quota_exhaustion is None and broker.revoked and broker.calls == []


async def test_unsupported_platform_rejects_before_verifier_or_launch(monkeypatch):
    base, broker = _gateway("success"), _Broker()
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported", lambda: False
    )

    class NoVerify:
        def verify(self, *_args):
            raise AssertionError("unsupported platform must not resolve evidence")

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("unsupported platform must not launch")

    result = await ClaudeGateway(
        base._installation, NoVerify(), broker=broker, supervisor=NoLaunch()
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))

    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.quota_exhaustion is None and result.launch_proof is None and broker.revoked


async def test_operational_probe_false_rejects_before_evidence_or_launch(monkeypatch):
    from forge.agents import claude_gateway as gateway

    base, broker = _gateway("success"), _Broker()
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported",
        gateway._claude_isolation_platform_supported,
    )
    monkeypatch.setattr(gateway, "_linux_memfd_proc_available", lambda: False)
    monkeypatch.setattr(gateway, "_linux_initial_id_map", lambda _path: True)
    monkeypatch.setattr(
        gateway, "_linux_credentials_and_capabilities_supported", lambda *args: True
    )
    monkeypatch.setattr(gateway, "_protected_root_directory", lambda _path: True)
    monkeypatch.setattr(gateway, "_path_strictly_absent", lambda _path: True)

    class NoVerify:
        def verify(self, *_args):
            raise AssertionError("operational probe failure must not resolve evidence")

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("operational probe failure must not launch")

    result = await ClaudeGateway(
        base._installation, NoVerify(), broker=broker, supervisor=NoLaunch()
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))

    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.quota_exhaustion is None and result.launch_proof is None and broker.revoked


async def test_claude_gateway_launch_environment_omits_managed_settings_path(tmp_path):
    home = tmp_path / "verified-home"
    home.mkdir()
    base = _gateway("success")
    installation = replace(base._installation, client_home=str(home))
    report = _report(client_home=str(home.resolve()))
    broker = _Broker()

    class CreatesPolicy:
        async def verify(self, *args):
            (home / "managed-settings.json").write_text("{}", encoding="utf-8")
            (home / "managed-settings.d").mkdir()
            (home / "managed-mcp.json").write_text("{}", encoding="utf-8")
            return _Verifier(report).verify(*args)

    launches = []

    class Capture:
        async def start(self, spec, **kwargs):
            launches.append(spec)
            return await ClientProcessSupervisor().start(spec, **kwargs)

    result = await ClaudeGateway(
        installation, CreatesPolicy(), broker=broker, supervisor=Capture()
    ).execute(_anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})))

    assert result.failure is None and result.launch_proof is not None
    assert "CLAUDE_CODE_MANAGED_SETTINGS_PATH" not in launches[0].environment
    assert result.quota_exhaustion is None and broker.revoked and broker.calls


@pytest.mark.parametrize("scenario", ["settings_drift", "init_drift", "foreign_init"])
async def test_effective_configuration_drift_rejects_before_user_or_callback(scenario):
    broker = _Broker()
    result = await _gateway(scenario, broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )

    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.quota_exhaustion is None and broker.calls == [] and broker.revoked


@pytest.mark.parametrize("scenario", ["duplicate_early_init", "late_init_drift"])
async def test_init_metadata_must_be_unique_and_match_the_effective_policy(scenario):
    broker = _Broker()
    result = await _gateway(scenario, broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )

    assert result.failure in {SubscriptionFailure.PROTOCOL, SubscriptionFailure.UNAVAILABLE}
    assert result.quota_exhaustion is None and broker.calls == [] and broker.revoked


@pytest.mark.parametrize("value", [None, "", ".", "missing", 1])
def test_client_home_requires_an_existing_absolute_directory(tmp_path, value):
    with pytest.raises(ValueError, match="explicit existing absolute client home"):
        ClaudeInstallation(
            executable=sys.executable,
            cwd=str(tmp_path),
            model="claude-test",
            effort="medium",
            client_home=value,
            account="test-account",
            executable_digest="b" * 64,
        )


def test_file_is_not_an_authentication_directory(tmp_path):
    file = tmp_path / "file"
    file.write_text("fixture")
    with pytest.raises(ValueError, match="explicit existing absolute client home"):
        ClaudeInstallation(
            executable=sys.executable,
            cwd=str(tmp_path),
            model="claude-test",
            effort="medium",
            client_home=str(file),
            account="test-account",
            executable_digest="b" * 64,
        )


def test_home_is_canonical_and_omitted_from_representations(tmp_path):
    home = tmp_path / "dedicated-client-home"
    home.mkdir()
    installation = ClaudeInstallation(
        executable=sys.executable,
        cwd=str(tmp_path),
        model="claude-test",
        effort="medium",
        client_home=str(home / ".." / home.name),
        account="test-account",
        executable_digest=_EXECUTABLE_DIGEST,
    )
    assert installation.client_home == str(home.resolve())
    scope = capability_scope(_anthropic_request())
    report = _Verifier(_report(client_home=str(home.resolve()))).verify(installation, scope)
    assert report.admits(installation, scope)
    assert "dedicated-client-home" not in repr(installation) + repr(report)


@pytest.mark.parametrize(
    "changes",
    [
        {"client_home": None},
        {"client_home": "relative"},
        {"installed_version": "2.1.268"},
        {"allowance_only_enforced": False},
        {"hooks_disabled": False},
    ],
)
def test_home_binding_does_not_supply_other_missing_capabilities(changes):
    gateway = _gateway("success")
    scope = capability_scope(_anthropic_request())
    report = _Verifier(_report(**changes)).verify(gateway._installation, scope)
    assert not report.admits(gateway._installation, scope)
