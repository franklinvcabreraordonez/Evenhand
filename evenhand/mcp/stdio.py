"""The stdio transport: the proxy launches the real tool server as a subprocess, and passes every line between
the agent's host and that server, looking only at the tool calls it was asked to witness."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import BinaryIO, Dict, List, Optional

from . import rpc
from .core import Engine

_SHUTDOWN_GRACE = 5.0


def resolve(argv: List[str]) -> List[str]:
    """Finds the program on the PATH the way a shell would, so that npx.cmd and the like work on Windows."""
    found = shutil.which(argv[0])
    return [found or argv[0]] + argv[1:]


def run_stdio(engine: Engine, argv: List[str], *, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
              stdin: Optional[BinaryIO] = None, stdout: Optional[BinaryIO] = None, stderr: Optional[BinaryIO] = None) -> int:
    """Runs until the server exits or the client closes its input. Returns the server's exit code."""
    client_in = stdin if stdin is not None else sys.stdin.buffer
    client_out = stdout if stdout is not None else sys.stdout.buffer
    client_err = stderr if stderr is not None else sys.stderr.buffer
    merged_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.Popen(resolve(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=merged_env, cwd=cwd)
    except OSError as exc:
        engine.log(f"cannot start {argv[0]}: {exc}")
        return 127
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    pending: Dict = {}
    out_lock = threading.Lock()

    def emit(data: bytes) -> None:
        with out_lock:
            client_out.write(data if data.endswith(b"\n") else data + b"\n")
            client_out.flush()

    def client_to_server() -> None:
        try:
            for raw in iter(client_in.readline, b""):
                if b"tools/call" in raw or b"notifications/cancelled" in raw:
                    message = rpc.parse(raw)
                    if message is not None:
                        try:
                            reply = engine.screen(message, pending)
                        except Exception as exc:  # never let the proxy's own trouble stop the traffic
                            engine.log(f"could not screen a message: {exc}")
                            reply = None
                        if reply is not None:
                            emit(reply)
                            continue
                try:
                    proc.stdin.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    break
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    def server_to_client() -> None:
        for raw in iter(proc.stdout.readline, b""):
            if pending:
                message = rpc.parse(raw)
                if message is not None:
                    try:
                        engine.observe(message, pending)
                    except Exception as exc:
                        engine.log(f"could not look at an answer: {exc}")
            try:
                emit(raw)
            except (BrokenPipeError, OSError, ValueError):
                break

    def server_stderr() -> None:
        for chunk in iter(lambda: proc.stderr.read1(4096), b""):
            try:
                client_err.write(chunk)
                client_err.flush()
            except (BrokenPipeError, OSError, ValueError):
                break

    threads = [threading.Thread(target=fn, name=name, daemon=True) for fn, name in
               ((client_to_server, "client-to-server"), (server_to_client, "server-to-client"), (server_stderr, "server-stderr"))]
    for t in threads:
        t.start()
    code = 0
    try:
        while True:
            try:
                code = proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if not threads[0].is_alive():  # the client has gone: give the server a moment to exit, then stop it
                    try:
                        code = proc.wait(timeout=_SHUTDOWN_GRACE)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            code = proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            code = proc.wait()
                    break
    except KeyboardInterrupt:
        proc.terminate()
        code = proc.wait()
    threads[1].join(timeout=3)  # let the last answers through
    threads[2].join(timeout=1)
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except (OSError, ValueError):
            pass
    engine.close(pending)
    return code