"""One private Xvfb per Linux crawler process; reuse an owner-provided display."""

import os
import select
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager


@contextmanager
def private_display():
    if sys.platform != "linux" or not shutil.which("Xvfb"):
        raise RuntimeError("Camoufox Linux 运行需要虚拟显示组件 Xvfb")
    # Keep session paths short for Unix-domain socket limits.
    with tempfile.TemporaryDirectory(prefix="dy-display-", dir="/tmp") as temp:
        old_display, old_tmp = os.environ.get("DISPLAY"), os.environ.get("TMPDIR")
        read_fd, write_fd = os.pipe()
        process = None
        try:
            process = subprocess.Popen(
                [
                    "Xvfb",
                    "-displayfd",
                    str(write_fd),
                    "-screen",
                    "0",
                    "1440x1000x24",
                    "-nolisten",
                    "tcp",
                ],
                pass_fds=(write_fd,),
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
            os.close(write_fd)
            write_fd = -1
            if not select.select([read_fd], [], [], 10)[0]:
                raise RuntimeError("虚拟显示器启动超时")
            display = os.read(read_fd, 32).decode().strip()
            if not display.isdigit() or process.poll() is not None:
                raise RuntimeError("虚拟显示器启动失败")
            os.environ["DISPLAY"] = ":" + display
            os.environ["TMPDIR"] = temp
            yield
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for key, value in (("DISPLAY", old_display), ("TMPDIR", old_tmp)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@contextmanager
def account_display():
    if sys.platform == "linux" and not os.environ.get("DISPLAY"):
        with private_display():
            yield
    else:
        yield
