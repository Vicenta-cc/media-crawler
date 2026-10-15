"""Build the local Firefox navigation policy without network addon installation."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

EXTENSION_ID = "navigation-policy@zhanyun.local"
EXTENSION_UUID = "272948b5-a40e-4e03-9031-645dac91c644"


def build_navigation_guard(profile_dir, echo_url=""):
    """Copy immutable executable policy and route-specific public echo URL."""
    source = Path(__file__).with_name("navigation_guard_extension")
    destination = Path(profile_dir) / "navigation-policy"
    contents = {
        name: (source / name).read_bytes()
        for name in ("manifest.json", "background.js", "ready.js")
    }
    contents["policy.js"] = (
        "const ZHANYUN_POLICY = "
        + json.dumps({"echoUrl": echo_url}, ensure_ascii=True)
        + ";\n"
    ).encode()
    profile_fd = os.open(profile_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_fd = None
    try:
        try:
            os.mkdir("navigation-policy", mode=0o700, dir_fd=profile_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(
            "navigation-policy",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=profile_fd,
        )
        os.fchmod(directory_fd, 0o700)
        for name, data in contents.items():
            temporary = ".policy-" + secrets.token_hex(16)
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(
                    temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
                )
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        os.fsync(directory_fd)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(profile_fd)
    digest = hashlib.sha256(
        b"".join(name.encode() + contents[name] for name in sorted(contents))
    ).hexdigest()
    return {
        "directory": str(destination.resolve()),
        "echo_url": echo_url,
        "ready_url": "https://www.douyin.com/__zhanyun_policy_ready__",
        "prefs": {
            "extensions.webextensions.uuids": json.dumps({EXTENSION_ID: EXTENSION_UUID})
        },
        "digest": digest,
    }


async def require_navigation_guard(context, guard):
    """A synthetic first-party document asks the extension background to ACK.

    Firefox Juggler does not reliably expose moz-extension documents. This
    exact URL is fulfilled locally and executes only our isolated-world content
    script; no page JavaScript or platform network request can forge readiness.
    """
    page = await context.new_page()

    async def ready_document(route):
        await route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><html><body></body></html>",
        )

    try:
        await page.route(guard["ready_url"], ready_document)
        await page.goto(
            guard["ready_url"], wait_until="domcontentloaded", timeout=10000
        )
        await page.wait_for_function(
            "document.body.dataset.ready === 'true'", timeout=10000
        )
        context._navigation_policy_guard_ready = True
        context._collection_native_echo_guard_url = guard["echo_url"]
    except Exception:  # noqa: BLE001 - Any readiness failure must fail closed.
        raise RuntimeError("NAVIGATION_POLICY_NOT_READY") from None
    finally:
        await page.close()
