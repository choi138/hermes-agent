"""Conservative intake categories; only authenticated messages reach this boundary."""
import re

_STATUS = re.compile(
    r'(?i)^(?:status(?: please)?|progress(?: please)?|done\??|are (?:you|we) done\??|'
    r'is (?:it|this) (?:done|complete)\??|what(?:\x27s| is) (?:the )?(?:status|progress)\??|'
    r'how (?:far|is (?:it|the (?:task|work)) going)\??|'
    r'완료(?:했어|됐어|되었어|했나|됐나)?\??|끝(?:났어|났나)?\??|'
    r'어디까지(?: 했어| 됐어)?\??|진행(?: 상황|상태)(?: 알려줘)?\??|상태(?: 알려줘)?\??)$')
_ACK = re.compile(r'(?i)^(?:ok(?:ay)?|yes|no|thanks|thank you|stop|pause|cancel|'
                  r'네|넵|응|ㅇㅇ|알겠어|고마워|중단|멈춰|취소)[.! ]*$')
_ADD = re.compile(r'(?i)\b(?:also|additionally|must|should|ensure|keep|without|include|add)\b|'
                  r'해줘|해주|해야|남겨줘|넣어줘|추가|되어야|돼야|하지 마|금지|유지해|보장해')


def task_status(text):
    if not isinstance(text, str):
        return False
    value = text.strip()
    if _STATUS.fullmatch(value):
        return True
    if re.search(r'(?i)please|can you|could you|해줘|해주|하라', value):
        return False
    return bool(re.search(r'(?i)\b(?:tests?|qa|task|work)\b|테스트|검증|작업|진행', value)
        and re.search(r'(?i)\b(?:done|complete|completed|finished|pass|passed|status|progress)\b|끝났|완료|통과|어디까지', value)
        and ('?' in value or re.search(r'(?i)^(?:are|is|did|have|has|what|how)\b', value)))


def adds_requirement(text, parent=None):
    from gateway.task_completion import coding_request, _QUESTION
    if not isinstance(text, str) or not text.strip() or task_status(text) or _ACK.fullmatch(text.strip()):
        return False
    if coding_request(text):
        return True
    if not parent or text.lstrip().startswith('/'):
        return False
    # Explicit additions/constraints, including imperative questions, extend scope.
    # Unknown substantive declarative followups are retained conservatively; ordinary
    # explanatory questions remain conversational. No prior requirement is removed.
    return bool(_ADD.search(text) or ('?' not in text and not _QUESTION.search(text)))


def source_context(parent, context=None):
    import json
    sources = parent['contract']['sources']
    context = dict(context or {})
    manifest = dict(context.get('manifest') or {}, parent_sources=sources)
    suffix = '\n\nAuthenticated parent requirement sources:\n' + json.dumps(
        sources, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return dict(context, manifest=manifest, text=str(context.get('text') or '') + suffix)
