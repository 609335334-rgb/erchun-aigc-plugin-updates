# -*- coding: utf-8 -*-
"""贰春开放 API v2 视频生成插件。"""

import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from plugin_utils import load_plugin_config


_PLUGIN_FILE = __file__
_PLUGIN_ID = "video_plugin_erchun_aigc"
_PLUGIN_VERSION = "1.1.0"
_DEFAULT_BASE_URL = "https://api.erchun.youkou.cc"
_DEFAULT_UPDATE_MANIFEST_URL = (
    "https://cdn.jsdelivr.net/gh/609335334-rgb/"
    "erchun-aigc-plugin-updates@main/manifest.json"
)
_DEFAULT_UPDATE_MANIFEST_URLS = (
    _DEFAULT_UPDATE_MANIFEST_URL,
    "https://raw.githubusercontent.com/609335334-rgb/"
    "erchun-aigc-plugin-updates/main/manifest.json",
)
_CATALOG_PATH = "/v2/catalog"
_ACCOUNT_PATH = "/v2/account"
_UPLOAD_PATH = "/v2/media/uploads"
_VIDEO_PATH = "/v2/videos"

_default_params = {
    "api_key": "",
    "base_url": _DEFAULT_BASE_URL,
    "model": "",
    "aspect_ratio": "9:16",
    "resolution": "480P",
    "duration": 4,
    "timeout": 120,
    "poll_interval": 5,
    "max_wait_minutes": 30,
    "update_manifest_url": _DEFAULT_UPDATE_MANIFEST_URL,
}


def _parse_version(version_text):
    parts = []
    for segment in str(version_text or "").strip().split("."):
        match = re.match(r"^(\d+)", segment)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts or [0])


def _is_newer_version(remote_version, local_version):
    remote = list(_parse_version(remote_version))
    local = list(_parse_version(local_version))
    length = max(len(remote), len(local))
    remote.extend([0] * (length - len(remote)))
    local.extend([0] * (length - len(local)))
    return tuple(remote) > tuple(local)


