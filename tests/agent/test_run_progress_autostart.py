import plistlib

from agent.run_progress_autostart import (
    LABEL,
    build_launch_agent_plist,
    default_launch_agent_path,
    main,
    write_launch_agent_plist,
)


def test_build_launch_agent_plist_points_at_repo_viewer(tmp_path):
    repo_root = tmp_path / "sts2-cli"
    repo_root.mkdir()
    plist = build_launch_agent_plist(repo_root, host="127.0.0.1", port=8765)

    assert plist["Label"] == LABEL
    assert plist["WorkingDirectory"] == str(repo_root)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == 10
    assert plist["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1"}
    assert plist["ProgramArguments"] == [
        str(repo_root / ".venv" / "bin" / "python"),
        str(repo_root / "agent" / "run_progress_viewer.py"),
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ]
    assert plist["StandardOutPath"] == str(repo_root / "logs" / "run_progress_viewer.launchd.out.log")
    assert plist["StandardErrorPath"] == str(repo_root / "logs" / "run_progress_viewer.launchd.err.log")


def test_write_launch_agent_plist_creates_parent_and_valid_xml(tmp_path):
    plist_path = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    plist = build_launch_agent_plist(tmp_path / "repo", host="127.0.0.1", port=8765)

    write_launch_agent_plist(plist_path, plist)

    with plist_path.open("rb") as handle:
        assert plistlib.load(handle) == plist


def test_default_launch_agent_path_uses_user_launch_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_launch_agent_path() == tmp_path / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def test_print_plist_cli_outputs_valid_plist_without_launchctl(tmp_path, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    exit_code = main(["print-plist", "--repo-root", str(repo_root), "--port", "9876"])

    assert exit_code == 0
    output = capsys.readouterr().out.encode("utf-8")
    plist = plistlib.loads(output)
    assert plist["ProgramArguments"][-1] == "9876"
    assert plist["WorkingDirectory"] == str(repo_root)
