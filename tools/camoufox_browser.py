"""One durable Camoufox identity per account; silent Mac or Linux private display."""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import os
import platform
import tempfile
from pathlib import Path

DEFAULT_BROWSER_VERSION = "156.0.1-beta.39"
_failed_profile_locks = {}


def _host_os():
    try:
        return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[
            platform.system()
        ]
    except KeyError:
        raise RuntimeError("Unsupported Camoufox host OS") from None


def _private_directory(path):
    if path.is_symlink():
        raise ValueError("Camoufox profile symlinks are forbidden")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return json.load(stream)


def _atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def profile_for(account_id: str, root: Path, version: str) -> dict:
    if not account_id.strip():
        raise ValueError("Camoufox requires MEDIACRAWLER_ACCOUNT_ID")
    if not version or version in {"latest", "stable"}:
        raise ValueError("Camoufox requires a pinned browser version")
    root = Path(root).expanduser().absolute()
    _private_directory(root)
    key = hashlib.sha256(("dy:" + account_id).encode()).hexdigest()
    directory = root / key
    _private_directory(directory)
    expected = {
        "schema": 1,
        "engine": "camoufox",
        "account_key": key,
        "version": version,
        "os": _host_os(),
        "display_mode": "headless" if _host_os() == "macos" else "headed",
    }
    metadata = directory / "profile.json"
    if metadata.exists() or metadata.is_symlink():
        existing = _read_json(metadata)
        if any(existing.get(k) != v for k, v in expected.items()):
            raise ValueError("Camoufox profile engine/version/OS identity mismatch")
        expected = existing
    return {**expected, "directory": directory}


def _installed_executable(version):
    from camoufox.pkgman import Version, launch_path

    explicit = os.environ.get("MEDIACRAWLER_CAMOUFOX_EXECUTABLE", "").strip()
    if explicit:
        executable = Path(explicit).expanduser().resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("Camoufox release executable is unavailable")
        install = next(
            (
                parent
                for parent in executable.parents
                if (parent / "version.json").is_file()
            ),
            None,
        )
        if install is None or Version.from_path(install).full_string != version:
            raise RuntimeError("Camoufox release executable version metadata mismatch")
        expected_sha = os.environ.get(
            "MEDIACRAWLER_CAMOUFOX_EXECUTABLE_SHA256", ""
        ).strip()
        if (
            expected_sha
            and hashlib.sha256(executable.read_bytes()).hexdigest() != expected_sha
        ):
            raise RuntimeError("Camoufox release executable SHA256 mismatch")
        if Path(launch_path(install)).resolve() != executable:
            raise RuntimeError(
                "Camoufox release executable does not match the installed layout"
            )
        return str(executable)
    from camoufox.multiversion import find_installed_version

    installed = find_installed_version("official/" + version)
    if installed is None or Version.from_path(installed).full_string != version:
        raise RuntimeError(
            "Pinned Camoufox browser is not installed; install the release artifact first"
        )
    return launch_path(installed)


def _generate_identity(executable, guard):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    timezone_name = os.getenv("MEDIACRAWLER_CAMOUFOX_TIMEZONE", "Asia/Shanghai").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Invalid initial Camoufox timezone") from None
    from camoufox import DefaultAddons
    from camoufox.fpgen_model import is_pinned
    from camoufox.pkgman import Version
    from camoufox.utils import launch_options

    if os.environ.get("FPGEN_MODEL_URL") or not is_pinned():
        raise RuntimeError(
            "Pinned Camoufox fingerprint model is missing; install the release artifact first"
        )
    # SDK 0.5.8 looks for application.ini beside the executable. A macOS
    # bundle keeps it in Resources, so that lookup incorrectly falls back to
    # the empty private HOME cache. Use the already verified release metadata
    # for the SDK's public Firefox-major option, never an invented UA version.
    install = next(
        (parent for parent in Path(executable).resolve().parents
         if (parent / "version.json").is_file()),
        None,
    )
    if install is None:
        raise RuntimeError("Verified Camoufox release version metadata is missing")
    release = Version.from_path(install)
    if release.full_string != os.getenv("MEDIACRAWLER_CAMOUFOX_BROWSER_VERSION", DEFAULT_BROWSER_VERSION):
        raise RuntimeError("Camoufox identity release version mismatch")
    firefox_major = int(release.version.split(".", 1)[0])
    options = launch_options(
        ff_version=firefox_major,
        headless=_host_os() == "macos",
        os=_host_os(),
        executable_path=executable,
        config={"timezone": timezone_name},
        i_know_what_im_doing=True,
        geoip=False,
        locale="zh-CN",
        humanize=False,
        env={},
        pin_cpu_cores=False,
        exclude_addons=[DefaultAddons.UBO],
        addons=[guard["directory"]],
        firefox_user_prefs={
            **guard["prefs"],
            "browser.startup.page": 0,
            "browser.startup.homepage": "about:blank",
            "browser.sessionstore.resume_from_crash": False,
            "browser.sessionstore.resume_session_once": False,
            "browser.startup.homepage_override.mstone": "ignore",
        },
    )
    return {
        "env": {
            k: v
            for k, v in options["env"].items()
            if k.startswith(("CAMOU_CONFIG_", "CAMOU_PREFS_")) or k == "FONTCONFIG_FILE"
        },
        "firefox_user_prefs": options["firefox_user_prefs"],
        "args": options["args"],
    }


