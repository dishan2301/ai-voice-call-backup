class VoiceCapture extends AudioWorkletProcessor {
 constructor(){super();this.samples=new Int16Array(2048);this.used=0;}
 process(inputs){const input=inputs[0]?.[0];if(input)for(const value of input){this.samples[this.used++]=Math.max(-32768,Math.min(32767,Math.round(value*32767)));if(this.used===this.samples.length){const packet=this.samples;this.port.postMessage(packet.buffer,[packet.buffer]);this.samples=new Int16Array(2048);this.used=0;}}return true;}
}
registerProcessor('voice-capture',VoiceCapture);
