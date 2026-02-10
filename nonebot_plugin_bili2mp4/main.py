from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Set, Tuple, Dict
from urllib.parse import parse_qs, unquote, urlparse

from nonebot import logger, on_message, require
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.plugin import get_plugin_config

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

from .config import Config

PLUGIN_NAME = "nonebot_plugin_bili2mp4"
DATA_DIR: Optional[Path] = None
STATE_PATH: Optional[Path] = None
DOWNLOAD_DIR: Optional[Path] = None
COOKIE_FILE_PATH: Optional[Path] = None

enabled_groups: Set[int] = set()
bilibili_cookie: str = ""
max_height: int = 0
max_filesize_mb: int = 0
max_duration_sec: int = 0
bili_super_admins: List[int] = []

# 映射路径 -> 真实路径 映射，例如 "/bilivideo" -> "C:\\...\\downloads"
path_mappings: Dict[str, str] = {}

_processing: Set[str] = set()

FFMPEG_DIR: Optional[str] = None

CMD_LIST = {"查看转换列表", "查看列表", "转换列表"}
CMD_ENABLE_RE = re.compile(r"^转换\s*(\d+)$", flags=re.IGNORECASE)
CMD_DISABLE_RE = re.compile(r"^停止转换\s*(\d+)$", flags=re.IGNORECASE)
CMD_SET_COOKIE_RE = re.compile(r"^设置B站COOKIE\s+(.+)$", flags=re.S)
CMD_CLEAR_COOKIE = {"清除B站COOKIE", "删除B站COOKIE"}
CMD_SET_HEIGHT_RE = re.compile(r"^设置清晰度\s*(\d+)$", flags=re.IGNORECASE)
CMD_SET_MAXSIZE_RE = re.compile(r"^设置最大大小\s*(\d+)\s*MB$", flags=re.IGNORECASE)
CMD_SET_MAXDUR_RE = re.compile(r"^设置最大时长\s*(\d+)\s*S$", flags=re.IGNORECASE)
CMD_SHOW_PARAMS = {"查看参数", "参数", "设置"}

# 映射命令
CMD_SET_MAPPING_RE = re.compile(r"^映射路径\s+(\S+)\s+(.+)$", flags=re.IGNORECASE)
CMD_REMOVE_MAPPING_RE = re.compile(r"^删除映射\s+(\S+)$", flags=re.IGNORECASE)
CMD_LIST_MAPPINGS = {"查看映射", "映射列表"}

# 域名匹配
BILI_URL_RE = re.compile(
    r"(https?://(?:[\w-]+\.)?(?:bilibili\.com|b23\.tv)/[^\s\"'<>]+)",
    flags=re.IGNORECASE,
)


# =========================
# 初始化函数
# =========================


def _init_plugin():
    global DATA_DIR, STATE_PATH, DOWNLOAD_DIR, COOKIE_FILE_PATH
    global bili_super_admins, FFMPEG_DIR, path_mappings

    if DATA_DIR is not None:
        return

    # 读取插件配置
    plugin_config = get_plugin_config(Config)
    bili_super_admins = plugin_config.bili_super_admins or []

    # 获取数据目录
    DATA_DIR = store.get_plugin_data_dir()
    STATE_PATH = DATA_DIR / "state.json"
    COOKIE_FILE_PATH = DATA_DIR / "bili_cookies.txt"
    DOWNLOAD_DIR = DATA_DIR / "downloads"
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"bili2mp4: DATA_DIR={DATA_DIR} STATE_PATH={STATE_PATH}")

    _load_state()

    # 解析FFmpeg路径
    if plugin_config.ffmpeg_path:
        ffmpeg_dir = Path(plugin_config.ffmpeg_path)
        ffmpeg_exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        ffmpeg_bin = ffmpeg_dir / ffmpeg_exe
        if ffmpeg_bin.exists():
            FFMPEG_DIR = str(ffmpeg_dir)
            logger.info(f"bili2mp4: 使用配置中的ffmpeg目录: {FFMPEG_DIR}")
        else:
            logger.warning(
                f"bili2mp4: 配置的ffmpeg目录不存在或无{ffmpeg_exe}: {ffmpeg_bin}"
            )
            FFMPEG_DIR = None
    else:
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            FFMPEG_DIR = os.path.dirname(ffmpeg_path)
            logger.info(f"bili2mp4: 从PATH找到ffmpeg: {ffmpeg_path}")
        else:
            logger.info("bili2mp4: 未找到ffmpeg")
            FFMPEG_DIR = None

    logger.info(f"bili2mp4: 初始化完成，超管={bili_super_admins}")