def _load_identity(profile, executable, guard):
    path = profile["directory"] / "launch-identity.json"
    metadata_path = profile["directory"] / "profile.json"
    if path.exists() or path.is_symlink():
        identity = _read_json(path)
    else:
        if metadata_path.exists():
            raise RuntimeError(
                "Camoufox identity is missing; refusing to regenerate the account fingerprint"
            )
        identity = {
            "binding": {
                k: profile[k]
                for k in (
                    "schema",
                    "engine",
                    "account_key",
                    "version",
                    "os",
                    "generation",
                    "display_mode",
                )
            },
            "options": _generate_identity(executable, guard),
        }
        _atomic_json(path, identity)
    if identity.get("binding") != {
        k: profile[k]
        for k in (
            "schema",
            "engine",
            "account_key",
            "version",
            "os",
            "generation",
            "display_mode",
        )
    }:
        raise ValueError("Camoufox saved identity engine/version/OS binding mismatch")
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if profile.get("identity_hash") not in (None, digest):
        raise RuntimeError("Camoufox identity integrity check failed")
    options = identity["options"]
    if set(identity) != {"binding", "options"} or set(options) != {
        "env",
        "firefox_user_prefs",
        "args",
    }:
        raise ValueError("Invalid Camoufox identity fields")
    if any(
        not (k.startswith(("CAMOU_CONFIG_", "CAMOU_PREFS_")) or k == "FONTCONFIG_FILE")
        for k in options["env"]
    ):
        raise ValueError("Unexpected environment in Camoufox identity")
    profile["identity_hash"] = digest
    _atomic_json(metadata_path, {k: v for k, v in profile.items() if k != "directory"})
    return copy.deepcopy(options)


