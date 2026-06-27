"""关键词自动回复插件 — 扩展页面可视化管理

架构 (照搬入群欢迎插件模式):
  - register_page(key, label, icon, html_file) 注册侧边栏扩展页面
  - register_route(METHOD, '/api/ext/keyword_reply/xxx') 注册 API 路由
  - panel.html 由框架 iframe 嵌入, URL 带 ?token=xxx 鉴权
  - data/config.yaml 持久化, 面板写入后全量回写; mtime 热重载兼容手动编辑

功能:
  - 三种匹配模式: exact / fuzzy / regex
  - 规则优先级 (越大越先), 多条命中仅回复最高优先级一条
  - 规则级启用/禁用, 作用域 (all/group/direct)
  - 全局开关 + 全局 Markdown, 规则级 use_markdown 三态覆盖
  - 变量替换: 标准变量集 + 正则分组 {match_0}~{match_9}
  - 全量消息触发 (ignore_at_check=True, 无需 @)
  - 面板: 增删改/上下移排序/测试匹配

依赖: ElainaBot v2 (core.plugin.* / core.base.*)
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime

import yaml
from aiohttp import web

import core.plugin.context as _ctx_mod
from core.base.logger import get_logger, PLUGIN, report_error
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

__plugin_meta__ = {
    'name': '关键词自动回复',
    'author': 'Astudlox',
    'description': '扩展页面管理关键词规则, 支持完全/模糊/正则匹配、优先级、Markdown、变量替换',
    'version': '3.1.0',
    'license': 'MIT',
}

log = get_logger(PLUGIN, '关键词自动回复')
ctx = _ctx_mod.ctx

# ==================== 常量 ====================

MATCH_MODES = ('exact', 'fuzzy', 'regex')
SCOPES = ('all', 'group', 'direct')
RULE_FIELDS = (
    'id', 'name', 'keyword', 'match_mode', 'reply', 'priority',
    'enabled', 'use_markdown', 'case_sensitive', 'scope',
    'created_at', 'updated_at',
)
_MSG_TYPE_TEXT = 0
_MSG_TYPE_MARKDOWN = 2

DEFAULT_CONFIG = {
    'global_enabled': True,
    'markdown_enabled': False,
    'default_match_mode': 'fuzzy',
}

_PAGE_KEY = 'keyword-autoreply'
_API = '/api/ext/keyword_reply'

DEFAULT_CONFIG_YAML = """# ==================== 关键词自动回复配置 ====================
# 可在框架「扩展页面 → 关键词自动回复」面板可视化编辑, 也可直接编辑本文件
# 直接编辑时, 保存后插件通过 mtime 检查自动热重载

global_enabled: true
markdown_enabled: false
default_match_mode: fuzzy

# 字段: name/keyword/match_mode/reply/priority/enabled/use_markdown/case_sensitive/scope
# 变量: {user_id} {username} {group_id} {content} {time} {date} {datetime} {match_0}~{match_9}
rules:
  - name: 欢迎语
    keyword: 你好
    match_mode: exact
    reply: 你好, {username}! 有什么可以帮你的吗?
    priority: 10
    enabled: true
    use_markdown: null
    case_sensitive: false
    scope: all

  - name: 天气查询
    keyword: 天气\\s*(.+)
    match_mode: regex
    reply: 你想查 {match_1} 的天气吗? 此功能开发中...
    priority: 5
    enabled: true
    use_markdown: null
    case_sensitive: false
    scope: all

  - name: 群规提醒
    keyword: 群规
    match_mode: fuzzy
    reply: |
      📜 **群规提醒**
      1. 友善交流
      2. 禁止刷屏
      3. 禁止广告
    priority: 1
    enabled: true
    use_markdown: true
    case_sensitive: false
    scope: group
