"""关键词自动回复插件 (违禁词同款指令化管理 + 审核 + 定时推送 + 多种输出)

功能:
  - 全局 / 分群关键词库 (规则), 命中后自动回复
  - 每条规则可选输出方式: 文本 / 原生markdown / 模板markdown / 图片 / 语音 / 视频 / ark
  - 指令化管理 (违禁词同款可点击「回车/输入框」按钮):
      关键词菜单 / 关键词开启 / 关键词关闭 (本群, 群主·管理)
      关键词全局开启 / 关键词全局关闭 (超管)
      新增关键词 <词> <内容> (群主·管理 -> 走审核; 超管直接生效)
      删除关键词 <词> (本群)
      新增全局关键词 / 删除全局关键词 (超管)
      关键词列表 (超管看全部, 群主·管理看本群)
  - 审核流程: 群主/管理新增关键词进待审核队列, 私信主动消息推给超管;
      超管发「通过 N [N...]」生效, 「拒绝 N」驳回, 「待审核」查看; 回复带点击按钮
  - 定时推送 (cron): 每条规则可配 cron 表达式 + 目标群, 调度器每分钟检查到点主动推送
  - Web 后台面板 (panel.html): 可视化管理规则 / 超管 / 待审核 / 各群开关 / cron / 媒体

依赖: ElainaBot v2 (core.plugin.* / core.message.*)
"""

import asyncio
import datetime
import json
import os
import re
import time
import urllib.parse
import uuid

from aiohttp import web

from core.base.logger import PLUGIN, get_logger, report_error
from core.message._http import MessageType
from core.message.keyboard import convert_simple_ark_data
from core.message.media import upload_media_bytes
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

import aiohttp

log = get_logger(PLUGIN, '关键词自动回复')

__plugin_meta__ = {
    'name': '关键词自动回复',
    'author': 'miaolik',
    'description': '全局/分群关键词自动回复, 指令化管理+审核+定时推送+多种输出, Web 后台',
    'version': '4.0.0',
    'license': 'MIT',
}

# ==================== 常量 ====================

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_PLUGIN_DIR, 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
_DATA_FILE = os.path.join(_DATA_DIR, 'keyword_reply.json')

# 默认超级管理员 (可在 Web / 指令修改)
_DEFAULT_SUPER_ADMINS = ['538389445D765D2988BFE31506C54799']

# 群主/管理添加本群关键词数量上限 (超管不受限)
_GROUP_LIMIT = 50

MATCH_MODES = ('exact', 'fuzzy', 'regex')
REPLY_TYPES = ('text', 'markdown', 'template_markdown', 'image', 'voice', 'video', 'ark')

_PAGE_KEY = 'keyword-autoreply'
_API = '/api/ext/keyword_reply'

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>'
    '</svg>'
)

_DEFAULT_HEADERS_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# ==================== 数据持久化 ====================
# 数据结构:
# {
#   "global_enabled": bool,                # 全局关键词总开关
#   "super_admins": [openid, ...],         # 超级管理员
#   "group_enabled": { gid: bool },        # 分群开关 (缺省视为关闭)
#   "rules": [ rule, ... ],                # 规则 (全局/分群)
#   "pending": [ pending, ... ],           # 待审核 (群主/管理提交)
#   "pending_seq": int                     # 待审核序号自增
# }
# rule = {
#   id, name, keyword, match_mode, scope("global"|"group"), group_id,
#   reply_type, reply, image_text, ark_type, markdown_template, keyboard_id,
#   priority, enabled,
#   cron_enabled, cron_expr, cron_group_ids,
#   created_at, updated_at
# }
_data: dict = {}


def _default_data() -> dict:
    return {
        'global_enabled': True,
        'forbid_group': False,
        'super_admins': list(_DEFAULT_SUPER_ADMINS),
        'group_enabled': {},
        'rules': [],
        'pending': [],
        'pending_seq': 0,
    }


def _sanitize_rule(r: dict) -> dict:
    if not isinstance(r, dict):
        r = {}
    d = {}
    d['id'] = str(r.get('id') or ('rule_' + uuid.uuid4().hex[:12]))
    d['name'] = str(r.get('name', '')).strip()[:60] or '未命名'
    d['keyword'] = str(r.get('keyword', ''))
    d['match_mode'] = r.get('match_mode') if r.get('match_mode') in MATCH_MODES else 'fuzzy'
    scope = r.get('scope')
    d['scope'] = scope if scope in ('global', 'group') else 'global'
    d['group_id'] = str(r.get('group_id') or '') if d['scope'] == 'group' else ''
    d['reply_type'] = r.get('reply_type') if r.get('reply_type') in REPLY_TYPES else 'text'
    d['reply'] = str(r.get('reply', ''))
    d['image_text'] = str(r.get('image_text', ''))
    try:
        d['ark_type'] = int(r.get('ark_type', 23))
    except (TypeError, ValueError):
        d['ark_type'] = 23
    d['ark_fields'] = r.get('ark_fields') if isinstance(r.get('ark_fields'), dict) else {}
    d['markdown_template'] = str(r.get('markdown_template', ''))
    d['keyboard_id'] = str(r.get('keyboard_id', ''))
    try:
        d['priority'] = int(r.get('priority', 0))
    except (TypeError, ValueError):
        d['priority'] = 0
    d['enabled'] = bool(r.get('enabled', True))
    d['cron_enabled'] = bool(r.get('cron_enabled', False))
    d['cron_expr'] = str(r.get('cron_expr', '')).strip()
    d['cron_group_ids'] = _normalize_group_ids(r.get('cron_group_ids', []))
    d['created_at'] = str(r.get('created_at') or datetime.datetime.now().isoformat(timespec='seconds'))
    d['updated_at'] = str(r.get('updated_at') or d['created_at'])
    return d


def _normalize(raw) -> dict:
    d = _default_data()
    if not isinstance(raw, dict):
        return d
    if 'global_enabled' in raw:
        d['global_enabled'] = bool(raw.get('global_enabled'))
    if 'forbid_group' in raw:
        d['forbid_group'] = bool(raw.get('forbid_group'))
    if isinstance(raw.get('super_admins'), list) and raw['super_admins']:
        d['super_admins'] = [str(a) for a in raw['super_admins'] if str(a).strip()]
    if isinstance(raw.get('group_enabled'), dict):
        for gid, val in raw['group_enabled'].items():
            d['group_enabled'][str(gid)] = bool(val)
    if isinstance(raw.get('rules'), list):
        d['rules'] = [_sanitize_rule(r) for r in raw['rules'] if isinstance(r, dict)]
    if isinstance(raw.get('pending'), list):
        d['pending'] = [p for p in raw['pending'] if isinstance(p, dict)]
    try:
        d['pending_seq'] = int(raw.get('pending_seq', 0))
    except (TypeError, ValueError):
        d['pending_seq'] = 0
    return d


