'use strict';
const status=document.getElementById('status'),start=document.getElementById('start'),stop=document.getElementById('stop');
let socket,context,stream,capture,source,mutedGain,nextTime=0,playing=new Set(),busy=false;
function clearAudio(){for(const node of playing){try{node.stop();}catch{}}playing.clear();nextTime=context?context.currentTime:0;}
async function end(){if(!busy)return;busy=false;start.disabled=false;stop.disabled=true;clearAudio();if(capture)capture.disconnect();if(source)source.disconnect();if(mutedGain)mutedGain.disconnect();if(stream)stream.getTracks().forEach(t=>t.stop());if(socket){socket.onclose=null;socket.close();}if(context)await context.close();context=null;status.textContent='Call ended.';}
start.onclick=async()=>{
 if(busy)return;busy=true;start.disabled=true;stop.disabled=false;status.textContent='Connecting…';
 try{
  stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
  context=new AudioContext();await context.resume();nextTime=context.currentTime;
  socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/talk/ws');socket.binaryType='arraybuffer';
  socket.onopen=()=>socket.send(JSON.stringify({token:document.getElementById('code').value,sample_rate:context.sampleRate}));
  socket.onmessage=async event=>{
   if(typeof event.data==='string'){
    const message=JSON.parse(event.data);
    if(message.type==='ready'){
     status.textContent=message.mail_connected?'Connected to Outlook. Ask your question.':'Connected. Say hello! Outlook is not connected for this test.';
     await context.audioWorklet.addModule('/voice-capture.js');
     source=context.createMediaStreamSource(stream);capture=new AudioWorkletNode(context,'voice-capture');
     mutedGain=context.createGain();mutedGain.gain.value=0;
     capture.port.onmessage=e=>{if(socket.readyState===WebSocket.OPEN&&socket.bufferedAmount<131072)socket.send(e.data);};
     source.connect(capture);capture.connect(mutedGain);mutedGain.connect(context.destination);
    }else if(message.type==='clear_audio')clearAudio();
    else if(message.type==='error'||message.type==='session.error'){const text=message.message;await end();status.textContent=text;}
    else if(message.type==='voice.provider.status')status.textContent='Recovering the voice connection…';
    return;
   }
   if(!context)return;
   const pcm=new Int16Array(event.data),buffer=context.createBuffer(1,pcm.length,24000),channel=buffer.getChannelData(0);
   for(let i=0;i<pcm.length;i++)channel[i]=pcm[i]/32768;
   if(nextTime-context.currentTime>10)clearAudio();
   const node=context.createBufferSource();node.buffer=buffer;node.connect(context.destination);playing.add(node);node.onended=()=>playing.delete(node);
   nextTime=Math.max(nextTime,context.currentTime+.025);node.start(nextTime);nextTime+=buffer.duration;
  };
  socket.onclose=async()=>{if(busy){await end();status.textContent='Connection closed. Tap Start call to reconnect.';}};
  socket.onerror=()=>{status.textContent='Could not connect. Check the access code and server status.';};
 }catch(error){await end();status.textContent='Microphone or voice connection unavailable. Allow microphone access and try again.';}
};
stop.onclick=end;
document.addEventListener('visibilitychange',()=>{if(document.hidden)end();});