"""

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>'
    '</svg>'
)


# ==================== 存储层 (YAML + mtime 热重载) ====================

class RuleStore:
    """内存缓存 + YAML 持久化 + mtime 热重载"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._rules = []
        self._config = {}
        self._mtime = 0.0
        self._path = ''
        self._loaded = False

    async def init(self):
        self._path = ctx.get_data_path('config.yaml')
        if not ctx.data_exists('config.yaml'):
            await ctx.save_data_async('config.yaml', DEFAULT_CONFIG_YAML)
            log.info('已生成默认 config.yaml')
        await self.maybe_reload(force=True)

    async def maybe_reload(self, force=False):
        try:
            mt = os.path.getmtime(self._path)
        except OSError:
            return
        if not force and mt == self._mtime:
            return
        cfg = await ctx.read_config_async() or {}
        if not isinstance(cfg, dict):
            cfg = {}
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        raw_rules = cfg.get('rules') or []
        cleaned = [self._sanitize(r) for r in raw_rules if isinstance(r, dict)]
        # 兼容旧数据: 给无 id 的规则补 id
        for r in cleaned:
            if not r.get('id'):
                r['id'] = 'rule_' + uuid.uuid4().hex[:12]
        async with self._lock:
            self._config = cfg
            self._rules = cleaned
            self._mtime = mt
        self._loaded = True
        log.info(f'配置已加载: {len(cleaned)} 条规则')

    @property
    def loaded(self):
        return self._loaded

    def get_rules(self):
        return [dict(r) for r in self._rules]

    def get_config(self):
        return dict(self._config)

    async def _save_locked(self):
        data = {
            'global_enabled': self._config.get('global_enabled', True),
            'markdown_enabled': self._config.get('markdown_enabled', False),
            'default_match_mode': self._config.get('default_match_mode', 'fuzzy'),
            'rules': [{k: v for k, v in r.items() if k in RULE_FIELDS} for r in self._rules],
        }
        content = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        await ctx.save_data_async('config.yaml', content)
        try:
            self._mtime = os.path.getmtime(self._path)
        except OSError:
            pass

    async def add_rule(self, data):
        rule = self._sanitize(data)
        rule['id'] = 'rule_' + uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec='seconds')
        rule['created_at'] = now
        rule['updated_at'] = now
        async with self._lock:
            self._rules.append(rule)
            await self._save_locked()
        return rule

    async def update_rule(self, rule_id, data):
        async with self._lock:
            for r in self._rules:
                if r.get('id') == rule_id:
                    sanitized = self._sanitize(data)
                    sanitized['id'] = rule_id
                    sanitized['created_at'] = r.get('created_at')
                    sanitized['updated_at'] = datetime.now().isoformat(timespec='seconds')
                    r.clear()
                    r.update(sanitized)
                    await self._save_locked()
                    return dict(r)
        return None

    async def delete_rule(self, rule_id):
        async with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.get('id') != rule_id]
            if len(self._rules) != before:
                await self._save_locked()
                return True
        return False

    async def toggle_rule(self, rule_id, enabled):
        async with self._lock:
            for r in self._rules:
                if r.get('id') == rule_id:
                    r['enabled'] = bool(enabled)
                    r['updated_at'] = datetime.now().isoformat(timespec='seconds')
                    await self._save_locked()
                    return dict(r)
        return None

    async def reorder(self, ordered_ids):
        async with self._lock:
            id_to_rule = {r.get('id'): r for r in self._rules}
            n = len(ordered_ids)
            now = datetime.now().isoformat(timespec='seconds')
            for i, rid in enumerate(ordered_ids):
                r = id_to_rule.get(rid)
                if r is not None:
                    r['priority'] = n - i
                    r['updated_at'] = now
            await self._save_locked()
        return True

    async def update_config(self, partial):
        async with self._lock:
            for k in DEFAULT_CONFIG:
                if k in partial:
                    self._config[k] = partial[k]
            await self._save_locked()
        return dict(self._config)

    @staticmethod
    def _sanitize(data):
        d = {k: data.get(k) for k in RULE_FIELDS if k in data}
        d['name'] = str(d.get('name', '')).strip()[:60] or '未命名规则'
        d['keyword'] = str(d.get('keyword', ''))
        d['match_mode'] = d.get('match_mode') if d.get('match_mode') in MATCH_MODES else 'fuzzy'
        d['reply'] = str(d.get('reply', ''))
        try:
            d['priority'] = int(d.get('priority', 0))
        except (TypeError, ValueError):
            d['priority'] = 0
        d['enabled'] = bool(d.get('enabled', True))
        um = d.get('use_markdown')
        if um is None or um == '' or (isinstance(um, str) and um.lower() == 'null'):
            d['use_markdown'] = None
        else:
            d['use_markdown'] = bool(um)
        d['case_sensitive'] = bool(d.get('case_sensitive', False))
        d['scope'] = d.get('scope') if d.get('scope') in SCOPES else 'all'
        return d


