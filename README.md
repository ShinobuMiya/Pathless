# pathless-archive

Command-line tool for compressing and extracting archives while replacing long
path names with short sequential names. The manifest is stored as a UTF-8
`meaning.tsv` file at the archive root.

## Usage

```sh
python3 pathless_archive.py compress ./input-folder -o ./result.zip
python3 pathless_archive.py compress ./input-folder ./result.zip
python3 pathless_archive.py decompress ./result.zip -o ./output-folder
# or
python3 pathless_archive.py decompress ./result.zip ./output-folder
```

If no extraction destination is specified, the default is a folder next to the
archive with the archive extension removed. The destination must either not
exist or be an empty folder.

By default, all paths are shortened. To shorten only path components longer
than 255 UTF-8 bytes, use:

```sh
python3 pathless_archive.py compress ./input-folder -o ./result.zip --mode long
```

Quote paths containing spaces or shell metacharacters such as `[` and `]`.

## RAR support

ZIP works without additional dependencies. RAR extraction requires `unrar`,
and RAR compression requires the `rar` command. Specifying `.rar` for
compression fails if the `rar` command is unavailable.

RAR extraction streams file data directly into shortened paths, so the
original long names are not created on the filesystem first.

## Manifest

Each line in `meaning.tsv` has this format:

```text
short-relative-path<TAB>original-relative-path
```

Within each directory, subdirectories are numbered first, and files continue
from the next number. For example, if subdirectories are numbered `001` to
`008`, the first file is `009`.

The number width is based on the total number of immediate children, with a
minimum of three digits. Files retain only their final extension.