def initialize_account_profile(account_id: str, *, root: Path, generation: str) -> dict:
    """Create/verify an offline migration identity under its account fence."""
    if not generation or not generation.strip():
        raise ValueError("Camoufox migration generation is required")
    version = os.getenv(
        "MEDIACRAWLER_CAMOUFOX_BROWSER_VERSION", DEFAULT_BROWSER_VERSION
    )
    profile = profile_for(account_id, root, version)
    fd = os.open(
        profile["directory"] / "adapter.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        profile = profile_for(account_id, root, version)
        if profile.get("generation") not in (None, generation):
            raise ValueError("Camoufox migration generation mismatch")
        profile["generation"] = generation
        from tools.navigation_guard_addon import build_navigation_guard

        guard = build_navigation_guard(profile["directory"])
        _load_identity(profile, _installed_executable(version), guard)
    return profile


async def launch_account_context(
    account_id: str,
    root: Path,
    *,
    headless: bool | None = None,
    proxy=None,
    route_snapshot=None,
    bound_proxy_session=None,
    probe_page_hook=None,
    browser_args=None,
):
    from tools.collection_route import CollectionRouteError, derive_profile_key

    required_headless = _host_os() == "macos"
    if headless is None:
        headless = required_headless
    if headless != required_headless or browser_args:
        raise ValueError(
            "Camoufox requires Mac silent/headless mode, Linux headed mode, and no Chromium browser arguments"
        )
    if _host_os() == "linux" and not os.environ.get("DISPLAY"):
        raise RuntimeError("Camoufox headed mode requires an owner-managed DISPLAY")
    if route_snapshot is not None:
        if (
            route_snapshot.account_id != account_id
            or route_snapshot.profile_key != derive_profile_key(account_id)
        ):
            raise CollectionRouteError("PROXY_CONFIG_INVALID")
        route_snapshot.require_not_expired(allow_unknown=True)
        proxy = route_snapshot.playwright_proxy()
        if bound_proxy_session is None:
            from tools.bound_proxy import BoundProxySession

            bound_proxy_session = await asyncio.to_thread(
                BoundProxySession, route_snapshot
            )
        elif bound_proxy_session.snapshot != route_snapshot:
            raise CollectionRouteError("PROXY_CONFIG_INVALID")
    elif bound_proxy_session is not None:
        raise CollectionRouteError("PROXY_CONFIG_INVALID")
    profile = profile_for(
        account_id,
        root,
        os.getenv("MEDIACRAWLER_CAMOUFOX_BROWSER_VERSION", DEFAULT_BROWSER_VERSION),
    )
    fd = os.open(
        profile["directory"] / "adapter.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    os.fchmod(fd, 0o600)
    lock = os.fdopen(fd, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        if route_snapshot is not None:
            raise CollectionRouteError("PROXY_BUSY") from None
        raise RuntimeError("Camoufox account profile is already in use") from None
    manager = None
    try:
        # Re-read metadata under the same account lock before generating identity.
        profile = profile_for(account_id, root, profile["version"])
        generation = os.getenv("MEDIACRAWLER_CAMOUFOX_PROFILE_GENERATION", "").strip()
        if not generation or profile.get("generation") not in (None, generation):
            raise ValueError(
                "Camoufox account profile generation is missing or mismatched"
            )
        if (profile["directory"] / "launch-identity.json").exists() and not profile.get(
            "generation"
        ):
            raise ValueError("Camoufox existing profile has no trusted generation")
        profile["generation"] = generation
        executable = _installed_executable(profile["version"])
        from tools.navigation_guard_addon import build_navigation_guard

        guard = build_navigation_guard(
            profile["directory"], getattr(bound_proxy_session, "echo_url", "")
        )
        identity = _load_identity(profile, executable, guard)
        options = {
            **identity,
            "executable_path": executable,
            "headless": headless,
            "user_data_dir": str(profile["directory"]),
            "accept_downloads": True,
            "service_workers": "block",
        }
        options["env"] = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CAMOU_CONFIG_", "CAMOU_PREFS_"))
        }
        options["env"].update(identity["env"])
        if proxy:
            options["proxy"] = proxy
        else:
            options["firefox_user_prefs"]["network.proxy.type"] = 0
        from camoufox.async_api import AsyncCamoufox

        manager = AsyncCamoufox(from_options=options, persistent_context=True)
        context = await manager.__aenter__()
    except BaseException:
        if manager is None:
            lock.close()
        else:
            # SDK launch failure can include failed driver teardown. Until the
            # owner proves that process has exited, do not admit this account.
            _failed_profile_locks[str(profile["directory"])] = lock
        raise
    close = context.close
    closed = False
    close_fence = asyncio.Lock()

    async def close_locked(*args, **kwargs):
        nonlocal closed
        async with close_fence:
            if closed:
                return
            try:
                if bound_proxy_session is not None:
                    await bound_proxy_session.close()
                await close(*args, **kwargs)
                # Context was already closed; stop the Playwright driver without
                # recursively calling our wrapped context.close through __aexit__.
                manager.browser = None
                await manager.__aexit__(None, None, None)
                if bound_proxy_session is not None:
                    bound_proxy_session.confirm_context_closed(context)
            except BaseException:
                _failed_profile_locks[str(profile["directory"])] = lock
                raise
            else:
                closed = True
                _failed_profile_locks.pop(str(profile["directory"]), None)
                lock.close()

    context.close = close_locked
    try:
        from tools.navigation_guard_addon import require_navigation_guard

        await require_navigation_guard(context, guard)
    except BaseException:
        await context.close()
        raise
    if bound_proxy_session is not None:
        try:
            for page in list(context.pages):
                await page.close()
            await bound_proxy_session.open_context(
                context, on_probe_page=probe_page_hook
            )
            profile["bound_proxy_session"] = bound_proxy_session
            context._bound_proxy_session = bound_proxy_session
        except BaseException:
            await context.close()
            raise
    return context, profile


async def prepare_account_login(context, profile):
    """Start an explicit fresh login in the existing profile, retaining its identity.

    Mark the profile first: a cancelled login must not resurrect an old database
    snapshot on the next crawl. Call only while holding the profile's launch lock.
    """
    import re
    from urllib.parse import urlsplit

    def first_party_origin(value):
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and (host == "douyin.com" or host.endswith(".douyin.com")):
            return parsed.scheme + "://" + parsed.netloc
        return None

    # Enumerate metadata only: storage_state may navigate third-party origins
    # and serialize an unfinished IndexedDB, neither of which reset requires.
    cookies = await asyncio.wait_for(context.cookies(), 15)
    origins = {"https://" + host for host in (
        "www.douyin.com", "douyin.com", "creator.douyin.com",
        "live.douyin.com", "douhot.douyin.com")}
    for cookie in cookies:
        domain = cookie.get("domain", "")
        if isinstance(domain, str):
            origin = first_party_origin("https://" + domain.lstrip("."))
            if origin:
                origins.add(origin)
    old_pages = list(context.pages)
    for existing in old_pages:
        for frame in [existing, *getattr(existing, "frames", [])]:
            origin = first_party_origin(getattr(frame, "url", ""))
            if origin:
                origins.add(origin)
    storage = profile["directory"] / "storage"
    if storage.is_symlink():
        raise RuntimeError("PROFILE_STORAGE_SYMLINK")
    for bucket in ("default", "permanent", "temporary"):
        directory = storage / bucket
        if directory.is_symlink():
            raise RuntimeError("PROFILE_STORAGE_SYMLINK")
        if not directory.exists():
            continue
        for entry in directory.iterdir():
            if entry.is_symlink():
                raise RuntimeError("PROFILE_STORAGE_SYMLINK")
            match = re.fullmatch(r"https\+\+\+([a-zA-Z0-9.-]+)(?:\+(\d+))?", entry.name.split("^", 1)[0])
            if match:
                origin = first_party_origin("https://" + match[1] + ((":" + match[2]) if match[2] else ""))
                if origin:
                    origins.add(origin)
    bound_session = profile.get("bound_proxy_session")
    if bound_session is not None:
        parsed_echo = urlsplit(bound_session.echo_url)
        origins.discard(parsed_echo.scheme + "://" + parsed_echo.netloc)
    # Preserve a blank renderer while closing handles that could block IDB reset.
    await context.new_page()
    for existing in old_pages:
        await asyncio.wait_for(existing.close(), 3)
    page = await context.new_page()

    async def reset_document(route):
        if route.request.is_navigation_request() and first_party_origin(route.request.url):
            await route.fulfill(status=200, content_type="text/html", body="<!doctype html><html></html>")
        else:
            await route.abort()

    try:
        await page.route("**/*", reset_document)
        for origin in sorted(origins):
            await page.goto(origin, wait_until="domcontentloaded", timeout=10000)
            await asyncio.wait_for(page.evaluate("""async () => {
                localStorage.clear(); sessionStorage.clear();
                if (indexedDB.databases) {
                    for (const db of await indexedDB.databases()) {
                        if (!db.name) continue;
                        await new Promise((resolve, reject) => {
                            const request = indexedDB.deleteDatabase(db.name);
                            request.onsuccess = resolve;
                            request.onerror = () => reject(new Error('IndexedDB reset failed'));
                            request.onblocked = () => reject(new Error('IndexedDB reset blocked'));
                        });
                    }
                } else { throw new Error('IndexedDB enumeration unavailable'); }
                if ('serviceWorker' in navigator) {
                    for (const registration of await navigator.serviceWorker.getRegistrations())
                        if (!await registration.unregister()) throw new Error('ServiceWorker reset failed');
                }
                for (const key of await caches.keys())
                    if (!await caches.delete(key)) throw new Error('CacheStorage reset failed');
            }"""), 10)
        await context.clear_cookies(domain=re.compile(r"(^|\.)douyin\.com$", re.IGNORECASE))
        (profile["directory"] / "auth-imported").touch(mode=0o600)
    finally:
        await page.close()