store = RuleStore()


# ==================== 匹配引擎 ====================

def _match_rule(rule, content):
    mode = rule.get('match_mode', 'fuzzy')
    kw = rule.get('keyword', '')
    if not kw:
        return None
    case = rule.get('case_sensitive', False)
    if mode == 'exact':
        if case:
            return True if content == kw else None
        return True if content.lower() == kw.lower() else None
    if mode == 'fuzzy':
        if case:
            return True if kw in content else None
        return True if kw.lower() in content.lower() else None
    if mode == 'regex':
        flags = 0 if case else re.IGNORECASE
        try:
            m = re.search(kw, content, flags | re.DOTALL)
            return m if m else None
        except re.error:
            return None
    return None


def _find_match(content, event):
    candidates = []
    for idx, r in enumerate(store._rules):
        if not r.get('enabled', True):
            continue
        scope = r.get('scope', 'all')
        if scope == 'group' and not getattr(event, 'is_group', False):
            continue
        if scope == 'direct' and not getattr(event, 'is_direct', False):
            continue
        candidates.append((idx, r))
    candidates.sort(key=lambda x: (-int(x[1].get('priority', 0)), x[0]))
    for _, r in candidates:
        m = _match_rule(r, content)
        if m:
            return r, (m if isinstance(m, re.Match) else None)
    return None, None


# ==================== 变量替换 ====================

def _build_vars(event, match):
    now = datetime.now()
    values = {
        'user_id': getattr(event, 'user_id', '') or '',
        'username': getattr(event, 'username', '') or '未知',
        'group_id': getattr(event, 'group_id', '') or '',
        'chat_type': getattr(event, 'chat_type', 'unknown') or 'unknown',
        'content': getattr(event, 'content', '') or '',
        'raw_content': getattr(event, 'raw_content', '') or '',
        'time': now.strftime('%H:%M:%S'),
        'date': now.strftime('%Y-%m-%d'),
        'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
        'message_id': getattr(event, 'message_id', '') or '',
    }
    if match is not None:
        values['match_0'] = match.group(0)
        for i in range(1, 10):
            try:
                values['match_' + str(i)] = match.group(i) or ''
            except (IndexError, Exception):
                values['match_' + str(i)] = ''
    else:
        values['match_0'] = values['content']
        for i in range(1, 10):
            values['match_' + str(i)] = ''
    return values


def _render_reply(template, values):
    if not template:
        return ''
    result = template
    for k, v in values.items():
        result = result.replace('{' + k + '}', str(v))
    return result


# ==================== 发送 (Markdown 三态) ====================

async def _send_reply(event, content, use_markdown):
    if use_markdown is True:
        return await event.reply(content, msg_type=_MSG_TYPE_MARKDOWN)
    if use_markdown is False:
        return await event.reply(content, msg_type=_MSG_TYPE_TEXT)
    return await event.reply(content)


# ==================== 全量消息分发 handler ====================

@handler(r'[\s\S]*', name='关键词自动回复', desc='根据关键词规则自动回复', priority=-9999, ignore_at_check=True)
async def autoreply_dispatcher(event, match):
    if getattr(event, 'is_bot', False):
        return
    if getattr(event, 'is_lifecycle', False):
        return
    content = getattr(event, 'content', '') or ''
    if not content.strip():
        return
    if not store.loaded:
        return
    await store.maybe_reload()
    config = store.get_config()
    if not config.get('global_enabled', True):
        return
    if not store._rules:
        return
    rule, m = _find_match(content, event)
    if not rule:
        return
    reply_text = _render_reply(rule.get('reply', ''), _build_vars(event, m))
    if not reply_text:
        return
    use_md = rule.get('use_markdown')
    if use_md is None:
        use_md = bool(config.get('markdown_enabled', False))
    try:
        await _send_reply(event, reply_text, use_md)
    except Exception as e:
        report_error(PLUGIN, '关键词自动回复', e, context={'rule': rule.get('name')})


