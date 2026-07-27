// Calliope Electron shell.
//
// Starts the FastAPI backend (PyInstaller exe in packaged mode, venv python in
// dev), waits for /api/health, then loads the SPA the backend serves at its own
// origin — so no CORS or API-base handling is needed here.

const { app, BrowserWindow, Menu } = require('electron');
const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const HOST = '127.0.0.1';
// CALLIOPE_PORT env pins the preferred port (handy for testing alongside a dev
// backend on 8247); otherwise the chain below is tried in order.
const PREFERRED_PORT = Number(process.env.CALLIOPE_PORT) || 8247;
const PORT_CANDIDATES = [...new Set([PREFERRED_PORT, 8247, 8248, 8249, 8250])];
const HEALTH_TIMEOUT_MS = 30_000;

let backendChild = null;
let mainWindow = null;

// ---------- small helpers ----------

function healthOk(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: HOST, port, path: '/api/health', timeout: 1500 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
    req.on('error', () => resolve(false));
  });
}

function portBusy(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(true));
    srv.once('listening', () => srv.close(() => resolve(false)));
    srv.listen(port, HOST);
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ---------- backend process ----------

function backendLaunch() {
  if (app.isPackaged) {
    // extraResources copied the onedir PyInstaller output to resources/backend/.
    // Frozen config.py anchors data/ and calliope_config.json next to this exe.
    const exe = path.join(process.resourcesPath, 'backend', 'calliope-backend.exe');
    return { cmd: exe, args: [], cwd: path.dirname(exe) };
  }
  // Dev: run the source backend from its venv; storage stays in calliope-backend/data.
  const backendRoot = path.resolve(__dirname, '..', 'calliope-backend');
  const py = path.join(backendRoot, '.venv', 'Scripts', 'python.exe');
  return { cmd: py, args: ['-m', 'calliope.main'], cwd: backendRoot };
}

function killBackend() {
  if (backendChild && backendChild.exitCode === null && !backendChild.killed) {
    try {
      backendChild.kill(); // TerminateProcess on Windows
    } catch {
      /* already gone */
    }
  }
  backendChild = null;
}

async function waitForHealth(port, child, spawnErrorRef) {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (spawnErrorRef.err) throw spawnErrorRef.err;
    if (child && child.exitCode !== null) {
      throw new Error(`backend exited early (code ${child.exitCode})`);
    }
    if (await healthOk(port)) return;
    await sleep(500);
  }
  throw new Error(`no /api/health response within ${HEALTH_TIMEOUT_MS / 1000}s`);
}

async function startBackend() {
  let lastErr = null;
  for (const port of PORT_CANDIDATES) {
    // Stale/previous instance already serving? Reuse it — no second backend.
    if (await healthOk(port)) {
      console.log(`[calliope] backend already healthy on :${port} — reusing it`);
      return port;
    }
    if (await portBusy(port)) {
      console.log(`[calliope] :${port} is busy (not a Calliope backend) — trying next port`);
      continue;
    }

    const { cmd, args, cwd } = backendLaunch();
    // NOTE: the backend reads host/port from settings/env, NOT argv (there is no
    // CLI parser). --host/--port are passed for future CLI support; the env vars
    // below are what actually bind the port.
    const fullArgs = [...args, '--host', HOST, '--port', String(port)];
    console.log(`[calliope] spawning ${cmd} ${fullArgs.join(' ')} (cwd=${cwd})`);

    const spawnErrorRef = { err: null };
    backendChild = spawn(cmd, fullArgs, {
      cwd,
      env: { ...process.env, CALLIOPE_HOST: HOST, CALLIOPE_PORT: String(port) },
      windowsHide: true, // never flash a console window for the backend
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    backendChild.on('error', (err) => {
      spawnErrorRef.err = err;
    });
    // Mirror backend logs into our stdout (visible when launched from a terminal).
    backendChild.stdout?.on('data', (d) => process.stdout.write(`[backend] ${d}`));
    backendChild.stderr?.on('data', (d) => process.stderr.write(`[backend] ${d}`));

    try {
      await waitForHealth(port, backendChild, spawnErrorRef);
      console.log(`[calliope] backend healthy on :${port}`);
      return port;
    } catch (err) {
      console.error(`[calliope] backend on :${port} failed: ${err.message}`);
      lastErr = err;
      killBackend();
    }
  }
  throw lastErr || new Error(`no free port among ${PORT_CANDIDATES.join(', ')}`);
}

// ---------- window ----------

function inlinePage(title, message) {
  const html = `<!doctype html>
<html><head><meta charset="utf-8"><title>${title}</title>
<style>
  body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
       background:#0a0a0c;color:#e7e7ea;font:14px/1.6 system-ui,sans-serif;text-align:center}
  h1{font-size:20px;font-weight:600;margin:0 0 12px}
  p{color:#9a9aa3;max-width:520px;margin:4px 16px}
  .spin{width:28px;height:28px;margin-bottom:20px;border:3px solid #2a2a30;border-top-color:#7c7cf0;
        border-radius:50%;animation:r .8s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  code{background:#17171c;padding:2px 6px;border-radius:4px}
</style></head>
<body><div class="spin" style="display:${title === 'Calliope' ? 'block' : 'none'}"></div>
<h1>${title}</h1><p>${message}</p></body></html>`;
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
}

function createWindow() {
  Menu.setApplicationMenu(null);
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    backgroundColor: '#0a0a0c',
    autoHideMenuBar: true,
    show: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  mainWindow.loadURL(inlinePage('Calliope', 'Starting the local studio…'));
}

async function boot() {
  createWindow();
  try {
    const port = await startBackend();
    await mainWindow.loadURL(`http://${HOST}:${port}/`);
  } catch (err) {
    console.error('[calliope] fatal:', err);
    await mainWindow.loadURL(
      inlinePage(
        'Calliope failed to start',
        `The backend could not be started (${String(err.message || err)}). ` +
          `If a previous run hung, close it or run <code>end.bat</code>, then try again.`
      )
    );
  }
}

// ---------- lifecycle ----------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(boot);

  app.on('before-quit', killBackend);
  app.on('window-all-closed', () => {
    killBackend();
    app.quit();
  });
  // Cover terminal kill paths so no orphan calliope-backend.exe is left behind.
  process.on('exit', killBackend);
  for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => {
      killBackend();
      app.quit();
    });
  }
}
