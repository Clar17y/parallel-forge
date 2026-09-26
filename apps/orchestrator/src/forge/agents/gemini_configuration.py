"""Forge-owned launch files for the pinned official Gemini CLI.

This is configuration materialization, not account/capability discovery. The
gateway still requires trusted verification of the dedicated home, remote policy,
exact model/effort and client conformance before admitting an installation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID


def launch_settings(model: str, effort: str | None) -> dict[str, object]:
    # These are ordinary settings in 0.59.0. Local `admin` settings are replaced
    # by remote policy and must never be used as an isolation control.
    policy = {
        "model": model,
        "isLastResort": True,
        "maxAttempts": 1,
        "actions": dict.fromkeys(("terminal", "transient", "not_found", "unknown"), "prompt"),
        "stateTransitions": dict.fromkeys(
            ("terminal", "transient", "not_found", "unknown"), "terminal"
        ),
    }
    main_config: dict[str, object] = {"model": model}
    if effort is not None:
        main_config["generateContentConfig"] = {"thinkingConfig": {"thinkingLevel": effort.upper()}}
    return {
        "security": {
            "auth": {
                "selectedType": "oauth-personal",
                "enforcedType": "oauth-personal",
                "useExternal": False,
            },
            "folderTrust": {"enabled": False},
        },
        "tools": {"core": [], "disableLLMCorrection": True, "sandbox": False},
        "billing": {"overageStrategy": "never"},
        "hooksConfig": {"enabled": False},
        "skills": {"enabled": False},
        "mcp": {"allowed": ["forge"]},
        "general": {"maxAttempts": 1, "plan": {"enabled": False, "modelRouting": False}},
        "model": {"name": model, "disableLoopDetection": True, "skipNextSpeakerCheck": True},
        "modelConfigs": {
            "customOverrides": [{"match": {"isChatModel": True}, "modelConfig": main_config}],
            "modelIdResolutions": {model: {"default": model, "contexts": []}},
            "modelChains": {
                key: [policy]
                for key in ("default", "preview", "auto-default", "auto-preview", "lite")
            },
        },
        "experimental": {
            "enableAgents": False,
            "extensionReloading": False,
            "autoMemory": False,
            "contextManagement": False,
            "dynamicModelConfiguration": True,
        },
        "context": {"includeDirectoryTree": False, "loadMemoryFromIncludeDirectories": False},
        "ide": {"enabled": False},
        "privacy": {"usageStatisticsEnabled": False},
        "telemetry": {"enabled": False},
        # --ignore-env/ignoreLocalEnv still searches parents and the auth home.
        # Empty owned files at BOTH supported locations stop that search for
        # either workspace-trust result. Never rely on environment scrubbing alone.
        "advanced": {"ignoreLocalEnv": False},
    }


class GeminiLaunchDirectory:
    """Exclusive attempt files; never modify or copy official-client credentials."""

    def __init__(self, base: str, attempt_id: UUID) -> None:
        self.path = Path(base) / f".forge-gemini-{attempt_id}"
        self._owned = False
        self._files: list[Path] = []
        self._directories: list[Path] = []
        self._identities: dict[Path, tuple[int, int]] = {}

    def _remember(self, path: Path) -> None:
        info = path.lstat()
        self._identities[path] = (info.st_dev, info.st_ino)

    def prepare(self, *, home: str, model: str, effort: str | None, prompt: str) -> dict[str, str]:
        self.path.mkdir(mode=0o700)
        self._owned = True
        self._directories.append(self.path)
        self._remember(self.path)
        gemini = self.path / ".gemini"
        gemini.mkdir(mode=0o700)
        self._directories.append(gemini)
        self._remember(gemini)
        for name, contents in {
            "system.md": prompt,
            "settings.json": json.dumps(launch_settings(model, effort), sort_keys=True) + "\n",
            "defaults.json": "{}\n",
            ".env": "",
            ".gemini/.env": "",
        }.items():
            path = self.path / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._files.append(path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                self._remember(path)
                output.write(contents)
        return {
            "GEMINI_SYSTEM_MD": str(self.path / "system.md"),
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(self.path / "settings.json"),
            "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(self.path / "defaults.json"),
            "GEMINI_CLI_HOME": home,
            "HOME": home,
            "USERPROFILE": home,
            "NO_BROWSER": "1",
        }

    def cleanup(self) -> None:
        if not self._owned:
            return
        # Do not recurse, remove a reused directory, follow replaced directories,
        # or delete unexpected client/operator evidence. An uncertain process must
        # retain these files; only the gateway's settled path calls cleanup.
        for path, identity in self._identities.items():
            info = path.lstat()
            if (info.st_dev, info.st_ino) != identity or path.resolve(strict=True) != path:
                raise OSError("Gemini launch directory identity changed")
        for directory in self._directories:
            expected = {
                path for path in self._files + self._directories if path.parent == directory
            }
            if set(directory.iterdir()) != expected:
                raise OSError("Gemini launch contains unexpected evidence")
        for path in reversed(self._files):
            path.unlink()
        for directory in reversed(self._directories):
            directory.rmdir()
        self._owned = False