@handler(r'^重载关键词$', name='重载关键词规则', desc='手动重载关键词配置', priority=100)
async def reload_cmd(event, match):
    try:
        await store.maybe_reload(force=True)
        count = len(store._rules)
        enabled = sum(1 for r in store._rules if r.get('enabled', True))
        await event.reply(f'✅ 关键词规则已重载\n共 {count} 条规则, {enabled} 条启用')
    except Exception as e:
        await event.reply(f'❌ 重载失败: {e}')
        report_error(PLUGIN, '关键词自动回复', e)


# ==================== Web 面板 API ====================

def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


@register_route('GET', f'{_API}/state')
async def api_state(request):
    """返回规则列表 + 全局配置"""
    return _json({'success': True, 'rules': store.get_rules(), 'config': store.get_config()})


@register_route('POST', f'{_API}/rule')
async def api_save_rule(request):
    """新增或更新规则 (body 带 id 则更新)"""
    body = await _body(request)
    if body.get('match_mode') == 'regex':
        try:
            re.compile(str(body.get('keyword', '')))
        except re.error:
            return _json({'success': False, 'message': '正则表达式无效'}, status=400)
    if not str(body.get('keyword', '')).strip():
        return _json({'success': False, 'message': '关键词不能为空'}, status=400)
    rule_id = body.get('id')
    if rule_id:
        updated = await store.update_rule(rule_id, body)
        if not updated:
            return _json({'success': False, 'message': '规则不存在'}, status=404)
        return _json({'success': True, 'rule': updated})
    rule = await store.add_rule(body)
    return _json({'success': True, 'rule': rule})


@register_route('POST', f'{_API}/delete')
async def api_delete(request):
    body = await _body(request)
    rid = body.get('id')
    if not rid:
        return _json({'success': False, 'message': '缺少 id'}, status=400)
    ok = await store.delete_rule(rid)
    return _json({'success': ok})


@register_route('POST', f'{_API}/toggle')
async def api_toggle(request):
    body = await _body(request)
    rid = body.get('id')
    enabled = bool(body.get('enabled', True))
    rule = await store.toggle_rule(rid, enabled)
    if not rule:
        return _json({'success': False, 'message': '规则不存在'}, status=404)
    return _json({'success': True, 'rule': rule})


@register_route('POST', f'{_API}/reorder')
async def api_reorder(request):
    """body: {ids: [按显示顺序的 id 列表]}, 首位 = 最高优先级"""
    body = await _body(request)
    ids = body.get('ids', [])
    if not isinstance(ids, list):
        return _json({'success': False, 'message': 'ids 必须为数组'}, status=400)
    await store.reorder(ids)
    return _json({'success': True, 'rules': store.get_rules()})


@register_route('POST', f'{_API}/config')
async def api_config(request):
    body = await _body(request)
    cfg = await store.update_config(body)
    return _json({'success': True, 'config': cfg})


@register_route('POST', f'{_API}/test')
async def api_test(request):
    """测试匹配: body {content, is_group} → 命中规则 + 渲染后回复"""
    import types
    body = await _body(request)
    content = str(body.get('content', ''))
    if not content:
        return _json({'success': True, 'matched': None, 'reply': ''})
    is_group = body.get('is_group', True)
    fake = types.SimpleNamespace(
        user_id='test_user_openid', username='测试用户',
        group_id='test_group_openid' if is_group else '',
        is_group=bool(is_group), is_direct=not bool(is_group),
        chat_type='group' if is_group else 'direct',
        content=content, raw_content=content, message_id='test_message_id',
    )
    rule, m = _find_match(content, fake)
    if not rule:
        return _json({'success': True, 'matched': None, 'reply': ''})
    reply_text = _render_reply(rule.get('reply', ''), _build_vars(fake, m))
    return _json({'success': True, 'matched': rule, 'reply': reply_text})


# ==================== 生命周期 ====================

@on_load
async def _on_load():
    await store.init()
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.html')
    register_page(
        key=_PAGE_KEY,
        label='关键词自动回复',
        source='plugin',
        source_name='关键词自动回复',
        html_file=html_path,
        icon=_ICON,
    )
    log.info('关键词自动回复插件已加载 (面板: 关键词自动回复)')


@on_unload
def _on_unload():
    unregister_page(_PAGE_KEY)
    log.info('关键词自动回复插件已卸载')
