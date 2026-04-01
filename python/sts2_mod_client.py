#!/usr/bin/env python3
"""
Async HTTP client for Sts2CliMod embedded server.

Provides interactive CLI for sending commands to the mod and receiving
game state updates via REST API.
"""

import asyncio
import aiohttp
import json
import sys
from typing import Any, Dict, Optional


class Sts2ModClient:
    """Async HTTP client for Sts2CliMod."""

    def __init__(self, host: str = "localhost", port: int = 12580):
        self.base_url = f"http://{host}:{port}"
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def health(self) -> Dict[str, Any]:
        """Check server health."""
        if not self.session:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        async with self.session.get(f"{self.base_url}/health") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def send_command(self, cmd: str, **kwargs) -> Dict[str, Any]:
        """Send a command to the mod."""
        if not self.session:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        payload = {"cmd": cmd, **kwargs}
        async with self.session.post(
            f"{self.base_url}/api/command",
            json=payload,
            headers={"Content-Type": "application/json"}
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def start_run(self, character: str = "Ironclad", ascension: int = 0,
                       seed: Optional[str] = None, lang: str = "en") -> Dict[str, Any]:
        """Start a new run."""
        return await self.send_command("start_run",
                                      character=character,
                                      ascension=ascension,
                                      seed=seed,
                                      lang=lang)

    async def get_map(self) -> Dict[str, Any]:
        """Get current map state."""
        return await self.send_command("get_map")

    async def get_state(self) -> Dict[str, Any]:
        """Get current game state."""
        async with self.session.get(f"{self.base_url}/state") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def action(self, action_name: str, **args) -> Dict[str, Any]:
        """Execute an action."""
        return await self.send_command("action", action=action_name, args=args if args else None)

    async def interactive_loop(self):
        """Run interactive command loop."""
        print(f"Connected to Sts2CliMod at {self.base_url}")
        print("Type 'help' for commands, 'quit' to exit")

        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, input, "sts2> "
                )
                line = line.strip()

                if not line:
                    continue
                if line.lower() in ("quit", "exit", "q"):
                    print("Goodbye!")
                    break
                if line.lower() == "help":
                    self.show_help()
                    continue
                if line.lower() == "health":
                    result = await self.health()
                    print(json.dumps(result, indent=2))
                    continue

                # Parse as JSON command
                try:
                    if line.startswith("{"):
                        cmd_data = json.loads(line)
                    else:
                        parts = line.split(maxsplit=1)
                        cmd_data = {"cmd": parts[0]}
                        if len(parts) > 1:
                            cmd_data["args"] = parts[1]

                    result = await self.send_command(**cmd_data)
                    print(json.dumps(result, indent=2))
                except json.JSONDecodeError as e:
                    print(f"Error: Invalid JSON - {e}")
                except aiohttp.ClientError as e:
                    print(f"Error: {e}")

            except KeyboardInterrupt:
                print("\nUse 'quit' to exit")
            except EOFError:
                print("\nGoodbye!")
                break

    def show_help(self):
        """Show available commands."""
        print("""
Available Commands:
  help              Show this help message
  health            Check server health
  quit/exit/q       Exit the client
  start_run         Start a new run
  get_map           Get current map state
  get_state         Get current game state
  action            Execute an action

Command Format:
  { "cmd": "command_name", "key": "value" }
  OR
  command_name arg1 arg2

Examples:
  {"cmd": "start_run", "character": "Ironclad"}
  {"cmd": "action", "action": "select_map_node", "args": {"col": 0, "row": 0}}
  get_state
""")


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Sts2CliMod Client")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=12580, help="Server port")
    parser.add_argument("--cmd", help="Send single command and exit")
    parser.add_argument("--health", action="store_true", help="Check health and exit")

    args = parser.parse_args()

    async with Sts2ModClient(args.host, args.port) as client:
        if args.health:
            result = await client.health()
            print(json.dumps(result, indent=2))
            return

        if args.cmd:
            try:
                cmd_data = json.loads(args.cmd) if args.cmd.startswith("{") else {"cmd": args.cmd}
                result = await client.send_command(**cmd_data)
                print(json.dumps(result, indent=2))
            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            return

        # Interactive mode
        await client.interactive_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)
