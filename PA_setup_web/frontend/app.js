// PA Setup Web — frontend logic
// Waveform data travels as binary WebSocket frames (Float32Array) for speed.
// Binary frame layout: [uint32 n] [float32 × n time_us] [float32 × n voltage_mv]

'use strict';

let scopeWs = null;
let isContinuous = false;
let lastTime = null;
let lastVoltage = null;
let uplot = null;

// ── Utilities ───────────────────────────────────────────────────────────────

async function api(method, path, body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== null) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    if (!r.ok) {
        const text = await r.text().catch(() => r.statusText);
        throw new Error(text || r.statusText);
    }
    return r.json();
}

function log(msg) {
    const box = document.getElementById('log');
    const ts = new Date().toTimeString().slice(0, 8);
    box.textContent += `[${ts}] ${msg}\n`;
    box.scrollTop = box.scrollHeight;
    document.getElementById('status-bar').textContent = msg;
}

function setDot(id, ok) {
    const el = document.getElementById(id);
    el.classList.toggle('ok', ok);
}

// ── uPlot ───────────────────────────────────────────────────────────────────

function initPlot() {
    const container = document.getElementById('plot-container');

    const opts = {
        width:  container.clientWidth  || 800,
        height: container.clientHeight || 320,
        cursor: { show: true, drag: { x: true, y: false } },
        series: [
            { label: 'Time (µs)' },
            { label: 'Voltage (mV)', stroke: '#00c8ff', width: 1 },
        ],
        axes: [
            { label: 'Time (µs)',    stroke: '#999', grid: { stroke: '#2a2a2a' }, ticks: { stroke: '#333' } },
            { label: 'Voltage (mV)', stroke: '#999', grid: { stroke: '#2a2a2a' }, ticks: { stroke: '#333' } },
        ],
        scales: { x: { time: false } },
        padding: [8, 16, 0, 8],
    };

    // uPlot needs at least one point to initialise axes
    uplot = new uPlot(opts, [new Float32Array(1), new Float32Array(1)], container);

    // Keep plot filling the container when window is resized
    new ResizeObserver(() => {
        if (uplot) uplot.setSize({
            width:  container.clientWidth,
            height: container.clientHeight,
        });
    }).observe(container);
}

function updatePlot(timeUs, voltageMv) {
    // uPlot 1.6+ accepts TypedArrays directly — no copy needed
    uplot.setData([timeUs, voltageMv]);
}

// ── WebSocket ───────────────────────────────────────────────────────────────

function openScopeWs(onOpen = null) {
    if (scopeWs && scopeWs.readyState === WebSocket.OPEN) {
        if (onOpen) onOpen();
        return;
    }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    scopeWs = new WebSocket(`${proto}://${location.host}/ws/scope`);
    scopeWs.binaryType = 'arraybuffer';   // receive ArrayBuffer, not Blob

    scopeWs.onopen = () => {
        log('Scope WS connected.');
        if (onOpen) onOpen();
    };
    scopeWs.onclose = () => {
        log('Scope WS closed.');
        _resetContinuousUI();
    };
    scopeWs.onerror = () => log('Scope WS error.');

    scopeWs.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
            // Parse binary frame
            const n        = new Uint32Array(event.data, 0, 1)[0];
            const timeUs   = new Float32Array(event.data,       4,     n);
            const voltMv   = new Float32Array(event.data, 4 + n * 4,   n);
            lastTime    = timeUs;
            lastVoltage = voltMv;
            updatePlot(timeUs, voltMv);
            document.getElementById('btn-save').disabled = false;
        } else {
            // JSON error message from server
            try {
                const msg = JSON.parse(event.data);
                if (msg.error) log(`Scope: ${msg.error}`);
            } catch (_) { /* ignore */ }
        }
    };
}

function getScopeConfig() {
    return {
        sample_rate_hz: parseFloat(document.getElementById('sel-rate').value),
        duration_ms:    parseFloat(document.getElementById('inp-duration').value),
        trigger_mv:     parseFloat(document.getElementById('inp-trigger').value),
        channel:        document.getElementById('sel-channel').value,
        coupling:       document.getElementById('sel-coupling').value,
        range_label:    document.getElementById('sel-range').value,
    };
}

