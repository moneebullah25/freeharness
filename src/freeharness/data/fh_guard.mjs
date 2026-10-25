// freeharness guard: a Cordis plugin loaded into the dsh runtime (see security.py).
//
// 1. Approval relay. dsh's `sdk` JSON-RPC profile never sends a server->client request, so a
//    request for approval (sandbox escalation) has no answerer there and resolves "unavailable".
//    When FH_APPROVAL_SOCK names a unix socket served by the freeharness client, requests are
//    relayed to it and the human's answer is returned. Anything else (no socket, connection
//    refused, closed, malformed answer, timeout) is "unavailable": the action stays denied.
//    The model never sees or influences this channel.
// 2. Read fence. The sandbox confines writes only; dsh's read/grep/glob tools run in-process and
//    read the whole filesystem. This refuses their paths when they resolve into the user's home
//    or other private roots, unless inside the session workspace. A policy fence in trusted code,
//    not a kernel boundary (bash is confined by the bwrap runner instead).
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const name = 'fh-guard';

const ANSWERS = new Set(['allowed-once', 'rejected', 'cancelled']);
const APPROVAL_TIMEOUT_MS = 15 * 60 * 1000;

function relay(request) {
  const sock = process.env.FH_APPROVAL_SOCK;
  if (!sock) return Promise.resolve('unavailable');
  return new Promise((resolve) => {
    let settled = false;
    let buf = '';
    const conn = net.createConnection(sock);
    const finish = (outcome) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      request.signal?.removeEventListener?.('abort', onAbort);
      conn.destroy();
      resolve(outcome);
    };
    const onAbort = () => finish('cancelled');
    const timer = setTimeout(() => finish('unavailable'), APPROVAL_TIMEOUT_MS);
    request.signal?.addEventListener?.('abort', onAbort, { once: true });
    conn.setEncoding('utf8');
    conn.on('connect', () => {
      conn.write(JSON.stringify({
        token: process.env.FH_APPROVAL_TOKEN ?? '',
        sessionId: request.agent?.session?.id ?? null,
        toolName: request.toolName ?? null,
        callId: request.callId ?? null,
        reason: request.reason ?? '',
      }) + '\n');
    });
    conn.on('data', (chunk) => {
      buf += chunk;
      const i = buf.indexOf('\n');
      if (i < 0) return;
      try {
        const { outcome } = JSON.parse(buf.slice(0, i));
        finish(ANSWERS.has(outcome) ? outcome : 'unavailable');
      } catch {
        finish('unavailable');
      }
    });
    conn.on('error', () => finish('unavailable'));
    conn.on('close', () => finish('unavailable'));
  });
}

// ---------------------------------------------------------------- read fence

function realish(p) {
  // realpath of the nearest existing ancestor, then the remainder (the file may not exist yet)
  let cur = path.resolve(p);
  const rest = [];
  for (;;) {
    try {
      return path.join(fs.realpathSync(cur), ...rest.reverse());
    } catch {
      const parent = path.dirname(cur);
      if (parent === cur) return path.resolve(p);
      rest.push(path.basename(cur));
      cur = parent;
    }
  }
}

function inside(child, parent) {
  const rel = path.relative(parent, child);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

function privateRoots() {
  const roots = [os.homedir(), '/root', '/mnt', '/media', '/srv', '/tmp', '/var/tmp', '/run', os.tmpdir()];
  return [...new Set(roots.filter(Boolean).map(realish))];
}

function allowedRoots(workspace) {
  // dsh spills oversized tool output to <tmpdir>/dsh-spill-*, which the model is told to read back
  const out = [realish(workspace)];
  try {
    for (const d of fs.readdirSync(os.tmpdir())) {
      if (d.startsWith('dsh-spill-')) out.push(realish(path.join(os.tmpdir(), d)));
    }
  } catch { /* none */ }
  return out;
}

export function checkReadPath(target, workspace) {
  const resolved = realish(path.isAbsolute(target) ? target : path.join(workspace, target));
  if (allowedRoots(workspace).some((r) => inside(resolved, r))) return null;
  if (privateRoots().some((r) => inside(resolved, r))) {
    return `[freeharness: reading ${target} is not allowed; it is outside the workspace and in a private location]`;
  }
  return null;
}

const PATH_ARGS = { read: ['file_path'], read_image: ['file_path', 'path'], grep: ['path'], glob: ['path'] };

function checkExec(exec) {
  const keys = PATH_ARGS[exec.name];
  if (!keys) return null;
  const args = exec.arguments && typeof exec.arguments === 'object' ? exec.arguments : {};
  const workspace = exec.agent?.session?.header?.cwd ?? process.cwd();
  for (const k of keys) {
    if (typeof args[k] === 'string' && args[k].trim()) {
      const why = checkReadPath(args[k], workspace);
      if (why) return why;
    }
  }
  if (exec.name === 'glob' && typeof args.pattern === 'string') {
    const pat = args.pattern;
    if (pat.split('/').includes('..')) return '[freeharness: glob patterns may not contain ..]';
    if (path.isAbsolute(pat)) {
      const prefix = pat.split(/[*?[{]/)[0];
      const why = checkReadPath(prefix || '/', workspace);
      if (why) return why;
    }
  }
  return null;
}

export function apply(ctx) {
  ctx.on('approval/request', (request) => relay(request));
  ctx.on('tools/execute', (exec, next) => {
    const why = checkExec(exec);
    if (why) throw new Error(why);
    return next();
  });
}
