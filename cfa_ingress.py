"""Text-preserving editor for cloudflared block-style YAML.

Not a general YAML parser. Flow mappings, anchors, aliases, multiline scalars and
ambiguous duplicate rules are rejected before editing rather than guessed.
"""
from __future__ import annotations

import json
import re


def _scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        match = re.match(r'^("(?:\\.|[^"\\])*")\s*(?:#.*)?$', value)
        if not match:
            raise ValueError('YAML 双引号标量格式不受支持')
        return json.loads(match[1])
    if value.startswith("'"):
        match = re.match(r"^'((?:''|[^'])*)'\s*(?:#.*)?$", value)
        if not match:
            raise ValueError('YAML 单引号标量格式不受支持')
        return match[1].replace("''", "'")
    value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
    if value.startswith(('{', '[', '&', '*', '|', '>')) or '\t' in value:
        raise ValueError('请使用普通 block YAML；flow、锚点、别名和多行标量不支持自动编辑')
    return value


def get_tunnel_id(text: str) -> str:
    values = [m[1] for line in text.splitlines() if (m := re.match(r'^tunnel:\s*(.*?)\s*$', line))]
    if len(values) > 1:
        raise ValueError('YAML 存在重复 tunnel 字段')
    return _scalar(values[0]) if values else ''


def _document(text: str):
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r'^ingress\s*:', line)]
    if len(starts) != 1:
        raise ValueError('配置应包含唯一顶层 ingress 段')
    start = starts[0]
    if re.sub(r'#.*', '', lines[start]).strip() != 'ingress:':
        raise ValueError('ingress 应使用 block YAML 列表，不支持 inline flow')
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line and not line[0].isspace() and not line.startswith(('#', '-')):
            end = i
            break
    items = []
    indent = None
    for i in range(start + 1, end):
        line = lines[i]
        if '\t' in line[:len(line) - len(line.lstrip())]:
            raise ValueError('YAML 缩进请使用空格')
        match = re.match(r'^( *)-\s+([A-Za-z][A-Za-z0-9]*):\s*(.*)$', line)
        if not match:
            if line.lstrip().startswith('-'):
                raise ValueError('不支持的 ingress 列表格式')
            continue
        current = len(match[1])
        if indent is None:
            indent = current
        if current != indent:
            raise ValueError('ingress 列表缩进不一致或嵌套列表暂不支持编辑')
        items.append({'start': i, 'first_key': match[2], 'first_value': match[3], 'indent': current})
    if not items:
        raise ValueError('ingress 列表为空或格式不受支持')
    for pos, item in enumerate(items):
        item['end'] = items[pos + 1]['start'] if pos + 1 < len(items) else end
        fields, positions, origin, origin_pos = {}, {}, {}, {}
        origin_indent = None
        for i in range(item['start'], item['end']):
            line = lines[i]
            if i == item['start']:
                key, value = item['first_key'], item['first_value']
            else:
                match = re.match(r'^( *)([A-Za-z][A-Za-z0-9]*):\s*(.*)$', line)
                if not match:
                    if line.strip() and not line.lstrip().startswith('#'):
                        raise ValueError('ingress 含不支持的 YAML 结构，请使用 block 键值')
                    continue
                width, key, value = len(match[1]), match[2], match[3]
                if width > item['indent'] + 2:
                    if origin_indent is not None and width == origin_indent + 2:
                        if key in origin:
                            raise ValueError('originRequest 含重复键')
                        origin[key] = _scalar(value)
                        origin_pos[key] = i
                    continue
                if width != item['indent'] + 2:
                    raise ValueError('ingress 属性缩进不一致')
                origin_indent = width if key == 'originRequest' else None
            if key in fields:
                raise ValueError('ingress 含重复键，拒绝歧义修改')
            if key == 'originRequest':
                if value and not value.startswith('#'):
                    raise ValueError('originRequest 应使用 block 键值')
                fields[key] = ''
                origin_indent = item['indent'] + 2
            else:
                fields[key] = _scalar(value)
            positions[key] = i
        item.update(fields=fields, positions=positions, origin=origin, origin_pos=origin_pos)
    global_insecure = False
    for i, line in enumerate(lines[:start]):
        if re.match(r'^originRequest:\s*(?:#.*)?$', line):
            for child in lines[i + 1:start]:
                if child and not child[0].isspace() and not child.lstrip().startswith('#'):
                    break
                if (m := re.match(r'^ +noTLSVerify:\s*(.*?)\s*$', child)):
                    global_insecure = _scalar(m[1]).lower() == 'true'
    return lines, start, end, items, global_insecure