# =========================
# 状态读写
# =========================


def _save_state():
    if not STATE_PATH:
        return
    data = {
        "enabled_groups": list(enabled_groups),
        "bilibili_cookie": bilibili_cookie,
        "max_height": max_height,
        "max_filesize_mb": max_filesize_mb,
        "max_duration_sec": max_duration_sec,
        "path_mappings": path_mappings,
    }
    try:
        with STATE_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"bili2mp4: 保存状态失败: {e}")


def _load_state():
    global enabled_groups, bilibili_cookie, max_height, max_filesize_mb, max_duration_sec, path_mappings

    if not STATE_PATH or not STATE_PATH.exists():
        return

    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        enabled_groups = set(map(int, data.get("enabled_groups", [])))
        bilibili_cookie = data.get("bilibili_cookie", "")
        max_height = int(data.get("max_height", 0))
        max_filesize_mb = int(data.get("max_filesize_mb", 0))
        max_duration_sec = int(data.get("max_duration_sec", 0))
        path_mappings = data.get("path_mappings", {}) or {}
    except Exception as e:
        logger.warning(f"bili2mp4: 状态加载失败: {e}")


def _get_help_message() -> str:
    """获取帮助信息"""
    return (
        "【nonebot-plugin-bili2mp4 帮助】\n\n"
        "管理员私聊命令：\n"
        "• 转换 <群号> - 开启指定群的B站视频转换功能\n"
        "• 停止转换 <群号> - 停止指定群的B站视频转换功能\n"
        "• 设置B站COOKIE <cookie字符串> - 设置B站Cookie以获取更高清晰度\n"
        "• 清除B站COOKIE - 清除已设置的B站Cookie\n"
        "• 设置清晰度 <数字> - 设置视频清晰度限制（如 720/1080，0 代表不限制）\n"
        "• 设置最大大小 <数字>MB - 设置视频大小限制（0 代表不限制）\n"
        "• 设置最大时长 <数字>S - 设置视频最大时长（秒，0 代表不限制）\n"
        "• 查看参数 - 查看当前配置参数\n"
        "• 查看转换列表 - 查看已开启转换功能的群列表\n"
        "• 映射路径 <映射路径> <真实路径> - 将服务器真实路径映射为映射路径（例如 /bilivideo）\n"
        "• 删除映射 <映射路径> - 删除已设置的映射\n"
        "• 查看映射 - 列出当前映射\n\n"
        "Cookie中至少需要包含SESSDATA、bili_jct、DedeUserID和buvid3/buvid4四个字段"
    )


def _find_urls_in_text(text: str) -> List[str]:
    urls = []
    for m in BILI_URL_RE.findall(text or ""):
        if m not in urls:
            urls.append(m)
    try:
        parsed = urlparse(text)
        if parsed and parsed.query:
            qs = parse_qs(parsed.query)
            for key in ("url", "qqdocurl", "jumpUrl", "webpageUrl"):
                for v in qs.get(key, []):
                    v = unquote(v)
                    for u in BILI_URL_RE.findall(v):
                        if u not in urls:
                            urls.append(u)
    except Exception:
        pass
    return urls


def _extract_bvid_from_url(url: str) -> Optional[str]:
    """从 B 站链接中提取 BV 号"""
    try:
        parsed = urlparse(url)
        # 1) 先看 query 里有没有 bvid
        qs = parse_qs(parsed.query)
        bvid_list = qs.get("bvid") or qs.get("bvids")
        if bvid_list:
            return bvid_list[0]

        # 2) 再从 path 中匹配 /video/BVxxxx
        m = re.search(r"/video/(BV[0-9A-Za-z]+)", parsed.path)
        if m:
            return m.group(1)

        return None
    except Exception:
        return None


