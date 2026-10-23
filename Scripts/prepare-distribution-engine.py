#!/usr/bin/env python3
"""Prepare a standalone, relocatable OpenSfM Engine distribution for macOS Apple Silicon.

Relinks all Mach-O binaries to relative @loader_path, prunes build headers,
applies sandboxing helper entitlements, and creates the engine distribution bundle.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)


def is_macho(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    magic_numbers = {
        b'\xcf\xfa\xed\xfe',  # 64-bit Mach-O
        b'\xce\xfa\xed\xfe',  # 32-bit Mach-O
        b'\xca\xfe\xba\xbe',  # Universal binary
        b'\xca\xfe\xba\xbf',
    }
    try:
        with path.open('rb') as handle:
            return handle.read(4) in magic_numbers
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path, help='Path to conda/micromamba build prefix')
    parser.add_argument('--opensfm', required=True, type=Path, help='Path to built OpenSfM checkout')
    parser.add_argument('--version', required=True, help='Release version tag (e.g. v1.1)')
    parser.add_argument('--commit', required=True, help='OpenSfM git commit SHA')
    parser.add_argument('--output', required=True, type=Path, help='Target staging directory (.build/dist)')
    parser.add_argument('--identity', default='-', help='Signing identity (defaults to - for ad-hoc)')
    parser.add_argument('--adhoc', action='store_true', help='Force ad-hoc signing for CI builds')
    parser.add_argument('--timestamp', action='store_true', help='Use secure timestamp during signing')

    args = parser.parse_args()
    original_prefix = args.runtime.resolve()
    opensfm_src = args.opensfm.resolve()
    output_dir = args.output.resolve()

    if args.adhoc:
        identity = '-'
    else:
        identity = args.identity

    print(f"=== Preparing OpenSfM Engine Distribution ===")
    print(f"Runtime Prefix: {original_prefix}")
    print(f"OpenSfM Source: {opensfm_src}")
    print(f"Release Tag:    {args.version}")
    print(f"Commit SHA:     {args.commit}")
    print(f"Output Dir:     {output_dir}")
    print(f"Signing ID:     {identity}")

    output_dir.mkdir(parents=True, exist_ok=True)
    engine_dir = output_dir / 'OrthomosaicEngine'
    if engine_dir.exists():
        shutil.rmtree(engine_dir)
    engine_dir.mkdir(parents=True)

    # 1. Copy runtime environment
    print("\n[1/7] Copying runtime environment...")
    dest_env = engine_dir / 'environment'
    run('/bin/cp', '-cR', str(original_prefix), str(dest_env))

    # 2. Copy OpenSfM source and bindings
    print("[2/7] Copying OpenSfM library...")
    dest_opensfm = engine_dir / 'OpenSfM'
    shutil.copytree(
        opensfm_src,
        dest_opensfm,
        symlinks=True,
        ignore=shutil.ignore_patterns('build', 'cmake_build', '__pycache__', '.git', '*.egg-info')
    )

    # 3. Relocate compiled C++ extensions from site-packages to OpenSfM directory
    print("[3/7] Relocating compiled C++ extensions...")
    site = dest_env / 'lib/python3.10/site-packages'
    if (site / 'opensfm').is_dir():
        for extension in (site / 'opensfm').glob('*.so'):
            shutil.copy2(extension, dest_opensfm / 'opensfm' / extension.name)
            print(f"  Relocated extension: {extension.name}")
        shutil.rmtree(site / 'opensfm')

    # Remove editable install markers
    for pattern in ('*editable*opensfm*', '*opensfm*editable*', 'opensfm*.dist-info'):
        for item in site.glob(pattern):
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

    # 4. Prune build-only tools, headers, and compiler artifacts
    print("[4/7] Pruning compiler toolchains, headers, and caches...")
    # Retain only necessary Python executables in bin/
    bin_dir = dest_env / 'bin'
    if bin_dir.is_dir():
        allowed_bins = {'python', 'python3', 'python3.10'}
        for entry in bin_dir.iterdir():
            if entry.name not in allowed_bins:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()

    # Prune development headers & cmake configs
    for name in ('include', 'libexec', 'compiler_compat', 'lib/clang', 'lib/cmake'):
        path = dest_env / name
        if path.is_dir():
            shutil.rmtree(path)

    # Clean __pycache__
    for cache in engine_dir.rglob('__pycache__'):
        shutil.rmtree(cache)

    # 5. Make symlinks relative
    print("[5/7] Normalizing symlinks to relative targets...")
    for link in engine_dir.rglob('*'):
        if link.is_symlink():
            try:
                target = os.readlink(link)
                if target.startswith(str(original_prefix) + '/'):
                    dest = dest_env / Path(target).relative_to(original_prefix)
                    link.unlink()
                    link.symlink_to(os.path.relpath(dest, link.parent))
            except Exception as e:
                print(f"  Warning: Could not process symlink {link}: {e}")

    # 6. Relink Mach-O binaries to @loader_path and codesign
    print("[6/7] Rewriting Mach-O rpaths to relative @loader_path and signing...")
    binaries = [p for p in engine_dir.rglob('*') if is_macho(p)]
    print(f"  Found {len(binaries)} Mach-O binaries to relink and sign.")

    python_bin = dest_env / 'bin/python3.10'
    helper_entitlements = output_dir / 'Helper.entitlements'
    helper_entitlements.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.app-sandbox</key>
    <true/>
    <key>com.apple.security.inherit</key>
    <true/>
</dict>
</plist>
""")

    for index, path in enumerate(binaries):
        # Set install name if absolute
        identifiers = [
            line for line in run('/usr/bin/otool', '-D', str(path)).splitlines()
            if line and not line.endswith(':') and '(architecture ' not in line
        ]
        identifier = identifiers[-1] if identifiers else None
        if identifier and identifier.startswith('/'):
            run('/usr/bin/install_name_tool', '-id', '@rpath/' + path.name, str(path))

        # Rewrite absolute LC_RPATHs to relative @loader_path
        commands = run('/usr/bin/otool', '-l', str(path))
        rpaths = re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset', commands)
        for old in dict.fromkeys(rpaths):
            if old.startswith(str(original_prefix) + '/'):
                mapped = dest_env / Path(old).relative_to(original_prefix)
                new = '@loader_path/' + os.path.relpath(mapped, path.parent)
                run('/usr/bin/install_name_tool', '-rpath', old, new, str(path))

        # Rewrite dependent dylibs to relative @loader_path
        for line in run('/usr/bin/otool', '-L', str(path)).splitlines()[1:]:
            if not line.startswith('\t'):
                continue
            dep = line.strip().split(' (compatibility')[0]
            if dep.startswith(str(original_prefix) + '/'):
                mapped = dest_env / Path(dep).relative_to(original_prefix)
                new = '@loader_path/' + os.path.relpath(mapped, path.parent)
                run('/usr/bin/install_name_tool', '-change', dep, new, str(path))

        # Codesign the binary
        sign_cmd = ['/usr/bin/codesign', '--force', '--sign', identity]
        if args.timestamp and identity != '-':
            sign_cmd.append('--timestamp')
        else:
            sign_cmd.append('--timestamp=none')

        if path == python_bin:
            sign_cmd += [
                '--options', 'runtime',
                '--identifier', 'com.hata.ControlDroneMapping.engine.python',
                '--entitlements', str(helper_entitlements),
            ]
        run(*(sign_cmd + [str(path)]))

        if (index + 1) % 50 == 0 or (index + 1) == len(binaries):
            print(f"  Processed {index + 1}/{len(binaries)} binaries...")

    # Stage OrthomosaicPython helper executable
    helper_dest = output_dir / 'OrthomosaicPython'
    shutil.copy2(python_bin, helper_dest)
    # Ensure helper also gets the dedicated helper signature
    helper_sign_cmd = [
        '/usr/bin/codesign', '--force', '--sign', identity,
        '--options', 'runtime',
        '--identifier', 'com.hata.ControlDroneMapping.engine.python',
        '--entitlements', str(helper_entitlements),
        '--timestamp=none',
        str(helper_dest),
    ]
    run(*helper_sign_cmd)

    # 7. Write engine metadata manifest
    print("\n[7/7] Generating engine.json manifest...")
    manifest = {
        "repository": "https://github.com/OpenSfM/OpenSfM",
        "version": args.version,
        "commit": args.commit,
        "architecture": "arm64",
        "packaged": True,
    }
    (engine_dir / 'engine.json').write_text(json.dumps(manifest, indent=2) + '\n')

    # Cleanup temporary entitlements file
    if helper_entitlements.exists():
        helper_entitlements.unlink()

    print("\n=== OpenSfM Engine Staged Successfully ===")
    print(f"Staged Directory: {engine_dir}")
    print(f"Helper Executable: {helper_dest}")


if __name__ == '__main__':
    main()
