"""Content-free semantic admission for a read-back-only input revision proposal."""
import re

from .learning_input_revision import FIELDS, duration_minutes, validate_arguments

CONTROL = 'learning_inputs.propose_revision'
CONTROLS = {CONTROL: ('propose_learning_input_revision', {})}
PROMPT = (
    'learning_inputs.propose_revision仅表示孩子明确请求修改、删除或撤销已记录作业/课堂项，'
    '不表示开始任务、完成任务或确认修改。额外且仅必填revision对象：kind(homework|recall)、'
    'operation(update|delete|undo|add)、target_quote(本轮逐字目标片段)、changes、change_quotes。'
    'update逐字段给出新值及本轮逐字片段；字段homework为subject/title/range_text/expected_minutes/'
    'special_requirements，recall为subject/summary；预计分钟数为正整数，其他新值必须等于对应原话片段。'
    'delete/undo的两个changes对象为空。不得给本地ID、历史内容或猜测缺失字段。'
    '学习中想起遗漏的新作业用add，仅kind=homework，subject/title必须明确逐字支持；'
    'target_quote等于新title；科目和内容明确但缺少时长时省略expected_minutes，进入未确认草稿追问，不猜时长。'
    '孩子报告作业时的老师转述或标题引用只进入待确认草稿，先核对是否自己的作业；仅复述别人命令仍用unknown。'
    '不确定是作答还是修改、否定/假设/混合命令或仅引用别人命令仍用mixed/unknown，'
    '其他role/control禁止revision。确认由后续本地读回流程处理，本控制绝不自动确认。'
)


def arguments(revision, transcript):
    if (not isinstance(revision, dict) or set(revision) != {
            'kind', 'operation', 'target_quote', 'changes', 'change_quotes'}):
        raise ValueError('INPUT_VOICE_REVISION_INVALID')
    kind, operation = revision['kind'], revision['operation']
    if not isinstance(kind, str) or kind not in FIELDS or operation not in ('update', 'delete', 'undo', 'add'):
        raise ValueError('INPUT_VOICE_REVISION_INVALID')
    target = revision['target_quote']
    if not isinstance(target, str) or not target.strip() or target not in transcript:
        raise ValueError('INPUT_VOICE_TARGET_NOT_GROUNDED')
    changes, quotes = revision['changes'], revision['change_quotes']
    if (not isinstance(changes, dict) or not isinstance(quotes, dict)
            or set(changes) - FIELDS[kind] or set(changes) != set(quotes)
            or bool(changes) != (operation in ('update', 'add'))):
        raise ValueError('INPUT_VOICE_CHANGES_INVALID')
    for field, value in changes.items():
        quote = quotes[field]
        if not isinstance(quote, str) or not quote.strip() or quote not in transcript:
            raise ValueError('INPUT_VOICE_VALUE_NOT_GROUNDED')
        if field == 'expected_minutes':
            if type(value) is not int or duration_minutes(quote) != value:
                raise ValueError('INPUT_VOICE_DURATION_NOT_GROUNDED')
        elif not isinstance(value, str) or value != quote or len(value) > 1000:
            raise ValueError('INPUT_VOICE_VALUE_NOT_GROUNDED')
    if operation == 'add' and (kind != 'homework' or not {'subject', 'title'} <= set(changes)
                               or changes['title'] != target):
        raise ValueError('INPUT_VOICE_ADDITION_FIELDS_REQUIRED')
    if re.search(r'(?:假如|假设|如果|什么意思|是否支持|能否支持|'
                 r'先别|不要|不用|不想|暂不|别再|然后|同时|顺便|并且|还要|再帮我)', transcript):
        raise ValueError('INPUT_VOICE_REQUEST_UNCERTAIN')
    from .learning_input_intake import needs_ownership
    if needs_ownership(transcript) and (kind != 'homework' or operation not in {'add', 'update'}):
        raise ValueError('INPUT_VOICE_REQUEST_UNCERTAIN')
    result = {**revision, 'request_quote': transcript}
    validate_arguments(CONTROL, result)
    return result


def parse(value, transcript, restricted):
    if (restricted or value['role'] != 'control' or value['control'] != CONTROL
            or value['confidence'] < .82):
        return {'status': 'clarify', 'reason': 'INPUT_VOICE_REQUEST_UNCERTAIN'}
    if set(value) != {'role', 'control', 'quote', 'confidence', 'revision'}:
        raise ValueError('INPUT_VOICE_REVISION_INVALID')
    try:
        arguments(value['revision'], transcript)
    except ValueError as error:
        if not str(error).startswith('INPUT_'):
            raise
        return {'status': 'clarify', 'reason': str(error)}
    return {'status': 'control', 'control': CONTROL, 'revision': value['revision']}


def clarification_response(decision, transcript):
    """Explain a refusal without promoting quoted/partial input to a proposal."""
    reason = decision.get('reason', '')
    local_refusal = reason.startswith('INPUT_')
    uncertain = decision.get('status') == 'non_control' and decision.get('role') in {'unknown', 'mixed'}
    if not local_refusal and not uncertain and reason not in {
            'CONTROL_INTENT_UNCERTAIN', 'CONTROL_INTENT_CONFLICT', 'CONTROL_TEXT_LOCAL_ONLY'}:
        return None
    if not local_refusal and not re.search(r'(?:作业|课堂).*(?:改|补|漏)|(?:改|补|漏|老师说).*(?:作业|课堂)', transcript):
        return None
    if re.search(r'[“”「」『』"]|(?:他说|她说|老师说|假如|假设|如果)', transcript):
        return ('这里有转述、引用或假设，我还没有把它当作你的修改决定，也没有改记录。'
                '请用自己的话说这次要补录还是修改：补录请说科目、作业内容和预计几分钟；'
                '修改请说原来哪一项、哪处改成什么。之后我会读回给你确认。')
    return ('这次还没有形成可确认的修改，记录和进度都保留。'
            '补录作业请把科目、内容和预计几分钟一起说一遍，我不会猜时长；'
            '修改请说原来哪一项、哪处改成什么。读回后再由你确认。')
