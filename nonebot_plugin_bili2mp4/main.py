from __future__ import annotations

import base64
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Set, Tuple
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
    global bili_super_admins, FFMPEG_DIR

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
    DOWNLOAD_DIR.mkdir(exist_ok=True)

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
    }
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_state():
    global enabled_groups, bilibili_cookie, max_height, max_filesize_mb, max_duration_sec

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
        "• 查看转换列表 - 查看已开启转换功能的群列表\n\n"
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

    # 2) 非 URL，且不是 av 前缀形式，直接返回（不再把纯数字当 AV 号）
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
                if COOKIE_FILE_PATH.exists():
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
                    path_obj.unlink()
                    return False
            except ValueError:
                pass

        return True
    except Exception:
        return False


def _get_bili_duration_seconds(url: str) -> Optional[int]:
    """
    通过 B 站开放接口获取视频时长（秒）
    - 支持 BV 链接 / AV 链接 / b23.tv（已在外部规范化）
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
    """发送视频（先转为 base64），发送后删除本地文件"""
    sent = False
    path_obj = Path(path)

    try:
        if not path_obj.exists():
            logger.warning(f"bili2mp4: 待发送文件不存在: {path}")
            return

        # 读取文件并转为 base64
        with path_obj.open("rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        file_field = f"base64://{b64}"

        # 通过 base64 发送视频
        await bot.send_group_msg(
            group_id=group_id,
            message=MessageSegment.video(file=file_field)
            + Message(f"\n{title or 'B站视频'}"),
        )
        logger.info(
            f"bili2mp4: 通过 base64 发送视频到群 {group_id}: {title or 'B站视频'}"
        )
        sent = True

    except Exception as e:
        error_msg = str(e)
        if not ("timeout" in error_msg.lower() and "websocket" in error_msg.lower()):
            logger.warning(
                f"bili2mp4: 发送 base64 视频失败: {path_obj.name} | group={group_id} | err={e}"
            )
    finally:
        try:
            if path_obj.exists():
                path_obj.unlink()
                logger.debug(f"bili2mp4: 已删除临时文件 {path}")
        except Exception as e:
            logger.debug(f"bili2mp4: 删除临时文件失败 {path}: {e}")


def _build_format_candidates(height_limit: int, size_limit_mb: int) -> List[str]:
    """构建格式候选列表"""
    h = height_limit if height_limit and height_limit > 0 else None

    if not h:
        return ["bv*+ba/best"]

    # 根据清晰度限制构建格式候选
    format_map = {
        1080: [
            f"bv*[height>=1080]+ba/best",
            f"bv*[height>=720]+ba/best",
            "bv*+ba/best",
        ],
        720: [f"bv*[height>=720]+ba/best", f"bv*[height>=480]+ba/best", "bv*+ba/best"],
        480: [f"bv*[height>=480]+ba/best", "bv*+ba/best"],
    }

    # 根据高度选择最适合的格式列表
    for threshold, formats in sorted(format_map.items(), reverse=True):
        if h >= threshold:
            return formats

    # 默认格式
    return ["bv*+ba/best"]


def _download_with_ytdlp(
    url: str, cookie: str, out_dir, height_limit: int, size_limit_mb: int
) -> Tuple[str, str]:
    try:
        from yt_dlp import YoutubeDL  # type: ignore
        from yt_dlp.utils import DownloadError  # type: ignore
    except Exception:
        raise ImportError("yt_dlp not installed")

    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    final_url = _expand_short_url(url)

    # 构建 Cookie 文件
    cookiefile = _ensure_cookiefile(cookie)
    candidates = _build_format_candidates(height_limit, size_limit_mb)
    last_err: Optional[Exception] = None

    for i, fmt in enumerate(candidates):
        headers = _build_browser_like_headers()
        ydl_opts = {
            "format": fmt,
            "outtmpl": str(out_dir / "%(title).80s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "merge_output_format": "mp4",
            "quiet": False,
            "no_warnings": False,
            "http_headers": headers,
            "extractor_args": {
                "bili": {
                    "player_client": ["android", "web"],
                    "lang": ["zh-CN"],
                }
            },
        }

        if FFMPEG_DIR:
            ydl_opts["ffmpeg_location"] = FFMPEG_DIR

        # 设置 Cookie
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
            logger.info(f"bili2mp4: 使用 cookiefile: {cookiefile}")
        elif cookie:
            headers["Cookie"] = cookie
            logger.info("bili2mp4: 使用 Cookie header")

        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(final_url, download=True)
                title = info.get("title") or "B站视频"

                # 获取下载信息
                height = info.get("height", 0)
                logger.info(f"bili2mp4: 下载完成: {title} ({height}p)")

                # 定位文件
                final_path = _locate_final_file(ydl, info)
                if not final_path or not Path(final_path).exists():
                    raise RuntimeError("未找到已下载的视频文件，可能未安装 ffmpeg")

                # 按最大大小限制检查，如果超限则删除并尝试下一档清晰度
                if size_limit_mb and Path(final_path).exists():
                    size_mb = Path(final_path).stat().st_size / (1024 * 1024)
                    if size_mb > size_limit_mb:
                        logger.info(
                            f"bili2mp4: 下载完成但超过大小限制 "
                            f"{size_mb:.2f}MB > {size_limit_mb}MB，尝试更低清晰度"
                        )
                        try:
                            Path(final_path).unlink()
                        except Exception as e:
                            logger.debug(
                                f"bili2mp4: 删除超限文件失败 {final_path}: {e}"
                            )
                        last_err = RuntimeError("文件超过大小限制")
                        # 不返回，继续尝试下一种格式
                        continue

                # 未超大小限制，返回该文件
                return final_path, title

        except DownloadError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue

    # 所有候选格式都失败或都超限
    if last_err:
        raise RuntimeError(str(last_err))
    raise RuntimeError("无法下载该视频")


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
            files = [dirpath / f for f in os.listdir(dirpath) if vid in f]
            if files:
                files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return str(files[0])
        except Exception:
            pass
    return None


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
            DOWNLOAD_DIR,  # 传递 Path 对象
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
        return

    # 发送视频
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
    global bilibili_cookie, max_height, max_filesize_mb, max_duration_sec

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

    return False


# =========================
# 事件监听
# =========================

# 群消息监听
group_listener = on_message(priority=100, block=False)


@group_listener.handle()
async def handle_group(bot: Bot, event: Event):
    try:
        _init_plugin()

        if not isinstance(event, GroupMessageEvent):
            return

        group_id = int(event.group_id)
        if group_id not in enabled_groups:
            return

        urls = _extract_bili_urls_from_event(event)
        if not urls:
            logger.debug(f"bili2mp4: 群{group_id} 未在该消息中发现B站链接")
            return

        url = urls[0]
        key = f"{group_id}|{url}"
        if key in _processing:
            logger.debug(f"bili2mp4: 已在处理中，忽略重复: {key}")
            return
        _processing.add(key)
        logger.info(f"bili2mp4: 检测到B站链接")

        async def work():
            try:
                await _download_and_send(bot, group_id, url)
            except Exception as e:
                logger.warning(f"bili2mp4: 处理失败: {e}")
            finally:
                _processing.discard(key)

        asyncio.create_task(work())
    except Exception as e:
        logger.warning(f"bili2mp4: 群消息处理异常: {e}")


# 私聊控制
ctrl_listener = on_message(priority=50, block=False)


@ctrl_listener.handle()
async def handle_private(bot: Bot, event: Event):
    _init_plugin()

    if not isinstance(event, PrivateMessageEvent):
        return

    try:
        uid = int(event.user_id)
    except Exception:
        return
    if uid not in bili_super_admins:
        return

    text = (event.get_message() or Message()).extract_plain_text().strip()
    if not text:
        return

    try:
        # 帮助
        if text == "fhelp":
            await bot.send(event, Message(_get_help_message()))
            return

        if await _handle_group_command(bot, event, text):
            return

        if await _handle_config_command(bot, event, text):
            return
    except Exception as e:
        logger.warning(f"bili2mp4: 处理管理员命令失败: {e}")

    return
