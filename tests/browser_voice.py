"""Run explicitly: .venv/bin/python tests/browser_voice.py (Playwright Chromium)."""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
DRAFT = {"id": 7, "to": "client@example.com", "subject": "Update", "body": "Thank you.\nPlease share the error code."}
READBACK = "To: client@example.com\nSubject: Update\n\nThank you.\nPlease share the error code."


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page(viewport={"width": 430, "height": 900})
        errors, requests = [], []
        offline = False
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.add_init_script("""
          window.stopped = false;
          window.micCalls = 0;
          window.Audio = class {
            constructor() {window.testAudio=this; this.paused=true; this.plays=0;}
            play() {this.paused=false; this.plays++; return Promise.resolve();}
            pause() {this.paused=true;}
          };
          window.MediaStream = class {constructor(tracks) {this.tracks=tracks;}};
          Object.defineProperty(window, 'speechSynthesis', {value: {
            cancel() {}, resume() {}, speak(value) {window.lastSpeech = value;}
          }});
          navigator.mediaDevices.getUserMedia = async () => {
            window.micCalls++;
            if (window.retryMic && window.micCalls === 1) {
              const error = Error('Could not start audio source'); error.name = 'NotReadableError'; throw error;
            }
            if (window.denyMic) throw Error('Microphone permission denied');
            const track = {enabled:true, stop(){window.stopped=true;}};
            return {getTracks:()=>[track], getAudioTracks:()=>[track]};
          };
          window.RTCPeerConnection = class {
            constructor() {window.testPeer=this; this.iceGatheringState='complete';}
            createDataChannel() {this.channel={readyState:'open',send(data){window.lastChannelMessage=JSON.parse(data);}}; return this.channel;}
            addTrack() {} createOffer() {return {sdp:'test',type:'offer'};}
            setLocalDescription(d) {this.localDescription=d;}
            setRemoteDescription() {this.channel.onopen();}
            close() {} addEventListener() {}
          };
        """)

        async def route(request):
            path = request.request.url.split('localhost:8765')[-1].split('?')[0]
            if path == '/phone/preview':
                return await request.fulfill(path=ROOT / 'app/static/phone-preview.html', content_type='text/html')
            if path == '/dashboard-test':
                return await request.fulfill(body='''<!doctype html><section class="assistant" data-voice-rate="165" data-voice-volume="1">
                  <button id="voice-toggle"></button><button id="voice-stop"></button><p id="voice-status"></p>
                  <form id="mail-question"><input id="mail-question-text"><button>Ask assistant</button></form>
                  <div id="mail-answer"></div><div id="mail-evidence"></div></section>
                  <script>window.SpeechRecognition=class {constructor(){window.testRecognition=this;} start(){} stop(){}};</script>
                  <script src="/static/dashboard.js"></script>''', content_type='text/html')
            if path.startswith('/static/'):
                return await request.fulfill(path=ROOT / 'app' / path.lstrip('/'), content_type='application/javascript')
            payload = request.request.post_data_json if request.request.method == 'POST' else {}
            requests.append((path, payload))
            result = {}
            if path == '/phone/daily/schedule':
                result = {'enabled': False, 'time': '09:00', 'timezone': 'Asia/Kolkata', 'vip_senders': []}
            elif path == '/phone/daily/runs':
                result = {'runs': [{'id': 'b'*32, 'status': 'COMPLETE'}]}
            elif path == '/phone/daily/runs/' + 'b'*32 + '/open':
                result = {'id': 'b'*32, 'status': 'COMPLETE', 'generated_at': '2026-11-05T03:30:00Z',
                    'stale': True, 'document': {'items': [{'message_id': 'm1', 'thread_id': 't1', 'sender': 'Supplier',
                    'subject': 'Saved daily source', 'summary': 'Please confirm the meeting.'}], 'calendar': []}}
            elif path == '/phone/briefing':
                if offline:
                    return await request.fulfill(status=503, json={"detail": "offline"})
                result = {"spoken_brief": "Two mails need you.", "generated_at": "2026-09-16T08:40:00Z",
                          "total_emails": 2, "critical_count": 0, "high_count": 2, "reply_count": 1,
                          "items": [{"importance": "HIGH", "section": "Replies", "sender": "Zydus",
                                     "subject": "Meeting", "summary": "Confirm attendance", "message_id": "m1", "thread_id": "t1"}]}
            elif path == '/phone/whatsapp/rewrite':
                assert set(payload) == {'message', 'instruction'}
                if 'shorter' in payload['instruction']:
                    return await request.fulfill(status=503, json={'detail': 'Gemini unavailable'})
                result = {'message': 'Meeting is tomorrow.'}
            elif path == '/phone/recent':
                result = {"count": 1, "emails": [{"importance": "MEDIUM", "sender": "Zydus",
                          "subject": "Software Demo Meeting", "summary": "Choose a demo time.",
                          "received_at": "2026-09-17T07:04:04Z", "message_id": "m4", "thread_id": "t4"}]}
            elif path == '/phone/commitments':
                result = {"count": 1, "you_owe": 0, "they_owe": 1,
                          "spoken_answer": "You owe zero. Others owe you one.",
                          "commitments": [{"direction": "THEY_OWE", "sender": "Zydus",
                                           "subject": "Quotation", "commitment": "We will send it by Friday.",
                                           "due": "by Friday", "message_id": "m2"}]}
            elif path == '/phone/waiting':
                result = {"count": 1, "days": 3, "spoken_answer": "One conversation is waiting for a reply.",
                          "threads": [{"subject": "Quotation follow-up", "last_message_at": "2026-09-12",
                                       "message_id": "m3"}]}
            elif path == '/phone/resume': result = {"state": "EMPTY", "answer": "No interrupted work is waiting."}
            elif path == '/phone/voice/offer': result = {"type":"answer", "sdp":"test", "session_id":"rtc-test"}
            elif path.endswith('/review'): result = {"draft_id":7, "review_ticket":"review-test", "readback":READBACK}
            elif path.endswith('/review-complete'): result = {"state":"CONFIRMING"}
            elif path == '/phone/planner/confirm': result = {'state': 'DONE', 'answer': 'The reviewed operation completed.'}
            elif path == '/phone/planner/ask':
                result = {'state': 'REVIEW', 'action_id': 'planner-test', 'confirmation_token': 'planner-token',
                          'answer': 'Review the exact details.', 'details': {'summary': 'Synthetic Calendar meeting',
                          'start_at': '2026-12-01T10:00:00+05:30', 'end_at': '2026-12-01T11:00:00+05:30'}}
            elif path == '/phone/agent':
                if 'calendar' in payload.get('message', '').lower():
                    result = {'state': 'REVIEW', 'action_id': 'planner-test', 'confirmation_token': 'planner-token',
                              'answer': 'Review the exact details.', 'details': {'summary': 'Synthetic Calendar meeting',
                              'start_at': '2026-12-01T10:00:00+05:30', 'end_at': '2026-12-01T11:00:00+05:30'}}
                else: result = {"answer":"Sent", "spoken_answer":"The email was sent.", "state":"SENT"}
            await request.fulfill(json=result)

        await page.route('**/*', route)
        await page.goto('http://localhost:8765/phone/preview')
        await page.wait_for_selector('#cards a')
        assert '2026' in await page.locator('#status').inner_text()
        assert await page.locator('#cards [data-reply]').count() == 1
        await page.evaluate("window.mailVoiceTextResult('any mail afro zydus')")
        await page.wait_for_function("document.querySelector('#answer').textContent==='Sent'")
        spoken_query = next(body for path, body in requests if path == '/phone/agent')
        assert spoken_query['input_mode'] == 'voice' and spoken_query['message'] == 'any mail afro zydus'
        await page.fill('#message', 'What does the latest email say?')
        await page.click('#submit')
        await page.wait_for_function("document.querySelector('#answer').textContent==='Sent'")
        assert any(path == '/phone/agent' and body.get('message') == 'What does the latest email say?'
                   and body.get('input_mode') == 'text' for path, body in requests)
        assert not await page.locator('#cards [data-reply]').is_visible()
        await page.click('#priority-inbox summary')
        await page.click('#cards [data-reply]')
        await page.wait_for_function("document.querySelector('#answer').textContent.includes('What should the reply say?')")
        assert await page.locator('#submit').inner_text() == 'Prepare reply draft'
        assert 'Reply instructions' in await page.locator('label[for="message"]').inner_text()
        await page.fill('#message', 'Tell them I can meet at 5 pm.')
        await page.click('#submit')
        await page.wait_for_function("document.querySelector('#answer').textContent==='Sent'")
        reply_request = next(body for path, body in requests if path == '/phone/agent' and body.get('reply_message_id'))
        assert reply_request['reply_message_id'] == 'm1' and reply_request['reply_thread_id'] == 't1'
        assert await page.locator('#submit').inner_text() == 'Ask assistant'
        await page.locator('details.tools').locator('summary').click()
        await page.click('#recent')
        await page.wait_for_function("document.querySelector('#answer').textContent.includes('Showing 1')")
        assert 'Software Demo Meeting' in await page.locator('#evidence').inner_text()
        assert '12:34' in await page.locator('#evidence').inner_text()
        assert 'IST' in await page.locator('#evidence').inner_text()
        offline = True
        await page.reload()
        await page.wait_for_function("document.querySelector('#status').textContent.includes('Offline')")
        assert await page.locator('#cards').inner_text() and 'Meeting' in await page.locator('#cards').inner_text()
        offline = False
        assert await page.locator('#ask').inner_text() and 'Gemini' in await page.locator('#ask').inner_text()
        await page.locator('details.tools').locator('summary').click()
        await page.click('#commitments')
        await page.wait_for_function("document.querySelector('#answer').textContent.includes('from others')")
        assert 'We will send it by Friday' in await page.locator('#evidence').inner_text()
        await page.click('#waiting')
        await page.wait_for_function("document.querySelector('#answer').textContent.includes('no reply')")
        assert 'Quotation follow-up' in await page.locator('#evidence').inner_text()
        await page.evaluate('window.retryMic = true')
        await page.click('#ask')
        await page.wait_for_function("document.querySelector('#voice-status').textContent==='listening'")
        assert await page.evaluate('window.micCalls') == 2
        await page.evaluate("window.testPeer.channel.onmessage({data:JSON.stringify({type:'transcript.partial',text:'meeting mail'})})")
        assert await page.locator('#transcript').inner_text() == 'meeting mail'
        await page.evaluate('window.lastSpeech = null')
        await page.evaluate("window.testPeer.channel.onmessage({data:JSON.stringify({type:'mail.result',result:{answer:'One meeting email',spoken_answer:'I found one meeting email. It needs your reply.',evidence:[{message_id:'m1',subject:'Meeting',sender:'Zydus'}]}})})")
        assert await page.locator('#evidence .card').count() == 1
        assert await page.locator('#evidence .card').is_visible()
        assert not await page.locator('#cards [data-reply]').is_visible()
        await page.evaluate("window.testPeer.ontrack({track:{kind:'audio'}})")
        assert await page.evaluate('window.lastSpeech === null && !window.testAudio.paused && !window.testAudio.muted && window.testAudio.plays === 1')
        # Browser speech remains a fallback only if Live audio generation fails.
        await page.evaluate("window.testPeer.channel.onmessage({data:JSON.stringify({type:'speech.fallback',text:'I found one meeting email. It needs your reply.'})})")
        assert await page.evaluate('window.lastSpeech.text') == 'I found one meeting email.'
        assert await page.evaluate('window.testAudio.paused && window.testAudio.muted && window.lastSpeech.volume === 1')
        # A late RTC track must not reclaim playback in the middle of local speech.
        await page.evaluate("window.testPeer.ontrack({track:{kind:'audio'}})")
        assert await page.evaluate('window.testAudio.paused && window.testAudio.muted')
        await page.evaluate('window.lastSpeech.onend()')
        assert await page.evaluate('window.lastSpeech.text') == 'It needs your reply.'
        assert await page.evaluate('window.testAudio.paused && window.testAudio.muted')
        await page.evaluate('window.lastSpeech.onend()')
        assert await page.evaluate('!window.testAudio.paused && !window.testAudio.muted')
        await page.evaluate("window.testPeer.channel.onmessage({data:JSON.stringify({type:'speech.fallback',text:'Playback failure.'})})")
        await page.evaluate('window.lastSpeech.onerror()')
        assert await page.evaluate('!window.testAudio.muted')
        result = {"type":"mail.result", "result":{"answer":"Draft ready", "draft":DRAFT,
                  "evidence":[{"message_id":"m1","subject":"Meeting","sender":"Zydus"}]}}
        await page.evaluate('(event)=>window.testPeer.channel.onmessage({data:JSON.stringify(event)})', result)
        await page.click('#readback')
        await page.wait_for_function('window.lastSpeech && window.lastSpeech.text.startsWith("To:")')
        assert await page.evaluate('window.lastSpeech.text') == 'To: client@example.com\nSubject: Update\n\nThank you.'
        await page.evaluate('window.lastSpeech.onend()')
        await page.wait_for_function('window.lastSpeech && window.lastSpeech.text.startsWith("Please share")')
        assert await page.evaluate('window.lastSpeech.text') == 'Please share the error code.'
        assert await page.evaluate('window.testAudio.paused && window.testAudio.muted')
        assert not any(path.endswith('review-complete') for path, _ in requests)
        await page.evaluate('window.lastSpeech.onend()')
        await page.wait_for_function("document.querySelector('#voice-status').textContent.includes('Read-back complete')")
        assert await page.evaluate('!window.testAudio.muted')
        await page.fill('#message', 'done')
        await page.click('#submit')
        await page.wait_for_function("document.querySelector('#answer').textContent==='Sent'")
        assert next(body for path, body in requests if path == '/phone/agent' and body.get('message') == 'done')['review_ticket'] == 'review-test'
        await page.click('#stop')
        assert await page.evaluate('window.stopped')
        await page.evaluate("window.mailAppLifecycle('background')")
        assert 'background' in (await page.locator('#voice-status').inner_text()).lower()
        await page.evaluate("window.mailAppLifecycle('foreground')")
        assert 'tap ask' in (await page.locator('#voice-status').inner_text()).lower()
        await page.evaluate('window.denyMic = true')
        await page.click('#ask')
        await page.wait_for_function("document.querySelector('#voice-status').textContent.includes('permission was denied')")
        assert await page.locator('#message').is_enabled()
        await page.click('#priority-inbox summary')
        await page.click('#cards [data-reply]')
        await page.evaluate("""window.MorningMailSpeech = {
          listen() { window.nativeListenCalled = true;
            window.mailVoiceTextResult('Confirm the 5 pm meeting', [{text:'Confirm the 5 pm meeting', confidence:0.91}]); },
          stop() {}, speak(_text, id) { window.mailSpeechComplete(id, true); }
        };""")
        await page.click('#ask')
        await page.wait_for_function('window.nativeListenCalled && document.querySelector("#answer").textContent === "Sent"')
        native_reply = [body for path, body in requests if path == '/phone/agent'][-1]
        assert native_reply['reply_message_id'] == 'm1' and native_reply['reply_thread_id'] == 't1'
        assert native_reply['alternatives'][0]['confidence'] == 0.91
        await page.evaluate("""window.MorningMailContacts = {
          search(_query, id) { window.mailContactsResult(id, 'ok', [
            {name:'Priya Shah', number:'+919111111111', exact:false},
            {name:'Priya Patel', number:'+919222222222', exact:false}]); },
          openWhatsApp(number, message, id) { window.waHandoff = {number, message}; window.mailWhatsAppResult(id, 'chat_opened'); }
        };""")
        await page.fill('#message', 'send a WhatsApp message to Priya saying hello')
        await page.click('#submit')
        assert await page.locator('[data-wa-index]').count() == 2
        await page.locator('[data-wa-index="1"]').click()
        assert 'Priya Patel' in await page.locator('#answer').inner_text()
        await page.fill('#message', 'tell them meeting is tomorrow')
        await page.click('#submit')
        await page.fill('#message', 'make it professional')
        await page.click('#submit')
        await page.wait_for_function("document.querySelector('#answer').textContent.includes('Review the revised message')")
        assert await page.evaluate("JSON.parse(sessionStorage.getItem('whatsapp-draft')).reviewed === ''")
        assert await page.evaluate("JSON.parse(sessionStorage.getItem('whatsapp-draft')).number === '+919222222222'")
        await page.fill('#message', 'make it shorter')
        await page.click('#submit')
        await page.wait_for_function("document.querySelector('#answer').textContent.includes('original draft is unchanged')")
        assert await page.evaluate("JSON.parse(sessionStorage.getItem('whatsapp-draft')).message === 'Meeting is tomorrow.'")
        assert await page.evaluate('!window.waHandoff')
        await page.fill('#message', 'send now')
        await page.click('#submit')
        assert 'Review the exact' in await page.locator('#answer').inner_text()
        await page.fill('#message', 'read it')
        await page.click('#submit')
        await page.fill('#message', 'send now')
        await page.click('#submit')
        assert await page.evaluate("window.waHandoff.number === '+919222222222'")
        assert await page.evaluate("window.waHandoff.message === 'Meeting is tomorrow.'")
        assert 'delivery is not confirmed' in await page.locator('#answer').inner_text()
        await page.click('[data-recent]')
        await page.wait_for_function("document.querySelector('[data-sources]').textContent.includes('Saved daily source')")
        assert 'stale' in await page.locator('section:has([data-recent]) [role=status]').inner_text()
        await page.evaluate('window.denyMic = false')
        await page.click('[data-play]')
        await page.wait_for_function("window.lastChannelMessage && window.lastChannelMessage.type === 'briefing.play'")
        assert await page.evaluate("window.lastChannelMessage.run_id === 'b'.repeat(32)")
        await page.fill('#message', 'create calendar meeting on December 1 at 10 to 11 IST')
        await page.click('#submit')
        await page.wait_for_selector('[data-confirm]:not([hidden])')
        assert 'Synthetic Calendar meeting' in await page.locator('section pre').inner_text()
        assert not any(path == '/phone/planner/confirm' for path, _ in requests)
        await page.click('[data-confirm]')
        await page.wait_for_function("document.querySelector('section [role=status]').textContent.includes('completed')")
        confirmation = next(body for path, body in requests if path == '/phone/planner/confirm')
        assert confirmation['confirmation_token'] == 'planner-token'
        assert confirmation['confirmation'] == 'confirm'
        await page.goto('http://localhost:8765/dashboard-test')
        await page.fill('#mail-question-text', 'Which customer is waiting?')
        await page.click('#mail-question button')
        await page.wait_for_function("document.querySelector('#mail-answer').textContent==='Sent'")
        assert any(path == '/phone/agent' and body.get('message') == 'Which customer is waiting?'
                   and body.get('input_mode') == 'text' for path, body in requests)
        await page.click('#voice-toggle')
        await page.evaluate("window.testRecognition.onresult({results:[[{transcript:'Hello Google, any mail from Zydus?'}]]})")
        await page.wait_for_function("document.querySelector('#mail-answer').textContent==='Sent'")
        assert any(path == '/phone/agent' and body.get('message') == 'any mail from Zydus?'
                   and body.get('input_mode') == 'voice' for path, body in requests)
        assert not errors, errors
        await browser.close()
        print('Phone voice/drafts and desktop Gemini question flow passed browser checks.')


if __name__ == '__main__': asyncio.run(main())
