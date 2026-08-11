#!/usr/bin/env python3
"""Short-path archiver for filesystems with a 255-byte path-component limit."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


MANIFEST = "meaning.tsv"


def extension(name: str) -> str:
    """Return only the final extension; dot-files have no extension."""
    if name.startswith(".") or "." not in name:
        return ""
    suffix = name.rsplit(".", 1)[1]
    return f".{suffix}" if suffix else ""


def width(count: int) -> int:
    return max(3, len(str(count)))


def safe_rel(path: Path, root: Path) -> str:
    value = path.relative_to(root).as_posix()
    if not value or value == ".":
        raise ValueError("入力フォルダ自身はアーカイブしません")
    return value


def make_short_tree(source: Path, staging: Path, mode: str) -> list[tuple[str, str]]:
    """Copy source into staging using per-directory short names."""
    entries: list[tuple[str, str]] = []

    def visit(original_dir: Path, short_dir: Path) -> None:
        children = sorted(original_dir.iterdir(), key=lambda p: p.name)
        dirs = [p for p in children if p.is_dir() and not p.is_symlink()]
        files = [p for p in children if not (p.is_dir() and not p.is_symlink())]
        digits = width(len(children))

        for index, child in enumerate(dirs, 1):
            numbered_name = f"{index:0{digits}d}"
            short_name = (
                numbered_name
                if mode == "all" or len(child.name.encode("utf-8")) > 255
                else child.name
            )
            target = short_dir / short_name
            target.mkdir()
            original = safe_rel(child, source)
            short = target.relative_to(staging).as_posix()
            entries.append((short, original))
            visit(child, target)

        for index, child in enumerate(files, 1):
            if child.is_symlink():
                raise ValueError(f"シンボリックリンクには対応していません: {child}")
            if not child.is_file():
                raise ValueError(f"通常ファイル以外には対応していません: {child}")
            suffix = extension(child.name)
            numbered_name = f"{len(dirs) + index:0{digits}d}{suffix}"
            short_name = (
                numbered_name
                if mode == "all" or len(child.name.encode("utf-8")) > 255
                else child.name
            )
            target = short_dir / short_name
            shutil.copy2(child, target)
            entries.append((target.relative_to(staging).as_posix(), safe_rel(child, source)))

    staging.mkdir(parents=True, exist_ok=True)
    visit(source, staging)
    return entries


def write_manifest(staging: Path, entries: list[tuple[str, str]]) -> None:
    manifest = staging / MANIFEST
    with manifest.open("w", encoding="utf-8", newline="\n") as stream:
        for short, original in entries:
            stream.write(f"{short}\t{original}\n")


def zip_directory(staging: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            archive.write(path, path.relative_to(staging).as_posix())


def rar_directory(staging: Path, output: Path) -> None:
    rar = shutil.which("rar")
    if not rar:
        raise RuntimeError("RAR圧縮にはRARコマンドが必要です（rarがPATHにありません）")
    subprocess.run(
        [rar, "a", "-idq", str(output), "."],
        cwd=staging,
        check=True,
    )


def compress(source: Path, output: Path, mode: str) -> None:
    if not source.is_dir():
        raise ValueError("圧縮元はフォルダで指定してください")
    if output.exists():
        raise FileExistsError(f"出力先は既に存在します: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pathless-") as temporary:
        staging = Path(temporary) / "archive"
        entries = make_short_tree(source, staging, mode)
        write_manifest(staging, entries)
        if output.suffix.lower() == ".zip":
            zip_directory(staging, output)
        elif output.suffix.lower() == ".rar":
            rar_directory(staging, output)
        else:
            raise ValueError("出力拡張子は .zip または .rar にしてください")


def archive_members(path: Path) -> list[str]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.suffix.lower() == ".rar":
        try:
            import rarfile  # type: ignore
            with rarfile.RarFile(path) as archive:
                return archive.namelist()
        except ImportError:
            return []
    raise ValueError("入力拡張子は .zip または .rar にしてください")


def validate_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"安全でないアーカイブパスです: {name}")
    return path


def extract_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            relative = validate_member(info.filename)
            if not relative.parts:
                continue
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as input_stream, target.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream)


def rar_command() -> str:
    command = shutil.which("unrar")
    if not command:
        raise RuntimeError("RAR解凍にはunrarが必要です")
    return command


def rar_env() -> dict[str, str]:
    environment = os.environ.copy()
    # Keep UTF-8 for archived Japanese names while retaining stable English
    # labels such as "Name", "Type", and "Size" in unrar's technical output.
    environment["LC_ALL"] = "C.UTF-8"
    environment["LANG"] = "C.UTF-8"
    return environment


def rar_members(source: Path) -> tuple[list[str], set[str], set[str], dict[str, int]]:
    command = rar_command()
    result = subprocess.run([command, "lb", "-scu", str(source)], capture_output=True, env=rar_env())
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"RAR一覧取得に失敗しました（終了コード{result.returncode}）: {detail}")
    members = [
        line.decode("utf-8", errors="surrogateescape").rstrip("\r\n")
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    if not members:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"RAR内のファイル一覧が空です{suffix}")

    # `lb` does not reliably mark directory entries. Read the technical
    # listing as well so directories are never sent to `unrar p`.
    technical = subprocess.run([command, "lt", "-scu", str(source)], capture_output=True, env=rar_env())
    if technical.returncode:
        detail = technical.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"RAR詳細一覧取得に失敗しました（終了コード{technical.returncode}）: {detail}")
    types: dict[str, str] = {}
    sizes: dict[str, int] = {}
    current: str | None = None
    for raw_line in technical.stdout.splitlines():
        line = raw_line.decode("utf-8", errors="surrogateescape").rstrip("\r\n")
        stripped = line.strip()
        if stripped.startswith("Name: ") or stripped.startswith("Pathname: "):
            prefix = "Name: " if stripped.startswith("Name: ") else "Pathname: "
            current = stripped[len(prefix):].replace("\\", "/")
        elif current is not None and stripped.startswith("Type: "):
            types[current.rstrip("/")] = stripped[len("Type: "):]
        elif current is not None and stripped.startswith("Size: "):
            sizes[current.rstrip("/")] = int(stripped[len("Size: "):].replace(",", ""))
    directories = {name for name, entry_type in types.items() if entry_type == "Directory"}
    files = {name for name, entry_type in types.items() if entry_type == "File"}
    if not types or not files and not directories:
        raise RuntimeError("RAR内のファイル種別を判定できませんでした")
    return members, directories, files, sizes


def rar_extract_member(command: str, source: Path, member: str, target: Path) -> None:
    with target.open("wb") as output:
        subprocess.run(
            [command, "p", "-idq", "-inul", "-scu", str(source), member],
            stdout=output,
            check=True,
            env=rar_env(),
        )


def extract_rar(source: Path, destination: Path) -> None:
    command = rar_command()
    members, directory_members, file_members, sizes = rar_members(source)
    normalized = [member.replace("\\", "/") for member in members]
    manifest_name = MANIFEST

    # Archives made by this tool already contain safe short names. Extract them
    # as-is, without trying to interpret the manifest.
    if manifest_name in normalized:
        for member, safe_name in zip(members, normalized):
            relative = validate_member(safe_name.rstrip("/"))
            if not relative.parts:
                continue
            target = destination.joinpath(*relative.parts)
            member_key = safe_name.rstrip("/")
            if member_key in directory_members or safe_name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            elif member_key in file_members:
                target.parent.mkdir(parents=True, exist_ok=True)
                rar_extract_member(command, source, member, target)
        return

    # For a normal RAR, make the short tree while reading each file. This is
    # the important difference from `unrar x`: long names never touch disk.
    children: dict[tuple[str, ...], dict[str, bool]] = {(): {}}
    files: list[tuple[str, tuple[str, ...]]] = []
    for raw_member, member in zip(members, normalized):
        relative = validate_member(member.rstrip("/"))
        if not relative.parts:
            continue
        parts = relative.parts
        member_key = member.rstrip("/")
        if member_key not in file_members and member_key not in directory_members:
            continue
        is_dir = member_key in directory_members or member.endswith("/")
        for index in range(len(parts)):
            parent = parts[:index]
            name = parts[index]
            children.setdefault(parent, {})[name] = is_dir if index == len(parts) - 1 else True
            children.setdefault(parts[: index + 1], {})
        if not is_dir:
            files.append((raw_member, parts))

    entries: list[tuple[str, str]] = []
    file_targets: dict[tuple[str, ...], Path] = {}

    def visit(original_dir: tuple[str, ...], short_dir: Path) -> None:
        items = children.get(original_dir, {})
        directories = sorted(name for name, is_dir in items.items() if is_dir)
        regular_files = sorted(name for name, is_dir in items.items() if not is_dir)
        digits = width(len(items))
        for index, name in enumerate(directories, 1):
            target = short_dir / f"{index:0{digits}d}"
            target.mkdir(parents=True, exist_ok=True)
            original = "/".join(original_dir + (name,))
            entries.append((target.relative_to(destination).as_posix(), original))
            visit(original_dir + (name,), target)
        for index, name in enumerate(regular_files, 1):
            number = len(directories) + index
            suffix = extension(name)
            target = short_dir / f"{number:0{digits}d}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            file_key = original_dir + (name,)
            file_targets[file_key] = target
            entries.append((target.relative_to(destination).as_posix(), "/".join(file_key)))

    visit((), destination)

    # Stream all file data once. This avoids passing long Unicode member names
    # to unrar as selectors; sizes from the technical listing delimit files.
    process = subprocess.Popen(
        [command, "p", "-idq", "-inul", "-scu", str(source)],
        stdout=subprocess.PIPE,
        env=rar_env(),
    )
    assert process.stdout is not None
    try:
        for raw_member, parts in files:
            key = "/".join(parts)
            if parts not in file_targets or key not in sizes:
                raise RuntimeError(f"RARのサイズ情報がありません: {key}")
            target = file_targets[parts]
            remaining = sizes[key]
            with target.open("wb") as output:
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError(f"RARのデータが途中で終了しました: {key}")
                    output.write(chunk)
                    remaining -= len(chunk)
    finally:
        process.stdout.close()
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"RARデータの読み出しに失敗しました（終了コード{return_code}）")
    write_manifest(destination, entries)


def copy_tree_contents(source: Path, destination: Path) -> None:
    for child in source.iterdir():
        target = destination / child.name
        if child.is_symlink():
            raise ValueError(f"シンボリックリンクには対応していません: {child}")
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def decompress(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        if any(destination.iterdir()):
            raise FileExistsError(f"解凍先は空のフォルダである必要があります: {destination}")
    else:
        destination.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="pathless-extract-") as temporary:
        staging = Path(temporary)
        if source.suffix.lower() == ".zip":
            extract_zip(source, staging)
        elif source.suffix.lower() == ".rar":
            extract_rar(source, staging)
        else:
            raise ValueError("入力拡張子は .zip または .rar にしてください")

        if (staging / MANIFEST).is_file():
            copy_tree_contents(staging, destination)
        else:
            entries = make_short_tree(staging, destination, "all")
            write_manifest(destination, entries)


def default_destination(archive: Path) -> Path:
    return archive.parent / archive.stem


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="長いパスを短い連番パスにしてZIP/RAR化します")
    commands = parser.add_subparsers(dest="command", required=True)
    compress_parser = commands.add_parser("compress", aliases=["c"], help="フォルダを圧縮")
    compress_parser.add_argument("source", type=Path)
    compress_parser.add_argument("destination", type=Path, nargs="?", help="出力アーカイブ")
    compress_parser.add_argument("-o", "--output", dest="output_option", type=Path, help="出力アーカイブ")
    compress_parser.add_argument("--mode", choices=["all", "long"], default="all", help="短縮対象（既定: all）")
    decompress_parser = commands.add_parser("decompress", aliases=["d"], help="アーカイブを空のフォルダへ解凍")
    decompress_parser.add_argument("archive", type=Path)
    decompress_parser.add_argument("destination", type=Path, nargs="?", help="解凍先フォルダ")
    decompress_parser.add_argument("-o", "--output", dest="output_option", type=Path, help="解凍先フォルダ")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in ("compress", "c"):
            if args.destination is not None and args.output_option is not None:
                raise ValueError("出力先は位置引数または--outputのどちらか一方で指定してください")
            output = args.destination or args.output_option
            if output is None:
                raise ValueError("出力アーカイブを指定してください")
            compress(args.source, output, args.mode)
            print(output)
        else:
            if args.destination is not None and args.output_option is not None:
                raise ValueError("解凍先は位置引数または--outputのどちらか一方で指定してください")
            destination = args.destination or args.output_option or default_destination(args.archive)
            decompress(args.archive, destination)
            print(destination)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