def _compute_sha256(file_path):
    hasher = hashlib.sha256()
    with open(file_path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def _read_package_version(main_path):
    try:
        text = Path(main_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    match = re.search(r'_PLUGIN_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1).strip() if match else ""


def _check_update_available(manifest_url=None):
    if manifest_url is None:
        manifest_url = get_params().get("update_manifest_url") or _DEFAULT_UPDATE_MANIFEST_URL
    configured_url = str(manifest_url or _DEFAULT_UPDATE_MANIFEST_URL).strip()
    if not configured_url.startswith(("http://", "https://")):
        return {"ok": False, "error": "更新清单地址必须以 http:// 或 https:// 开头"}
    candidates = []
    for candidate in (configured_url, *_DEFAULT_UPDATE_MANIFEST_URLS):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    errors = []
    best_manifest_url = ""
    best_remote = None
    for candidate in candidates:
        try:
            response = requests.get(candidate, timeout=30)
            if response.status_code != 200:
                raise RuntimeError("HTTP %s" % response.status_code)
            manifest = response.json()
            plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
            if not isinstance(plugins, list):
                raise RuntimeError("manifest.json 格式错误：缺少 plugins")
            remote = next(
                (
                    item for item in plugins
                    if isinstance(item, dict) and item.get("plugin_id") == _PLUGIN_ID
                ),
                None,
            )
            if not remote:
                raise RuntimeError("清单中未找到插件：%s" % _PLUGIN_ID)
            remote_version = str(remote.get("version") or "").strip()
            if not remote_version:
                raise RuntimeError("更新项缺少 version")
        except Exception as exc:
            errors.append("%s: %s" % (candidate, exc))
            continue
        if best_remote is None or _is_newer_version(remote_version, best_remote.get("version")):
            best_manifest_url = candidate
            best_remote = remote
    if best_remote is None:
        return {"ok": False, "error": "拉取更新清单失败：" + " | ".join(errors)}
    remote_version = str(best_remote.get("version") or "").strip()
    if not _is_newer_version(remote_version, _PLUGIN_VERSION):
        return {
            "ok": True,
            "has_update": False,
            "message": "当前已是最新版（本地 %s，远端 %s）" % (_PLUGIN_VERSION, remote_version),
            "manifest_url": best_manifest_url,
        }
    return {
        "ok": True,
        "has_update": True,
        "local_version": _PLUGIN_VERSION,
        "remote_version": remote_version,
        "changelog": str(best_remote.get("changelog") or "无"),
        "download_url": str(best_remote.get("download_url") or "").strip(),
        "sha256": str(best_remote.get("sha256") or "").strip().lower(),
        "manifest_url": best_manifest_url,
    }


def _safe_extract_zip(archive, destination):
    root = Path(destination).resolve()
    for member in archive.infolist():
        target = (root / member.filename).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError("更新包包含非法路径")
    archive.extractall(root)


def _find_update_root(package_path, work_dir):
    package_path = Path(package_path)
    if package_path.suffix.lower() == ".py":
        return package_path.parent, package_path
    if package_path.suffix.lower() != ".zip":
        raise RuntimeError("更新包仅支持 .py 或 .zip")
    extract_dir = Path(work_dir) / "extract"
    extract_dir.mkdir()
    with zipfile.ZipFile(package_path, "r") as archive:
        _safe_extract_zip(archive, extract_dir)
    candidates = [extract_dir, extract_dir / _PLUGIN_ID]
    candidates.extend(item.parent for item in extract_dir.rglob("main.py"))
    for candidate in candidates:
        if (candidate / "main.py").is_file():
            return candidate, candidate / "main.py"
    raise RuntimeError("更新包中未找到 main.py")


def _execute_update(download_url, expected_sha256="", expected_version=""):
    if not str(download_url or "").startswith(("http://", "https://")):
        return {"ok": False, "error": "download_url 必须是 http(s) 地址"}
    work_dir = Path(tempfile.mkdtemp(prefix=_PLUGIN_ID + "_update_"))
    try:
        filename = Path(urlparse(download_url).path).name or "plugin_update.zip"
        package_path = work_dir / filename
        with requests.get(download_url, timeout=120, stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError("下载失败：HTTP %s" % response.status_code)
            with open(package_path, "wb") as file_obj:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        file_obj.write(chunk)
        if expected_sha256 and _compute_sha256(package_path) != str(expected_sha256).lower():
            raise RuntimeError("SHA-256 校验失败，已取消安装")

        source_dir, source_main = _find_update_root(package_path, work_dir)
        package_version = _read_package_version(source_main)
        if not package_version:
            raise RuntimeError("更新包内 main.py 缺少版本号，已取消安装")
        if expected_version and package_version != expected_version:
            raise RuntimeError(
                "更新包版本与更新清单不一致（清单 %s / 包内 %s），已取消安装"
                % (expected_version, package_version)
            )
        if not _is_newer_version(package_version, _PLUGIN_VERSION):
            raise RuntimeError(
                "更新包版本异常（包内 %s 不高于当前 %s），已取消安装"
                % (package_version, _PLUGIN_VERSION)
            )

        target_dir = Path(_PLUGIN_FILE).parent
        backup = target_dir / ("main.py.bak.%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        shutil.copy2(_PLUGIN_FILE, backup)
        restore_dir = work_dir / "restore"
        restore_dir.mkdir()
        restore_items = []
        try:
            shutil.copy2(source_main, _PLUGIN_FILE)
            if package_path.suffix.lower() == ".zip":
                for item in source_dir.iterdir():
                    if item.name == "main.py":
                        continue
                    destination = target_dir / item.name
                    if destination.exists():
                        saved = restore_dir / item.name
                        if destination.is_dir():
                            shutil.copytree(destination, saved)
                        else:
                            shutil.copy2(destination, saved)
                        restore_items.append((destination, saved, destination.is_dir()))
                    if item.is_dir():
                        if destination.exists():
                            shutil.rmtree(destination)
                        shutil.copytree(item, destination)
                    else:
                        shutil.copy2(item, destination)
        except Exception:
            shutil.copy2(backup, _PLUGIN_FILE)
            for destination, saved, was_dir in reversed(restore_items):
                try:
                    if destination.exists():
                        if destination.is_dir():
                            shutil.rmtree(destination)
                        else:
                            destination.unlink()
                    if was_dir:
                        shutil.copytree(saved, destination)
                    else:
                        shutil.copy2(saved, destination)
                except Exception as restore_error:
                    print("[WARN] 回滚 %s 失败: %s" % (destination.name, restore_error))
            raise
        return {
            "ok": True,
            "message": "插件已更新到 %s，已备份为 %s。请重启字字动画后生效。"
            % (package_version, backup.name),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _normalize_base_url(value):
    base = str(value or _DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/v2"):
        base = base[:-3]
    if not base.startswith(("http://", "https://")):
        raise ValueError("API 地址必须以 http:// 或 https:// 开头")
    return base.rstrip("/")


def _api_url(base_url, path):
    return _normalize_base_url(base_url) + path


def get_params():
    params = _default_params.copy()
    params.update(load_plugin_config(_PLUGIN_FILE))
    params["base_url"] = _normalize_base_url(params.get("base_url"))
    return params


def get_info():
    return {
        "name": "贰春·生视频",
        "description": "通过贰春开放 API v2 生成视频，支持动态模型目录与私有参考素材。",
        "version": _PLUGIN_VERSION,
        "author": "erchun-aigc",
        "images_per_batch": 1,
    }


def _auth_headers(api_key):
    return {"Authorization": "Bearer " + api_key}


def _response_error(response):
    request_id = response.headers.get("X-Request-Id", "")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = error.get("message") or payload.get("message") or response.text[:300] or "请求失败"
    code = error.get("code") or "http_" + str(response.status_code)
    suffix = ""
    if error.get("param"):
        suffix += "（参数：%s）" % error["param"]
    if request_id or payload.get("request_id"):
        suffix += " [request_id: %s]" % (request_id or payload.get("request_id"))
    return "%s: %s%s" % (code, message, suffix)


def _get_catalog(params):
    api_key = str(params.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("请先在插件设置中填写贰春 API Token")
    response = requests.get(
        _api_url(params["base_url"], _CATALOG_PATH),
        headers=_auth_headers(api_key), timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(_response_error(response))
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("模型目录响应格式无效")
    return payload


def _get_account(params):
    api_key = str(params.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("请先在插件设置中填写贰春 API Token")
    response = requests.get(
        _api_url(params["base_url"], _ACCOUNT_PATH),
        headers=_auth_headers(api_key), timeout=30,
    )
    if response.status_code != 200:
        message = _response_error(response)
        if response.status_code == 403:
            message += "；请在贰春平台为当前 Token 显式添加 account:read 权限"
        raise RuntimeError(message)
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("账户响应格式无效")
    balances = payload.get("balances")
    quota = payload.get("quota")
    if not isinstance(balances, dict) or not isinstance(quota, dict):
        raise RuntimeError("账户响应缺少 balances 或 quota")
    return payload


def _video_models(catalog):
    return [
        model for model in catalog.get("data", [])
        if isinstance(model, dict)
        and model.get("output_type") == "video"
        and (model.get("result_delivery") or {}).get("status") == "supported"
    ]


def _select_model(catalog, requested):
    requested = str(requested or "").strip()
    if not requested:
        raise ValueError("请先刷新并选择一个支持直连结果交付的视频模型")
    models = _video_models(catalog)
    by_id = [item for item in models if item.get("id") == requested]
    if by_id:
        return by_id[0]
    by_name = [item for item in models if item.get("name") == requested]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        choices = ", ".join(item.get("id", "") for item in by_name)
        raise ValueError("模型名称重名，请改用稳定 ID：%s" % choices)
    raise ValueError("当前 Token 的视频目录中没有该模型，或它不支持 v2 直连结果交付")


def _options(model, key):
    value = model.get(key)
    return [str(item) for item in value] if isinstance(value, list) else []


def _pick_value(params, model, config_key, catalog_key, default_key):
    wanted = str(params.get(config_key) or "").strip()
    values = _options(model, catalog_key)
    if wanted:
        if values and wanted not in values:
            raise ValueError("%s 不在当前模型允许值中：%s" % (config_key, ", ".join(values)))
        return wanted
    default = model.get(default_key)
    if default not in (None, ""):
        return str(default)
    return values[0] if values else None


def _pick_duration(params, model, scene_duration):
    raw = scene_duration if scene_duration not in (None, "") else params.get("duration")
    duration = None
    if raw not in (None, ""):
        try:
            duration = int(float(raw))
        except (TypeError, ValueError):
            raise ValueError("时长必须是整数秒")
    spec = model.get("durations")
    if not isinstance(spec, dict):
        return duration
    if duration is None:
        duration = spec.get("default")
        if duration is None:
            if spec.get("mode") == "enum":
                options = spec.get("options") or []
                duration = options[0] if options else None
            elif spec.get("mode") == "range":
                duration = spec.get("min")
    if duration is None:
        return None
    if spec.get("mode") == "enum" and duration not in (spec.get("options") or []):
        raise ValueError("时长必须是模型支持的档位：%s" % ", ".join(map(str, spec.get("options") or [])))
    if spec.get("mode") == "range":
        minimum, maximum, step = spec.get("min"), spec.get("max"), spec.get("step")
        if not isinstance(minimum, int) or not isinstance(maximum, int) or not minimum <= duration <= maximum:
            raise ValueError("时长必须在 %s 到 %s 秒之间" % (minimum, maximum))
        if step and (duration - minimum) % step:
            raise ValueError("时长必须按 %s 秒步长设置" % step)
    return duration


def _estimate_billing(model, resolution, duration):
    billing = model.get("billing") or {}
    if not isinstance(billing, dict):
        return None
    billing_type = str(billing.get("type") or billing.get("mode") or "").strip()
    if billing_type != "video_per_second":
        return None
    try:
        seconds = float(duration)
        base = float(billing.get("base") or 0)
    except (TypeError, ValueError):
        return None
    rates = billing.get("per_second_by_resolution") or {}
    rate = None
    if isinstance(rates, dict) and resolution not in (None, ""):
        wanted = str(resolution).lower()
        for key, value in rates.items():
            if str(key).lower() == wanted:
                rate = value
                break
    if rate in (None, ""):
        rate = billing.get("per_second_default")
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return None
    return {
        "type": billing_type,
        "base": base,
        "rate": rate,
        "seconds": seconds,
        "estimated": base + rate * seconds,
    }


def _required_parameter(model, name):
    data = (model.get("parameter_status") or {}).get(name) or {}
    return data.get("status") == "supported" and data.get("required") is True


def _value_or_error(value, label, model, parameter):
    if value is None and _required_parameter(model, parameter):
        raise ValueError("当前模型要求明确设置%s，请刷新目录后选择允许值" % label)
    return value


def _as_paths(value):
    if not value:
        return []
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (str, Path)):
        value = [value]
    result = []
    for item in value if isinstance(value, (list, tuple, set)) else []:
        if isinstance(item, dict):
            item = item.get("path") or item.get("file_path") or item.get("url")
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}


def _media_entries(value):
    """Preserve host media metadata while normalizing containers to entries."""
    if not value:
        return []
    if isinstance(value, dict):
        if any(key in value for key in ("path", "file_path", "url")):
            return [value]
        return list(value.values())
    if isinstance(value, (str, Path)):
        return [value]
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _entry_path_and_kind(entry, default_kind=None):
    declared = ""
    if isinstance(entry, dict):
        path = entry.get("path") or entry.get("file_path") or entry.get("url")
        declared = str(
            entry.get("media_type") or entry.get("mediaType")
            or entry.get("type") or entry.get("kind") or ""
        ).lower()
    else:
        path = entry
    text = str(path or "").strip()
    if not text:
        return "", None
    for kind in ("audio", "video", "image"):
        if kind in declared:
            return text, kind
    suffix = Path(text.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    if suffix in _AUDIO_EXTENSIONS:
        return text, "audio"
    if suffix in _VIDEO_EXTENSIONS:
        return text, "video"
    return text, default_kind or "image"


def _context_media(context):
    images, videos, audios = [], [], []

    def collect(value, default_kind=None):
        for entry in _media_entries(value):
            path, kind = _entry_path_and_kind(entry, default_kind)
            if not path:
                continue
            target = {"image": images, "video": videos, "audio": audios}[kind]
            if path not in target:
                target.append(path)

    # 宿主可能把音频混放在 reference_images/reference_items，必须按实际类型归类。
    collect(context.get("reference_images"), "image")
    collect(context.get("reference_items"), "image")
    collect(context.get("reference_videos"), "video")
    collect(context.get("reference_audios"), "audio")
    collect(context.get("audio_references"), "audio")
    for key in ("first_frame_path", "last_frame_path", "first_frame", "last_frame"):
        collect(context.get(key), "image")
    print("[素材分类] 图片 %d，视频 %d，音频 %d" % (len(images), len(videos), len(audios)))
    return _dedupe(images), _dedupe(videos), _dedupe(audios)


def _dedupe(values):
    result = []
    for item in values:
        if item not in result:
            result.append(item)
    return result


def _mime_for(path):
    suffix = Path(str(path).split("?", 1)[0]).suffix.lower()
    extension_mimes = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
    }
    if suffix in extension_mimes:
        return extension_mimes[suffix]
    mime, _ = mimetypes.guess_type(path)
    return (mime or "application/octet-stream").lower()


def _mime_alias(mime):
    """Return a canonical MIME for equivalent spellings used by Windows/catalogs."""
    value = str(mime or "").strip().lower()
    return {
        "audio/mp3": "audio/mpeg",
        "audio/x-mp3": "audio/mpeg",
        "audio/mpeg3": "audio/mpeg",
        "audio/wave": "audio/wav",
        "audio/x-wav": "audio/wav",
        "video/quicktime; codecs=": "video/quicktime",
    }.get(value, value)


def _mime_values(value):
    """Normalize catalog MIME values whether returned as arrays or delimited text."""
    if isinstance(value, str):
        value = value.replace(";", ",").replace("/ ", "/")
        values = re.split(r"[,|\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return {_mime_alias(item) for item in values if str(item).strip()}


def _check_media(paths, kind, capability, limits):
    if not paths:
        return
    if capability.get("status") != "supported":
        raise ValueError("当前模型不支持%s参考素材" % {"image": "图片", "video": "视频", "audio": "音频"}[kind])
    maximum = capability.get("max_count")
    if maximum is None:
        raise ValueError("当前模型的%s数量限制尚未同步，不能猜测后继续提交" % kind)
    if len(paths) > maximum:
        raise ValueError("当前模型最多允许 %s 个%s参考素材" % (maximum, kind))
    for path in paths:
        if path.startswith(("http://", "https://", "data:")):
            raise ValueError("贰春 v2 参考素材必须是本地文件，插件会先私有上传，不能直接传 URL 或 Base64")
        if not os.path.isfile(path):
            raise ValueError("参考素材不存在：%s" % path)
        # 按贰春文档直接提交原始 file part；格式、大小、时长由服务端最终判定。


def _validate_references(model, limits, images, videos, audios):
    capabilities = model.get("capabilities") or {}
    if capabilities.get("sync_status") != "synced":
        raise ValueError("当前模型的参考素材能力尚未同步，不能猜测素材参数")
    image_cap = capabilities.get("image") or {}
    video_cap = capabilities.get("video") or {}
    audio_cap = capabilities.get("audio") or {}
    _check_media(images, "image", image_cap, limits)
    _check_media(videos, "video", video_cap, limits)
    _check_media(audios, "audio", audio_cap, limits)
    mode = image_cap.get("mode")
    if mode == "none" and images:
        raise ValueError("当前模型不接受图片参考素材")
    if mode == "first" and len(images) != 1:
        raise ValueError("当前模型要求恰好 1 张首帧图片")
    if mode == "first_last" and len(images) != 2:
        raise ValueError("当前模型要求 2 张图片，顺序为首帧、尾帧")
    if model.get("requires_image") is True and not images:
        raise ValueError("当前模型必须提供参考图片")
    mix = capabilities.get("mix") or {}
    populated = sum(bool(value) for value in (images, videos, audios))
    if populated > 1:
        if mix.get("status") != "supported" or mix.get("allow_mixed") is not True:
            raise ValueError("当前模型不支持混合图片、视频或音频参考素材")
    total = len(images) + len(videos) + len(audios)
    if mix.get("max_total") is not None and total > mix["max_total"]:
        raise ValueError("参考素材总数超过当前模型限制")
    if audios and (capabilities.get("constraints") or {}).get("audio_requires_image_or_video") is True and not (images or videos):
        raise ValueError("当前模型的音频参考必须同时提供图片或视频")


def _upload_file(base_url, api_key, path, expected_kind, timeout):
    retry_after = 0
    for attempt in range(3):
        if retry_after:
            time.sleep(retry_after)
        with open(path, "rb") as handle:
            response = requests.post(
                _api_url(base_url, _UPLOAD_PATH), headers=_auth_headers(api_key),
                files={"file": (os.path.basename(path), handle, _mime_for(path))}, timeout=timeout,
        )
        if response.status_code == 201:
            raw = response.json()
            data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
            if not isinstance(data, dict):
                raise RuntimeError("上传响应不是对象")
            media_id = data.get("media_id") or data.get("mediaId") or data.get("id")
            if not media_id:
                raise RuntimeError("上传响应缺少 media_id")
            # 上传接口已成功接收素材。按 v2 创建合同只提交 media_id，
            # 不以响应中可选的类型展示字段阻断后续任务创建。
            return {"media_id": str(media_id)}
        retry_after_header = response.headers.get("Retry-After", "")
        if response.status_code == 429 and retry_after_header.isdigit() and int(retry_after_header) > 0 and attempt < 2:
            retry_after = int(retry_after_header)
            continue
        raise RuntimeError(_response_error(response))
    raise RuntimeError("素材上传重试次数已用尽")


def _submit_video(base_url, api_key, payload, timeout):
    headers = _auth_headers(api_key)
    headers.update({"Content-Type": "application/json", "Idempotency-Key": "zz-erchun-" + uuid.uuid4().hex})
    retry_after = 0
    for attempt in range(3):
        if retry_after:
            time.sleep(retry_after)
        response = requests.post(_api_url(base_url, _VIDEO_PATH), headers=headers, json=payload, timeout=timeout)
        if response.status_code == 202:
            data = response.json()
            if not data.get("id") or not data.get("status_url"):
                raise RuntimeError("创建响应缺少任务 ID 或状态地址")
            return data
        retryable = False
        try:
            retryable = bool((response.json().get("error") or {}).get("retryable"))
        except ValueError:
            pass
        retry_after_header = response.headers.get("Retry-After", "")
        if retryable and attempt < 2:
            retry_after = int(retry_after_header) if retry_after_header.isdigit() and int(retry_after_header) > 0 else 2 ** (attempt + 1)
            continue
        raise RuntimeError(_response_error(response))
    raise RuntimeError("创建任务重试次数已用尽")


def _download_content(url, timeout):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError("上游结果地址不是 http(s) URL")
    # 结果下载是独立请求，绝不附带平台 Bearer 或 Cookie。
    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    suffix = Path(parsed.path).suffix or ".mp4"
    target = os.path.join(tempfile.mkdtemp(prefix="erchun_video_"), "result" + suffix)
    with open(target, "wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    if os.path.getsize(target) == 0:
        raise RuntimeError("上游结果下载为空")
    return target


def _poll_task(base_url, api_key, status_url, timeout, interval, max_wait_seconds):
    if not re.fullmatch(r"/v2/tasks/[1-9][0-9]*", str(status_url or "")):
        raise RuntimeError("创建响应返回了不合法的状态地址")
    headers = _auth_headers(api_key)
    etag = None
    deadline = time.monotonic() + max_wait_seconds
    last_status = ""
    while time.monotonic() < deadline:
        request_headers = dict(headers)
        if etag:
            request_headers["If-None-Match"] = etag
        response = requests.get(_api_url(base_url, status_url), headers=request_headers, timeout=timeout)
        if response.status_code == 304:
            time.sleep(interval)
            continue
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit() and int(retry_after) > 0:
                time.sleep(int(retry_after))
                continue
        if response.status_code != 200:
            raise RuntimeError(_response_error(response))
        etag = response.headers.get("ETag") or etag
        task = response.json()
        if task.get("requires_attention") is True:
            raise RuntimeError("任务需要人工处理：%s（任务 ID: %s）" % (task.get("attention_message") or "请联系贰春平台", task.get("id")))
        status = task.get("status")
        if status != last_status:
            print("贰春任务状态：%s（任务 ID: %s）" % (status, task.get("id")))
            last_status = status
        if status == "completed":
            if not task.get("content_url"):
                raise RuntimeError("任务已完成但未返回上游结果地址")
            return task["content_url"]
        if status == "failed":
            error = task.get("error") or {}
            raise RuntimeError("任务失败：%s" % (error.get("message") or error.get("code") or "未知原因"))
        time.sleep(interval)
    raise RuntimeError("等待任务超时；请保留任务 ID 并在平台继续查询")


def _scene_duration(context):
    for key in ("duration", "duration_seconds", "scene_duration"):
        if context.get(key) not in (None, ""):
            return context[key]
    return None


def generate(context):
    params = get_params()
    api_key = str(params.get("api_key") or "").strip()
    prompt = str(context.get("prompt") or context.get("text") or "").strip()
    if not prompt:
        raise ValueError("缺少视频提示词")
    catalog = _get_catalog(params)
    limits = catalog.get("limits") or {}
    max_prompt = (limits.get("requests") or {}).get("prompt_max_utf8_bytes")
    if isinstance(max_prompt, int) and len(prompt.encode("utf-8")) > max_prompt:
        raise ValueError("提示词超过当前目录规定的 UTF-8 字节限制")
    model = _select_model(catalog, params.get("model"))
    images, videos, audios = _context_media(context)
    _validate_references(model, limits, images, videos, audios)
    timeout = max(30, int(params.get("timeout") or 120))
    payload = {"model": model["id"], "prompt": prompt, "count": 1, "output_format": "mp4"}
    ratio = _value_or_error(_pick_value(params, model, "aspect_ratio", "aspect_ratios", "default_aspect_ratio"), "视频比例", model, "aspect_ratio")
    resolution = _value_or_error(_pick_value(params, model, "resolution", "resolutions", "default_resolution"), "清晰度", model, "resolution")
    duration = _value_or_error(_pick_duration(params, model, _scene_duration(context)), "时长", model, "duration_seconds")
    if ratio is not None:
        payload["aspect_ratio"] = ratio
    if resolution is not None:
        payload["resolution"] = resolution
    if duration is not None:
        payload["duration_seconds"] = duration
    estimate = _estimate_billing(model, resolution, duration)
    if estimate:
        print(
            "贰春任务预计消耗：%.6f 积分（基础 %.6f + %.6f/秒 × %s 秒）"
            % (estimate["estimated"], estimate["base"], estimate["rate"], duration)
        )
    if images:
        payload["input_images"] = [_upload_file(params["base_url"], api_key, item, "image", timeout) for item in images]
    if videos:
        payload["input_videos"] = [_upload_file(params["base_url"], api_key, item, "video", timeout) for item in videos]
    if audios:
        payload["input_audios"] = [_upload_file(params["base_url"], api_key, item, "audio", timeout) for item in audios]
    created = _submit_video(params["base_url"], api_key, payload, timeout)
    print("贰春任务已创建：%s" % created["id"])
    interval = max(1, int(params.get("poll_interval") or 5))
    max_wait = max(1, int(params.get("max_wait_minutes") or 30)) * 60
    url = _poll_task(params["base_url"], api_key, created["status_url"], timeout, interval, max_wait)
    return [_download_content(url, timeout)]


def handle_action(action, data=None):
    if action == "check_update":
        data = data or {}
        return _check_update_available(data.get("manifest_url"))
    if action == "do_update":
        data = data or {}
        return _execute_update(
            data.get("download_url", ""),
            data.get("sha256", ""),
            data.get("remote_version", ""),
        )
    if action == "check_account":
        try:
            return {"ok": True, "account": _get_account(get_params())}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    if action != "list_models":
        return {"ok": False, "error": "未知动作: %s" % action}
    try:
        catalog = _get_catalog(get_params())
        models = _video_models(catalog)
        return {
            "ok": True,
            "models": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "display_name": item.get("display_name"),
                    "aspect_ratios": item.get("aspect_ratios"),
                    "default_aspect_ratio": item.get("default_aspect_ratio"),
                    "resolutions": item.get("resolutions"),
                    "default_resolution": item.get("default_resolution"),
                    "durations": item.get("durations"),
                    "parameter_status": item.get("parameter_status"),
                    "requires_image": item.get("requires_image"),
                    "capabilities": item.get("capabilities"),
                    "billing": item.get("billing"),
                }
                for item in models
            ],
            "count": len(models),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
