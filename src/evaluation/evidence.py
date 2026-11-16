"""Project exact model-visible tool messages for evidence-grounded judging."""

import hashlib


def visible_tool_evidence(messages: list[dict]) -> list[dict]:
    evidence = []
    for message in messages:
        content = message.get('content') or ''
        if content.startswith('[UNTRUSTED_TOOL_RESULT'):
            evidence.append({
                'document_id': 'visible-' + hashlib.sha256(content.encode()).hexdigest()[:16],
                'title': 'Model-visible tool evidence', 'text': content,
            })
    return evidence
