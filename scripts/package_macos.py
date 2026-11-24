"""Build the Apple Silicon Chem Code application and drag-to-install disk image."""

import hashlib
import importlib.metadata
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

# Configuration
output = Path(__file__).resolve().parents[1] / "output" / "macos"
python_version = "3.11.15"
minimum_macos = "14.0"

ROOT = Path(__file__).resolve().parents[1]
MACHO_MAGIC = {
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
}


def run(*command: str | Path, **options: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, check=True, text=True, **options)


def make_icon(resources: Path, build: Path) -> None:
    mark = (ROOT / "src/chemistry_agent/web/favicon.svg").read_text()
    mark = re.sub(r"<style>.*?</style>", "", mark).replace("currentColor", "#000000")
    source = build / "mark.svg"
    source.write_text(mark)
    run("qlmanage", "-t", "-s", "1024", "-o", build, source)
    with Image.open(build / "mark.svg.png") as preview:
        white = Image.new("RGBA", preview.size, "white")
        white.alpha_composite(preview.convert("RGBA"))
        mask = ImageOps.invert(white.convert("L"))
        mask = mask.crop(mask.getbbox())
    icon = Image.new("RGBA", (1024, 1024))
    ImageDraw.Draw(icon).rounded_rectangle((96, 96, 928, 928), radius=190, fill="#20242b")
    mask.thumbnail((620, 620), Image.Resampling.LANCZOS)
    icon.paste("#f4f6fa", ((1024 - mask.width) // 2, (1024 - mask.height) // 2), mask)
    iconset = build / "ChemCode.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            suffix = "@2x" if scale == 2 else ""
            icon.resize((size * scale, size * scale), Image.Resampling.LANCZOS).save(
                iconset / f"icon_{size}x{size}{suffix}.png"
            )
    run("iconutil", "-c", "icns", "-o", resources / "ChemCode.icns", iconset)


def compile_launcher(executable: Path) -> None:
    run(
        "xcrun",
        "clang",
        "-fobjc-arc",
        "-fmodules",
        "-O2",
        "-arch",
        "arm64",
        f"-mmacosx-version-min={minimum_macos}",
        "-framework",
        "AppKit",
        "-framework",
        "WebKit",
        ROOT / "desktop/macos/ChemCode.m",
        "-o",
        executable,
    )


def install_runtime(resources: Path, build: Path) -> Path:
    managed = build / "managed"
    interpreter = f"cpython-{python_version}-macos-aarch64-none"
    run("uv", "python", "install", "--install-dir", managed, "--no-bin", interpreter)
    runtime = resources / "Python"
    shutil.copytree(managed / interpreter, runtime, symlinks=True)
    executable = runtime / "bin/python3.11"
    wheel_dir = build / "wheel"
    run("uv", "build", "--wheel", "--out-dir", wheel_dir)
    wheel = next(wheel_dir.glob("*.whl"))
    site_packages = runtime / "lib/python3.11/site-packages"
    # Only accept arm64 wheels whose deployment targets support the app's declared minimum.
    env = os.environ | {"MACOSX_DEPLOYMENT_TARGET": minimum_macos}
    install = (
        "uv",
        "pip",
        "install",
        "--no-config",
        "--python",
        executable,
        "--target",
        site_packages,
        "--python-platform",
        "aarch64-apple-darwin",
        "--only-binary",
        ":all:",
        "--link-mode",
        "copy",
    )
    run(*install, "-r", "pyproject.toml", "--extra", "automation", env=env)
    run(*install, "--no-deps", wheel, env=env)
    make_runtime_relocatable(runtime, managed / interpreter, site_packages)
    shutil.copy2(ROOT / "LICENSE", resources / "LICENSE")
    versions = sorted(
        f"{item.metadata['Name']}=={item.version}"
        for item in importlib.metadata.distributions(path=[str(site_packages)])
    )
    (resources / "Bundled Packages.txt").write_text("\n".join(versions) + "\n")
    return executable


def make_runtime_relocatable(runtime: Path, source: Path, site_packages: Path) -> None:
    """Remove installation-only launchers and make sysconfig follow the installed app."""
    run(
        "install_name_tool",
        "-id",
        "@rpath/libpython3.11.dylib",
        runtime / "lib/libpython3.11.dylib",
    )
    for config in (runtime / "lib/python3.11").glob("_sysconfigdata*.py"):
        text = config.read_text().replace(str(source), "@CHEM_CODE_PYTHON@")
        config.write_text(
            text
            + "\nimport sys as _sys\n"
            + "build_time_vars = {key: value.replace('@CHEM_CODE_PYTHON@', _sys.base_prefix)\n"
            + "                   if isinstance(value, str) else value\n"
            + "                   for key, value in build_time_vars.items()}\n"
        )
    for command in (runtime / "bin").iterdir():
        if command.name not in {"python", "python3", "python3.11"}:
            command.unlink()
    generated = site_packages / "bin"
    if generated.exists():
        shutil.rmtree(generated)
    for metadata in site_packages.glob("zotero_chemistry_agent-*.dist-info/direct_url.json"):
        metadata.unlink()
    for cache in runtime.rglob("__pycache__"):
        shutil.rmtree(cache)


def check_private_paths(app: Path, build: Path) -> None:
    """Fail the build if package metadata or binaries reintroduce a developer path."""
    prefixes = {
        str(root).encode() + b"/" for root in (ROOT, build, Path.home()) if root.parent != root
    }
    private_names = {".env", "auth.json", "agent.sqlite3", "knowledge-mcp.token"}
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if path.name in private_names or any(prefix in data for prefix in prefixes):
            raise RuntimeError(f"Private build data in {path.relative_to(app)}")


def sign_and_check(app: Path, executable: Path) -> None:
    baseline = tuple(map(int, minimum_macos.split(".")))
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) not in MACHO_MAGIC:
                continue
        metadata = run("xcrun", "vtool", "-arch", "arm64", "-show-build", path, capture_output=True)
        match = re.search(r"\bminos (\d+(?:\.\d+)+)", metadata.stdout)
        if match and tuple(map(int, match[1].split("."))) > baseline:
            raise RuntimeError(f"{path.name} requires macOS {match[1]}, above {minimum_macos}.")
        run("codesign", "--force", "--sign", "-", path, capture_output=True)
    run("codesign", "--force", "--sign", "-", app)
    run("codesign", "--verify", "--strict", "--deep", app)
    run(
        executable,
        "-I",
        "-B",
        "-c",
        (
            "from chemistry_agent.api import WEB_DIRECTORY; "
            "from rdkit import Chem; "
            "import asyncssh, ase, keyring, matplotlib, scipy, websockets; "
            "assert Chem.MolFromSmiles('CCO') and (WEB_DIRECTORY / 'index.html').is_file()"
        ),
    )


def main() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Build this package on Apple Silicon macOS.")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    output.mkdir(parents=True, exist_ok=True)
    image = output / f"Chem-Code-{version}-macOS-arm64.dmg"
    with tempfile.TemporaryDirectory(prefix="chem-code-", dir=output) as temporary:
        build = Path(temporary)
        staging = build / "disk"
        app = staging / "Chem Code.app"
        resources = app / "Contents/Resources"
        launcher = app / "Contents/MacOS/ChemCode"
        resources.mkdir(parents=True)
        launcher.parent.mkdir()
        info = {
            "CFBundleDevelopmentRegion": "zh_CN",
            "CFBundleDisplayName": "Chem Code",
            "CFBundleExecutable": "ChemCode",
            "CFBundleIconFile": "ChemCode.icns",
            "CFBundleIdentifier": "org.chemcode.desktop",
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": "Chem Code",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "LSApplicationCategoryType": "public.app-category.education",
            "LSMinimumSystemVersion": minimum_macos,
            "NSHighResolutionCapable": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        }
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        compile_launcher(launcher)
        make_icon(resources, build)
        executable = install_runtime(resources, build)
        sign_and_check(app, executable)
        check_private_paths(app, build)
        (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
        staged_image = build / image.name
        run(
            "hdiutil",
            "create",
            "-srcfolder",
            staging,
            "-volname",
            "Chem Code",
            "-format",
            "UDZO",
            "-fs",
            "HFS+",
            staged_image,
        )
        run("hdiutil", "verify", staged_image)
        staged_image.replace(image)
    with image.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    image.with_suffix(".dmg.sha256").write_text(f"{digest}  {image.name}\n")
    print(image)


if __name__ == "__main__":
    main()
