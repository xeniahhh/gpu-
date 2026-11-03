"""Drive Edge through CDP for a real-browser C-role smoke test.

This is an optional acceptance tool. It requires the ``websockets`` package in
the selected Python environment and Microsoft Edge installed on Windows.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import websockets


class CDP:
    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.sequence = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        call_id = self.sequence
        await self.websocket.send(
            json.dumps({"id": call_id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(await self.websocket.recv())
            if message.get("id") != call_id:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})

    async def evaluate(self, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
        return result.get("result", {}).get("value")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_page(port: int, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/list"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                pages = json.load(response)
            candidates = [page for page in pages if page.get("type") == "page"]
            if candidates:
                return candidates[0]["webSocketDebuggerUrl"]
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Edge DevTools endpoint did not become ready")


def _javascript_string(value: str) -> str:
    return json.dumps(value)


async def _run_browser(
    base_url: str,
    token: str,
    output: Path,
    edge_path: Path,
) -> dict[str, Any]:
    port = _free_port()
    checks: list[dict[str, Any]] = []
    screenshots: list[str] = []

    with tempfile.TemporaryDirectory(prefix="fabric-c-browser-") as profile:
        process = subprocess.Popen(
            [
                str(edge_path),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            websocket_url = _wait_for_page(port)
            async with websockets.connect(websocket_url, max_size=8 * 1024 * 1024) as ws:
                cdp = CDP(ws)
                await cdp.call("Page.enable")
                await cdp.call("Runtime.enable")
                await cdp.call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 1440,
                        "height": 1100,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )
                await cdp.call("Page.navigate", {"url": f"{base_url}/workload"})

                ready = await cdp.evaluate(
                    """(async () => {
                      for (let i = 0; i < 200; i++) {
                        if (document.readyState === 'complete') {
                          return {
                            title: document.title,
                            body: document.body.innerText.slice(0, 12000)
                          };
                        }
                        await new Promise(resolve => setTimeout(resolve, 50));
                      }
                      throw new Error('page did not finish loading');
                    })()"""
                )
                checks.append(
                    {
                        "name": "Edge loads workload page",
                        "passed": "工作负载与故障分析" in ready["body"],
                        "detail": {"title": ready["title"]},
                    }
                )

                encoded_token = base64.b64encode(token.encode("ascii")).decode("ascii")
                connected = await cdp.evaluate(
                    f"""(async () => {{
                      const decode = value => atob(value);
                      document.getElementById('token').value = decode({_javascript_string(encoded_token)});
                      document.getElementById('connectButton').click();
                      for (let i = 0; i < 200; i++) {{
                        if (document.getElementById('connection').textContent.includes('已连接')) {{
                          return {{
                            connection: document.getElementById('connection').textContent,
                            scenarios: document.getElementById('scenario').options.length
                          }};
                        }}
                        await new Promise(resolve => setTimeout(resolve, 50));
                      }}
                      throw new Error(document.getElementById('error').textContent);
                    }})()"""
                )
                checks.append(
                    {
                        "name": "browser token connects and loads scenarios",
                        "passed": connected["scenarios"] >= 10,
                        "detail": connected,
                    }
                )

                async def run_scenario(label: str) -> dict[str, Any]:
                    outcome = await cdp.evaluate(
                        f"""(async () => {{
                          const select = document.getElementById('scenario');
                          const index = [...select.options].findIndex(
                            option => option.textContent.includes({_javascript_string(label)})
                          );
                          if (index < 0) throw new Error('scenario not found: {label}');
                          select.value = String(index);
                          select.dispatchEvent(new Event('change'));
                          const previousIdentity = document.getElementById('identity').textContent;
                          document.getElementById('runButton').click();
                          for (let i = 0; i < 400; i++) {{
                            const status = document.getElementById('status');
                            const body = document.body.innerText;
                            const identity = document.getElementById('identity').textContent;
                            if (identity !== previousIdentity
                                && status.classList.contains('success')
                                && body.includes('状态时间线')) {{
                              return {{
                                status: status.textContent,
                                identity,
                                timeline: body.includes('仿真记录回放'),
                                checks: document.getElementById('checks').innerText.slice(0, 4000),
                                comparison: document.getElementById('comparison').innerText.slice(0, 4000)
                              }};
                            }}
                            if (status.classList.contains('failed')) {{
                              throw new Error(document.getElementById('error').textContent);
                            }}
                            await new Promise(resolve => setTimeout(resolve, 50));
                          }}
                          throw new Error('scenario did not complete: {label}');
                        }})()"""
                    )
                    shot = await cdp.call(
                        "Page.captureScreenshot",
                        {"format": "png", "captureBeyondViewport": True},
                    )
                    screenshot_name = (
                        "browser_competition.png"
                        if "共享链路" in label
                        else "browser_fault_no_spare.png"
                    )
                    screenshot_path = output.parent / screenshot_name
                    screenshot_path.write_bytes(base64.b64decode(shot["data"]))
                    screenshots.append(str(screenshot_path))
                    return outcome

                competition = await run_scenario("跨机架共享链路")
                checks.append(
                    {
                        "name": "browser submits and renders shared-link competition",
                        "passed": competition["status"] == "评估完成"
                        and competition["timeline"]
                        and "输入" in competition["identity"],
                        "detail": competition,
                    }
                )

                fault = await run_scenario("故障后没有备用资源")
                checks.append(
                    {
                        "name": "browser submits and renders no-spare fault recovery",
                        "passed": fault["status"] == "评估完成"
                        and fault["timeline"]
                        and fault["identity"] != competition["identity"]
                        and "丢失步数" in fault["comparison"]
                        and "重启次数" in fault["comparison"],
                        "detail": fault,
                    }
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    return {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "kind": "LIVE_BROWSER_ACCEPTANCE",
        "browser": str(edge_path),
        "base_url": base_url,
        "checks": checks,
        "screenshots": screenshots,
        "passed": all(check["passed"] for check in checks),
        "passed_count": sum(check["passed"] for check in checks),
        "total_count": len(checks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--edge",
        type=Path,
        default=Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    )
    args = parser.parse_args()
    token = os.getenv("FABRIC_EDITOR_TOKEN", "")
    if not token:
        parser.error("FABRIC_EDITOR_TOKEN is required")
    if not args.edge.is_file():
        parser.error(f"Edge executable not found: {args.edge}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = asyncio.run(_run_browser(args.base_url, token, args.output, args.edge))
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(
        f"C browser acceptance: {evidence['passed_count']}/{evidence['total_count']} "
        f"checks passed; evidence={args.output}"
    )
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