// ── Scope connection ────────────────────────────────────────────────────────

async function connectScope() {
    const btn = document.getElementById('btn-scope-connect');
    try {
        if (btn.dataset.connected === 'true') {
            stopContinuous();
            await api('POST', '/api/scope/disconnect');
            setDot('dot-scope', false);
            btn.dataset.connected = 'false';
            btn.textContent = 'Connect Scope';
            log('Scope disconnected.');
        } else {
            await api('POST', '/api/scope/connect');
            setDot('dot-scope', true);
            btn.dataset.connected = 'true';
            btn.textContent = 'Disconnect Scope';
            log('PicoScope connected.');
            openScopeWs();
        }
    } catch (e) { log(`Scope: ${e.message}`); }
}

// ── Scope acquisition ───────────────────────────────────────────────────────

function singleCapture() {
    stopContinuous();
    const cfg = { type: 'single', ...getScopeConfig() };
    openScopeWs(() => {
        scopeWs.send(JSON.stringify(cfg));
        log('Single capture triggered.');
    });
}

function toggleContinuous() {
    isContinuous ? stopContinuous() : startContinuous();
}

function startContinuous() {
    const cfg = { type: 'start_continuous', ...getScopeConfig() };
    openScopeWs(() => {
        scopeWs.send(JSON.stringify(cfg));
        isContinuous = true;
        const btn = document.getElementById('btn-continuous');
        btn.textContent = '● Continuous';
        btn.classList.add('active-orange');
        document.getElementById('btn-stop').disabled = false;
        log('Continuous acquisition started.');
    });
}

function stopContinuous() {
    if (scopeWs && scopeWs.readyState === WebSocket.OPEN && isContinuous) {
        scopeWs.send(JSON.stringify({ type: 'stop' }));
        log('Acquisition stopped.');
    }
    _resetContinuousUI();
}

function _resetContinuousUI() {
    isContinuous = false;
    const btn = document.getElementById('btn-continuous');
    btn.textContent = 'Continuous';
    btn.classList.remove('active-orange');
    document.getElementById('btn-stop').disabled = true;
}

// ── Laser ───────────────────────────────────────────────────────────────────

async function connectLaser() {
    const btn  = document.getElementById('btn-laser-connect');
    const port = document.getElementById('inp-laser-port').value;
    try {
        if (btn.dataset.connected === 'true') {
            await api('POST', '/api/laser/disconnect');
            setDot('dot-laser', false);
            btn.dataset.connected = 'false';
            btn.textContent = 'Connect';
            _setLaserEmission(false);
            _enableLaserControls(false);
            log('Laser disconnected.');
        } else {
            await api('POST', '/api/laser/connect', { port });
            setDot('dot-laser', true);
            btn.dataset.connected = 'true';
            btn.textContent = 'Disconnect';
            _enableLaserControls(true);
            log(`Laser connected on ${port}.`);
        }
    } catch (e) { log(`Laser: ${e.message}`); }
}

async function toggleLaserEmission() {
    const btn = document.getElementById('btn-laser-emit');
    const enabled = btn.dataset.on !== 'true';
    try {
        await api('POST', '/api/laser/enable', { enabled });
        _setLaserEmission(enabled);
        log(`Laser emission ${enabled ? 'ON' : 'OFF'}.`);
    } catch (e) { log(`Laser: ${e.message}`); }
}

function _setLaserEmission(on) {
    const btn = document.getElementById('btn-laser-emit');
    btn.dataset.on = on ? 'true' : 'false';
    btn.textContent = on ? 'Emission ON' : 'Emission OFF';
    btn.classList.toggle('active-green', on);
}

async function _sendLaserPower(mw) {
    try {
        await api('POST', '/api/laser/power', { power_mw: parseFloat(mw) });
    } catch (e) { log(`Laser: ${e.message}`); }
}

function _enableLaserControls(on) {
    ['btn-laser-emit', 'sl-laser-power', 'inp-laser-power', 'sel-laser-mode']
        .forEach(id => { document.getElementById(id).disabled = !on; });
}

