"""A first hint stops at its first child check, never assumes a response.

Only a violating response is projected to a new synchronized local board.
Normal structured presentations and explicit higher-level help stay intact.
This pacing boundary is not a semantic correctness or mastery certificate.
"""
from .tutor_blackboard import build_degraded_presentation


def bound_first_check(answer, presentation, subject, hint_level):
    if hint_level != 1:
        return answer, presentation
    closing = {'“': '”', '‘': '’', '「': '」', '『': '』', '"': '"'}
    quote = None
    for i, char in enumerate(answer):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in closing:
            quote = closing[char]
        elif char in '?？' and answer[i + 1:].strip():
            prefix = answer[:i + 1]
            # Later computations can be present even in the first model board
            # block. Rebuild both channels from the same retained narration.
            return prefix, build_degraded_presentation(prefix, subject, hint_level, 'first_check_boundary')
        elif char in '?？':
            break
    return answer, presentation
