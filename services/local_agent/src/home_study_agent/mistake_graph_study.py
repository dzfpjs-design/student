"""Graph grounding for the wrong-question study workflow."""

import json

from .cloud_privacy import sanitize_cloud_text


def study_targets(graph):
    if not graph or graph['status'] != 'matched' or not graph.get('primary'):
        return []
    primary = graph['primary']
    seen = {primary['key']}
    targets = []
    for point in graph['prerequisites']:
        if point['key'] in seen:
            continue
        seen.add(point['key'])
        targets.append({
            'ref': f'P{len(targets) + 1}', 'key': point['key'], 'title': point['title'],
            'kind': 'prerequisite', 'reviewed': point['review_status'] == 'approved'
            and point['relation_review_status'] == 'approved',
        })
        if len(targets) == 3:
            break
    return targets + [{'ref': 'MAIN', 'key': primary['key'], 'title': primary['title'],
                       'kind': 'primary', 'reviewed': primary['review_status'] == 'approved'}]


def cloud_targets(targets, names):
    return [{'ref': target['ref'], 'title': sanitize_cloud_text(target['title'], names).text[:180],
             'kind': target['kind'], 'reviewed': target['reviewed']} for target in targets]


def prerequisite_checks(value, targets, source):
    points = {target['ref']: target for target in targets if target['kind'] == 'prerequisite'}
    if not points:
        return []
    supplied = value.get('prerequisite_checks', [])
    if not isinstance(supplied, list) or len(supplied) > len(points):
        raise ValueError()
    checks = {}
    # The current source can contain the child's answer in the original spoken question.
    evidence = (source['original_answer'], source['question'])
    for entry in supplied:
        if not isinstance(entry, dict):
            raise ValueError()
        ref, question = entry.get('checkpoint_ref'), entry.get('question')
        if ref not in points or ref in checks or not isinstance(question, str) or not 3 <= len(question.strip()) <= 240:
            raise ValueError()
        check = {'title': points[ref]['title'], 'question': question.strip()}
        quote = entry.get('evidence_quote')
        if isinstance(quote, str) and 3 <= len(quote.strip()) <= 180 and any(quote.strip() in text for text in evidence):
            check['evidence_quote'] = quote.strip()
        checks[ref] = check
    return sorted(checks.values(), key=lambda check: 'evidence_quote' not in check)


def review_targets(c, row, graph, graph_sha, analysis):
    targets = study_targets(graph)
    if not targets:
        return [], set()
    checks = {check['title']: i for i, check in enumerate((analysis or {}).get('prerequisite_checks', []))}
    targets.sort(key=lambda target: (target['kind'] == 'primary', checks.get(target['title'], len(checks))))
    results = {}
    for item in c.execute('''SELECT validation_json,feedback_json,help_count FROM mistake_learning_item
        WHERE mistake_id=? AND source_revision=? AND feedback_json IS NOT NULL
        ORDER BY rowid''', (row['mistake_id'], row['source_revision'])):
        binding = json.loads(item['validation_json']).get('graph_target')
        if binding and binding.get('graph_sha') == graph_sha:
            feedback = json.loads(item['feedback_json'])
            if feedback['judgement'] != 'uncertain':
                results[binding['key']] = feedback['judgement'] == 'correct' and item['help_count'] == 0
    checked = {key for key, passed in results.items() if passed}
    pending = [target for target in targets if target['key'] not in checked or target['kind'] == 'primary']
    return pending, checked


def practice_graph_target(item, graph_sha):
    target = json.loads(item['validation_json']).get('graph_target')
    if target and target.get('graph_sha') == graph_sha:
        return {k: target[k] for k in ('title', 'kind')}
    return None


def practice_graph_is_stale(item, graph_sha):
    target = json.loads(item['validation_json']).get('graph_target')
    return bool(target and (not graph_sha or target.get('graph_sha') != graph_sha))
