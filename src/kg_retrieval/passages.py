"""Exact, bounded passage selection. Partial context never establishes applicability."""
import hashlib
import json
import re


VERSION = 'paragraph-offsets-v2'
OMISSION = '\n\n【此处省略未选取原文，须结合完整来源复核】\n\n'
NUMBER = r'一二三四五六七八九十百千万零〇两\d'
MARKER = re.compile(rf'(?m)^[ \t　]*(第[{NUMBER}]+条|[{NUMBER}]+[、．]|[（(][{NUMBER}]+[）)]|\d+[.、．])(?!\d)')
GUARD = re.compile(r'适用|条件|例外|除|不包括|不含|不予|不得|但|仅|限于|必须|须|应当|如果|若|凡|满足|具备|前提|范围|是指|定义|施行|生效|废止|修改|修订|自.{0,16}日起')
REFERENCE = re.compile(rf'前[款项条]|上述|下述|本条|本款|第[{NUMBER}]+[条款项]|附件|附表')


def structural_units(text):
    markers = list(MARKER.finditer(text))
    if len(markers) < 2:
        return []
    # Use one peer level only. Keep complete articles, including nested lists,
    # unnumbered conditions and exceptions. Nested lists must not become peers.
    def level(label):
        return 0 if label.startswith('第') else 1 if label.endswith('、') and not label[0].isdigit() else 2 if label[0] in '（(' else 3
    top = min(level(m[1]) for m in markers)
    markers = [m for m in markers if level(m[1]) == top]
    if len(markers) < 2 or len({m[1] for m in markers}) != len(markers):
        return []
    starts = sorted({0, *[m.start() for m in markers], len(text)})
    return [(a, b) for a, b in zip(starts, starts[1:])]


def select(text, matches, *, required=False, dependency=False):
    """Return whole structural units plus global guards, or fall back to original.

    Required rule sources and dependency/parent/change evidence have no reviewed
    sub-clause binding: preserve them in full. Optional retrieval excerpts are
    explicitly incomplete even when all detected guards have been retained.
    """
    full = {'text': text}
    if required or dependency or len(text) < 1200:
        return full
    units = structural_units(text)
    if not units:
        return full
    checksum = hashlib.sha256(text.encode()).hexdigest()
    anchors = []
    for match in matches:
        reason = match.get('reason') or {}
        if not isinstance(reason, dict):
            continue
        for span in reason.get('spans', []):
            a, b = span.get('source_char_start'), span.get('source_char_end')
            if (span.get('original_text_sha256') != checksum or type(a) is not int or type(b) is not int
                    or not 0 <= a < b <= len(text)):
                return full
            anchors.append((a, b))
        for term in reason.get('matched_terms', []):
            if isinstance(term, str) and len(term) >= 3:
                anchors.extend((m.start(), m.end()) for m in re.finditer(re.escape(term), text, re.IGNORECASE))
    chosen = {i for i, (a, b) in enumerate(units) if any(a < y and b > x for x, y in anchors)}
    if not chosen:
        return full
    chosen.add(0)  # Preamble / first provision: identity, scope and definitions.
    chosen |= {i for i, (a, b) in enumerate(units) if GUARD.search(text[a:b])}
    # A cross-reference requires semantic resolution; do not guess its target.
    # Strip the unit's own heading before checking for references in its body.
    for i in chosen:
        a, b = units[i]
        body = MARKER.sub('', text[a:b], count=1)
        if REFERENCE.search(body):
            return full
    merged = []
    for i in sorted(chosen):
        a, b = units[i]
        if merged and merged[-1][1] == a:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    retained = sum(b - a for a, b in merged)
    if retained >= len(text):
        return full
    output, spans, cursor = [], [], 0
    for a, b in merged:
        if output:
            output.append(OMISSION)
            cursor += len(OMISSION)
        output.append(text[a:b])
        spans.append({'source_char_start': a, 'source_char_end': b,
                      'text_char_start': cursor, 'text_char_end': cursor + b - a})
        cursor += b - a
    selection = {'text': ''.join(output), 'partial_clause': True, 'context_complete': False,
            'selection_kind': 'structural_passages_v1', 'evidence_spans': spans,
            'original_text_sha256': checksum, 'original_char_count': len(text),
            'omitted_char_count': len(text) - retained}
    # Provenance and the explicit review gap also cost space. Keep the whole
    # source if a small omission would make the final payload larger.
    if len(json.dumps(selection, ensure_ascii=False).encode()) + 512 >= len(json.dumps(full, ensure_ascii=False).encode()):
        return full
    return selection


def fragment(item, start, end):
    """Slice delivery text while mapping every retained source character exactly."""
    text = item['text']
    result = {**item, 'text': text[start:end], 'partial_clause': True,
              'original_text_sha256': item.get('original_text_sha256') or hashlib.sha256(text.encode()).hexdigest()}
    if 'evidence_spans' in item:
        spans = []
        for span in item['evidence_spans']:
            a, b = max(start, span['text_char_start']), min(end, span['text_char_end'])
            if a < b:
                source_start = span['source_char_start'] + a - span['text_char_start']
                spans.append({'source_char_start': source_start, 'source_char_end': source_start + b - a,
                              'text_char_start': a - start, 'text_char_end': b - start})
        result['evidence_spans'] = spans
        # Non-contiguous source selections cannot be represented by one interval.
        result.pop('source_char_start', None)
        result.pop('source_char_end', None)
    else:
        offset = item.get('source_char_start', 0)
        result.update(source_char_start=offset + start, source_char_end=offset + end)
    return result


def exact_source(item, original):
    """Verify complete text or the independently reconstructible selection."""
    if item['text'] == original:
        return True
    if (not item.get('partial_clause') or item.get('context_complete') is not False
            or item.get('selection_kind') != 'structural_passages_v1'
            or item.get('original_text_sha256') != hashlib.sha256(original.encode()).hexdigest()
            or item.get('original_char_count') != len(original)):
        return False
    spans = item.get('evidence_spans')
    if not isinstance(spans, list) or not spans:
        return False
    source_end, output_end, retained, pieces = -1, 0, 0, []
    for span in spans:
        values = [span.get(k) for k in ('source_char_start', 'source_char_end', 'text_char_start', 'text_char_end')]
        if any(type(v) is not int for v in values):
            return False
        a, b, x, y = values
        if not (source_end < a < b <= len(original) and x == output_end + (len(OMISSION) if pieces else 0)
                and y == x + b - a and item['text'][x:y] == original[a:b]):
            return False
        source_end, output_end, retained = b, y, retained + b - a
        pieces.append(original[a:b])
    return (OMISSION.join(pieces) == item['text'] and
            item.get('omitted_char_count') == len(original) - retained > 0)