async function setLaserMode(mode) {
    try {
        await api('POST', '/api/laser/mode', { mode });
        log(`Laser mode → ${mode}.`);
    } catch (e) { log(`Laser: ${e.message}`); }
}

// ── Galvo ───────────────────────────────────────────────────────────────────

async function connectGalvo() {
    const btn = document.getElementById('btn-galvo-connect');
    const xch = document.getElementById('inp-galvo-x').value;
    const ych = document.getElementById('inp-galvo-y').value;
    try {
        if (btn.dataset.connected === 'true') {
            await api('POST', '/api/galvo/disconnect');
            setDot('dot-galvo', false);
            btn.dataset.connected = 'false';
            btn.textContent = 'Connect Galvo';
            _enableGalvoControls(false);
            log('Galvo disconnected.');
        } else {
            await api('POST', '/api/galvo/connect', { x_channel: xch, y_channel: ych });
            setDot('dot-galvo', true);
            btn.dataset.connected = 'true';
            btn.textContent = 'Disconnect Galvo';
            _enableGalvoControls(true);
            log(`Galvo connected (X=${xch}, Y=${ych}).`);
        }
    } catch (e) { log(`Galvo: ${e.message}`); }
}

async function _moveGalvo() {
    const x = parseFloat(document.getElementById('inp-galvo-xv').value);
    const y = parseFloat(document.getElementById('inp-galvo-yv').value);
    try { await api('POST', '/api/galvo/move', { x_v: x, y_v: y }); }
    catch (e) { log(`Galvo: ${e.message}`); }
}

async function centerGalvo() {
    document.getElementById('inp-galvo-xv').value = '0.00';
    document.getElementById('sl-galvo-x').value   = '0';
    document.getElementById('inp-galvo-yv').value = '0.00';
    document.getElementById('sl-galvo-y').value   = '0';
    try {
        await api('POST', '/api/galvo/center');
        log('Galvo centered.');
    } catch (e) { log(`Galvo: ${e.message}`); }
}

function _enableGalvoControls(on) {
    ['sl-galvo-x', 'inp-galvo-xv', 'sl-galvo-y', 'inp-galvo-yv', 'btn-galvo-center']
        .forEach(id => { document.getElementById(id).disabled = !on; });
}

// ── Trigger ──────────────────────────────────────────────────────────────────

async function connectTrigger() {
    const btn = document.getElementById('btn-trig-connect');
    const ch  = document.getElementById('inp-trig-ch').value;
    try {
        if (btn.dataset.connected === 'true') {
            await api('POST', '/api/trigger/disconnect');
            setDot('dot-trig', false);
            btn.dataset.connected = 'false';
            btn.textContent = 'Connect';
            _enableTriggerControls(false);
            log('Trigger disconnected.');
        } else {
            await api('POST', '/api/trigger/connect', { channel: ch });
            setDot('dot-trig', true);
            btn.dataset.connected = 'true';
            btn.textContent = 'Disconnect';
            _enableTriggerControls(true);
            log(`Trigger connected on ${ch}.`);
        }
    } catch (e) { log(`Trigger: ${e.message}`); }
}

async function startTrigger() {
    const freq_hz    = parseFloat(document.getElementById('inp-trig-freq').value);
    const duty_cycle = parseFloat(document.getElementById('inp-trig-duty').value) / 100;
    try {
        await api('POST', '/api/trigger/start', { freq_hz, duty_cycle });
        setDot('dot-trig-run', true);
        document.getElementById('btn-trig-start').disabled = true;
        document.getElementById('btn-trig-stop').disabled  = false;
        log(`Trigger started: ${freq_hz} Hz, duty ${(duty_cycle*100).toFixed(1)} %.`);
    } catch (e) { log(`Trigger: ${e.message}`); }
}

async function stopTrigger() {
    try {
        await api('POST', '/api/trigger/stop');
        setDot('dot-trig-run', false);
        document.getElementById('btn-trig-start').disabled = false;
        document.getElementById('btn-trig-stop').disabled  = true;
        log('Trigger stopped.');
    } catch (e) { log(`Trigger: ${e.message}`); }
}

function setTrigPreset(hz) {
    document.getElementById('inp-trig-freq').value = hz;
    _updatePulseWidthLabel();
}

