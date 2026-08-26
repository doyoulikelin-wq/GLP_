#!/usr/bin/env python3
"""可靠下载 SAbDab2 大型结构快照。

这个脚本只处理一个官方 bulk 文件。它把文件切成少量字节区间并发下载，
逐段核对 ``Content-Range`` 和预期长度，最后按顺序合并并验证 gzip/tar。

设计目的：

1. 避免普通 ``curl --retry`` 在连接中断后从头覆盖大文件；
2. 允许中断后继续运行，已经完整的分片不会重复下载；
3. 只有所有分片和归档结构都通过验证，才原子替换最终文件。

注意：并发数默认只有 4，避免对公共学术数据库造成不必要压力。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import os
import re
import shutil
import tarfile
import time
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_URL = "https://sabdab.opig.stats.ox.ac.uk/api/download/all-sd-h-structures"
USER_AGENT = "GLP1-scaffold-research/1.0 (single SAbDab2 bulk snapshot)"


def remote_size(url: str) -> int:
    """通过 1 字节 Range 请求读取完整文件长度。

    SAbDab2 的下载端点不接受 HEAD，但明确支持 Range。响应必须为 206，
    并包含形如 ``bytes 0-0/541400281`` 的 ``Content-Range``。
    """

    request = Request(url, headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        if response.status != 206:
            raise RuntimeError(f"Range 预检应返回 206，实际为 {response.status}")
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes 0-0/(\d+)", content_range)
        if not match:
            raise RuntimeError(f"无法解析 Content-Range: {content_range!r}")
        response.read(1)
        return int(match.group(1))


def split_ranges(total: int, workers: int) -> list[tuple[int, int]]:
    """把 ``[0,total)`` 切成近似等长且互不重叠的闭区间。"""

    chunk = (total + workers - 1) // workers
    ranges: list[tuple[int, int]] = []
    for index in range(workers):
        start = index * chunk
        if start >= total:
            break
        end = min(total - 1, start + chunk - 1)
        ranges.append((start, end))
    return ranges


def download_part(url: str, path: Path, start: int, end: int, retries: int = 8) -> dict:
    """下载一个区间，并在已有前缀基础上安全续传。

    每次响应都核对服务端返回的区间起点、终点与总长度。若网络中断，
    保留已写入字节并从新偏移继续；若本地分片反而比目标区间更大，
    立即失败而不是静默截断。
    """

    expected = end - start + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.stat().st_size if path.exists() else 0
    if existing > expected:
        raise RuntimeError(f"分片 {path.name} 超过预期长度：{existing}>{expected}")

    attempt = 0
    while existing < expected:
        current_start = start + existing
        headers = {
            "Range": f"bytes={current_start}-{end}",
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
        }
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=180) as response:
                if response.status != 206:
                    raise RuntimeError(f"{path.name} 应返回 206，实际为 {response.status}")
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                if not match:
                    raise RuntimeError(f"{path.name} Content-Range 非法：{content_range!r}")
                got_start, got_end, got_total = map(int, match.groups())
                if got_start != current_start or got_end != end:
                    raise RuntimeError(
                        f"{path.name} 服务端区间不符：{got_start}-{got_end}，"
                        f"预期 {current_start}-{end}"
                    )
                # 总长度应在所有分片间一致；至少保证当前区间不越界。
                if got_total <= end:
                    raise RuntimeError(f"{path.name} 服务端总长度异常：{got_total}")

                with path.open("ab") as handle:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())

            existing = path.stat().st_size
            if existing > expected:
                raise RuntimeError(f"{path.name} 下载后超过预期长度")
            attempt = 0
        except Exception as exc:  # 网络错误需要保留分片并退避重试。
            attempt += 1
            if attempt > retries:
                raise RuntimeError(f"{path.name} 重试耗尽：{exc}") from exc
            wait_seconds = min(30, 2 ** min(attempt, 4))
            print(f"{path.name}: {exc}; {wait_seconds}s 后续传", flush=True)
            time.sleep(wait_seconds)
            existing = path.stat().st_size if path.exists() else 0

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"part": path.name, "bytes": expected, "sha256": digest}


def finish_part_with_parallel_tails(
    url: str,
    path: Path,
    start: int,
    end: int,
    tail_workers: int,
) -> dict:
    """把一个尚未完成的大分片的“剩余尾部”再次切段并发下载。

    公共下载端点有时会限制单连接吞吐量。若前三个主分片已经完成、只剩一个
    分片缓慢续传，继续只用一条连接会浪费很长时间。本函数保留已验证的本地
    前缀，把剩余互不重叠的字节区间切成少量尾分片，再原子合并；任何中断都
    不会覆盖原前缀。
    """

    expected = end - start + 1
    existing = path.stat().st_size if path.exists() else 0
    if existing > expected:
        raise RuntimeError(f"分片 {path.name} 超过预期长度：{existing}>{expected}")
    if existing == expected or tail_workers == 1:
        return download_part(url, path, start, end)

    remaining_start = start + existing
    remaining_size = end - remaining_start + 1
    relative_ranges = split_ranges(remaining_size, tail_workers)
    absolute_ranges = [
        (remaining_start + relative_start, remaining_start + relative_end)
        for relative_start, relative_end in relative_ranges
    ]
    tails_dir = path.parent / f".{path.name}.tails_from_{existing}"
    tails_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=tail_workers) as pool:
        for index, (tail_start, tail_end) in enumerate(absolute_ranges):
            tail_path = tails_dir / f"tail_{index:03d}.bin"
            jobs.append(pool.submit(download_part, url, tail_path, tail_start, tail_end))
        for future in concurrent.futures.as_completed(jobs):
            print(f"complete-tail {future.result()}", flush=True)

    # 用新临时文件组装“原前缀 + 所有尾分片”；只有长度完全正确才原子替换。
    combined = path.parent / f".{path.name}.combining"
    with combined.open("wb") as target:
        if path.exists():
            with path.open("rb") as source:
                shutil.copyfileobj(source, target, length=4 * 1024 * 1024)
        for index in range(len(absolute_ranges)):
            with (tails_dir / f"tail_{index:03d}.bin").open("rb") as source:
                shutil.copyfileobj(source, target, length=4 * 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    if combined.stat().st_size != expected:
        raise RuntimeError(f"尾分片合并长度不符：{combined.stat().st_size}!={expected}")
    os.replace(combined, path)
    shutil.rmtree(tails_dir)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"part": path.name, "bytes": expected, "sha256": digest}


def verify_tar_gz(path: Path) -> tuple[int, int]:
    """顺序读取归档目录，验证 gzip/tar 完整性并统计 mmCIF 条目。"""

    member_count = 0
    cif_count = 0
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive:
            member_count += 1
            if member.isfile() and member.name.endswith("_sabdab.cif"):
                cif_count += 1
    if cif_count == 0:
        raise RuntimeError("归档可打开，但未找到 *_sabdab.cif")
    return member_count, cif_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--tail-workers",
        type=int,
        default=1,
        help="对每个未完成主分片的剩余尾部再并发切段；断点续传提速时可设为4",
    )
    parser.add_argument(
        "--reuse-prefix",
        type=Path,
        help="可选：把一个已验证为文件前缀的旧 partial 用作第 0 分片起点",
    )
    args = parser.parse_args()

    if not 1 <= args.workers <= 8:
        raise SystemExit("workers 必须在 1..8；公共数据库默认使用 4")
    if not 1 <= args.tail_workers <= 8:
        raise SystemExit("tail-workers 必须在 1..8；公共数据库建议不超过 4")

    total = remote_size(args.url)
    ranges = split_ranges(total, args.workers)
    parts_dir = args.output.with_suffix(args.output.suffix + ".parts")
    parts_dir.mkdir(parents=True, exist_ok=True)

    if args.reuse_prefix and args.reuse_prefix.exists():
        first_part = parts_dir / "part_000.bin"
        if first_part.exists() and first_part.stat().st_size:
            raise RuntimeError("已有 part_000，不能同时导入旧 partial")
        max_first = ranges[0][1] - ranges[0][0] + 1
        if args.reuse_prefix.stat().st_size > max_first:
            raise RuntimeError("旧 partial 大于第 0 分片")
        shutil.move(str(args.reuse_prefix), str(first_part))

    print(f"remote_bytes={total}; parts={len(ranges)}", flush=True)
    if args.tail_workers > 1:
        # 续传提速模式逐个处理主分片，避免“主分片并发 × 尾分片并发”造成请求爆炸。
        for index, (start, end) in enumerate(ranges):
            part_path = parts_dir / f"part_{index:03d}.bin"
            result = finish_part_with_parallel_tails(
                args.url, part_path, start, end, args.tail_workers
            )
            print(f"complete {result}", flush=True)
    else:
        jobs = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for index, (start, end) in enumerate(ranges):
                part_path = parts_dir / f"part_{index:03d}.bin"
                jobs.append(pool.submit(download_part, args.url, part_path, start, end))
            for future in concurrent.futures.as_completed(jobs):
                print(f"complete {future.result()}", flush=True)

    assembling = args.output.with_suffix(args.output.suffix + ".assembling")
    digest = hashlib.sha256()
    with assembling.open("wb") as target:
        for index in range(len(ranges)):
            part_path = parts_dir / f"part_{index:03d}.bin"
            with part_path.open("rb") as source:
                while True:
                    block = source.read(4 * 1024 * 1024)
                    if not block:
                        break
                    target.write(block)
                    digest.update(block)
        target.flush()
        os.fsync(target.fileno())

    if assembling.stat().st_size != total:
        raise RuntimeError(f"合并长度不符：{assembling.stat().st_size}!={total}")
    member_count, cif_count = verify_tar_gz(assembling)
    os.replace(assembling, args.output)
    shutil.rmtree(parts_dir)
    print(
        f"verified output={args.output} bytes={total} sha256={digest.hexdigest()} "
        f"tar_members={member_count} cif_files={cif_count}",
        flush=True,
    )


if __name__ == "__main__":
    main()