def parse_entries(text: str) -> list[dict]:
    if not text.strip():
        return []
    _, _, _, items, global_insecure = _document(text)
    result = []
    for item in items:
        fields, origin = item['fields'], item['origin']
        if 'hostname' not in fields:
            continue
        entry = {'hostname': fields['hostname'], 'service': fields.get('service', ''),
                 'no_tls_verify': origin.get('noTLSVerify', str(global_insecure)).lower() == 'true',
                 'origin_request': dict(origin)}
        if 'path' in fields:
            entry['path'] = fields['path']
        result.append(entry)
    return result


def _editable(text: str, hostname: str):
    doc = _document(text)
    items = doc[3]
    catchalls = [i for i in items if 'hostname' not in i['fields'] and 'path' not in i['fields']]
    if len(catchalls) != 1 or catchalls[0] is not items[-1] or not catchalls[0]['fields'].get('service'):
        raise ValueError('写入要求唯一且位于最后的 catch-all service 规则')
    matches = [item for item in items if item['fields'].get('hostname') == hostname]
    if len(matches) > 1 or any('path' in i['fields'] for i in matches):
        raise ValueError('该域名存在重复或 path 规则，请手动编辑，避免丢失匹配逻辑')
    return doc, matches


def _quoted(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in '\r\n\x00'):
        raise ValueError('字段必须为不含控制字符的字符串')
    return json.dumps(value, ensure_ascii=False)


def upsert(text: str, hostname: str, service: str, no_tls_verify: bool | None = None) -> str:
    _quoted(hostname)
    _quoted(service)
    (lines, _, _, items, global_insecure), matches = _editable(text, hostname)
    flag = bool(no_tls_verify) and service.startswith('https://')
    if not matches:
        indent = ' ' * items[-1]['indent']
        new = [f'{indent}- hostname: {_quoted(hostname)}', f'{indent}  service: {_quoted(service)}']
        if flag or global_insecure:
            new += [f'{indent}  originRequest:', f'{indent}    noTLSVerify: {str(flag).lower()}']
        lines[items[-1]['start']:items[-1]['start']] = new
    else:
        item = matches[0]
        positions = item['positions']
        service_line = positions.get('service')
        if service_line is None:
            raise ValueError('目标规则缺 service，拒绝猜测')
        if item['fields']['service'] != service:
            prefix = lines[service_line][:len(lines[service_line]) - len(lines[service_line].lstrip())]
            prefix += '- ' if service_line == item['start'] else ''
            lines[service_line] = f'{prefix}service: {_quoted(service)}'
        tls_line = item['origin_pos'].get('noTLSVerify')
        if tls_line is not None:
            if item['origin']['noTLSVerify'].lower() != str(flag).lower():
                lines[tls_line] = re.sub(r'(noTLSVerify:\s*)\S+', lambda m: m[1] + str(flag).lower(), lines[tls_line], count=1)
        elif flag or global_insecure:
            origin_line = positions.get('originRequest')
            if origin_line is not None:
                width = item['indent'] + 4
                lines.insert(origin_line + 1, ' ' * width + 'noTLSVerify: ' + str(flag).lower())
            else:
                pos = service_line + 1
                lines[pos:pos] = [' ' * (item['indent'] + 2) + 'originRequest:',
                                  ' ' * (item['indent'] + 4) + 'noTLSVerify: ' + str(flag).lower()]
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def remove(text: str, hostname: str) -> str:
    (lines, _, _, _, _), matches = _editable(text, hostname)
    if not matches:
        return text
    item = matches[0]
    # Keep trailing blank/comment lines belonging to the next rule/document.
    end = item['end']
    while end > item['start'] and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith('#')):
        end -= 1
    del lines[item['start']:end]
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')