function _updatePulseWidthLabel() {
    const freq = parseFloat(document.getElementById('inp-trig-freq').value) || 0;
    const duty = parseFloat(document.getElementById('inp-trig-duty').value) || 0;
    const lbl  = document.getElementById('lbl-pulse-width');
    if (freq > 0) {
        const pw_us = (duty / 100) / freq * 1e6;
        lbl.textContent = `= ${pw_us.toFixed(1)} µs pulse`;
    } else {
        lbl.textContent = '';
    }
}

function _enableTriggerControls(on) {
    ['inp-trig-freq', 'inp-trig-duty', 'btn-trig-start',
     'pb100', 'pb1k', 'pb10k', 'pb50k']
        .forEach(id => { document.getElementById(id).disabled = !on; });
    if (!on) {
        document.getElementById('btn-trig-stop').disabled = true;
        setDot('dot-trig-run', false);
    }
}

// ── Save CSV ─────────────────────────────────────────────────────────────────

function saveTrace() {
    if (!lastTime || !lastVoltage) return;
    let csv = 'time_us,voltage_mv\n';
    for (let i = 0; i < lastTime.length; i++) {
        csv += `${lastTime[i]},${lastVoltage[i]}\n`;
    }
    const blob = new Blob([csv], { type: 'text/csv' });
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement('a'), {
        href:     url,
        download: `trace_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.csv`,
    });
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    log('Trace saved.');
}

// ── Slider ↔ number input sync ───────────────────────────────────────────────

function _bindSlider(sliderId, inputId, scale, onChangeFn) {
    const slider = document.getElementById(sliderId);
    const input  = document.getElementById(inputId);

    slider.addEventListener('input', () => {
        input.value = (slider.value / scale).toFixed(2);
        onChangeFn(input.value);
    });
    input.addEventListener('change', () => {
        slider.value = Math.round(parseFloat(input.value) * scale);
        onChangeFn(input.value);
    });
}

// ── Mock auto-connect ────────────────────────────────────────────────────────

async function _autoConnectMock() {
    await api('POST', '/api/laser/connect', { port: 'MOCK' });
    setDot('dot-laser', true);
    document.getElementById('btn-laser-connect').dataset.connected = 'true';
    document.getElementById('btn-laser-connect').textContent = 'Disconnect';
    _enableLaserControls(true);

    await api('POST', '/api/galvo/connect', { x_channel: 'Dev1/ao0', y_channel: 'Dev1/ao1' });
    setDot('dot-galvo', true);
    document.getElementById('btn-galvo-connect').dataset.connected = 'true';
    document.getElementById('btn-galvo-connect').textContent = 'Disconnect Galvo';
    _enableGalvoControls(true);

    await api('POST', '/api/scope/connect');
    setDot('dot-scope', true);
    document.getElementById('btn-scope-connect').dataset.connected = 'true';
    document.getElementById('btn-scope-connect').textContent = 'Disconnect Scope';
    openScopeWs();

    await api('POST', '/api/trigger/connect', { channel: 'Dev1/ctr0' });
    setDot('dot-trig', true);
    document.getElementById('btn-trig-connect').dataset.connected = 'true';
    document.getElementById('btn-trig-connect').textContent = 'Disconnect';
    _enableTriggerControls(true);

    log('Mock mode: all instruments auto-connected.');
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
    initPlot();

    _bindSlider('sl-laser-power', 'inp-laser-power', 10,   _sendLaserPower);
    _bindSlider('sl-galvo-x',     'inp-galvo-xv',    100,  _moveGalvo);
    _bindSlider('sl-galvo-y',     'inp-galvo-yv',    100,  _moveGalvo);

    // Live pulse-width readout next to duty cycle input
    ['inp-trig-freq', 'inp-trig-duty'].forEach(id => {
        document.getElementById(id).addEventListener('input', _updatePulseWidthLabel);
    });
    _updatePulseWidthLabel();

    try {
        const info = await api('GET', '/api/info');
        if (info.mock) {
            document.title = 'PA Setup Control [MOCK]';
            document.getElementById('mock-badge').style.display = 'inline';
            await _autoConnectMock();
        }
    } catch (e) {
        log(`Init: ${e.message}`);
    }
});
