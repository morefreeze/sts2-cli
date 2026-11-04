# Sts2Advisor 预编译插件

《杀戮尖塔 2》（Slay the Spire 2）的游戏内建议模组。它在屏幕右下角的面板里给出**战斗外**决策的建议和理由：选牌奖励、篝火（休息处）、事件、商店、地图路线。战斗本身不归它管，交给 CombatSolver 模组。

可选的「全托管」（autopilot）会替你点掉所有战斗外的选择，直到一局结束。

这个目录是**可以直接复制的成品**：`Sts2Advisor.dll` 和 `Sts2Advisor.json` 已经编译好，不需要在你的电脑上装 .NET。但模组只是一个「面板 + 点击器」，建议本身由仓库里的 Python 服务（`agent/advisor_server.py`，监听 `127.0.0.1:8767`）算出来，所以**整个仓库**都要在这台电脑上，不能只拷这个目录。

| 文件 | 作用 |
| --- | --- |
| `Sts2Advisor.dll`, `Sts2Advisor.json` | 模组本体，安装时复制到游戏目录的 `mods\Sts2Advisor\` |
| `BUILD_INFO.txt` | 这份 DLL 是哪个源码提交、哪个游戏版本、什么时候编译的 |
| `README.md` | 本文件 |

## 版本要求

- **游戏版本必须和 `BUILD_INFO.txt` 里的 `game version` 一致**（目前是 v0.111.0）。模组是按那一版游戏的 `sts2.dll` 编译的，游戏更新后内部类型一变，模组会停用，面板显示「游戏版本未适配」。这时要回到 Mac 上用 `tools/package_advisor_mod.sh` 重新打包，再 `git pull`。
- **CombatSolver（创意工坊模组）**：想让全托管把战斗也打完就需要它；只看面板上的建议不需要。全托管本身不会碰进行中的战斗。
- Python 3.11（服务用）。依赖见 `requirements-advisor.txt`：numpy、gymnasium、scikit-learn，都固定了版本（`deck_predictor_v2.pkl` 是 scikit-learn 的 pickle，换版本可能读不出来）。
- git 是可选的：没有它服务照样能用，只是 `/health` 里的 commit 显示 unknown。

## Windows 安装

1. 装 Python 3.11（python.org，勾选 py launcher 或 Add to PATH）。
2. 把整个仓库放到本机，例如 `git clone` 或拷贝，路径里最好没有中文和空格（有也能用，但出问题时不好排查）。
3. 退出游戏（游戏运行时安装脚本会拒绝，因为覆盖已加载的 DLL 会把它写坏）。
4. 在仓库根目录打开 PowerShell，运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\install_advisor_mod.ps1 -SetupPython
   ```

   脚本会：
   - 自己在 Steam 注册表和 `libraryfolders.vdf` 里找到 `steamapps\common\Slay the Spire 2`（找不到就加 `-GameDir "D:\SteamLibrary\steamapps\common\Slay the Spire 2"`）；
   - 把 `Sts2Advisor.dll` 和 `Sts2Advisor.json` 复制到 `<游戏目录>\mods\Sts2Advisor\`；
   - 在 `%APPDATA%\SlayTheSpire2\Sts2Advisor\config.json` 写入默认配置（已存在就不动）；
   - `-SetupPython`：建 `.venv`（先 `py -3.11 -m venv`，不行再 `python -m venv`）并安装 `requirements-advisor.txt`。
5. 启动游戏。进游戏后右下角出现面板。`F8` 显示或隐藏面板。

不想用脚本也可以手动做：把 `Sts2Advisor.dll`、`Sts2Advisor.json` 复制到 `<游戏目录>\mods\Sts2Advisor\`（游戏 `.exe` 旁边的 `mods` 文件夹，没有就新建），自己建好 `.venv` 装依赖，再按下面「config.json」写配置。

**Windows 上没有热重载**：Mac 上提交策略改动后约 30 秒内正在运行的服务会自己重载，Windows 上不会（`os.execv` 在 Windows 上会新起进程并退出旧进程，模组会以为服务崩了），所以模组在 Windows 上不传 `--reload-on-commit`，日志里有一行 `hot reload is off on Windows`。`git pull` 之后重启游戏即可。

**更新**：`git pull` 之后重新运行安装脚本（Steam 更新或「验证游戏文件完整性」也可能清掉 `mods\`，同样重新运行）。

## macOS 安装

在仓库根目录运行 `tools/install_advisor_mod.sh`。它会**自己编译**（需要 `~/.dotnet-arm64/dotnet`）并装进 `SlayTheSpire2.app/Contents/MacOS/mods/Sts2Advisor/`，不用这个目录里的预编译件。写配置的位置是 `~/Library/Application Support/SlayTheSpire2/Sts2Advisor/config.json`。

## config.json

位置：Windows `%APPDATA%\SlayTheSpire2\Sts2Advisor\config.json`，macOS `~/Library/Application Support/SlayTheSpire2/Sts2Advisor/config.json`。改完要**重启游戏**才生效。缺的键或类型不对的键取默认值；整个文件不是合法 JSON 时用全部默认值，并在日志里写一行 `bad ...config.json`。路径用正斜杠，或把反斜杠写成 `\\`。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `repo_path` | 空 | 仓库根目录。必须填，服务从这里启动 |
| `python` | 空 | Python 解释器。留空则用 `<repo_path>/.venv/bin/python`（存在的话），否则 `<repo_path>/.venv/Scripts/python.exe`（Windows） |
| `port` | `8767` | 建议服务的端口，1 到 65535 |
| `autostart` | `true` | 模组是否自己启动服务。`false` 则等你手动启动，或复用已在该端口运行的服务 |
| `hotkey` | `"F8"` | 显示或隐藏面板（Godot 按键名）。Mac 笔记本上 F8 是媒体键，要按 Fn+F8 |
| `autopilot_hotkey` | `"F9"` | 开关全托管 |
| `step_hotkey` | `"F10"` | 只执行一步（一个建议的选择，停在下一个需要决策的地方） |
| `step_pause_seconds` | `2.5` | 全托管每一步之后停多久再处理下一屏，限制在 0 到 10，0 为不停 |
| `instant_mode` | `false` | 游戏启动后把游戏速度设为 Instant |
| `panel_language` | `"auto"` | 面板语言：`auto`（跟游戏）、`zh`、`en` |
| `map_policy` | `"lookahead"` | 路线策略：`lookahead`（默认，规划到 Boss 的整条路径）或 `hpaware`（只看下一步） |

两个热键设成同一个键会禁用全托管热键（面板上的按钮仍然可用）。`map_policy` 是服务启动时读的，换了以后要结束旧的服务进程（见下）再重启游戏。

战役模式（无人值守连续打多局）用同目录下的 `campaign.json` 配置，格式见仓库 `CLAUDE.md` 的 Sts2Advisor 一节。

## 排查问题

**日志在哪**：`%APPDATA%\SlayTheSpire2\Sts2Advisor\Sts2Advisor.log`（macOS 是上面那个 Library 路径下同名文件）。模组加载、服务启动、服务的标准错误输出（带 `[advisor_server]` 前缀）都在这里。每个决策和服务的回复在同目录的 `advisor_trace.jsonl`。

- **面板不出现，日志里根本没有 `loaded; config ...`**：模组没被加载。确认 `<游戏目录>\mods\Sts2Advisor\` 里有 `Sts2Advisor.dll` 和 `Sts2Advisor.json`，（如果游戏的模组界面有开关，也要确认它是启用的）。Steam 更新或验证文件后 `mods\` 可能被删，重新运行安装脚本。
- **面板显示「游戏版本未适配」**：游戏版本和 `BUILD_INFO.txt` 不一致，模组读游戏内部类型时报了 `TypeLoadException` 或 `MissingMemberException`，本局已停用。需要用新版游戏重新打包 DLL。
- **面板显示离线 / 日志有 `cannot start the advice service: python=... script=...`**：`python` 或 `repo_path` 不对。日志里这行会打出实际用的两个路径，逐个确认文件存在；`.venv` 没建就重新运行安装脚本并带上 `-SetupPython`。
- **日志有 `sidecar exited with code N`**：服务启动后崩了，原因在前面几行 `[advisor_server]` 的 Python traceback 里。常见的是缺包（`ModuleNotFoundError`，重新 `-SetupPython`）或缺数据文件（`git pull`，`data\` 里要有 `deck_predictor_v2.pkl` 等）。
- **端口 8767 被占用**：先看是不是上次留下的服务，PowerShell 里 `netstat -ano | findstr 8767` 找到 pid，`taskkill /PID <pid> /F`；或者在 config.json 里改 `port`。模组会复用已经在该端口健康运行的服务，所以旧服务如果跑的是旧代码，也要先结束它。正常情况下游戏退出时服务会自己退出。
- **想手动试服务**：在仓库根目录运行 `.venv\Scripts\python.exe agent\advisor_server.py --port 8767`，然后访问 `http://127.0.0.1:8767/health`。
- **PowerShell 说脚本被禁止运行**：用上面带 `-ExecutionPolicy Bypass` 的写法运行。
- **建议和 Mac 上不一样**：先确认 `git pull` 到了同一个提交、`.venv` 里的包版本和 `requirements-advisor.txt` 一致，以及没有设置 `STS2_` 开头的环境变量（服务启动时会把被改动的行为开关写进日志的 WARNING）。

## 维护者：刷新这个目录

模组源码（`src/Sts2Advisor`、`src/Sts2Advisor.Core`、`src/Sts2Shared`）改动并提交之后，在 Mac 上运行 `tools/package_advisor_mod.sh`，它用 Release 配置编译 git HEAD，更新这里的 DLL、`Sts2Advisor.json` 和 `BUILD_INFO.txt`，然后把结果提交。Windows 这边 `git pull` 再运行安装脚本即可。