def _walk_strings(obj) -> List[str]:
    out: List[str] = []
    try:
        if isinstance(obj, dict):
            for v in obj.values():
                out.extend(_walk_strings(v))
        elif isinstance(obj, list):
            for it in obj:
                out.extend(_walk_strings(it))
        elif isinstance(obj, str):
            out.append(obj)
    except Exception:
        pass
    return out


def _extract_bili_urls_from_event(event: GroupMessageEvent) -> List[str]:
    urls: List[str] = []
    try:
        # 遍历消息段
        for seg in event.message:
            # 1) 纯文本
            if seg.type == "text":
                txt = seg.data.get("text", "")
                for u in _find_urls_in_text(txt):
                    if u not in urls:
                        urls.append(u)

            # 2) JSON 卡片
            elif seg.type == "json":
                raw = seg.data.get("data") or seg.data.get("content") or ""
                for u in _find_urls_in_text(raw):
                    if u not in urls:
                        urls.append(u)
                try:
                    obj = json.loads(raw)
                    for s in _walk_strings(obj):
                        for u in _find_urls_in_text(s):
                            if u not in urls:
                                urls.append(u)
                except Exception:
                    pass

            # 3) XML 卡片
            elif seg.type == "xml":
                raw = seg.data.get("data") or seg.data.get("content") or ""
                for u in _find_urls_in_text(raw):
                    if u not in urls:
                        urls.append(u)

            # 4) 分享卡片
            elif seg.type == "share":
                u = seg.data.get("url") or ""
                for u2 in _find_urls_in_text(u):
                    if u2 not in urls:
                        urls.append(u2)

            # 5) 其他消息段
            else:
                s = str(seg)
                for u in _find_urls_in_text(s):
                    if u not in urls:
                        urls.append(u)

        try:
            full_text = event.get_plaintext()
        except Exception:
            full_text = ""

        # 匹配 av123456（不匹配纯数字）
        for m in re.findall(r"(?i)\bav(\d+)\b", full_text):
            av_str = f"av{m}"
            if av_str not in urls:
                urls.append(av_str)

        # 匹配 AV 链接（如 /video/av123456/）
        for m in re.findall(r"https?://[^\s\"'<>]*/video/av(\d+)", full_text, flags=re.IGNORECASE):
            av_url = f"https://www.bilibili.com/video/av{m}/"
            if av_url not in urls:
                urls.append(av_url)

    except Exception as e:
        logger.debug(f"bili2mp4: 提取链接异常: {e}")

    return urls


def _extract_aid_from_url(url: str) -> Optional[int]:
    """从 B 站链接中提取 AV 号"""
    try:
        parsed = urlparse(url)
        # /video/av123456
        m = re.search(r"/video/av(\d+)", parsed.path, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))

        # query 中的 aid / avid
        qs = parse_qs(parsed.query)
        for key in ("aid", "avid"):
            vals = qs.get(key)
            if vals:
                num_m = re.search(r"(\d+)", vals[0])
                if num_m:
                    return int(num_m.group(1))

        return None
    except Exception:
        return None


def _bili_av_to_bv(aid: int) -> Optional[str]:
    """将 AV 号转换为 BV 号"""
    try:
        table = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
        s = [11, 10, 3, 8, 4, 6]
        xor = 177451812
        add = 8728348608

        x = (aid ^ xor) + add
        r = list("BV1  4 1 7  ")
        for i in range(6):
            r[s[i]] = table[x // 58**i % 58]
        return "".join(r)
    except Exception:
        return None


def _normalize_bili_url(raw: str) -> str:
    u = (raw or "").strip()

    # 1) av123456 / AV123456 这种纯 AV 前缀形式
    m = re.fullmatch(r"(?i)av(\d+)", u)
    if m:
        aid = int(m.group(1))
        bv = _bili_av_to_bv(aid)
        if bv:
            return f"https://www.bilibili.com/video/{bv}"
        return raw

    # 2) 非 URL，且不是 av 前缀形式，直接返回
    if not u.lower().startswith(("http://", "https://")):
        return raw

    # 3) 先展开 b23.tv 短链
    u2 = _expand_short_url(u)

    # 4) 如果是 AV 链接，转为 BV 链接
    aid = _extract_aid_from_url(u2)
    if aid is not None:
        bv = _bili_av_to_bv(aid)
        if bv:
            return f"https://www.bilibili.com/video/{bv}"

    # 5) 其他情况（BV 链接等）直接返回展开后的 URL
    return u2


def _build_browser_like_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }


