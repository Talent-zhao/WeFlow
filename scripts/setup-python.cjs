/**
 * Downloads and sets up a portable Python 3.12 runtime for the WeFlow bot.
 *
 * Uses python-build-standalone (indygreg) — self-contained, redistributable,
 * no system installation required.
 *
 * Idempotent: checks a sentinel file (.weflow-setup-done) and skips if
 * already set up. Pass --force to rebuild.
 *
 * Platform-gated: only runs on Windows x64. macOS/Linux use system Python.
 */

'use strict';

const https = require('https');
const http = require('http');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { execSync } = require('child_process');
const { pipeline } = require('stream');
const { promisify } = require('util');

const streamPipeline = promisify(pipeline);

// ── Configuration ──────────────────────────────────────────────────────────

const PROJECT_ROOT = path.resolve(__dirname, '..');
const PYTHON_DIR = path.join(PROJECT_ROOT, 'resources', 'python');
const SENTINEL = path.join(PYTHON_DIR, '.weflow-setup-done');
const CACHE_DIR = path.join(__dirname, '.cache');
const REQUIREMENTS_PATH = path.join(PROJECT_ROOT, 'resources', 'bot', 'requirements.txt');

// Pinned python-build-standalone version
const PYTHON_VERSION = '3.12.11';
const RELEASE_TAG = '20251007';
const ARCH = 'x86_64-pc-windows-msvc';
const VARIANT = 'install_only';
const FILENAME = `cpython-${PYTHON_VERSION}+${RELEASE_TAG}-${ARCH}-${VARIANT}.tar.gz`;
const DOWNLOAD_URL = `https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE_TAG}/${FILENAME}`;

// ── Helpers ────────────────────────────────────────────────────────────────

function log(msg) {
  const ts = new Date().toISOString().slice(11, 19);
  process.stdout.write(`\x1b[36m[setup-python ${ts}]\x1b[0m ${msg}\n`);
}

function warn(msg) {
  const ts = new Date().toISOString().slice(11, 19);
  process.stderr.write(`\x1b[33m[setup-python ${ts}] WARN\x1b[0m ${msg}\n`);
}

function err(msg) {
  const ts = new Date().toISOString().slice(11, 19);
  process.stderr.write(`\x1b[31m[setup-python ${ts}] ERROR\x1b[0m ${msg}\n`);
}

function findExecutable(dir, name) {
  const candidates = [
    path.join(dir, name),
    path.join(dir, 'bin', name),
  ];
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch {}
  }
  return null;
}

/**
 * Download a file with progress logging and redirect support.
 * Returns the local path on success.
 */
