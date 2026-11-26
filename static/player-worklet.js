"use strict";

const CAPACITY_SECONDS = 10;

/**
 * Continuous stereo output fed from a ring buffer, so frame boundaries never reach the speakers.
 * Waits for `leadSamples` before starting (and after an underrun) to absorb network jitter.
 */
class StereoStreamProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.leadSamples = options.processorOptions.leadSamples;
    this.capacity = Math.ceil(sampleRate * CAPACITY_SECONDS);
    this.left = new Float32Array(this.capacity);
    this.right = new Float32Array(this.capacity);
    this.readIndex = 0;
    this.available = 0;
    this.playing = false;
    this.underruns = 0;
    this.port.onmessage = ({ data }) => {
      if (data.type === "reset") {
        this.readIndex = 0;
        this.available = 0;
        this.playing = false;
        this.underruns = 0;
      } else {
        this.write(data.left, data.right);
      }
    };
  }

  write(left, right) {
    const count = Math.min(left.length, this.capacity - this.available); // overflow: drop the excess
    let writeIndex = (this.readIndex + this.available) % this.capacity;
    for (let i = 0; i < count; i++) {
      this.left[writeIndex] = left[i];
      this.right[writeIndex] = right[i];
      writeIndex = (writeIndex + 1) % this.capacity;
    }
    this.available += count;
  }

  process(_inputs, outputs) {
    const [outLeft, outRight] = outputs[0];
    if (!this.playing && this.available >= this.leadSamples) this.playing = true;
    if (!this.playing) return true; // outputs are already zeroed
    for (let i = 0; i < outLeft.length; i++) {
      if (this.available === 0) {
        this.playing = false;
        this.underruns += 1;
        this.port.postMessage({ underruns: this.underruns });
        break;
      }
      outLeft[i] = this.left[this.readIndex];
      outRight[i] = this.right[this.readIndex];
      this.readIndex = (this.readIndex + 1) % this.capacity;
      this.available -= 1;
    }
    return true;
  }
}

registerProcessor("stereo-stream", StereoStreamProcessor);
