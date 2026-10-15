from .llm_schema import compact_json, conservative_token_estimate
REFERENCE_TOKEN_LIMIT = 400
SEGMENT_TOKEN_LIMIT = 600
SEGMENT_CRITICAL_LIMIT = 3

def _tokens(value):
    try:
        return conservative_token_estimate(compact_json(value))
    except (TypeError, ValueError):
        return conservative_token_estimate(str(value))

def _prefix(text, fits):
    text = str(text)
    if not text or fits(text):
        return (text, False)
    low, high, best = (0, len(text), 0)
    while low <= high:
        middle = (low + high) // 2
        if fits(text[:middle]):
            best, low = (middle, middle + 1)
        else:
            high = middle - 1
    if best <= 0:
        return ('', True)
    cut = text[:best]
    for index in range(len(cut) - 1, -1, -1):
        if cut[index] in '。！？!?；;\n':
            return (cut[:index + 1], True)
    return ('', True)

def _compact_reference(report, limit):
    if not isinstance(report, dict):
        return (None, True, False) if _tokens(report) > limit else (report, False, False)
    if _tokens(report) <= limit:
        return (report, False, False)
    compacted = {key: value for key, value in report.items() if not isinstance(value, (list, dict)) and key != 'summary'}
    compacted['truncated'] = True
    used = _tokens(compacted)
    if used > limit:
        return (None, True, False)
    has_items = any((isinstance(value, list) and value for value in report.values()))
    ceiling = limit - max(1, (limit - used) // 3) if has_items else limit
    summary = report.get('summary')
    if isinstance(summary, str) and summary:
        text, _ = _prefix(summary, lambda part: _tokens({**compacted, 'summary': part}) <= ceiling)
        if text:
            compacted['summary'] = text
    for key, values in report.items():
        if not isinstance(values, list) or not values:
            continue
        kept = []
        for item in values:
            if _tokens({**compacted, key: kept + [item]}) <= limit:
                kept.append(item)
            elif isinstance(item, str):
                text, _ = _prefix(item, lambda part: _tokens({**compacted, key: kept + [part]}) <= limit)
                if text:
                    kept.append(text)
        if kept:
            compacted[key] = kept
    return (compacted, False, True) if _tokens(compacted) <= limit else (None, True, False)

def _skeleton(segment):
    return {key: value for key, value in segment.items() if key not in ('summary', 'critical')}

def _score(segment):
    critical = segment.get('critical')
    count = len(critical) if isinstance(critical, list) else 0
    summary = segment.get('summary')
    try:
        events = int(segment.get('event_count') or 0)
    except (TypeError, ValueError):
        events = 0
    return (0 if count else 1, -count, -events, -(len(summary) if isinstance(summary, str) else 0))

def _preference_order(segments):
    if not segments:
        return []
    windows = {}
    for segment in segments:
        start, end = (segment.get('period_start'), segment.get('period_end'))
        key = (str(start), str(end)) if start is not None or end is not None else ''
        windows.setdefault(key, []).append(segment)
    grouped = [sorted(items, key=_score) for items in windows.values()]
    spread, pending = ([], [(0, len(grouped) - 1)])
    while pending:
        low, high = pending.pop(0)
        if low > high:
            continue
        middle = (low + high) // 2
        for index in (low, high, middle):
            if index not in spread:
                spread.append(index)
        pending += [pair for pair in ((low, middle - 1), (middle + 1, high)) if pair[0] <= pair[1]]
    critical = [i for i in spread if _score(grouped[i][0])[0] == 0]
    priority = critical + [i for i in spread if i not in set(critical)]
    return [segment for i in priority for segment in grouped[i]]

def _fits(kept, candidate, limit, cap):
    return _tokens(kept + [candidate]) <= limit and _tokens([candidate]) <= cap

def _fit_segment(segment, kept, limit, cap):
    if _fits(kept, segment, limit, cap):
        return (segment, False)
    skeleton = _skeleton(segment)
    if not _fits(kept, skeleton, limit, cap):
        return (None, False)
    summary = segment.get('summary') if isinstance(segment.get('summary'), str) else ''
    critical = segment.get('critical')
    critical = [item for item in critical if isinstance(item, dict)] if isinstance(critical, list) else []
    items = critical[:SEGMENT_CRITICAL_LIMIT]
    base = dict(skeleton, compacted=True)
    for count in range(len(items), -1, -1):
        trial = dict(base, **{'critical': items[:count]} if count else {})
        if _fits(kept, trial, limit, cap):
            base = trial
            break
    else:
        return (None, False)
    if summary:
        text, _ = _prefix(summary, lambda part: _fits(kept, dict(base, summary=part), limit, cap))
        if text:
            base['summary'] = text
    return (base, True)

def _select_segments(segments, limit):
    candidates, duplicates, seen = ([], 0, set())
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        summary = segment.get('summary')
        if isinstance(summary, str) and summary:
            if summary in seen:
                duplicates += 1
                candidates.append({key: value for key, value in segment.items() if key != 'summary'})
                continue
            seen.add(summary)
        candidates.append(segment)
    cap = min(limit, max(100, limit // 4))
    kept, compacted = ([], 0)
    for segment in _preference_order(candidates):
        fitted, changed = _fit_segment(segment, kept, limit, cap)
        if fitted is not None:
            kept.append(fitted)
            compacted += 1 if changed else 0
    return (kept, compacted, len(segments) - len(kept), duplicates)

def compact_optional_context(model_payload, *, reference_token_limit=REFERENCE_TOKEN_LIMIT, segment_token_limit=SEGMENT_TOKEN_LIMIT):
    diagnostics = {'segments_available': 0, 'segments_compacted': 0, 'segments_omitted': 0, 'segments_duplicates_collapsed': 0, 'segment_tokens_before': 0, 'segment_tokens_after': 0, 'reference_tokens_before': 0, 'reference_tokens_after': 0, 'reference_truncated': 0, 'reference_omitted': 0}
    if not isinstance(model_payload, dict):
        return diagnostics
    if 'reference_report' in model_payload:
        reference = model_payload['reference_report']
        diagnostics['reference_tokens_before'] = _tokens(reference)
        replacement, omitted, truncated = _compact_reference(reference, reference_token_limit)
        if omitted:
            model_payload.pop('reference_report', None)
            diagnostics['reference_omitted'] = 1
        else:
            model_payload['reference_report'] = replacement
            diagnostics['reference_truncated'] = 1 if truncated else 0
            diagnostics['reference_tokens_after'] = _tokens(replacement)
    if 'memory_segments' in model_payload:
        segments = model_payload['memory_segments']
        diagnostics['segment_tokens_before'] = _tokens(segments)
        if not isinstance(segments, list):
            diagnostics['segment_tokens_after'] = diagnostics['segment_tokens_before']
        elif segment_token_limit <= 0:
            model_payload.pop('memory_segments', None)
            diagnostics.update(segments_available=len(segments), segments_omitted=len(segments))
        else:
            kept, compacted, omitted, duplicates = _select_segments(segments, segment_token_limit)
            model_payload['memory_segments'] = kept
            diagnostics.update(segments_available=len(segments), segments_compacted=compacted, segments_omitted=omitted, segments_duplicates_collapsed=duplicates, segment_tokens_after=_tokens(kept))
    return diagnostics