function downloadFile(url, destPath, retries = 3) {
  return new Promise((resolve, reject) => {
    function attempt(remaining) {
      log(`Downloading: ${url}`);
      const file = fs.createWriteStream(destPath);

      const transport = url.startsWith('https:') ? https : http;

      const req = transport.get(url, { timeout: 300000 }, (res) => {
        // Follow redirects
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          file.close();
          fs.unlinkSync(destPath);
          const redirectUrl = new URL(res.headers.location, url).href;
          downloadFile(redirectUrl, destPath, remaining).then(resolve).catch(reject);
          return;
        }

        if (res.statusCode !== 200) {
          file.close();
          fs.unlinkSync(destPath);
          if (remaining > 1) {
            log(`HTTP ${res.statusCode}, retrying... (${remaining - 1} left)`);
            setTimeout(() => attempt(remaining - 1), 2000);
            return;
          }
          reject(new Error(`Download failed: HTTP ${res.statusCode}`));
          return;
        }

        const total = parseInt(res.headers['content-length'] || '0', 10);
        let downloaded = 0;
        let lastReport = Date.now();

        res.on('data', (chunk) => {
          downloaded += chunk.length;
          const now = Date.now();
          if (total > 0 && (now - lastReport > 1000)) {
            lastReport = now;
            const pct = ((downloaded / total) * 100).toFixed(1);
            const mb = (downloaded / 1024 / 1024).toFixed(1);
            const totalMb = (total / 1024 / 1024).toFixed(1);
            process.stdout.write(`\r  ${pct}% (${mb} / ${totalMb} MB)`);
          }
        });

        streamPipeline(res, file).then(() => {
          if (total > 0) process.stdout.write('\n');
          log(`Download complete (${(downloaded / 1024 / 1024).toFixed(1)} MB)`);
          resolve(destPath);
        }).catch((e) => {
          file.close();
          try { fs.unlinkSync(destPath); } catch {}
          if (remaining > 1) {
            log(`Download interrupted, retrying... (${remaining - 1} left)`);
            setTimeout(() => attempt(remaining - 1), 2000);
            return;
          }
          reject(e);
        });
      });

      req.on('error', (e) => {
        file.close();
        try { fs.unlinkSync(destPath); } catch {}
        if (remaining > 1) {
          log(`Network error: ${e.message}, retrying... (${remaining - 1} left)`);
          setTimeout(() => attempt(remaining - 1), 2000);
          return;
        }
        reject(e);
      });

      req.on('timeout', () => {
        req.destroy();
        file.close();
        try { fs.unlinkSync(destPath); } catch {}
        if (remaining > 1) {
          log(`Timeout, retrying... (${remaining - 1} left)`);
          setTimeout(() => attempt(remaining - 1), 2000);
          return;
        }
        reject(new Error('Download timed out'));
      });
    }
    attempt(retries);
  });
}

/**
 * Extract a .tar.gz file. Prefers system tar, falls back to pure Node.js.
 */
