#!/usr/bin/env python3
"""A remote store of files: a bucket, or a directory standing in for one.

  remote.py get URL KEY DEST          download; exit 1 when absent
  remote.py put URL KEY SRC [--once]  upload; with --once only if the key is absent
                                      (exit 3 when it exists)
  remote.py ls  URL [PREFIX]          keys under the prefix

URL is s3://bucket/prefix (needs boto3) or a directory, which the tests and a laptop
use.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import NamedTuple


class Exists(Exception):
    """put(once=True) found the key already written."""


class Entry(NamedTuple):
    key: str
    etag: str               # changes when the object's content does
    modified: dt.datetime


class DirRemote:
    def __init__(self, root):
        self.root = Path(root)

    def get(self, key: str, dest: Path) -> bool:
        src = self.root / key
        if not src.is_file():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return True

    def put(self, key: str, src: Path, once: bool = False) -> None:
        dst = self.root / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
        shutil.copyfile(src, tmp)
        try:
            if once:
                os.link(tmp, dst)   # fails if dst exists, atomically
            else:
                os.replace(tmp, dst)
        except FileExistsError:
            raise Exists(key) from None
        finally:
            tmp.unlink(missing_ok=True)

    def list(self, prefix: str = "") -> list[Entry]:
        entries = []
        for path in sorted(self.root.rglob("*")):
            key = path.relative_to(self.root).as_posix()
            if path.is_file() and key.startswith(prefix) and not key.endswith(".tmp"):
                stat = path.stat()
                entries.append(Entry(key, f"{stat.st_size}-{stat.st_mtime_ns}", dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc)))
        return entries


class S3Remote:
    def __init__(self, url: str):
        import boto3

        bucket, _, prefix = url[len("s3://"):].partition("/")
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.s3 = boto3.client("s3")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def get(self, key: str, dest: Path) -> bool:
        from botocore.exceptions import ClientError

        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return False
            raise
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            shutil.copyfileobj(response["Body"], out)
        return True

    def put(self, key: str, src: Path, once: bool = False) -> None:
        from botocore.exceptions import ClientError

        condition = {"IfNoneMatch": "*"} if once else {}
        try:
            with open(src, "rb") as body:
                self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=body, **condition)
        except ClientError as error:
            # 412: the key exists. 409: another conditional write to it is in flight.
            if once and error.response["ResponseMetadata"]["HTTPStatusCode"] in (409, 412):
                raise Exists(key) from error
            raise

    def list(self, prefix: str = "") -> list[Entry]:
        strip = len(self._key(""))
        entries = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            for obj in page.get("Contents", []):
                entries.append(Entry(obj["Key"][strip:], obj["ETag"], obj["LastModified"]))
        return entries


def remote(url: str):
    return S3Remote(url) if url.startswith("s3://") else DirRemote(url)


def read_json(store, key: str, scratch: Path):
    """The JSON object at key, or None when it is absent."""
    dest = Path(scratch) / key.replace("/", "_")
    return json.loads(dest.read_text()) if store.get(key, dest) else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("get")
    g.add_argument("url"), g.add_argument("key"), g.add_argument("dest", type=Path)
    p = sub.add_parser("put")
    p.add_argument("url"), p.add_argument("key"), p.add_argument("src", type=Path)
    p.add_argument("--once", action="store_true")
    ls = sub.add_parser("ls")
    ls.add_argument("url"), ls.add_argument("prefix", nargs="?", default="")
    args = ap.parse_args(argv)
    store = remote(args.url)
    if args.command == "get":
        return 0 if store.get(args.key, args.dest) else 1
    if args.command == "ls":
        for entry in store.list(args.prefix):
            print(entry.key)
        return 0
    try:
        store.put(args.key, args.src, args.once)
    except Exists:
        print(f"remote: {args.key} is already written", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