def _load():
    global _data
    if not os.path.isfile(_DATA_FILE):
        _data = _default_data()
        _save()
        return
    try:
        with open(_DATA_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        raw = None
    _data = _normalize(raw)


def _save():
    with open(_DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(_data, f, ensure_ascii=False, indent=2)


# ==================== 工具函数 ====================


def _is_admin_or_owner(event) -> bool:
    return getattr(event, 'member_role', '') in ('admin', 'owner')


def _is_super_admin(event) -> bool:
    return (getattr(event, 'user_id', '') or '') in _data.get('super_admins', [])


def _is_full_access(event) -> bool:
    return getattr(event, 'event_type', '') == 'GROUP_MESSAGE_CREATE'


def _group_enabled(gid: str) -> bool:
    return bool(_data.get('group_enabled', {}).get(str(gid)))


def _global_enabled() -> bool:
    return bool(_data.get('global_enabled'))


def _forbid_group() -> bool:
    """禁止分群: 为 True 时分群无法自行开启关键词 (超管豁免)"""
    return bool(_data.get('forbid_group'))


def _normalize_group_ids(raw):
    if isinstance(raw, str):
        raw = re.split(r'[,\n，\s]+', raw)
    result = []
    for gid in raw or []:
        gid = str(gid).strip()
        if gid and gid not in result:
            result.append(gid)
    return result


def _btn(label: str, command: str, enter: bool = True) -> str:
    """生成可点击的「回车指令」按钮 (markdown inlinecmd)。

    enter=True: 点击直接发送; enter=False: 仅把指令填入输入框待补全参数。"""
    cmd = command.replace(' ', '+')
    e = 'true' if enter else 'false'
    return f'[{label}](mqqapi://aio/inlinecmd?command={cmd}&enter={e}&reply=false)'


# 管理指令前缀: 这些消息不触发关键词自动回复 (否则指令本身可能命中关键词)
_MGMT_PREFIXES = (
    '关键词菜单', '关键词列表',
    '关键词全局开启', '关键词全局关闭',
    '关键词开启', '关键词关闭',
    '一键开启分群', '一键关闭分群',
    '禁止分群开启', '禁止分群关闭',
    '新增全局关键词', '删除全局关键词',
    '新增关键词', '删除关键词',
    '待审核', '通过', '拒绝',
)


def _is_mgmt_command(content: str) -> bool:
    c = (content or '').strip()
    return any(c.startswith(p) for p in _MGMT_PREFIXES)


def _strip_cmd(content: str, prefix_re: str) -> str:
    text = re.sub(prefix_re, '', content or '', count=1)
    text = re.sub(r'<@!?[^>]+>', '', text)
    return text.strip()


def _rule_match(rule, content) -> bool:
    mode = rule.get('match_mode', 'fuzzy')
    kw = rule.get('keyword', '')
    if not kw:
        return False
    if mode == 'exact':
        return content == kw
    if mode == 'fuzzy':
        return kw in content
    if mode == 'regex':
        try:
            return re.search(kw, content, re.DOTALL) is not None
        except re.error:
            return False
    return False


def _find_rule(content: str, gid: str):
    """返回命中的最高优先级规则 (全局受全局开关, 本群受本群开关控制)。"""
    if not content:
        return None
    candidates = []
    for idx, r in enumerate(_data.get('rules', [])):
        if not r.get('enabled', True):
            continue
        if r.get('scope') == 'global':
            if not _global_enabled():
                continue
        else:
            if str(r.get('group_id')) != str(gid):
                continue
            if not _group_enabled(gid):
                continue
        candidates.append((idx, r))
    candidates.sort(key=lambda x: (-int(x[1].get('priority', 0)), x[0]))
    for _, r in candidates:
        if _rule_match(r, content):
            return r
    return None


def _group_rules(gid: str):
    return [r for r in _data.get('rules', []) if r.get('scope') == 'group' and str(r.get('group_id')) == str(gid)]


def _global_rules():
    return [r for r in _data.get('rules', []) if r.get('scope') == 'global']


# ==================== ARK / 模板参数解析 ====================


def _parse_params_from_template(template_str):
    if not template_str:
        return []
    params = []
    current = ''
    depth = 0
    array_items = []
    for char in str(template_str):
        if char == '(' and depth == 0:
            if current.strip():
                params.append(current.strip())
                current = ''
            depth = 1
            array_items = []
        elif char == ')' and depth == 1:
            if current.strip():
                array_items.append(current.strip())
                current = ''
            params.append(array_items)
            depth = 0
            array_items = []
        elif char == ',' and depth == 0:
            if current.strip():
                params.append(current.strip())
            current = ''
        elif char == ',' and depth == 1:
            if current.strip():
                array_items.append(current.strip())
            current = ''
        else:
            current += char
    if current.strip():
        params.append(current.strip())
    return params


def _parse_ark_params(data):
    all_params = _parse_params_from_template(str(data))
    normal_params = []
    list_items = []
    for param in all_params:
        if isinstance(param, list):
            list_items.append(param)
        else:
            normal_params.append(param)
    if list_items:
        return normal_params + [list_items]
    return normal_params


def _build_ark_simple_data(rule):
    """按结构化 ark_fields 构建 convert_simple_ark_data 所需的简化数据。

    23: (desc, prompt, [[条目desc, 条目link], ...])
    24: (desc, prompt, title, metadesc, img, link, subtitle)
    37: (prompt, metatitle, metasubtitle, metacover, metaurl)
    无 ark_fields 时返回 None (回退到旧的逗号参数解析)。"""
    f = rule.get('ark_fields') or {}
    if not isinstance(f, dict) or not f:
        return None
    try:
        t = int(rule.get('ark_type', 23))
    except (TypeError, ValueError):
        t = 23
    if t == 23:
        lst = []
        for it in (f.get('list') or []):
            if not isinstance(it, dict):
                continue
            desc = str(it.get('desc', '')).strip()
            link = str(it.get('link', '')).strip()
            if desc or link:
                lst.append([desc, link])
        return (str(f.get('desc', '')), str(f.get('prompt', '')), lst)
    if t == 24:
        return (
            str(f.get('desc', '')), str(f.get('prompt', '')), str(f.get('title', '')),
            str(f.get('metadesc', '')), str(f.get('img', '')), str(f.get('link', '')),
            str(f.get('subtitle', '')),
        )
    if t == 37:
        return (
            str(f.get('prompt', '')), str(f.get('metatitle', '')), str(f.get('metasubtitle', '')),
            str(f.get('metacover', '')), str(f.get('metaurl', '')),
        )
    return None


def _ark_send_data(rule):
    """优先使用结构化字段; 否则回退旧的逗号参数。"""
    sd = _build_ark_simple_data(rule)
    if sd is not None:
        return sd
    return tuple(_parse_ark_params(rule.get('reply', '')))


# ==================== 变量替换 ====================

_VAR_TOKEN_RE = re.compile(r'\{([A-Za-z0-9_]+)\}')


def _build_vars(rule, content, event):
    """构建可在回复内容里引用的变量字典。

    - 正则捕获: {0}=整段命中, {1}/{2}...=捕获组, {名字}=命名组
    - 上下文: {content} {user_id} {group_id} {nickname}
    - 时间: {date} {time} {datetime}
    """
    now = datetime.datetime.now()
    variables = {
        'content': content or '',
        'user_id': str(getattr(event, 'user_id', '') or ''),
        'group_id': str(getattr(event, 'group_id', '') or ''),
        'nickname': str(getattr(event, 'username', '') or getattr(event, 'nickname', '') or ''),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
    }
    if rule.get('match_mode') == 'regex' and content:
        try:
            mo = re.search(rule.get('keyword', ''), content, re.DOTALL)
        except re.error:
            mo = None
        if mo:
            variables['0'] = mo.group(0)
            for i, g in enumerate(mo.groups(), start=1):
                variables[str(i)] = g if g is not None else ''
            for k, v in (mo.groupdict() or {}).items():
                variables[k] = v if v is not None else ''
    return variables


def _apply_vars(text, variables):
    """替换已识别的 {变量}; 未识别的 {...} 原样保留 (不破坏 markdown/ark)。"""
    if not text:
        return text

    def _repl(mo):
        key = mo.group(1)
        return str(variables[key]) if key in variables else mo.group(0)

    return _VAR_TOKEN_RE.sub(_repl, str(text))


# ==================== 被动回复 (多种输出) ====================


async def _send_rule_reply(event, rule, content=''):
    reply_type = rule.get('reply_type', 'text')
    variables = _build_vars(rule, content, event)
    data = _apply_vars(rule.get('reply', ''), variables)
    image_text = _apply_vars(rule.get('image_text', ''), variables)
    try:
        if reply_type == 'text':
            await event.reply(str(data), msg_type=MessageType.MSG_TYPE_TEXT)
        elif reply_type == 'markdown':
            await event.reply(str(data), msg_type=MessageType.MSG_TYPE_MARKDOWN)
        elif reply_type == 'template_markdown':
            await _reply_template_markdown(event, rule, data)
        elif reply_type == 'image':
            await event.reply_image(str(data), image_text)
        elif reply_type == 'voice':
            await event.reply_voice(str(data))
        elif reply_type == 'video':
            await event.reply_video(str(data))
        elif reply_type == 'ark':
            await event.reply_ark(int(rule.get('ark_type', 23)), _ark_send_data(rule))
        else:
            await event.reply(str(data))
    except Exception as e:
        report_error(PLUGIN, '关键词自动回复', e, context={'rule': rule.get('name')})


async def _reply_template_markdown(event, rule, data):
    params = _parse_params_from_template(str(data))
    payload = {
        'msg_type': MessageType.MSG_TYPE_MARKDOWN,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'markdown': {
            'custom_template_id': str(rule.get('markdown_template', '1')),
            'params': [{'key': f'text{i + 1}', 'values': [str(p)]} for i, p in enumerate(params)],
        },
    }
    keyboard_id = (rule.get('keyboard_id') or '').strip()
    if keyboard_id:
        payload['keyboard'] = {'id': keyboard_id}
    from core.message._media_send import _set_msg_or_event_id
    _set_msg_or_event_id(payload, event)
    sender = event.sender
    endpoint = event.reply_endpoint
    if sender and endpoint:
        await sender.post_json(endpoint, payload)


# ==================== 主动推送 (定时, 多种输出) ====================

_MAX_MEDIA_DOWNLOAD = 100 * 1024 * 1024
_JSDELIVR_FALLBACK_HOSTS = ('cdn.jsdelivr.net', 'fastly.jsdelivr.net', 'gcore.jsdelivr.net')
_JSDELIVR_PATH_PREFIXES = ('/gh/', '/npm/', '/wp/', '/combine/', '/hg/')


def _media_url_candidates(url):
    candidates = [url]
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return candidates
    if (parsed.path or '').startswith(_JSDELIVR_PATH_PREFIXES):
        for host in _JSDELIVR_FALLBACK_HOSTS:
            if host == parsed.netloc:
                continue
            alt = urllib.parse.urlunparse(parsed._replace(netloc=host))
            if alt not in candidates:
                candidates.append(alt)
    return candidates


async def _download_media_bytes(url):
    headers = {'User-Agent': _DEFAULT_HEADERS_UA}
    timeout = aiohttp.ClientTimeout(total=60)
    candidates = _media_url_candidates(url)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for candidate in candidates:
            try:
                async with session.get(candidate, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning(f'下载媒体失败: HTTP {resp.status} ({candidate})')
                        continue
                    cl = int(resp.headers.get('Content-Length', 0) or 0)
                    if cl > _MAX_MEDIA_DOWNLOAD:
                        return None
                    body = await resp.read()
                    if len(body) > _MAX_MEDIA_DOWNLOAD:
                        return None
                    if candidate != url:
                        log.info(f'镜像回退下载成功: {candidate}')
                    return body
            except Exception as e:
                log.warning(f'下载媒体失败: {e} ({candidate})')
    return None


async def _upload_media_robust(sender, group_id, data, file_type):
    endpoint = f'/v2/groups/{group_id}/files'
    if isinstance(data, bytes):
        return await upload_media_bytes(sender, data, file_type, endpoint)
    media = str(data)
    file_info = await upload_media_bytes(sender, media, file_type, endpoint)
    if file_info:
        return file_info
    if media.startswith(('http://', 'https://')):
        body = await _download_media_bytes(media)
        if body:
            return await upload_media_bytes(sender, body, file_type, endpoint)
    return None


async def _push_media(sender, group_id, data, file_type, content=''):
    file_info = await _upload_media_robust(sender, group_id, data, file_type)
    if not file_info:
        log.warning(f'定时推送媒体上传失败 (group={group_id})')
        return
    payload = {
        'msg_type': MessageType.MSG_TYPE_MEDIA,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'content': content or '',
        'media': {'file_info': file_info},
    }
    await sender.post_json(f'/v2/groups/{group_id}/messages', payload)


async def _push_ark(sender, group_id, template_id, kv_data):
    if isinstance(kv_data, tuple | list) and template_id in (23, 24, 37):
        kv_data = convert_simple_ark_data(template_id, kv_data)
    payload = {
        'msg_type': MessageType.MSG_TYPE_ARK,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'content': '',
        'ark': {'template_id': template_id, 'kv': kv_data},
    }
    await sender.post_json(f'/v2/groups/{group_id}/messages', payload)


async def _push_template_markdown(sender, group_id, rule, data):
    params = _parse_params_from_template(str(data))
    payload = {
        'msg_type': MessageType.MSG_TYPE_MARKDOWN,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'markdown': {
            'custom_template_id': str(rule.get('markdown_template', '1')),
            'params': [{'key': f'text{i + 1}', 'values': [str(p)]} for i, p in enumerate(params)],
        },
    }
    keyboard_id = (rule.get('keyboard_id') or '').strip()
    if keyboard_id:
        payload['keyboard'] = {'id': keyboard_id}
    await sender.post_json(f'/v2/groups/{group_id}/messages', payload)


async def _push_rule_to_group(sender, group_id, rule):
    reply_type = rule.get('reply_type', 'text')
    # 主动推送无消息/捕获, 仅时间类与 {group_id} 等变量可用
    variables = _build_vars(rule, '', type('E', (), {'group_id': group_id})())
    data = _apply_vars(rule.get('reply', ''), variables)
    image_text = _apply_vars(rule.get('image_text', ''), variables)
    if reply_type == 'text':
        await sender.send_to_group(group_id, str(data), msg_type=MessageType.MSG_TYPE_TEXT)
    elif reply_type == 'markdown':
        await sender.send_to_group(group_id, str(data), msg_type=MessageType.MSG_TYPE_MARKDOWN)
    elif reply_type == 'template_markdown':
        await _push_template_markdown(sender, group_id, rule, data)
    elif reply_type == 'image':
        await _push_media(sender, group_id, data, 1, image_text)
    elif reply_type == 'voice':
        await _push_media(sender, group_id, data, 3)
    elif reply_type == 'video':
        await _push_media(sender, group_id, data, 2)
    elif reply_type == 'ark':
        await _push_ark(sender, group_id, int(rule.get('ark_type', 23)), _ark_send_data(rule))
    else:
        await sender.send_to_group(group_id, str(data))


def _get_sender(appid=''):
    try:
        from core.bot.manager import _bot_manager_ref
        if not _bot_manager_ref:
            return None
        bots = getattr(_bot_manager_ref, '_bots', None)
        if not bots:
            return None
        appid = (appid or '').strip()
        if appid and appid in bots:
            return bots[appid].sender
        return next(iter(bots.values())).sender
    except Exception as e:
        log.warning(f'获取 sender 失败: {e}')
        return None


# ==================== Cron 解析 ====================


def _parse_cron_field(field, lo, hi):
    values = set()
    for part in field.split(','):
        part = part.strip()
        if not part:
            continue
        step = 1
        rng = part
        if '/' in part:
            rng, step_str = part.split('/', 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f'步长无效: {part}')
        if rng == '*':
            start, end = lo, hi
        elif '-' in rng:
            a, b = rng.split('-', 1)
            start, end = int(a), int(b)
        else:
            start = end = int(rng)
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                values.add(v)
    return values


def _cron_match(expr, dt):
    if not expr or not isinstance(expr, str):
        return False
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    try:
        if dt.minute not in _parse_cron_field(minute, 0, 59):
            return False
        if dt.hour not in _parse_cron_field(hour, 0, 23):
            return False
        if dt.month not in _parse_cron_field(month, 1, 12):
            return False
        cron_dow = (dt.weekday() + 1) % 7
        dom_set = _parse_cron_field(dom, 1, 31)
        dow_set = _parse_cron_field(dow, 0, 7)
        if 7 in dow_set:
            dow_set.add(0)
        dom_restricted = dom.strip() != '*'
        dow_restricted = dow.strip() != '*'
        if dom_restricted and dow_restricted:
            return dt.day in dom_set or cron_dow in dow_set
        if dom_restricted:
            return dt.day in dom_set
        if dow_restricted:
            return cron_dow in dow_set
        return True
    except (ValueError, TypeError):
        return False


async def _run_due_tasks(now):
    for rule in list(_data.get('rules', [])):
        if not rule.get('cron_enabled'):
            continue
        if not rule.get('enabled', True):
            continue
        if not _cron_match(rule.get('cron_expr', ''), now):
            continue
        group_ids = _normalize_group_ids(rule.get('cron_group_ids', []))
        if not group_ids:
            continue
        sender = _get_sender()
        if not sender:
            log.warning('定时推送无可用机器人 (sender 为空), 跳过')
            continue
        for gid in group_ids:
            try:
                await _push_rule_to_group(sender, gid, rule)
            except Exception as e:
                report_error(PLUGIN, '关键词自动回复', e)
                log.warning(f'定时推送到群 {gid} 失败: {e}')


async def _scheduler_loop():
    try:
        while True:
            now = datetime.datetime.now()
            sleep_secs = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(max(sleep_secs, 1))
            await _run_due_tasks(datetime.datetime.now())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        report_error(PLUGIN, '关键词自动回复', e)


_SCHEDULER_TASK_NAME = 'keyword_reply_scheduler'
_scheduler_task = None


def _cancel_existing_schedulers():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for task in asyncio.all_tasks(loop):
        if task.get_name() == _SCHEDULER_TASK_NAME and not task.done():
            task.cancel()


# ==================== 全量消息分发 handler (自动回复) ====================


@handler(r'[\s\S]*', name='关键词自动回复', desc='根据关键词规则自动回复', priority=-9999, ignore_at_check=True)
async def autoreply_dispatcher(event, match):
    if getattr(event, 'is_bot', False):
        return
    content = getattr(event, 'content', '') or ''
    if not content.strip():
        return
    if _is_mgmt_command(content):
        return
    gid = str(getattr(event, 'group_id', '') or '')
    rule = _find_rule(content, gid)
    if not rule:
        return
    await _send_rule_reply(event, rule, content)


# ==================== 指令: 开关 ====================


@handler(r'^关键词开启$', name='关键词开启', desc='开启本群关键词自动回复', group_only=True, ignore_at_check=True)
async def enable_group(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    if _forbid_group() and not _is_super_admin(event):
        await event.reply('🔒 超管已开启「禁止分群」, 本群无法开启关键词。如需使用请联系超管。')
        return
    _data.setdefault('group_enabled', {})[str(event.group_id)] = True
    _save()
    nav = ' '.join([_btn('新增关键词', '新增关键词', enter=False), _btn('关键词关闭', '关键词关闭'), _btn('关键词菜单', '关键词菜单')])
    await event.reply('✅ 已开启本群关键词自动回复\n' + nav)


@handler(r'^关键词关闭$', name='关键词关闭', desc='关闭本群关键词自动回复', group_only=True, ignore_at_check=True)
async def disable_group(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    _data.setdefault('group_enabled', {})[str(event.group_id)] = False
    _save()
    nav = ' '.join([_btn('关键词开启', '关键词开启'), _btn('关键词菜单', '关键词菜单')])
    await event.reply('🛑 已关闭本群关键词自动回复\n' + nav)


@handler(r'^关键词全局开启$', name='关键词全局开启', desc='开启全局关键词 (对所有群生效, 超管)', ignore_at_check=True)
async def enable_global(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局开关')
        return
    _data['global_enabled'] = True
    _save()
    nav = ' '.join([_btn('新增全局关键词', '新增全局关键词', enter=False), _btn('关键词全局关闭', '关键词全局关闭'), _btn('关键词列表', '关键词列表')])
    await event.reply('✅ 已开启全局关键词 (对所有群生效)\n' + nav)


@handler(r'^关键词全局关闭$', name='关键词全局关闭', desc='关闭全局关键词 (超管)', ignore_at_check=True)
async def disable_global(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局开关')
        return
    _data['global_enabled'] = False
    _save()
    nav = ' '.join([_btn('关键词全局开启', '关键词全局开启'), _btn('关键词菜单', '关键词菜单')])
    await event.reply('🛑 已关闭全局关键词\n' + nav)


# ==================== 指令: 一键开关 / 禁止分群 (超管) ====================


def _bulk_set_groups(value: bool) -> int:
    """把所有已记录的分群开关统一设为 value, 返回受影响的群数。"""
    enabled = _data.setdefault('group_enabled', {})
    changed = 0
    for gid in list(enabled.keys()):
        if bool(enabled.get(gid)) != value:
            changed += 1
        enabled[gid] = value
    return changed


@handler(r'^一键开启分群$', name='一键开启分群', desc='开启所有已记录分群的关键词开关 (超管)', ignore_at_check=True)
async def bulk_enable_groups(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作')
        return
    n = _bulk_set_groups(True)
    _save()
    nav = ' '.join([_btn('一键关闭分群', '一键关闭分群'), _btn('关键词菜单', '关键词菜单')])
    await event.reply(f'✅ 已开启所有分群关键词开关 (共 {n} 个群变更)\n' + nav)


@handler(r'^一键关闭分群$', name='一键关闭分群', desc='关闭所有已记录分群的关键词开关 (超管)', ignore_at_check=True)
async def bulk_disable_groups(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作')
        return
    n = _bulk_set_groups(False)
    _save()
    nav = ' '.join([_btn('一键开启分群', '一键开启分群'), _btn('关键词菜单', '关键词菜单')])
    await event.reply(f'🛑 已关闭所有分群关键词开关 (共 {n} 个群变更)\n' + nav)


@handler(r'^禁止分群开启$', name='禁止分群开启', desc='开启禁止分群并关闭所有分群开关 (超管)', ignore_at_check=True)
async def enable_forbid_group(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作禁止分群')
        return
    _data['forbid_group'] = True
    closed = _bulk_set_groups(False)
    _save()
    nav = ' '.join([_btn('禁止分群关闭', '禁止分群关闭'), _btn('关键词菜单', '关键词菜单')])
    await event.reply(f'🔒 已开启禁止分群\n各群将无法自行开启/新增关键词, 已将 {closed} 个已开启的群全部关闭。\n(全局关键词与超管豁免不受影响)\n' + nav)


@handler(r'^禁止分群关闭$', name='禁止分群关闭', desc='解除禁止分群 (超管); 已关闭的群仍默认关闭', ignore_at_check=True)
async def disable_forbid_group(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作禁止分群')
        return
    _data['forbid_group'] = False
    _save()
    nav = ' '.join([_btn('禁止分群开启', '禁止分群开启'), _btn('关键词菜单', '关键词菜单')])
    await event.reply('✅ 已解除禁止分群\n各群可自行开启关键词; 之前已关闭的群仍保持关闭(需手动开启)。\n' + nav)


# ==================== 指令: 新增/删除 (本群, 带审核) ====================


def _split_kw_reply(text: str):
    """把「关键词 回复内容」拆成 (keyword, reply)。第一个空白前为关键词。"""
    text = text.strip()
    if not text:
        return '', ''
    m = re.match(r'^(\S+)\s+([\s\S]+)$', text)
    if m:
        return m.group(1), m.group(2).strip()
    return text, ''


@handler(r'^新增关键词', name='新增关键词', desc='新增关键词 <词> <回复内容> (群主/管理需审核, 超管直接生效)', group_only=True, ignore_at_check=True)
async def add_group_rule(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    if _forbid_group() and not _is_super_admin(event):
        await event.reply('🔒 超管已开启「禁止分群」, 本群无法新增关键词。如需使用请联系超管。')
        return
    body = _strip_cmd(event.content, r'^新增关键词\s*')
    keyword, reply = _split_kw_reply(body)
    if not keyword or not reply:
        await event.reply('用法: 新增关键词 <关键词> <回复内容>\n' + _btn('新增关键词', '新增关键词', enter=False))
        return
    gid = str(event.group_id)

    if _is_super_admin(event):
        rule = _sanitize_rule({
            'name': keyword, 'keyword': keyword, 'match_mode': 'fuzzy',
            'scope': 'group', 'group_id': gid, 'reply_type': 'text', 'reply': reply,
        })
        _data.setdefault('rules', []).append(rule)
        _save()
        nav = ' '.join([_btn('新增关键词', '新增关键词', enter=False), _btn('关键词列表', '关键词列表'), _btn('关键词菜单', '关键词菜单')])
        await event.reply(f'✅ 已新增本群关键词「{keyword}」(超管直接生效)\n' + nav)
        return

    # 群主/管理: 数量限制 + 审核
    existing = len(_group_rules(gid))
    pending_same = sum(1 for p in _data.get('pending', []) if str(p.get('group_id')) == gid)
    if existing + pending_same >= _GROUP_LIMIT:
        await event.reply(f'❌ 本群关键词已达上限 {_GROUP_LIMIT} 个 (含待审核)，无法再提交。')
        return

    _data['pending_seq'] = int(_data.get('pending_seq', 0)) + 1
    seq = _data['pending_seq']
    pending = {
        'id': 'pend_' + uuid.uuid4().hex[:12],
        'seq': seq,
        'group_id': gid,
        'group_openid': str(getattr(event, 'group_openid', '') or gid),
        'submitter': str(event.user_id),
        'submitter_name': str(getattr(event, 'username', '') or event.user_id),
        'keyword': keyword,
        'reply': reply,
        'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    _data.setdefault('pending', []).append(pending)
    _save()

    await _notify_super_admins_pending(event, pending)

    nav = ' '.join([_btn('新增关键词', '新增关键词', enter=False), _btn('关键词菜单', '关键词菜单')])
    await event.reply(f'📩 已提交审核 (序号 {seq})\n关键词「{keyword}」需超级管理员通过后才会生效。\n' + nav)


async def _notify_super_admins_pending(event, pending):
    """私信主动消息通知所有超管有新的待审核请求。"""
    sender = getattr(event, 'sender', None) or _get_sender()
    if not sender:
        log.warning('无可用 sender, 无法私信通知超管待审核')
        return
    btn = _btn(f'通过 {pending["seq"]}', f'通过 {pending["seq"]}')
    text = (
        '📩 关键词新增待审核\n'
        f'序号: {pending["seq"]}\n'
        f'群: {pending["group_id"]}\n'
        f'提交人: {pending["submitter_name"]} ({pending["submitter"]})\n'
        f'关键词: {pending["keyword"]}\n'
        f'回复内容: {pending["reply"]}\n'
        '———\n'
        '通过: 通过 序号 (可多个, 如 通过 1 2)\n'
        '拒绝: 拒绝 序号\n'
        + btn
    )
    for admin in _data.get('super_admins', []):
        try:
            await sender.send_to_user(admin, text)
        except Exception as e:
            log.warning(f'通知超管 {admin} 失败: {e}')


@handler(r'^删除关键词', name='删除关键词', desc='删除关键词 <词> (本群)', group_only=True, ignore_at_check=True)
async def del_group_rule(event, match):
    if not _is_full_access(event):
        await event.reply('仅限全量群使用')
        return
    if not (_is_admin_or_owner(event) or _is_super_admin(event)):
        await event.reply('仅管理员或群主可操作')
        return
    keywords = _strip_cmd(event.content, r'^删除关键词\s*').split()
    if not keywords:
        await event.reply('用法: 删除关键词 <词1> <词2> ...')
        return
    gid = str(event.group_id)
    before = len(_data.get('rules', []))
    _data['rules'] = [
        r for r in _data.get('rules', [])
        if not (r.get('scope') == 'group' and str(r.get('group_id')) == gid and r.get('keyword') in keywords)
    ]
    removed = before - len(_data['rules'])
    _save()
    nav = ' '.join([_btn('新增关键词', '新增关键词', enter=False), _btn('关键词菜单', '关键词菜单')])
    if removed:
        await event.reply(f'✅ 已删除本群关键词 {removed} 条: {" ".join(keywords)}\n' + nav)
    else:
        await event.reply('未找到要删除的本群关键词\n' + nav)


# ==================== 指令: 全局新增/删除 (超管) ====================


@handler(r'^新增全局关键词', name='新增全局关键词', desc='新增全局关键词 <词> <内容> (超管)', ignore_at_check=True)
async def add_global_rule(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局关键词')
        return
    body = _strip_cmd(event.content, r'^新增全局关键词\s*')
    keyword, reply = _split_kw_reply(body)
    if not keyword or not reply:
        await event.reply('用法: 新增全局关键词 <关键词> <回复内容>')
        return
    rule = _sanitize_rule({
        'name': keyword, 'keyword': keyword, 'match_mode': 'fuzzy',
        'scope': 'global', 'reply_type': 'text', 'reply': reply,
    })
    _data.setdefault('rules', []).append(rule)
    _save()
    nav = ' '.join([_btn('新增全局关键词', '新增全局关键词', enter=False), _btn('关键词列表', '关键词列表'), _btn('关键词菜单', '关键词菜单')])
    await event.reply(f'✅ 已新增全局关键词「{keyword}」\n' + nav)


@handler(r'^删除全局关键词', name='删除全局关键词', desc='删除全局关键词 <词> (超管)', ignore_at_check=True)
async def del_global_rule(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可操作全局关键词')
        return
    keywords = _strip_cmd(event.content, r'^删除全局关键词\s*').split()
    if not keywords:
        await event.reply('用法: 删除全局关键词 <词1> <词2> ...')
        return
    before = len(_data.get('rules', []))
    _data['rules'] = [
        r for r in _data.get('rules', [])
        if not (r.get('scope') == 'global' and r.get('keyword') in keywords)
    ]
    removed = before - len(_data['rules'])
    _save()
    nav = ' '.join([_btn('关键词列表', '关键词列表'), _btn('关键词菜单', '关键词菜单')])
    if removed:
        await event.reply(f'✅ 已删除全局关键词 {removed} 条: {" ".join(keywords)}\n' + nav)
    else:
        await event.reply('未找到要删除的全局关键词\n' + nav)


# ==================== 指令: 审核 (超管) ====================


@handler(r'^待审核$', name='待审核', desc='查看待审核关键词 (超管)', ignore_at_check=True)
async def list_pending(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可查看待审核')
        return
    pend = _data.get('pending', [])
    if not pend:
        await event.reply('✅ 当前没有待审核的关键词')
        return
    lines = ['【待审核关键词】']
    for p in pend:
        lines.append(f'序号 {p["seq"]} | 群 {p["group_id"]} | 提交 {p["submitter_name"]}')
        lines.append(f'    关键词「{p["keyword"]}」→ {p["reply"]}')
    btns = []
    for p in pend[:20]:
        btns.append(_btn(f'通过 {p["seq"]}', f'通过 {p["seq"]}'))
    lines.append('\n' + ' '.join(btns))
    lines.append(_btn('关键词菜单', '关键词菜单'))
    await event.reply('\n'.join(lines))


@handler(r'^通过(\s+\d+)+\s*$', name='通过审核', desc='通过 序号 [序号...] 审核通过 (超管)', ignore_at_check=True)
async def approve_pending(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可审核')
        return
    seqs = [int(x) for x in re.findall(r'\d+', _strip_cmd(event.content, r'^通过'))]
    if not seqs:
        await event.reply('用法: 通过 序号 [序号...]')
        return
    pend = _data.get('pending', [])
    by_seq = {int(p.get('seq')): p for p in pend}
    approved = []
    notify = []  # (group_openid, keyword)
    for s in seqs:
        p = by_seq.get(s)
        if not p:
            continue
        rule = _sanitize_rule({
            'name': p.get('keyword'), 'keyword': p.get('keyword'), 'match_mode': 'fuzzy',
            'scope': 'group', 'group_id': p.get('group_id'), 'reply_type': 'text', 'reply': p.get('reply'),
        })
        _data.setdefault('rules', []).append(rule)
        approved.append(s)
        notify.append(p)
    _data['pending'] = [p for p in pend if int(p.get('seq')) not in approved]
    _save()
    if not approved:
        await event.reply('未找到对应序号的待审核项')
        return
    # 通知提交群关键词已生效
    sender = getattr(event, 'sender', None) or _get_sender()
    if sender:
        for p in notify:
            try:
                target = p.get('group_openid') or p.get('group_id')
                await sender.send_to_group(target, f'✅ 关键词「{p.get("keyword")}」已通过审核并生效')
            except Exception as e:
                log.warning(f'通知群 {p.get("group_id")} 审核通过失败: {e}')
    nav = ' '.join([_btn('待审核', '待审核'), _btn('关键词列表', '关键词列表')])
    await event.reply(f'✅ 已通过审核序号: {" ".join(str(s) for s in approved)}\n' + nav)


@handler(r'^拒绝(\s+\d+)+\s*$', name='拒绝审核', desc='拒绝 序号 [序号...] 审核驳回 (超管)', ignore_at_check=True)
async def reject_pending(event, match):
    if not _is_super_admin(event):
        await event.reply('仅超级管理员可审核')
        return
    seqs = [int(x) for x in re.findall(r'\d+', _strip_cmd(event.content, r'^拒绝'))]
    if not seqs:
        await event.reply('用法: 拒绝 序号 [序号...]')
        return
    pend = _data.get('pending', [])
    rejected = [int(p.get('seq')) for p in pend if int(p.get('seq')) in seqs]
    _data['pending'] = [p for p in pend if int(p.get('seq')) not in rejected]
    _save()
    nav = ' '.join([_btn('待审核', '待审核'), _btn('关键词菜单', '关键词菜单')])
    if rejected:
        await event.reply(f'🛑 已拒绝审核序号: {" ".join(str(s) for s in rejected)}\n' + nav)
    else:
        await event.reply('未找到对应序号的待审核项\n' + nav)


# ==================== 指令: 列表 ====================


_LIST_BTN_CAP = 30


@handler(r'^关键词列表$', name='关键词列表', desc='查看关键词 (超管看全部, 群主/管理看本群)', ignore_at_check=True)
async def list_rules(event, match):
    is_super = _is_super_admin(event)
    gid = str(getattr(event, 'group_id', '') or '')
    glb_status = '开启' if _global_enabled() else '关闭'
    grp_status = '开启' if _group_enabled(gid) else '关闭'
    lines = []

    if is_super:
        g = _global_rules()
        lines.append(f'全局开关: {glb_status}    本群开关: {grp_status}')
        lines.append(f'\n全局关键词({len(g)}):')
        for r in g[:_LIST_BTN_CAP]:
            cron = ' ⏰' if r.get('cron_enabled') else ''
            lines.append(f'· {r.get("keyword")} [{r.get("reply_type")}]{cron} → {str(r.get("reply"))[:30]}')
        if gid:
            grp = _group_rules(gid)
            lines.append(f'\n本群关键词({len(grp)}):')
            for r in grp[:_LIST_BTN_CAP]:
                lines.append(f'· {r.get("keyword")} [{r.get("reply_type")}] → {str(r.get("reply"))[:30]}')
            if grp:
                pbtns = ' '.join(_btn(f'删除 {r.get("keyword")}', f'删除关键词 {r.get("keyword")}') for r in grp[:_LIST_BTN_CAP])
                lines.append('\n点击删除本群词:\n' + pbtns)
        if g:
            gbtns = ' '.join(_btn(f'删除 {r.get("keyword")}', f'删除全局关键词 {r.get("keyword")}') for r in g[:_LIST_BTN_CAP])
            lines.append('\n点击删除全局词:\n' + gbtns)
        nav = ' '.join([_btn('新增全局关键词', '新增全局关键词', enter=False), _btn('待审核', '待审核'), _btn('关键词菜单', '关键词菜单')])
        lines.append('\n' + nav)
    else:
        if not _is_admin_or_owner(event):
            await event.reply('仅群主/管理可查看本群关键词\n' + _btn('关键词菜单', '关键词菜单'))
            return
        grp = _group_rules(gid)
        lines.append(f'本群开关: {grp_status}')
        lines.append(f'\n本群关键词({len(grp)}):')
        for r in grp[:_LIST_BTN_CAP]:
            lines.append(f'· {r.get("keyword")} → {str(r.get("reply"))[:30]}')
        if grp:
            pbtns = ' '.join(_btn(f'删除 {r.get("keyword")}', f'删除关键词 {r.get("keyword")}') for r in grp[:_LIST_BTN_CAP])
            lines.append('\n点击删除本群词:\n' + pbtns)
        nav = ' '.join([_btn('新增关键词', '新增关键词', enter=False), _btn('关键词菜单', '关键词菜单')])
        lines.append('\n' + nav)

    await event.reply('\n'.join(lines))


# ==================== 指令: 菜单 ====================


@handler(r'^关键词菜单$', name='关键词菜单', desc='查看关键词插件指令说明', ignore_at_check=True)
async def menu(event, match):
    is_super = _is_super_admin(event)
    lines = [
        '【关键词自动回复 · 使用说明】',
        '命中关键词的群消息会自动回复 (按规则优先级)。',
        '',
        '【开关】',
        '· 关键词开启 / 关键词关闭：开关本群 (群主/管理)',
        '· 关键词全局开启 / 关键词全局关闭：开关全局 (超管)',
        '· 一键开启分群 / 一键关闭分群：批量开关所有分群 (超管)',
        '· 禁止分群开启 / 禁止分群关闭：开启后各群不能自行开启/新增且全部关闭, 超管豁免 (超管)',
        '',
        '【新增/删除】',
        f'· 新增关键词 <词> <回复内容>：群主/管理提交需超管审核 (本群上限 {_GROUP_LIMIT}); 超管直接生效',
        '· 删除关键词 <词>：删除本群词',
        '· 新增全局关键词 <词> <内容> / 删除全局关键词 <词>：超管',
        '',
        '【审核 (超管)】',
        '· 待审核：查看待审核列表',
        '· 通过 序号 [序号...]：通过 (可多个, 如 通过 1 2)',
        '· 拒绝 序号 [序号...]：驳回',
        '',
        '【查看】',
        '· 关键词列表：超管看全部+本群; 群主/管理看本群',
        '',
        '提示：多种输出方式(图片/语音/视频/markdown/ark)与定时推送(cron)可在 Web 后台「关键词自动回复」面板配置。',
    ]
    btns = [_btn('关键词开启', '关键词开启'), _btn('新增关键词', '新增关键词', enter=False), _btn('关键词列表', '关键词列表')]
    if is_super:
        btns.append(_btn('待审核', '待审核'))
        btns.append(_btn('一键关闭分群', '一键关闭分群'))
        forbid_cmd = '禁止分群关闭' if _forbid_group() else '禁止分群开启'
        btns.append(_btn(forbid_cmd, forbid_cmd))
    await event.reply('\n'.join(lines) + '\n' + ' '.join(btns))


# ==================== Web 后台 API ====================


def _json(obj, status=200):
    return web.json_response(obj, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


@register_route('GET', f'{_API}/state')
async def api_state(request):
    return _json({
        'success': True,
        'config': {
            'global_enabled': _data.get('global_enabled', True),
            'forbid_group': _data.get('forbid_group', False),
            'super_admins': _data.get('super_admins', []),
            'group_enabled': _data.get('group_enabled', {}),
        },
        'rules': _data.get('rules', []),
        'pending': _data.get('pending', []),
    })


@register_route('POST', f'{_API}/config')
async def api_config(request):
    body = await _body(request)
    if 'global_enabled' in body:
        _data['global_enabled'] = bool(body['global_enabled'])
    if 'forbid_group' in body:
        _data['forbid_group'] = bool(body['forbid_group'])
    if isinstance(body.get('super_admins'), list):
        _data['super_admins'] = [str(a).strip() for a in body['super_admins'] if str(a).strip()]
    if isinstance(body.get('group_enabled'), dict):
        _data['group_enabled'] = {str(k): bool(v) for k, v in body['group_enabled'].items()}
    _save()
    return _json({'success': True})


@register_route('POST', f'{_API}/rule')
async def api_save_rule(request):
    body = await _body(request)
    if not str(body.get('keyword', '')).strip():
        return _json({'success': False, 'message': '关键词不能为空'}, status=400)
    if body.get('match_mode') == 'regex':
        try:
            re.compile(str(body.get('keyword', '')))
        except re.error:
            return _json({'success': False, 'message': '正则表达式无效'}, status=400)
    rid = body.get('id')
    rules = _data.setdefault('rules', [])
    if rid:
        for i, r in enumerate(rules):
            if r.get('id') == rid:
                merged = dict(r)
                merged.update(body)
                merged['updated_at'] = datetime.datetime.now().isoformat(timespec='seconds')
                rules[i] = _sanitize_rule(merged)
                _save()
                return _json({'success': True, 'rule': rules[i]})
        return _json({'success': False, 'message': '规则不存在'}, status=404)
    rule = _sanitize_rule(body)
    rules.append(rule)
    _save()
    return _json({'success': True, 'rule': rule})


@register_route('POST', f'{_API}/delete')
async def api_delete_rule(request):
    body = await _body(request)
    rid = body.get('id')
    if not rid:
        return _json({'success': False, 'message': '缺少 id'}, status=400)
    before = len(_data.get('rules', []))
    _data['rules'] = [r for r in _data.get('rules', []) if r.get('id') != rid]
    _save()
    return _json({'success': len(_data['rules']) != before})


@register_route('POST', f'{_API}/delete_batch')
async def api_delete_rules(request):
    body = await _body(request)
    ids = body.get('ids')
    if not isinstance(ids, list) or not ids:
        return _json({'success': False, 'message': '缺少 ids'}, status=400)
    idset = {str(i) for i in ids}
    before = len(_data.get('rules', []))
    _data['rules'] = [r for r in _data.get('rules', []) if str(r.get('id')) not in idset]
    removed = before - len(_data['rules'])
    _save()
    return _json({'success': True, 'removed': removed})


@register_route('POST', f'{_API}/bulk_group')
async def api_bulk_group(request):
    """一键开启/关闭所有分群开关。body: {enabled: bool}"""
    body = await _body(request)
    value = bool(body.get('enabled'))
    changed = _bulk_set_groups(value)
    _save()
    return _json({'success': True, 'changed': changed, 'group_enabled': _data.get('group_enabled', {})})


@register_route('POST', f'{_API}/forbid_group')
async def api_forbid_group(request):
    """禁止分群开关。body: {enabled: bool}; 开启时同时关闭所有分群。"""
    body = await _body(request)
    value = bool(body.get('enabled'))
    _data['forbid_group'] = value
    changed = _bulk_set_groups(False) if value else 0
    _save()
    return _json({'success': True, 'forbid_group': value, 'closed': changed, 'group_enabled': _data.get('group_enabled', {})})


@register_route('POST', f'{_API}/toggle')
async def api_toggle_rule(request):
    body = await _body(request)
    rid = body.get('id')
    enabled = bool(body.get('enabled', True))
    for r in _data.get('rules', []):
        if r.get('id') == rid:
            r['enabled'] = enabled
            r['updated_at'] = datetime.datetime.now().isoformat(timespec='seconds')
            _save()
            return _json({'success': True, 'rule': r})
    return _json({'success': False, 'message': '规则不存在'}, status=404)


@register_route('POST', f'{_API}/approve')
async def api_approve(request):
    body = await _body(request)
    seqs = body.get('seqs') or ([body.get('seq')] if body.get('seq') is not None else [])
    try:
        seqs = [int(s) for s in seqs]
    except (TypeError, ValueError):
        return _json({'success': False, 'message': '序号无效'}, status=400)
    pend = _data.get('pending', [])
    by_seq = {int(p.get('seq')): p for p in pend}
    approved = []
    for s in seqs:
        p = by_seq.get(s)
        if not p:
            continue
        rule = _sanitize_rule({
            'name': p.get('keyword'), 'keyword': p.get('keyword'), 'match_mode': 'fuzzy',
            'scope': 'group', 'group_id': p.get('group_id'), 'reply_type': 'text', 'reply': p.get('reply'),
        })
        _data.setdefault('rules', []).append(rule)
        approved.append(s)
    _data['pending'] = [p for p in pend if int(p.get('seq')) not in approved]
    _save()
    return _json({'success': True, 'approved': approved})


@register_route('POST', f'{_API}/reject')
async def api_reject(request):
    body = await _body(request)
    seqs = body.get('seqs') or ([body.get('seq')] if body.get('seq') is not None else [])
    try:
        seqs = [int(s) for s in seqs]
    except (TypeError, ValueError):
        return _json({'success': False, 'message': '序号无效'}, status=400)
    pend = _data.get('pending', [])
    rejected = [int(p.get('seq')) for p in pend if int(p.get('seq')) in seqs]
    _data['pending'] = [p for p in pend if int(p.get('seq')) not in rejected]
    _save()
    return _json({'success': True, 'rejected': rejected})


@register_route('POST', f'{_API}/test')
async def api_test(request):
    body = await _body(request)
    content = str(body.get('content', ''))
    gid = str(body.get('group_id', '') or '')
    if not content:
        return _json({'success': True, 'matched': None})
    rule = _find_rule(content, gid)
    return _json({'success': True, 'matched': rule})


# ==================== 生命周期 ====================


@on_load
async def _on_load():
    global _scheduler_task
    _load()
    register_page(
        key=_PAGE_KEY,
        label='关键词自动回复',
        source='plugin',
        source_name='关键词自动回复',
        html_file=os.path.join(_PLUGIN_DIR, 'panel.html'),
        icon=_ICON,
    )
    _cancel_existing_schedulers()
    _scheduler_task = asyncio.create_task(_scheduler_loop(), name=_SCHEDULER_TASK_NAME)
    log.info('关键词自动回复插件已加载 (定时推送调度器已启动)')


@on_unload
def _on_unload():
    global _scheduler_task
    _cancel_existing_schedulers()
    _scheduler_task = None
    unregister_page(_PAGE_KEY)
    log.info('关键词自动回复插件已卸载')
