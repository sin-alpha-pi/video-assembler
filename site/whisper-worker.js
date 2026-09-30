// Runs Whisper (transformers.js) off the main thread so the page stays responsive.
const TRANSFORMERS_URL = 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3';

let lib = null;
let asr = null;
let current = null; // `${model}|${device}`

async function hasWebGPU() {
  try { return !!(navigator.gpu && await navigator.gpu.requestAdapter()); } catch { return false; }
}

async function load(model, device) {
  if (!lib) {
    lib = await import(TRANSFORMERS_URL);
    lib.env.allowLocalModels = false;
  }
  const key = `${model}|${device}`;
  if (asr && current === key) return;
  asr = await lib.pipeline('automatic-speech-recognition', model, {
    device,
    dtype: device === 'webgpu' ? { encoder_model: 'fp32', decoder_model_merged: 'fp32' } : 'q8',
    progress_callback: (p) => self.postMessage({ type: 'progress', p }),
  });
  current = key;
  self.postMessage({ type: 'ready', device });
}

async function run(model, audio, device) {
  await load(model, device);
  const out = await asr(audio, { return_timestamps: 'word', chunk_length_s: 30, stride_length_s: 5 });
  return out.chunks || [];
}

self.onmessage = async (e) => {
  const { id, model, audio } = e.data;
  try {
    let chunks;
    if (await hasWebGPU()) {
      try {
        chunks = await run(model, audio, 'webgpu');
      } catch (err) {
        self.postMessage({ type: 'log', msg: `WebGPU failed (${err.message || err}), using CPU instead.` });
        asr = null;
        chunks = await run(model, audio, 'wasm');
      }
    } else {
      chunks = await run(model, audio, 'wasm');
    }
    const words = chunks.map((c) => ({
      w: String(c.text).trim(),
      s: c.timestamp[0],
      e: c.timestamp[1] ?? c.timestamp[0] + 0.3,
    }));
    self.postMessage({ id, type: 'result', words });
  } catch (err) {
    self.postMessage({ id, type: 'error', message: String((err && err.message) || err) });
  }
};
