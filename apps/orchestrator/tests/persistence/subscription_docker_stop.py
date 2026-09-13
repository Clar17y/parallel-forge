"""Observe a real policy check's container without replacing its launch or stop."""

import asyncio
import json
import re
import subprocess

from forge.tools.docker import DockerRunner

_FORMAT = (
    '{"container_id":{{json .Id}},"name":{{json .Name}},'
    '"started_at":{{json .State.StartedAt}},"running":{{json .State.Running}},'
    '"image_reference":{{json .Config.Image}},"user":{{json .Config.User}},'
    '"network_mode":{{json .HostConfig.NetworkMode}},'
    '"readonly_rootfs":{{json .HostConfig.ReadonlyRootfs}},'
    '"privileged":{{json .HostConfig.Privileged}}}'
)


class DockerCheckObservation:
    def __init__(self, monkeypatch, image):
        self.image = image
        self.name = None
        self.before = None
        self.after = None
        build = DockerRunner.build_argv

        def observed_build(runner, **kwargs):
            argv = build(runner, **kwargs)
            if kwargs["spec"].name == "slow-unit":
                assert self.name is None
                self.name = argv[argv.index("--name") + 1]
            return argv

        monkeypatch.setattr(DockerRunner, "build_argv", observed_build)

    async def _inspect(self, reference):
        return await asyncio.to_thread(
            subprocess.run,
            ["docker", "container", "inspect", "--format", _FORMAT, reference],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    async def assert_running(self):
        assert self.name is not None
        result = await self._inspect(self.name)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert re.fullmatch(r"[a-f0-9]{64}", value["container_id"])
        assert value["name"] == "/" + self.name and value["running"] is True
        assert value["started_at"] and not value["started_at"].startswith("0001-")
        assert value["image_reference"] == self.image
        assert value["user"] == "10001:10001" and value["network_mode"] == "none"
        assert value["readonly_rootfs"] is True and value["privileged"] is False
        self.before = value

    async def assert_gone(self):
        assert self.before is not None
        identity = self.before["container_id"]
        result = await self._inspect(identity)
        assert result.returncode == 1
        assert re.fullmatch(
            r"(?:Error(?: response from daemon)?:\s*)?No such (?:container|object):\s*"
            + re.escape(identity),
            result.stderr.strip(),
            re.IGNORECASE,
        ), result.stderr
        self.after = {"container_id": identity, "gone": True}
