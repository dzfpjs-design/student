"""Graph grounding for the wrong-question study workflow."""

import json
import hashlib

from .cloud_privacy import sanitize_cloud_text


def reviewed_graph_subject(graph):
    """Only an approved matched node may supply an inferred subject."""
    if not isinstance(graph,dict) or graph.get('status') != 'matched':
        return None
    primary=graph.get('primary')
    if not isinstance(primary,dict) or primary.get('review_status') != 'approved':
        return None
    subject=graph.get('subject')
    return subject.strip() if isinstance(subject,str) and subject.strip() else None


def study_targets(graph):
    if not graph or graph['status'] != 'matched' or not graph.get('primary'):
        return []
    primary = graph['primary']
    # Matching is not a review decision. Unreviewed graph content remains
    # visible as a hypothesis, but must not drive practice or review.
    if primary.get('review_status') != 'approved':
        return []
    seen = {primary['key']}
    targets = []
    for point in graph['prerequisites']:
        if (point['key'] in seen or point.get('review_status') != 'approved'
                or point.get('relation_review_status') != 'approved'):
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
        if binding and practice_graph_target(item,graph_sha,graph):
            feedback = json.loads(item['feedback_json'])
            if feedback['judgement'] != 'uncertain':
                results[binding['key']] = feedback['judgement'] == 'correct' and item['help_count'] == 0
    checked = {key for key, passed in results.items() if passed}
    pending = [target for target in targets if target['key'] not in checked or target['kind'] == 'primary']
    return pending, checked


def practice_graph_target(item, graph_sha, graph=None):
    try:
        target = json.loads(item['validation_json']).get('graph_target')
        if not isinstance(target,dict) or not graph_sha or target.get('graph_sha') != graph_sha:
            return None
        for current in study_targets(graph):
            if all(target.get(key) == current.get(key) for key in ('key','title','kind')):
                return {key:target[key] for key in ('title','kind')}
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    return None


def independent_practice_proof(child_id, mistake_id, source_revision, question, expected, explanation):
    """Bind a server-recorded graph-free generation to its exact source and content."""
    value=['graph_free_practice_v1',child_id,mistake_id,source_revision,question,expected,explanation]
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def independently_sourced_practice(item):
    try:
        value=json.loads(item['validation_json'])
        proof=value.get('graph_free_origin') if isinstance(value,dict) else None
        if not isinstance(proof,dict) or set(proof)!={'version','digest'} or proof['version']!='source_and_child_input_v1': return False
        expected=independent_practice_proof(item['child_id'],item['mistake_id'],item['source_revision'],
            item['question'],json.loads(item['expected_json']),item['explanation'])
        return isinstance(proof['digest'],str) and proof['digest']==expected
    except (TypeError, ValueError, KeyError, IndexError):
        return False


def reviewed_coach_practice(item, graph_sha, graph):
    try:
        proof=json.loads(item['validation_json']).get('reviewed_coach_origin')
        if not isinstance(proof,dict) or set(proof)!={'version','graph_sha','digest'} or proof['version']!='reviewed_graph_v1': return False
        if not graph_sha or proof['graph_sha']!=graph_sha or not study_targets(graph): return False
        expected=independent_practice_proof(item['child_id'],item['mistake_id'],item['source_revision'],
            item['question'],json.loads(item['expected_json']),item['explanation'])
        return isinstance(proof['digest'],str) and proof['digest']==expected
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        return False


def practice_graph_is_stale(item, graph_sha, graph=None):
    try:
        value=json.loads(item['validation_json'])
        if not isinstance(value,dict): return True
        if 'graph_target' not in value:
            return not (independently_sourced_practice(item) or reviewed_coach_practice(item,graph_sha,graph))
        return practice_graph_target(item,graph_sha,graph) is None
    except (TypeError, ValueError, KeyError, IndexError):
        return True
