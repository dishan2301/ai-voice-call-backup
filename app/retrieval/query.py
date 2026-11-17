"""Validated retrieval constraints; no model can replace mailbox authorization."""
from datetime import datetime, timezone, timedelta
import math
import re
import shlex
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearchSpec(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    text: str = Field(default='', max_length=512)
    sender: str = ''
    recipient: str = ''
    subject: str = ''
    start: datetime | None = None
    end: datetime | None = None  # half-open
    thread_id: str = ''
    message_id: str = ''
    company: str = ''
    contact: str = ''
    unread: bool | None = None
    has_attachments: bool | None = None
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator('sender', 'recipient')
    @classmethod
    def exact_address(cls, value):
        if value and not re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+', value):
            raise ValueError('Use an exact email address or resolve a contact first')
        return value.casefold()

    @model_validator(mode='after')
    def dates(self):
        if any(value and value.tzinfo is None for value in [self.start, self.end]):
            raise ValueError('Date bounds require a timezone')
        if self.start and self.end and self.start >= self.end:
            raise ValueError('End must follow start')
        return self


def from_arguments(args, tz):
    values = {k: args[k] for k in SearchSpec.model_fields if k in args}
    values['limit'] = min(20, max(1, int(args.get('limit', 10))))
    for key, target in [('start_date', 'start'), ('end_date', 'end')]:
        if args.get(key):
            dt = datetime.fromisoformat(str(args[key]))
            values[target] = dt.replace(tzinfo=tz) if dt.tzinfo is None else dt
    if args.get('date_range'):
        day = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if args['date_range'] == 'today':
            values.update(start=day, end=day+timedelta(days=1))
        elif args['date_range'] == 'yesterday':
            values.update(start=day-timedelta(days=1), end=day)
        else:
            raise ValueError('Use explicit timezone-aware bounds for this date range')
    if args.get('query'):
        terms = []
        for token in shlex.split(str(args['query'])):
            key, sep, value = token.partition(':')
            if not sep:
                if any(c in token for c in '{}()') or token in {'OR', 'AND', 'NOT'} or token.startswith('-'):
                    raise ValueError('Unsupported index query; use original provider search')
                terms.append(token)
            elif key in {'from', 'to', 'subject'}:
                values[{'from':'sender','to':'recipient','subject':'subject'}[key]] = value
            elif key in {'after','before'}:
                dt = datetime.fromtimestamp(int(value), timezone.utc) if value.isdigit() else datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    dt=dt.replace(tzinfo=tz)
                values['start' if key=='after' else 'end'] = dt
            elif (key,value) in {('is','unread'),('has','attachment')}:
                values['unread' if key=='is' else 'has_attachments'] = True
            else:
                raise ValueError('Unsupported index operator; use original provider search')
        values['text'] = ' '.join(terms)
    return SearchSpec(**values)


def fuse(*rankings, k=60):
    scores = {}
    for ranking in rankings:
        for rank, mid in enumerate(dict.fromkeys(ranking), 1):
            scores[mid] = scores.get(mid, 0) + 1/(k+rank)
    return sorted(scores, key=lambda mid: (-scores[mid], mid))


def vector_literal(values):
    if len(values) != 384 or any(not math.isfinite(float(v)) for v in values):
        raise ValueError('Embedding must contain 384 finite values')
    if not any(float(v) for v in values):
        raise ValueError('Zero embedding is invalid')
    return '[' + ','.join(str(float(v)) for v in values) + ']'
