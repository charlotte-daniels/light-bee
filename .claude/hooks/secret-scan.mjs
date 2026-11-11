#!/usr/bin/env node
// secret-scan.mjs :: PreToolUse guardrail for Write|Edit|MultiEdit.
// Reads the hook payload on stdin, scans the content being written for credential
// patterns and for banned anti-patterns (client-side access gate; Dockerfile ENV PORT),
// and BLOCKS the write (exit code 2) if any match. Deterministic: same input, same verdict.
//
// Claude Code passes a JSON payload on stdin with tool_name and tool_input.
// Exit 0 = allow. Exit 2 = block; stderr is shown to the model as the reason.

import { readFileSync } from 'node:fs';

let raw = '';
try { raw = readFileSync(0, 'utf8'); } catch { process.exit(0); }

let payload = {};
try { payload = JSON.parse(raw || '{}'); } catch { process.exit(0); }

const ti = payload.tool_input || {};
// Collect every string that could carry new content across Write/Edit/MultiEdit.
const parts = [ti.content, ti.new_string, ti.file_text];
if (Array.isArray(ti.edits)) for (const e of ti.edits) parts.push(e && e.new_string);
const text = parts.filter(s => typeof s === 'string').join('\n');
if (!text) process.exit(0);

// Mirrors tools/secret_patterns.py; keep the two in step. Token formats are
// unambiguous and nothing suppresses them. Credential assignments are judged
// by the NAME being assigned (password, token, udl_pw, userPass, api_key...),
// exempt when its last segment marks it as a label (SESSION_TOKEN_COOKIE,
// API_KEY_HEADER, TOKEN_URL) or the value is plainly a placeholder. The pragma
// `secrets-gate: not-a-secret` suppresses assignments only, never token formats.
const TOKEN_RULES = [
  ['AWS access key id',          /\bAKIA[0-9A-Z]{16}\b/],
  ['Basic auth value',           /\bBasic\s+[A-Za-z0-9+/]{16,}={0,2}/],
  ['Basic auth assembled',       /["']Basic\s*["']\s*\+\s*["'][A-Za-z0-9+/=]{12,}/],
  ['Credential in a URL',        /[a-z][a-z0-9+.\-]*:\/\/[^\/\s:@]+:[^\/\s@]+@/],
  ['GitHub token',               /\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,})/],
  ['SonarQube token',            /\bsq[upa]_[0-9a-f]{30,}/],
  ['JSON web token',             /\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\./],
  ['Bearer token',               /\bBearer\s+[A-Za-z0-9._\-]{20,}/],
  ['Private key block',          /-----BEGIN[ A-Z0-9_-]{0,40}PRIVATE KEY/],
  ['LLM provider key',           /\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}/],
  ['Google API key',             /\bAIza[0-9A-Za-z_\-]{35}/],
  ['Slack token',                /\bxox[baprs]-[0-9A-Za-z\-]{10,}/],
  ['GitLab personal token',      /\bglpat-[0-9A-Za-z_\-]{20,}/],
  // Banned anti-pattern: a hardcoded client-side access gate (public artifact PIN).
  ['Client-side access gate',    /\b(?:ADMIN_)?PIN\s*=\s*['"][0-9A-Za-z]{4,}['"]/],
  // Banned anti-pattern: ENV PORT in a Dockerfile silently overrides the platform port 8080.
  ['Dockerfile ENV PORT',        /^\s*ENV\s+PORT(\s*=|\s+\S)/im]
];
const PRAGMA = 'secrets-gate: not-a-secret';
const CONTAINED = ['password', 'passwd', 'passphrase', 'credential', 'apikey'];
const EXACT = new Set(['pwd', 'pw', 'pass', 'passcode', 'passkey', 'cred', 'creds', 'login', 'auth']);
const SUFFIXES = ['token', 'tokens', 'secret', 'secrets'];
const KEY_PAIRS = new Set(['api', 'private', 'access', 'secret', 'signing', 'client', 'session',
  'hmac', 'encryption', 'master', 'jwt']);
const ROLES = new Set(['cookie', 'header', 'name', 'field', 'env', 'var', 'type', 'param', 'label',
  'url', 'uri', 'urn', 'hint', 'prompt', 'length', 'len', 'min', 'max', 'path', 'file', 'pattern',
  'regex', 'endpoint', 'scope', 'expiry', 'ttl', 'keyword', 'dir', 'directory', 'count', 'message',
  'msg', 'error', 'failed', 'required', 'rate', 'limit', 'bucket', 'mode', 'scheme', 'page', 'total',
  'failures', 'realm']);
const Q = '[rbfuRBFU]{0,2}(?:"""|\'\'\'|"|\')';
const PQ = '([rbfuRBFU]{0,2})(?:"""|\'\'\'|"|\')';
const NAME = '([A-Za-z_][\\w.\\-]*)';
// Groups: 1 name, 2 prefix, 3 value.
const QUOTED = new RegExp(NAME + '["\']?\\]?\\s*(?::[^=\\n"\']{0,60})?[:=]\\s*\\(?\\s*' + PQ + '([^"\'\\n]{6,})["\']', 'g');
const FORM_FILL = new RegExp('\\.(?:fill|type)\\(\\s*' + Q + '[^"\'\\n]*(?:password|passwd|pwd|token|secret)[^"\'\\n]*["\']\\s*,\\s*' + Q + '([^"\'\\n]{6,})["\']', 'gi');
const UNQUOTED = new RegExp('(?:^|\\s)(?:export\\s+|ENV\\s+|ARG\\s+)?' + NAME + '\\s*[:=]\\s*([^\\s"\'#\\[{][^\\s#]{5,})', 'g');
const LEGACY_ENV = new RegExp('^\\s*(?:ENV|ARG)\\s+' + NAME + '\\s+([^\\s=]\\S{5,})', 'g');
const ENV_WRITE = new RegExp('\\b(?:setdefault|putenv|setenv|getenv|get)\\(\\s*["\']' + NAME + '["\']\\s*,\\s*' + Q + '([^"\'\\n]{6,})["\']', 'g');
const AUTH_TUPLE = new RegExp('\\b(?:auth\\s*=\\s*\\(|BasicAuth\\()\\s*' + Q + '[^"\'\\n]+["\']\\s*,\\s*' + Q + '([^"\'\\n]{6,})["\']', 'g');
const PLACEHOLDER = /^(\[REDACTED:[a-z_-]+\]|\*+|\$\{[A-Z0-9_]+\}|<[a-z][a-z _-]*>|absent|missing|anonymous|none|null|guest|(not-real|fake|dummy|example|test|placeholder|changeme)([-_][a-z0-9]+)*)$/;
const CALL = /^[A-Za-z_][\w.]*\(.*\)$/;
const LABEL_VALUE = /^(.*\s.*|[a-z][a-z0-9+.\-]*:\/\/\S*|\S*\/\S*|#\S+|X-[A-Za-z]+(-[A-Za-z]+)*|[A-Z][a-z]+|[a-z][a-z0-9]*([-_.][a-z0-9]+)*)$/;
const CONFIG_SUFFIXES = ['.properties', '.toml', '.cfg', '.ini', '.yaml', '.yml', '.txt', '.env', '.md'];

function segments(name) {
  const last = name.split('.').pop();
  return last.replace(/(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])/g, '_').toLowerCase()
    .split(/[_\-.]+/).map(p => p.replace(/[0-9]+$/, '')).filter(Boolean);
}
function credentialName(name) {
  const parts = segments(name);
  if (parts.some(p => CONTAINED.some(w => p.includes(w)))) return true;
  if (parts.some(p => EXACT.has(p) || SUFFIXES.some(s => p.endsWith(s)))) return true;
  if (parts.some(p => p.endsWith('key') && KEY_PAIRS.has(p.slice(0, -3)))) return true;
  return parts.some((p, i) => KEY_PAIRS.has(p) && parts[i + 1] === 'key');
}
function isLabel(name) {
  const parts = segments(name);
  return parts.length > 0 && ROLES.has(parts[parts.length - 1]);
}
function namedHit(name, prefix, value, unquoted) {
  const v = value.trim();
  if (prefix.toLowerCase().includes('f') && v.includes('{')) return false;
  if (!credentialName(name) || !realValue(v, unquoted)) return false;
  return !(isLabel(name) && LABEL_VALUE.test(v));
}
const realValue = (v, unquoted = false) => !(unquoted && CALL.test(v.trim())) && !PLACEHOLDER.test(v.trim());
const filePath = String(ti.file_path || ti.path || '');
const base = filePath.split(/[\\/]/).pop().toLowerCase();
const isConfig = CONFIG_SUFFIXES.some(s => base.endsWith(s)) || base === '.dockerignore' || base === 'dockerfile';

function assignmentHit(line) {
  if (line.includes(PRAGMA)) return false;
  for (const m of line.matchAll(QUOTED)) if (namedHit(m[1], m[2], m[3], false)) return true;
  const named = [[ENV_WRITE, false], ...(isConfig ? [[UNQUOTED, true], [LEGACY_ENV, true]] : [])];
  for (const [re, unquoted] of named) {
    for (const m of line.matchAll(re)) if (namedHit(m[1], '', m[2], unquoted)) return true;
  }
  for (const re of [AUTH_TUPLE, FORM_FILL]) {
    for (const m of line.matchAll(re)) if (realValue(m[1])) return true;
  }
  return false;
}

const hits = [];
for (const [label, re] of TOKEN_RULES) if (re.test(text)) hits.push(label);
const joined = text.replace(/([=(\[,]|["']:)[ \t]*\n[ \t]*/g, '$1 ');
if ([...text.split('\n'), ...joined.split('\n')].some(assignmentHit)) hits.push('Credential assignment');

if (hits.length) {
  console.error(
    'BLOCKED by bluestaq-foundations secret-scan hook. The content matches: ' +
    hits.join(', ') + '.\n' +
    'No secret may be written to source: use an environment variable or a runtime ' +
    'bring-your-own-key input, and render the value as [REDACTED:type] in any file. ' +
    'A client-side access gate (a hardcoded PIN) is banned; see skills/security-hardening. ' +
    'A Dockerfile "ENV PORT=" line is banned; the app must read process.env.PORT and ' +
    'default to 8080; see skills/release-and-deploy and skills/app-store-deployment.'
  );
  process.exit(2); // block
}
process.exit(0); // allow
