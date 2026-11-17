"""Bounded intent-aware recall and source-supported decisions, not similarity confidence."""
import re
from app.retrieval.query import SearchSpec

# Grammar words only: entity/domain vocabulary stays open and model-planned.
STOP = set('find show search email emails mail mails please the a an about regarding for of me my what which is are was were has have did does do tell read newest latest it that this from to and with'.split())
STOP.update('બતાવો શોધો ઇમેઇલ મેઇલ ના ની નું મને શું છે વિશે दिखाओ खोजो ईमेल मेल का की के में क्या है बताओ wala wale dikhao batao ke ka ki mein'.split())
STATUS = {'approved','paid','confirmed','delivered','resolved','accepted'}


def terms(text):
    return list(dict.fromkeys(word.strip('\"\'.:()').casefold() for word in re.findall(r'[^\s,;!?।]+',text)
                             if word.casefold() not in STOP))


def plan(spec):
    """Broaden wording only. Every immutable hard filter survives every plan."""
    tokens=terms(spec.text)
    variants=[spec.text]
    if tokens:
        variants.append(' '.join(tokens))
        # Disjoint clues recover older sources missed by strict conjunctions.
        variants.extend(sorted(tokens,key=len,reverse=True))
    return [spec.model_copy(update={'text':value,'limit':20}) for value in dict.fromkeys(variants)][:4]


def source_text(row):
    return ' '.join(str(row.get(key) or '') for key in ('subject','sender','body'))


def negated_status(text, status):
    return bool(re.search(r'\b(?:not|never|no|unpaid|unconfirmed)\b[^.!?\n]{0,35}\b'+re.escape(status)+r'\b',text,re.I)
                or status=='paid' and re.search(r'\bunpaid\b',text,re.I))


def verify_answer(answer, evidence):
    """Reject a positive status assertion when current sources only negate it.

    This is a narrow contradiction guard, not universal semantic entailment.
    """
    rows=list(evidence.values()) if isinstance(evidence,dict) else evidence
    for status in STATUS:
        if re.search(r'\b'+status+r'\b',answer,re.I) and not negated_status(answer,status):
            mentions=[source_text(row) for row in rows if re.search(r'\b'+status+r'\b',source_text(row),re.I)]
            if mentions and all(negated_status(text,status) for text in mentions):
                return False
    return True


class AdaptiveRetrieval:
    def __init__(self, index, *, recover_scoped=True):
        self.index=index
        self.recover_scoped=recover_scoped

    def search(self, spec: SearchSpec, provider, *, request=''):
        clues=terms(spec.text)
        action=bool(re.search(r'\b(reply|draft|respond|read it)\b|જવાબ|जवाब',request,re.I))
        if not clues and not any(getattr(spec,key) for key in ('sender','recipient','subject','thread_id','message_id','company','contact','start','end')) and spec.unread is None and spec.has_attachments is None:
            return self.clarify([], 'Which sender, subject or time window should I check?', True, [])
        rows={};runs=[];complete=True;failed=False
        anchored=any(getattr(spec,key) for key in ('sender','recipient','thread_id','message_id','company','contact'))
        variants=plan(spec)
        # Reserve one bounded attempt for originals within an explicit identity scope.
        if anchored and self.recover_scoped:
            variants=variants[:3]
        for variant in variants:
            result=self.index.search(variant,provider)
            runs.append({'text':variant.text,'count':len(result.get('messages',[]))})
            complete=complete and result.get('completeness',{}).get('complete',False)
            if result.get('pending_question'):
                return result
            if result.get('error'):
                failed=True
            failed=failed or bool(result.get('completeness',{}).get('verification_failures'))
            for row in result.get('messages',[]):
                # The index contract requires original-provider verification.
                if row.get('provider_verified'):
                    rows.setdefault(row['message_id'],row)
        rescue=False
        if self.recover_scoped and anchored and clues and not any(set(clues)<=set(terms(source_text(row))) for row in rows.values()):
            result=self.index.search(spec.model_copy(update={'text':'','limit':20}),provider)
            runs.append({'text':'','count':len(result.get('messages',[])),'purpose':'identity_scoped_recovery'})
            complete=complete and result.get('completeness',{}).get('complete',False)
            failed=failed or bool(result.get('error') or result.get('completeness',{}).get('verification_failures'))
            if result.get('pending_question'):
                return result
            for row in result.get('messages',[]):
                if row.get('provider_verified'):
                    rows.setdefault(row['message_id'],row)
            rescue=True
        ranked=[]
        for row in rows.values():
            hay=source_text(row).casefold()
            matched=[term for term in clues if term in terms(hay)]
            if clues and not matched and not rescue:
                continue
            subject=set(terms(str(row.get('subject',''))))
            score=(len(matched),sum(term in subject for term in matched))
            ranked.append((score,str(row.get('received_at','')),str(row['message_id']),
                           {**row,'query_evidence':{'matched_terms':matched,'missing_terms':[t for t in clues if t not in matched]}}))
        ranked.sort(key=lambda entry:(-entry[0][0],-entry[0][1],entry[2]))
        supported=[row for _,_,_,row in ranked if not row['query_evidence']['missing_terms']]
        candidates=[row for _,_,_,row in ranked][:spec.limit]
        if not supported:
            question=('I found partial clues. Which source do you mean?' if candidates else
                      'I could not verify a matching email. Can you give a sender or subject?')
            return self.clarify(candidates,question,complete and not failed,runs)
        if action and len({row['thread_id'] for row in supported})>1 and not spec.thread_id and not spec.message_id:
            return self.clarify(supported[:spec.limit],'Which email conversation should I use?',complete and not failed,runs)
        result_rows=supported[:spec.limit]
        return {'messages':result_rows,'retrieval_backend':'postgres','decision':'ANSWER',
                'search_attempts':runs,'completeness':{'complete':complete and not failed and len(supported)<=spec.limit,
                                                     'verification_failed':failed}}

    @staticmethod
    def clarify(candidates,question,complete,runs):
        choices=list(dict.fromkeys(str(row.get('subject','')) for row in candidates))[:3]
        if choices:
            question=question.rstrip('?')+' '+ '; '.join(choices)+'?'
        return {'messages':[],'candidate_choices':[{'message_id':row['message_id'],'thread_id':row['thread_id'],
                    'subject':row.get('subject',''),'sender':row.get('sender','')} for row in candidates],
                'pending_question':question,'error':question,'decision':'CLARIFY','retrieval_backend':'postgres',
                'search_attempts':runs,'completeness':{'complete':complete}}