def _expand_short_url(u: str, timeout: float = 8.0) -> str:
    try:
        host = urlparse(u).hostname or ""
        if host.lower() not in {"b23.tv", "www.b23.tv"}:
            return u
        hdrs = {
            "User-Agent": _build_browser_like_headers()["User-Agent"],
            "Referer": "https://www.bilibili.com/",
        }
        try:
            req = urllib.request.Request(u, headers=hdrs, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final = resp.geturl()
                return final or u
        except Exception:
            req = urllib.request.Request(u, headers=hdrs, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final = resp.geturl()
                return final or u
    except Exception as e:
        logger.debug(f"bili2mp4: 短链展开失败，使用原链接（{u}）：{e}")
        return u


def _ensure_cookiefile(cookie_string: str) -> Optional[str]:
    """
    将 Cookie 字符串转为 Netscape 格式，供 yt-dlp 使用。
    """
    if COOKIE_FILE_PATH is None:
        return None

    cookie_string = (cookie_string or "").strip().strip(";")
    if not cookie_string:
        if COOKIE_FILE_PATH.exists():
            try:
                COOKIE_FILE_PATH.unlink()
            except Exception:
                pass
        return None

    pairs = []
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k and v:
            pairs.append((k.strip(), v.strip()))

    if not pairs:
        return None

    expiry = int(time.time()) + 180 * 24 * 3600
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by nonebot_plugin_bili2mp4",
        "",
    ]

    for k, v in pairs:
        # domain include_subdomains path secure expiry name value
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t{expiry}\t{k}\t{v}")

    try:
        with COOKIE_FILE_PATH.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info("bili2mp4: Cookie 已设置")
        return str(COOKIE_FILE_PATH)
    except Exception:
        return None


def _check_video_file(path: str) -> bool:
    """检查视频分辨率（大小限制在 _download_with_ytdlp 中处理）"""
    try:
        path_obj = Path(path)

        # 如果文件不存在，直接失败
        if not path_obj.exists():
            return False

        # 检查视频分辨率
        ffprobe_exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        cmd = [ffprobe_exe]
        if FFMPEG_DIR:
            cmd[0] = str(Path(FFMPEG_DIR) / ffprobe_exe)

        cmd.extend(
            [
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                path,
            ]
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            try:
                width, height = result.stdout.strip().split(",")
                # 检查是否设置了高度限制
                if max_height and int(height) > max_height:
                    path_obj.unlink(missing_ok=True)
                    return False
            except ValueError:
                pass

        return True
    except Exception:
        return False


def _get_bili_duration_seconds(url: str) -> Optional[int]:
    """
    通过 B 站开放接口获取视频时长（秒）
    """
    try:
        norm = _normalize_bili_url(url)
        bvid = _extract_bvid_from_url(norm)
        if not bvid:
            return None

        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        req = urllib.request.Request(
            api_url,
            headers=_build_browser_like_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        data = json.loads(raw)

        if data.get("code") != 0:
            return None

        d = data.get("data") or {}
        dur = d.get("duration")
        if isinstance(dur, int):
            return dur
        return None
    except Exception as e:
        logger.debug(f"bili2mp4: 获取视频时长失败: {e}")
        return None


async def _send_video_with_timeout(
    bot: Bot, group_id: int, path: str, title: str
) -> None:
    path_obj = Path(path)

    try:
        if not path_obj.exists():
            logger.warning(f"bili2mp4: 待发送文件不存在: {path}")
            return

        # 如果存在映射，使用映射后的虚拟路径发送
        send_path = str(path_obj)
        for virt, real in path_mappings.items():
            try:
                real_p = str(Path(real).resolve())
                p_resolved = str(path_obj.resolve())
                if p_resolved.startswith(real_p):
                    # 构造虚拟路径：映射路径 + 相对路径
                    rel = p_resolved[len(real_p):].replace("\\", "/")
                    if not rel.startswith("/"):
                        rel = "/" + rel
                    send_path = virt.rstrip("/") + rel
                    logger.debug(f"bili2mp4: 使用映射发送路径 {send_path} (real={p_resolved})")
                    break
            except Exception:
                continue

        # 通过文件路径发送视频
        await bot.send_group_msg(
            group_id=group_id,
            message=MessageSegment.video(file=send_path)
            + Message(f"\n{title or 'B站视频'}"),
        )
        logger.info(f"bili2mp4: 发送视频到群 {group_id}: {title or 'B站视频'}")

    except Exception as e:
        logger.warning(f"bili2mp4: 发送视频失败: {e}")
    finally:
        try:
            if path_obj.exists():
                path_obj.unlink(missing_ok=True)
                logger.debug(f"bili2mp4: 已删除临时文件 {path}")
        except Exception as e:
            logger.debug(f"bili2mp4: 删除临时文件失败 {path}: {e}")


def _locate_final_file(ydl, info) -> Optional[str]:
    for key in ("requested_downloads", "requested_formats"):
        arr = info.get(key)
        if isinstance(arr, list):
            for it in arr:
                fp = it.get("filepath")
                if fp and os.path.exists(fp):
                    return fp
    for key in ("filepath", "_filename"):
        fp = info.get(key)
        if fp and os.path.exists(fp):
            return fp
    # 预测合并后 mp4
    base = ydl.prepare_filename(info)
    root, _ = os.path.splitext(base)
    candidate = root + ".mp4"
    if os.path.exists(candidate):
        return candidate
    # 兜底：按视频ID在目录中搜
    vid = info.get("id") or ""
    if vid:
        dirpath = os.path.dirname(base) or os.getcwd()
        try:
            files = [Path(dirpath) / f for f in os.listdir(dirpath) if vid in f]
            if files:
                files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return str(files[0])
        except Exception:
            pass
    return None


def _download_with_ytdlp(
    url: str, cookie: str, out_dir, height_limit: int, size_limit_mb: int
) -> Tuple[str, str]:
    try:
        from yt_dlp import YoutubeDL  # type: ignore
        from yt_dlp.utils import DownloadError  # type: ignore
    except Exception:
        raise ImportError("yt_dlp not installed")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    final_url = _expand_short_url(url)
    cookiefile = _ensure_cookiefile(cookie)

    headers = _build_browser_like_headers()
    base_opts = {
        "outtmpl": str(out_dir / "%(title).80s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "http_headers": headers,
        "extractor_args": {"bili": {"player_client": ["android", "web"], "lang": ["zh-CN"]}},
    }
    if FFMPEG_DIR:
        base_opts["ffmpeg_location"] = FFMPEG_DIR
    if cookiefile:
        base_opts["cookiefile"] = cookiefile
        logger.info(f"bili2mp4: 使用 cookiefile: {cookiefile}")
    elif cookie:
        headers["Cookie"] = cookie
        logger.info("bili2mp4: 使用 Cookie header")

    # 1) 先获取所有可用格式（不下载）
    try:
        with YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(final_url, download=False)
            title = info.get("title") or "B站视频"
            formats = info.get("formats", []) or []
            # 过滤掉仅音频或无视频的格式
            formats = [f for f in formats if f.get("vcodec") != "none"]
            # 按高度和码率排序，从高到低
            formats.sort(key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)), reverse=True)
    except Exception as e:
        raise RuntimeError(f"获取视频格式信息失败: {e}")

    last_err: Optional[Exception] = None

    # 2) 逐个预检 formats（使用 metadata 判断大小与高度），只对通过预检的格式进行下载
    for fmt in formats:
        fmt_id = fmt.get("format_id")
        h = fmt.get("height") or 0

        # 跳过超过高度限制的格式
        if height_limit and h and h > height_limit:
            logger.debug(f"bili2mp4: 预检跳过格式 {fmt_id}，高度 {h} 超过限制 {height_limit}")
            continue

        # 使用格式元数据判断文件大小（优先 filesize_approx，再 filesize）
        filesize_bytes = None
        if fmt.get("filesize_approx"):
            try:
                filesize_bytes = int(fmt.get("filesize_approx"))
            except Exception:
                filesize_bytes = None
        if filesize_bytes is None and fmt.get("filesize"):
            try:
                filesize_bytes = int(fmt.get("filesize"))
            except Exception:
                filesize_bytes = None

        if size_limit_mb and filesize_bytes is not None:
            size_mb_est = filesize_bytes / (1024 * 1024)
            if size_mb_est > size_limit_mb:
                logger.info(
                    f"bili2mp4: 预检跳过格式 {fmt_id}，估算大小 {size_mb_est:.2f}MB 超过限制 {size_limit_mb}MB"
                )
                continue

        # 如果没有 filesize 信息但用户设置了大小限制，仍可尝试，但记录为不确定
        logger.info(f"bili2mp4: 预检通过，准备下载格式 {fmt_id} 高度={h} 估算大小={'未知' if filesize_bytes is None else f'{filesize_bytes/(1024*1024):.2f}MB'}")

        # 构造下载选项，仅下载该 format_id
        opts = dict(base_opts)
        opts["format"] = fmt_id

        try:
            with YoutubeDL(opts) as ydl:
                info2 = ydl.extract_info(final_url, download=True)
                final_path = _locate_final_file(ydl, info2)
                if not final_path or not Path(final_path).exists():
                    logger.debug(f"bili2mp4: 未找到下载后的文件，格式 {fmt_id}")
                    last_err = RuntimeError("下载后未找到文件")
                    # 尝试下一个候选
                    continue

                # 如果下载后仍有 size_limit_mb，二次确认
                if size_limit_mb:
                    try:
                        size_mb = Path(final_path).stat().st_size / (1024 * 1024)
                        if size_mb > size_limit_mb:
                            logger.info(
                                f"bili2mp4: 下载后文件 {final_path} 大小 {size_mb:.2f}MB 超过限制 {size_limit_mb}MB，删除并尝试更低清晰度"
                            )
                            try:
                                Path(final_path).unlink(missing_ok=True)
                            except Exception as e:
                                logger.debug(f"bili2mp4: 删除超限文件失败 {final_path}: {e}")
                            last_err = RuntimeError("文件超过大小限制")
                            continue
                    except Exception:
                        # 无法读取文件大小时，仍当作成功处理（但记录日志）
                        logger.debug(f"bili2mp4: 无法读取已下载文件大小以确认限制: {final_path}")

                # 成功且未超限
                return final_path, title

        except DownloadError as e:
            last_err = e
            logger.warning(f"bili2mp4: 格式 {fmt_id} 下载失败: {e}")
            continue
        except Exception as e:
            last_err = e
            logger.warning(f"bili2mp4: 格式 {fmt_id} 异常: {e}")
            continue

    # 所有候选格式都失败或都超限
    if last_err:
        raise RuntimeError(str(last_err))
    raise RuntimeError("无法下载该视频（所有候选格式均不满足条件或下载失败）")


async def _download_and_send(bot: Bot, group_id: int, url: str) -> None:
    # 规范化链接
    norm_url = _normalize_bili_url(url)

    # 如果设置了最大时长，通过 API 检查
    try:
        if max_duration_sec and max_duration_sec > 0:
            dur = _get_bili_duration_seconds(norm_url)
            if dur is not None:
                if dur > max_duration_sec:
                    logger.info(
                        f"bili2mp4: 视频时长 {dur}s 超过最大限制 {max_duration_sec}s，跳过下载"
                    )
                    return
                else:
                    logger.info(
                        f"bili2mp4: 视频时长 {dur}s 在限制 {max_duration_sec}s 内，继续下载"
                    )
    except Exception as e:
        logger.debug(f"bili2mp4: 最大时长预检查失败，忽略并继续下载: {e}")

    # 执行下载（内部仍会再次处理短链，但已是标准 BV 链接）
    try:
        path, title = await asyncio.to_thread(
            _download_with_ytdlp,
            norm_url,
            bilibili_cookie,
            DOWNLOAD_DIR,
            max_height,
            max_filesize_mb,
        )
    except (ImportError, RuntimeError) as e:
        logger.warning(f"下载环境异常: {e}")
        return
    except Exception as e:
        logger.error(f"bili2mp4: 下载异常: {e}")
        return

    # 检查文件大小和分辨率
    if not _check_video_file(path):
        logger.info(f"bili2mp4: 文件 {path} 未通过分辨率检查或已被删除")
        return

    # 发送视频（使用文件路径）
    await _send_video_with_timeout(bot, group_id, path, title)


async def _handle_group_command(
    bot: Bot, event: PrivateMessageEvent, text: str
) -> bool:
    """处理群相关命令"""
    global enabled_groups

    # 开启群
    m = CMD_ENABLE_RE.fullmatch(text)
    if m:
        gid = int(m.group(1))
        if gid in enabled_groups:
            await bot.send(event, Message(f"ℹ️ 群 {gid} 已开启转换"))
        else:
            enabled_groups.add(gid)
            _save_state()
            await bot.send(event, Message(f"✅ 已开启群 {gid} 的B站视频转换"))
        return True

    # 关闭群
    m = CMD_DISABLE_RE.fullmatch(text)
    if m:
        gid = int(m.group(1))
        if gid in enabled_groups:
            enabled_groups.discard(gid)
            _save_state()
            await bot.send(event, Message(f"🛑 已停止群 {gid} 的B站视频转换"))
        else:
            await bot.send(event, Message(f"ℹ️ 群 {gid} 未开启转换"))
        return True

    # 查看列表
    if text in CMD_LIST:
        if enabled_groups:
            sorted_g = sorted(list(enabled_groups))
            await bot.send(
                event, Message("当前已开启转换的群：" + ", ".join(map(str, sorted_g)))
            )
        else:
            await bot.send(event, Message("暂无开启转换的群"))
        return True

    return False


async def _handle_config_command(
    bot: Bot, event: PrivateMessageEvent, text: str
) -> bool:
    """处理配置相关命令"""
    global bilibili_cookie, max_height, max_filesize_mb, max_duration_sec, path_mappings

    # 设置Cookie
    m = CMD_SET_COOKIE_RE.fullmatch(text)
    if m:
        bilibili_cookie = m.group(1).strip()
        _save_state()
        await bot.send(event, Message("✅ 已设置B站 Cookie"))
        return True

    # 清除Cookie
    if text in CMD_CLEAR_COOKIE:
        bilibili_cookie = ""
        _save_state()
        await bot.send(event, Message("🧹 已清除B站 Cookie"))
        return True

    # 设置清晰度
    m = CMD_SET_HEIGHT_RE.fullmatch(text)
    if m:
        h = int(m.group(1))
        if h < 0:
            h = 0
        max_height = h
        _save_state()
        await bot.send(
            event, Message(f"⏱ 清晰度已设置为 {'不限制' if h == 0 else f'<= {h}p'}")
        )
        return True

    # 设置最大大小（MB）
    m = CMD_SET_MAXSIZE_RE.fullmatch(text)
    if m:
        lim = int(m.group(1))
        if lim < 0:
            lim = 0
        max_filesize_mb = lim
        _save_state()
        await bot.send(
            event,
            Message(f"📦 文件大小限制为 {'不限制' if lim == 0 else f'<= {lim}MB'}"),
        )
        return True

    # 设置最大时长（秒）
    m = CMD_SET_MAXDUR_RE.fullmatch(text)
    if m:
        d = int(m.group(1))
        if d < 0:
            d = 0
        max_duration_sec = d
        _save_state()
        await bot.send(
            event,
            Message(
                f"⏱ 最大时长已设置为 {'不限制' if d == 0 else f'<= {d} 秒'}"
            ),
        )
        return True

    # 查看参数
    if text in CMD_SHOW_PARAMS:
        await bot.send(
            event,
            Message(
                f"参数：清晰度<= {max_height or '不限'}；"
                f"大小<= {str(max_filesize_mb) + 'MB' if max_filesize_mb else '不限'}；"
                f"最大时长<= {str(max_duration_sec) + '秒' if max_duration_sec else '不限'}；"
                f"Cookie={'已设置' if bool(bilibili_cookie) else '未设置'}；启用群数={len(enabled_groups)}"
            ),
        )
        return True

    # 设置映射
    m = CMD_SET_MAPPING_RE.fullmatch(text)
    if m:
        virt = m.group(1).strip()
        real = m.group(2).strip()
        # 支持带引号路径
        if (real.startswith('"') and real.endswith('"')) or (real.startswith("'") and real.endswith("'")):
            real = real[1:-1].strip()
        # 规范化
        if not virt.startswith("/"):
            virt = "/" + virt
        try:
            real_p = str(Path(real).resolve())
        except Exception as e:
            logger.warning(f"bili2mp4: 映射路径解析失败 raw={real} err={e}")
            await bot.send(event, Message(f"❌ 路径解析失败: {e}"))
            return True

        # 可选：检查路径是否存在（这里提示并仍允许保存）
        if not Path(real_p).exists():
            await bot.send(event, Message(f"⚠️ 目标路径不存在: {real_p}，请确认路径或创建后重试"))
            # 仍然保存映射以便管理员后续修正；如需强制存在可改为 return True
            # return True

        path_mappings[virt] = real_p
        _save_state()
        logger.info(f"bili2mp4: 已添加映射 {real_p} -> {virt}")
        await bot.send(event, Message(f"✅ 已映射 {real_p} -> {virt}"))
        return True

    # 删除映射
    m = CMD_REMOVE_MAPPING_RE.fullmatch(text)
    if m:
        virt = m.group(1).strip()
        if not virt.startswith("/"):
            virt = "/" + virt
        if virt in path_mappings:
            path_mappings.pop(virt, None)
            _save_state()
            await bot.send(event, Message(f"🗑 已删除映射 {virt}"))
        else:
            await bot.send(event, Message(f"ℹ️ 未找到映射 {virt}"))
        return True

    # 查看映射
    if text in CMD_LIST_MAPPINGS:
        if path_mappings:
            lines = [f"{virt} -> {real}" for virt, real in path_mappings.items()]
            await bot.send(event, Message("当前映射：\n" + "\n".join(lines)))
        else:
            await bot.send(event, Message("暂无映射"))
        return True

    return False


# =========================
# 消息处理器注册
# =========================


try:
    _init_plugin()
except Exception as e:
    logger.exception(f"bili2mp4: 初始化失败: {e}")


matcher = on_message(priority=5)

@matcher.handle()
async def _bili2mp4_message_handler(bot: Bot, event: Event):
    try:
        _init_plugin()

        # 私聊命令处理
        if isinstance(event, PrivateMessageEvent):
            try:
                text = event.get_plaintext().strip()
            except Exception:
                text = str(event.message)

            logger.debug(f"bili2mp4: 收到私聊消息 from={getattr(event, 'user_id', 'unknown')} text={text}")

            try:
                sender = int(getattr(event, "user_id", 0))
            except Exception:
                sender = 0

            # 仅超管可执行配置命令（按需调整）
            if sender in (bili_super_admins or []):
                handled = await _handle_group_command(bot, event, text)
                if handled:
                    return
                handled = await _handle_config_command(bot, event, text)
                if handled:
                    return
                # 未匹配任何命令，忽略或回复帮助
                logger.debug(f"bili2mp4: 私聊命令未匹配 text={text}")
                return
            else:
                logger.debug(f"bili2mp4: 非超管尝试执行命令 user={sender} text={text}")
                return

        # 群消息处理：提取 B 站链接并触发下载
        if isinstance(event, GroupMessageEvent):
            try:
                group_id = int(getattr(event, "group_id", 0))
            except Exception:
                group_id = 0

            # 只在已启用的群处理
            if group_id not in enabled_groups:
                return

            urls = _extract_bili_urls_from_event(event)
            if not urls:
                return

            # 去重并异步下载发送
            for u in urls:
                if u in _processing:
                    logger.debug(f"bili2mp4: 链接已在处理队列 {u}")
                    continue
                _processing.add(u)

                async def _task_wrapper(bot: Bot, group_id: int, u: str):
                    try:
                        await _download_and_send(bot, group_id, u)
                    finally:
                        try:
                            _processing.discard(u)
                        except Exception:
                            pass

                asyncio.create_task(_task_wrapper(bot, group_id, u))
    except Exception as e:
        logger.exception(f"bili2mp4: 消息处理器异常: {e}")
