import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { Num, type Json } from '../src/index.js';

// Compiled tests live in dist/test, so the package is two levels up and the repository is two more.
export const packageRoot = path.resolve(import.meta.dirname, '..', '..');
export const repoRoot = path.resolve(packageRoot, '..', '..');
function resolvePython(): string {
  const chosen = process.env['PYTHON'];
  if (chosen) return chosen;
  if (process.platform !== 'win32') return 'python3';
  // On Windows "py" is a launcher that starts the real interpreter as a child process, and stopping the launcher
  // would leave that child (our test server) running. So ask the launcher where the real interpreter is.
  const asked = spawnSync('py', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' });
  const real = (asked.stdout ?? '').trim();
  return asked.status === 0 && real !== '' ? real : 'python';
}

/** The Python that runs the witness and generates the fixtures. Set PYTHON to choose another. */
export const python = resolvePython();

/** Test cases written by the Python code, which is the source of truth for what a correct answer is. */
export function fixtures<T>(mode: 'canonical' | 'bundles'): T[] {
  const dir = mkdtempSync(path.join(tmpdir(), 'evenhand-ts-'));
  const out = path.join(dir, `${mode}.json`);
  try {
    const run = spawnSync(python, [path.join(packageRoot, 'scripts', 'make_fixtures.py'), mode, out], {
      cwd: repoRoot,
      encoding: 'utf8',
      maxBuffer: 64 * 1024 * 1024,
    });
    if (run.status !== 0) {
      throw new Error(`could not generate ${mode} fixtures with "${python}" (set PYTHON to override): ${run.stderr || String(run.error)}`);
    }
    return JSON.parse(readFileSync(out, 'utf8')) as T[];
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

export interface RunningServer {
  base: string;
  witnessKey: string;
  stop: () => Promise<void>;
}

/** Starts the real Python witness on a free port and waits until it has printed its address and key. */
export async function startServer(extraArgs: string[] = []): Promise<RunningServer> {
  const child: ChildProcess = spawn(python, ['-u', '-m', 'evenhand.server', '--port', '0', ...extraArgs], {
    cwd: repoRoot,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const stdout = child.stdout as NodeJS.ReadableStream;
  const lines: string[] = [];
  let errors = '';
  child.stderr?.on('data', (chunk: Buffer) => {
    errors += chunk.toString();
  });
  const rl = readline.createInterface({ input: stdout });
  let base = '';
  let witnessKey = '';
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`the server did not start in 20 seconds:\n${lines.join('\n')}\n${errors}`)), 20000);
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`the server exited with code ${code}:\n${lines.join('\n')}\n${errors}`));
    });
    rl.on('line', (line) => {
      lines.push(line);
      const address = /^Evenhand witness on (http:\/\/\S+)$/.exec(line);
      if (address) base = address[1] as string;
      const key = /: ([0-9a-f]{64})$/.exec(line);
      if (line.startsWith('Witness public key') && key) witnessKey = key[1] as string;
      if (base && witnessKey) {
        clearTimeout(timer);
        child.removeAllListeners('exit');
        resolve();
      }
    });
  });
  return {
    base,
    witnessKey,
    stop: () =>
      new Promise<void>((resolve) => {
        rl.close();
        if (child.exitCode !== null || child.signalCode !== null) return resolve();
        child.once('close', () => resolve());
        child.kill();
      }),
  };
}

/** JSON text for a lossless tree, keeping every number as it was written. Lets a test edit a chain without disturbing its numbers. */
export function stringifyLossless(v: Json): string {
  if (v === null) return 'null';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'string') return JSON.stringify(v);
  if (v instanceof Num) return v.lexeme;
  if (Array.isArray(v)) return '[' + v.map(stringifyLossless).join(',') + ']';
  return '{' + Object.entries(v).map(([k, x]) => `${JSON.stringify(k)}:${stringifyLossless(x)}`).join(',') + '}';
}