function extractTarball(tarballPath, destDir) {
  // Clean destination to avoid partial-extraction corruption
  if (fs.existsSync(destDir)) {
    fs.rmSync(destDir, { recursive: true, force: true });
  }
  fs.mkdirSync(destDir, { recursive: true });

  // 1. Try Windows native tar.exe (handles Windows paths natively)
  if (process.platform === 'win32') {
    const winTar = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe');
    if (fs.existsSync(winTar)) {
      try {
        log('Trying Windows native tar...');
        execSync(`"${winTar}" -xzf "${tarballPath}" -C "${destDir}"`, {
          stdio: 'pipe',
          timeout: 120000,
          windowsHide: true,
        });
        log('Extracted via Windows native tar.');
        return;
      } catch (e) {
        log(`Windows tar failed (${e.message}), trying alternatives...`);
      }
    }
  }

  // 2. Try Unix tar (Git Bash / MSYS2) with Unix-style paths
  try {
    const unixTarball = tarballPath.replace(/\\/g, '/').replace(/^([A-Z]):/, '/$1');
    const unixDest = destDir.replace(/\\/g, '/').replace(/^([A-Z]):/, '/$1');
    execSync(`tar -xzf "${unixTarball}" -C "${unixDest}"`, {
      stdio: 'pipe',
      timeout: 120000,
      windowsHide: true,
    });
    log('Extracted via Unix tar.');
    return;
  } catch (e) {
    log(`Unix tar also failed (${e.message}), using Node.js fallback...`);
  }

  // 3. Pure Node.js fallback: gunzip + tar parser
  // Use Buffer.alloc for clean copies to avoid subarray corruption
  const gzipped = fs.readFileSync(tarballPath);
  const gunzipped = zlib.gunzipSync(gzipped);

  let pos = 0;
  while (pos < gunzipped.length - 512) {
    // Extract header as a clean copy to avoid shared-buffer issues
    const header = Buffer.from(gunzipped.subarray(pos, pos + 512));

    // Check for end-of-archive (two zero blocks)
    if (header.every(b => b === 0)) {
      if (pos + 1024 <= gunzipped.length) {
        const nextBlock = Buffer.from(gunzipped.subarray(pos + 512, pos + 1024));
        if (nextBlock.every(b => b === 0)) break;
      }
    }

    // Parse header fields
    const name = header.toString('utf8', 0, 100).replace(/\0/g, '');
    const prefix = header.toString('utf8', 345, 500).replace(/\0/g, '');
    const sizeStr = header.toString('utf8', 124, 136).replace(/\0/g, '').trim();
    const size = parseInt(sizeStr, 8) || 0;
    const typeFlag = String.fromCharCode(header[156]);

    // Combine prefix and name for USTAR format
    const fullName = prefix ? path.posix.join(prefix, name) : name;

    pos += 512;

    // Skip PaxHeader extended attributes
    if (fullName.includes('PaxHeader') || fullName.includes('ustar')) {
      pos += Math.ceil(size / 512) * 512;
      continue;
    }

    if (!fullName) {
      pos += Math.ceil(size / 512) * 512;
      continue;
    }

    const destPath = path.join(destDir, fullName.replace(/^\.\//, ''));

    if (typeFlag === 'L') {
      // GNU long name — read the long name, then process the next entry
      const longName = Buffer.from(gunzipped.subarray(pos, pos + size)).toString('utf8').replace(/\0/g, '');
      pos += Math.ceil(size / 512) * 512;

      if (pos >= gunzipped.length - 512) break;
      const realHeader = Buffer.from(gunzipped.subarray(pos, pos + 512));
      const realName = realHeader.toString('utf8', 0, 100).replace(/\0/g, '');
      const realPrefix = realHeader.toString('utf8', 345, 500).replace(/\0/g, '');
      const realFullName = realPrefix ? path.posix.join(realPrefix, realName) : realName;
      const realSizeStr = realHeader.toString('utf8', 124, 136).replace(/\0/g, '').trim();
      const realSize = parseInt(realSizeStr, 8) || 0;
      const realTypeFlag = String.fromCharCode(realHeader[156]);
      pos += 512;

      // Use the long name instead of the header name
      const actualName = longName || realFullName;
      const realPath = path.join(destDir, actualName.replace(/^\.\//, ''));
      if (realTypeFlag === '5' || actualName.endsWith('/')) {
        fs.mkdirSync(realPath, { recursive: true });
      } else {
        fs.mkdirSync(path.dirname(realPath), { recursive: true });
        const content = Buffer.from(gunzipped.subarray(pos, pos + realSize));
        fs.writeFileSync(realPath, content);
      }

      pos += Math.ceil(realSize / 512) * 512;
      continue;
    }

    if (typeFlag === '5' || fullName.endsWith('/')) {
      fs.mkdirSync(destPath, { recursive: true });
    } else if (typeFlag === '0' || typeFlag === '\x00' || typeFlag === '7') {
      fs.mkdirSync(path.dirname(destPath), { recursive: true });
      const content = Buffer.from(gunzipped.subarray(pos, pos + size));
      fs.writeFileSync(destPath, content);

      // Restore permissions from mode field
      const modeStr = header.toString('utf8', 100, 108).replace(/\0/g, '').trim();
      const mode = parseInt(modeStr, 8);
      if (mode && !isNaN(mode)) {
        try { fs.chmodSync(destPath, mode & 0o777); } catch {}
      }
    }

    pos += Math.ceil(size / 512) * 512;
  }

  log('Extracted via Node.js tar parser.');
}

/**
 * Find the main python executable in the extracted directory.
 */
function findPythonExe(dir) {
  const exe = findExecutable(dir, 'python.exe');
  if (exe) return exe;

  // python-build-standalone may have a "python/" subdirectory
  const innerDir = path.join(dir, 'python');
  if (fs.existsSync(innerDir)) {
    const innerExe = findExecutable(innerDir, 'python.exe');
    if (innerExe) return innerExe;
  }

  // Deep search
  function walk(d, depth) {
    if (depth > 3) return null;
    try {
      for (const entry of fs.readdirSync(d)) {
        const full = path.join(d, entry);
        if (entry === 'python.exe' || entry === 'python3.exe') return full;
        try {
          if (fs.statSync(full).isDirectory()) {
            const found = walk(full, depth + 1);
            if (found) return found;
          }
        } catch {}
      }
    } catch {}
    return null;
  }
  return walk(dir, 0);
}

/**
 * Find and patch python._pth to enable site-packages.
 */
function patchPth(dir) {
  // python-build-standalone uses "python._pth" in the install root
  const candidates = ['python._pth', 'python312._pth'];
  for (const name of candidates) {
    const pthPath = path.join(dir, name);
    if (!fs.existsSync(pthPath)) continue;

    log(`Patching: ${pthPath}`);
    let content = fs.readFileSync(pthPath, 'utf8');
    const lines = content.split(/\r?\n/);
    const result = [];

    for (const line of lines) {
      if (line.trim() === '#import site') {
        result.push('import site');
      } else {
        result.push(line);
      }
    }

    // Ensure Lib/site-packages is in the path
    if (!result.some(l => l.includes('Lib/site-packages') || l.includes('Lib\\site-packages'))) {
      result.push('Lib/site-packages');
    }

    // Ensure Scripts is in the path (for DLLs that pywin32 puts there)
    if (!result.some(l => l.includes('Scripts') && !l.startsWith('#'))) {
      result.push('Scripts');
    }

    fs.writeFileSync(pthPath, result.join('\n'), 'utf8');
    log(`Patched ${name} successfully.`);
    return;
  }
  warn('No python._pth file found to patch.');
}

/**
 * Run pywin32 post-install script to register COM components.
 */
function runPywin32PostInstall(pythonExe) {
  const pyDir = path.dirname(pythonExe);
  const candidates = [
    path.join(pyDir, 'Scripts', 'pywin32_postinstall.py'),
    path.join(pyDir, 'Lib', 'site-packages', 'pywin32_system32', 'pywin32_postinstall.py'),
    path.join(pyDir, 'Lib', 'site-packages', 'win32', 'scripts', 'pywin32_postinstall.py'),
  ];

  for (const script of candidates) {
    if (fs.existsSync(script)) {
      log(`Running pywin32_postinstall.py...`);
      try {
        execSync(`"${pythonExe}" "${script}" -install -silent`, {
          stdio: 'pipe',
          timeout: 60000,
          windowsHide: true,
        });
        log('pywin32 post-install complete.');
      } catch (e) {
        warn(`pywin32 post-install failed (non-fatal): ${e.message}`);
      }
      return;
    }
  }
  warn('pywin32_postinstall.py not found (may not be needed).');
}

// ── Main ───────────────────────────────────────────────────────────────────

async function main() {
  // Platform gate
  if (process.platform !== 'win32') {
    log(`Skipping — platform is ${process.platform}. macOS/Linux use system Python.`);
    process.exit(0);
  }

  // Architecture gate
  if (process.arch !== 'x64') {
    log(`Skipping — arch is ${process.arch}. Only x64 Windows is bundled.`);
    process.exit(0);
  }

  // Idempotency
  const force = process.argv.includes('--force');
  if (!force && fs.existsSync(SENTINEL)) {
    const versionInfo = fs.readFileSync(SENTINEL, 'utf8').trim();
    log(`Already set up (${versionInfo || 'previous run'}). Use --force to rebuild.`);
    process.exit(0);
  }

  log(`Setting up portable Python ${PYTHON_VERSION} from python-build-standalone...`);

  // Clean partial prior state
  if (force && fs.existsSync(PYTHON_DIR)) {
    log('Removing previous installation...');
    fs.rmSync(PYTHON_DIR, { recursive: true, force: true });
  }

  // Ensure target directory
  fs.mkdirSync(PYTHON_DIR, { recursive: true });
  fs.mkdirSync(CACHE_DIR, { recursive: true });

  // Check cached tarball first
  let tarballPath = path.join(CACHE_DIR, FILENAME);
  if (fs.existsSync(tarballPath)) {
    log(`Using cached tarball: ${tarballPath}`);
  } else {
    // Download
    tarballPath = path.join(PYTHON_DIR, FILENAME);
    try {
      await downloadFile(DOWNLOAD_URL, tarballPath);
    } catch (e) {
      err(`Download failed: ${e.message}`);
      err(`You can manually download the file and place it at:`);
      err(`  ${tarballPath}`);
      err(`URL: ${DOWNLOAD_URL}`);
      process.exit(1);
    }
  }

  // Extract
  log('Extracting...');
  try {
    extractTarball(tarballPath, PYTHON_DIR);
  } catch (e) {
    err(`Extraction failed: ${e.message}`);
    process.exit(1);
  }

  // Clean up tarball if downloaded to PYTHON_DIR
  if (tarballPath.startsWith(PYTHON_DIR)) {
    try { fs.unlinkSync(tarballPath); } catch {}
  }

  // Find python executable
  let pythonExe = findPythonExe(PYTHON_DIR);
  if (!pythonExe) {
    err('Could not find python.exe in extracted directory.');
    err('Contents of python directory:');
    try {
      for (const entry of fs.readdirSync(PYTHON_DIR)) {
        err(`  ${entry}`);
      }
    } catch {}
    process.exit(1);
  }
  log(`Found Python: ${pythonExe}`);

  // Verify Python works
  try {
    const ver = execSync(`"${pythonExe}" --version`, { encoding: 'utf8', timeout: 10000, windowsHide: true });
    log(`Python version: ${ver.trim()}`);
  } catch (e) {
    err(`Python executable exists but does not run: ${e.message}`);
    process.exit(1);
  }

  // Patch python._pth
  const pythonDir = path.dirname(pythonExe);
  patchPth(pythonDir);

  // Install pip packages
  if (!fs.existsSync(REQUIREMENTS_PATH)) {
    warn(`requirements.txt not found at ${REQUIREMENTS_PATH}, skipping pip install.`);
  } else {
    log('Upgrading pip...');
    try {
      execSync(`"${pythonExe}" -m pip install --upgrade pip --quiet`, {
        stdio: 'inherit',
        timeout: 120000,
        windowsHide: true,
      });
    } catch (e) {
      warn(`pip upgrade failed (continuing): ${e.message}`);
    }

    log(`Installing packages from requirements.txt...`);
    try {
      execSync(`"${pythonExe}" -m pip install -r "${REQUIREMENTS_PATH}" --quiet`, {
        stdio: 'inherit',
        timeout: 300000,
        windowsHide: true,
      });
      log('All Python packages installed successfully.');
    } catch (e) {
      warn(`Some packages may have failed to install: ${e.message}`);
      warn('The bot may still work if only optional packages are missing.');
    }
  }

  // pywin32 post-install
  runPywin32PostInstall(pythonExe);

  // Copy cached tarball to cache dir for future runs
  if (!tarballPath.startsWith(CACHE_DIR) && fs.existsSync(tarballPath)) {
    try {
      fs.copyFileSync(tarballPath, path.join(CACHE_DIR, FILENAME));
    } catch {}
  }

  // Write sentinel
  fs.writeFileSync(SENTINEL, `${PYTHON_VERSION} (${RELEASE_TAG})\n`);
  log(`Setup complete. Portable Python ready in ${PYTHON_DIR}`);
  log(`Installer will include: ${PYTHON_DIR} (~${getDirSizeMb(PYTHON_DIR)} MB)`);
}

function getDirSizeMb(dir) {
  let size = 0;
  function walk(d) {
    try {
      for (const entry of fs.readdirSync(d)) {
        const p = path.join(d, entry);
        try {
          const st = fs.statSync(p);
          if (st.isDirectory()) walk(p);
          else size += st.size;
        } catch {}
      }
    } catch {}
  }
  walk(dir);
  return (size / 1024 / 1024).toFixed(0);
}

main().catch((e) => {
  err(`Unexpected error: ${e.message}`);
  console.error(e);
  process.exit(1);
});
