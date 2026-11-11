"""Versioned long-horizon workload; oracle metadata never enters role prompts."""

from dataclasses import asdict, dataclass
import hashlib
import json
import random
import re

VERSION = 'integrated-memory-v1'


@dataclass(frozen=True)
class Event:
    step: int
    observation: str
    subject: str
    predicate: str
    expected: tuple[str, ...]
    category: str
    source_steps: tuple[int, ...]
    recall_gap: int | None
    pin_subject: str | None = None

    @property
    def task(self):
        priority = '' if self.pin_subject is None else f' Critical: preserve {self.pin_subject}.'
        return (f'Observation: {self.observation or "none"}{priority}\n'
                f'Question: current {self.predicate} values for {self.subject}? '
                'In content write one sentence per value, using the subject name, predicate, '
                'actual value, and a period. Write only Unknown. if unsupported.')

    @property
    def reference_answer(self):
        return ' '.join(f'{self.subject} {self.predicate} {v}.' for v in self.expected) or 'Unknown.'


def workload(turns=60, variant=1):
    if type(turns) is not int or not 60 <= turns <= 80:
        raise ValueError('integrated workload requires 60-80 turns')
    if type(variant) is not int or not 1 <= variant <= 8:
        raise ValueError('variant must be 1-8')
    rng = random.Random(12000 + variant)
    suffix = ''.join(rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(4))
    cedar, aurora, birch, dune, hub = [s + suffix for s in ('Cedar', 'Aurora', 'Birch', 'Dune', 'Hub')]
    ca, cb, cc = [s + suffix for s in ('CycleA', 'CycleB', 'CycleC')]
    values = ['DB' + ''.join(rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(6)) for _ in range(5)]
    a, b, c, d, e = values
    schedule = {
        1: (cedar, 'uses', a), 2: (aurora, 'uses', b), 3: (birch, 'uses', c),
        4: (dune, 'uses', d), 5: (hub, 'supports', 'FeatureA' + suffix),
        6: (hub, 'supports', 'FeatureB' + suffix), 7: (cedar, 'uses', e),
        8: (cedar, 'uses', a), 9: (ca, 'links', cb),
        10: (cb, 'links', cc), 11: (cc, 'links', ca),
    }
    questions = {1:(cedar,'uses','initial'), 2:(aurora,'uses','pinned'),
        3:(birch,'uses','initial'), 4:(dune,'uses','abandoned_initial'),
        5:(hub,'supports','multivalued'), 6:(hub,'supports','multivalued'),
        7:(cedar,'uses','update'), 8:(cedar,'uses','reversion'),
        18:(hub,'supports','multivalued'), 28:(aurora,'uses','pinned'),
        40:(birch,'uses','delayed'), 50:(aurora,'uses','pinned'),
        54:(hub,'supports','multivalued'), 56:(birch,'uses','delayed'),
        59:(ca,'links','cycle'), 60:(aurora,'uses','pinned')}
    current, sources, last_exposure, result = {}, {}, {}, []
    for step in range(1, turns + 1):
        fact = schedule.get(step)
        if fact is None and step % 4 != 0:
            fact = ('Noise' + suffix + str(step), 'uses', 'Cache' + str(rng.randrange(100000, 999999)))
        if fact:
            s, p, obj = fact
            if p == 'uses':
                current[(s,p)] = {obj}
                sources[(s,p)] = {obj:step}
            else:
                current.setdefault((s,p), set()).add(obj)
                sources.setdefault((s,p), {})[obj] = step
            last_exposure[(s,p,obj)] = step
        if step in questions:
            subject, predicate, category = questions[step]
        elif step % 10 == 0:
            subject, predicate, category = 'Unknown' + suffix, 'uses', 'unknown'
        elif step > 60 and step % 3 == 2:
            subject, predicate, category = birch, 'uses', 'delayed'
        elif step % 3 == 0 and fact:
            subject, predicate, category = fact[0], fact[1], 'immediate'
        else:
            subject, predicate, category = cedar, 'uses', 'frequent'
        expected = tuple(sorted(current.get((subject,predicate), ())))
        source_steps = tuple(sources[(subject,predicate)][v] for v in expected)
        recall_gap = None if not expected else max(step-last_exposure[(subject,predicate,v)] for v in expected)
        for value in expected:last_exposure[(subject,predicate,value)] = step
        result.append(Event(step, '' if fact is None else f'{fact[0]} {fact[1]} {fact[2]}.',
            subject, predicate, expected, category, source_steps, recall_gap, aurora if step == 2 else None))
    return tuple(result)


def records(turns=60, variant=1):
    return [asdict(e) | {'task':e.task} for e in workload(turns,variant)]


def fingerprint(turns=60, variant=1):
    return hashlib.sha256(json.dumps({'version':VERSION,'events':records(turns,variant)},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()


def evaluate(answer, event):
    """Score normalized fact sets; format validity is reported independently."""
    text = ' '.join(answer.split())
    if text.casefold() == 'unknown.':
        return True, not event.expected
    pattern = re.compile(re.escape(event.subject) + r'\s+' + re.escape(event.predicate) + r'\s+([A-Za-z0-9]+)\.', re.I)
    matches = list(pattern.finditer(text))
    residue = pattern.sub('',text).strip()
    valid = bool(matches) and not residue
    actual = {m[1].casefold() for m in matches}
    return valid, valid and actual == {v.casefold() for v in event.expected}


def role_at(step):
    role = ('planner','researcher','verifier')[(step-1)%3]
    return role, 'node2' if role == 'researcher' else 'node1'
