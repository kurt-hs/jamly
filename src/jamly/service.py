"""Install and control a per-user backend using the currently installed Python."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path, PurePath
import plistlib
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from .bootstrap import summarize, detect_ffmpeg, check_runtime
from .config import add_pipeline_arguments, pairing_token, pipeline_options, runtime_paths, token_command
from .runtime import browser_assets, module_command

LABEL = "com.jamly.backend"
UNIT = "jamly.service"
TASK = "Jamly"


def server_command(python: str, config: Path) -> list[str]:
    return module_command("jamly.server", "--config", str(config), executable=python)


def _systemd_quote(value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise ValueError("Service paths must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_service(platform: str, python: str, config: PurePath, state: PurePath,
                   *, username: str = "", home: Path | None = None) -> tuple[Path, bytes]:
    home = home or Path.home()
    command = server_command(python, config)
    if platform == "linux":
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
        content = "\n".join((
            "[Unit]", "Description=Jamly local audio backend", "After=network.target", "",
            "[Service]", "Type=simple", "WorkingDirectory=" + _systemd_quote(str(state)),
            "ExecStart=" + " ".join(_systemd_quote(arg).replace("$", "$$") for arg in command),
            "Restart=on-failure", "RestartSec=5", "", "[Install]", "WantedBy=default.target", "",
        ))
        return root / "systemd" / "user" / UNIT, content.encode()
    if platform == "darwin":
        data = {
            "Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(state),
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(state / "server.log"), "StandardErrorPath": str(state / "server-error.log"),
        }
        return home / "Library" / "LaunchAgents" / (LABEL + ".plist"), plistlib.dumps(data)
    if platform == "win32":
        root = ET.Element("Task", version="1.2", xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task")
        trigger = ET.SubElement(ET.SubElement(root, "Triggers"), "LogonTrigger")
        ET.SubElement(trigger, "Enabled").text = "true"
        ET.SubElement(trigger, "UserId").text = username
        principal = ET.SubElement(ET.SubElement(root, "Principals"), "Principal", id="Author")
        ET.SubElement(principal, "UserId").text = username
        ET.SubElement(principal, "LogonType").text = "InteractiveToken"
        ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
        settings = ET.SubElement(root, "Settings")
        ET.SubElement(settings, "ExecutionTimeLimit").text = "PT0S"
        ET.SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
        ET.SubElement(settings, "StopIfGoingOnBatteries").text = "false"
        restart = ET.SubElement(settings, "RestartOnFailure")
        ET.SubElement(restart, "Interval").text = "PT1M"
        ET.SubElement(restart, "Count").text = "3"
        action = ET.SubElement(ET.SubElement(root, "Actions", Context="Author"), "Exec")
        ET.SubElement(action, "Command").text = command[0]
        ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(command[1:])
        ET.SubElement(action, "WorkingDirectory").text = str(state)
        return Path(state) / "scheduled-task.xml", ET.tostring(root, encoding="utf-16", xml_declaration=True)
    raise ValueError("Service installation is supported on Windows, macOS and Linux")


def service_commands(platform: str, action: str, definition: Path) -> list[list[str]]:
    if platform == "linux":
        commands = {
            "install": [["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "enable", "--now", UNIT]],
            "uninstall": [["systemctl", "--user", "disable", "--now", UNIT]],
            "start": [["systemctl", "--user", "start", UNIT]],
            "stop": [["systemctl", "--user", "stop", UNIT]],
            "restart": [["systemctl", "--user", "restart", UNIT]],
            "status": [["systemctl", "--user", "status", UNIT, "--no-pager"]],
        }
    elif platform == "darwin":
        domain = "gui/%d" % os.getuid()
        target = domain + "/" + LABEL
        commands = {
            "install": [["launchctl", "bootstrap", domain, str(definition)]],
            "uninstall": [["launchctl", "bootout", target]],
            "start": [["launchctl", "bootstrap", domain, str(definition)]],
            "stop": [["launchctl", "bootout", target]],
            "restart": [["launchctl", "kickstart", "-k", target]],
            "status": [["launchctl", "print", target]],
        }
    elif platform == "win32":
        commands = {
            "install": [["schtasks.exe", "/Create", "/TN", TASK, "/XML", str(definition), "/F"],
                        ["schtasks.exe", "/Run", "/TN", TASK]],
            "uninstall": [["schtasks.exe", "/End", "/TN", TASK],
                          ["schtasks.exe", "/Delete", "/TN", TASK, "/F"]],
            "start": [["schtasks.exe", "/Run", "/TN", TASK]],
            "stop": [["schtasks.exe", "/End", "/TN", TASK]],
            "restart": [["schtasks.exe", "/End", "/TN", TASK],
                        ["schtasks.exe", "/Run", "/TN", TASK]],
            "status": [["schtasks.exe", "/Query", "/TN", TASK, "/V", "/FO", "LIST"]],
        }
    else:
        raise ValueError("Unsupported service platform")
    return commands[action]


def export_browser_assets(state: Path, kind: str) -> Path:
    source = browser_assets(kind)
    destination = state / "browser" / kind
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file() and path.suffix in (".js", ".json", ".html") and not path.name.endswith("_test.js"):
            shutil.copyfile(path, destination / path.name)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage the per-user Jamly backend")
    parser.add_argument("action", choices=(
        "install", "uninstall", "start", "stop", "restart", "status", "token", "run", "browser", "userscript",
    ))
    add_pipeline_arguments(parser)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dry-run", action="store_true", help="Print installation files and commands without changing the OS")
    args = parser.parse_args(argv)
    state, queue_path = runtime_paths(args)
    settings_path = state / "server.json"
    try:
        if args.action == "token":
            print(pairing_token(state))
            return 0
        if args.action in ("browser", "userscript"):
            kind = "extension" if args.action == "browser" else "userscripts"
            print(export_browser_assets(state, kind))
            return 0
        if args.action == "run":
            from .server import main as serve
            return serve(["--config", str(settings_path)] if settings_path.exists()
                         else ["--state-dir", str(state), "--port", str(args.port)])
        username = getpass.getuser()
        if sys.platform == "win32" and os.environ.get("USERDOMAIN"):
            username = os.environ["USERDOMAIN"] + "\\" + username
        definition, content = render_service(sys.platform, sys.executable, settings_path, state,
                                             username=username)
        commands = service_commands(sys.platform, args.action, definition)
        if args.action == "install":
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            report = summarize()
            if args.ffmpeg_dir:
                from dataclasses import replace
                report = replace(report, ffmpeg=detect_ffmpeg([args.ffmpeg_dir]))
            report = check_runtime(report)
            if not report.ready:
                raise ValueError("Run jamly-doctor and install missing dependencies before installing the service")
            settings = dict(pipeline_options(args), state_dir=str(state), host="127.0.0.1",
                            port=args.port, ffmpeg_dir=report.ffmpeg.path, queue=str(queue_path))
            if args.dry_run:
                print(json.dumps({"config": settings, "definition": str(definition), "commands": commands}, indent=2))
                print(content.decode("utf-16" if sys.platform == "win32" else "utf-8"))
                return 0
            state.mkdir(parents=True, exist_ok=True)
            pairing_token(state)
            settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            definition.parent.mkdir(parents=True, exist_ok=True)
            definition.write_bytes(content)
        elif args.dry_run:
            print(json.dumps(commands, indent=2))
            return 0
        for command in commands:
            subprocess.run(command, check=True)
        if args.action == "uninstall":
            definition.unlink(missing_ok=True)
            if sys.platform == "linux":
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            print("Service removed. Your queue, pairing token and exports were kept.")
        elif args.action == "install":
            print("Installed with Python: %s" % sys.executable)
            print("Pair your browser with: " + token_command(state))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("jamly-service: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
