#!/usr/bin/env node
'use strict';

/**
 * hg_tool.cjs — Thin Dynamic Wrapper CLI for upstream `jackotom/hongguo-drama`.
 *
 * Design contract (anti-hardcoding):
 *   - NEVER hardcodes upstream Hongguo private API routes, signing logic, or SDK internals.
 *   - Dynamically introspects and delegates to the locally installed `tools/hongguo-drama`
 *     open-source release (`src/native/*` and `scripts/sdk-playback/resolve_cli.py`).
 *   - Provides `check-upstream [--sync]` so agents always verify the latest upstream release
 *     and inspect live exported APIs before sourcing or downloading.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const readline = require('readline');
const { execFileSync, spawn } = require('child_process');

const UPSTREAM_REPO = 'jackotom/hongguo-drama';
const UPSTREAM_GIT_URL = `https://github.com/${UPSTREAM_REPO}.git`;
const UPSTREAM_RELEASE_API = `https://api.github.com/repos/${UPSTREAM_REPO}/releases/latest`;

const NATIVE_MODULES = [
  'hongguo-app-catalog.js',
  'hongguo-app-new.js',
  'hongguo-app-search.js',
  'hongguo.js',
  'app-source.js',
  'sdk-playback.js',
  'hongguo-creator-album.js',
  'public-details.js',
];

const REQUIRED_HOOKS = [
  'browseList',
  'listNewReleases',
  'searchApp',
  'fetchEpisodeList',
  'fetchAppPlayUrl',
  'deriveKey',
  'catalogCover',
];

function isValidUpstreamRepo(dir) {
  return Boolean(
    dir
    && fs.existsSync(path.join(dir, 'package.json'))
    && fs.existsSync(path.join(dir, 'src', 'native'))
  );
}

function walkUpRepoCandidates(startDir, maxDepth = 6) {
  const out = [];
  let cur = path.resolve(startDir);
  for (let i = 0; i <= maxDepth; i++) {
    out.push(path.join(cur, 'tools', 'hongguo-drama'));
    const parent = path.dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  return out;
}

function resolveRepoDir(flags = {}) {
  const candidates = [
    flags.repo,
    process.env.AICN_HONGGUO_REPO,
    ...walkUpRepoCandidates(process.cwd()),
    path.resolve(__dirname, '../../../tools/hongguo-drama'),
    path.join(os.homedir(), 'tools', 'hongguo-drama'),
  ].filter(Boolean);
  for (const dir of candidates) {
    if (isValidUpstreamRepo(dir)) return path.resolve(dir);
  }
  throw new Error(
    `hongguo-drama repo not found. Run: node "${__filename}" check-upstream --sync (or clone https://github.com/${UPSTREAM_REPO} into tools/hongguo-drama and run npm install --omit=dev --ignore-scripts)`
  );
}

function defaultCloneTarget(flags = {}) {
  if (flags.repo) return path.resolve(flags.repo);
  if (process.env.AICN_HONGGUO_REPO) return path.resolve(process.env.AICN_HONGGUO_REPO);
  return path.resolve(process.cwd(), 'tools', 'hongguo-drama');
}

function inspectUpstream(repoDir) {
  const pkgPath = path.join(repoDir, 'package.json');
  const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));

  let localCommit = '';
  let localTag = '';
  try {
    localCommit = execFileSync('git', ['-C', repoDir, 'rev-parse', '--short', 'HEAD'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {}
  try {
    localTag = execFileSync('git', ['-C', repoDir, 'describe', '--tags', '--exact-match'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    try {
      localTag = execFileSync('git', ['-C', repoDir, 'describe', '--tags', '--abbrev=0'], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
    } catch {}
  }

  const nativeDir = path.join(repoDir, 'src', 'native');
  const modules = {};
  const merged = {};
  const loadErrors = {};

  for (const file of NATIVE_MODULES) {
    const full = path.join(nativeDir, file);
    if (!fs.existsSync(full)) {
      modules[file] = null;
      continue;
    }
    try {
      delete require.cache[require.resolve(full)];
      const mod = require(full);
      const keys = Object.keys(mod);
      modules[file] = keys;
      for (const k of keys) {
        if (!(k in merged)) merged[k] = mod[k];
      }
    } catch (err) {
      modules[file] = [];
      loadErrors[file] = err.message;
    }
  }

  const missingHooks = REQUIRED_HOOKS.filter(h => typeof merged[h] !== 'function');
  const sdkCliScript = path.join(repoDir, 'scripts', 'sdk-playback', 'resolve_cli.py');
  const sdkCliExists = fs.existsSync(sdkCliScript);
  let sdkBinary = null;
  if (typeof merged.executablePath === 'function') {
    try {
      const bin = merged.executablePath(null);
      if (bin && fs.existsSync(bin)) sdkBinary = bin;
    } catch {}
  }

  let sdkCapabilities = [];
  if (sdkCliExists) {
    try {
      const src = fs.readFileSync(sdkCliScript, 'utf8');
      for (const flag of ['--serve', '--videos', '--content-type']) {
        if (src.includes(flag)) sdkCapabilities.push(flag);
      }
    } catch {}
  }

  return {
    repo_dir: repoDir,
    local_version: pkg.version || '',
    local_tag: localTag,
    local_commit: localCommit,
    node_modules_present: fs.existsSync(path.join(repoDir, 'node_modules')),
    api_surface: modules,
    load_errors: loadErrors,
    missing_hooks: missingHooks,
    wrapper_contract_ok: missingHooks.length === 0 && Object.keys(loadErrors).length === 0,
    sdk_cli: {
      script: sdkCliExists ? sdkCliScript : null,
      binary: sdkBinary,
      capabilities: sdkCapabilities,
    },
    _merged: merged,
  };
}

let _cachedRepoDir = null;
let _hgNative = null;
function loadHongguo(flags = {}) {
  const repoDir = resolveRepoDir(flags);
  if (_hgNative && _cachedRepoDir === repoDir) return _hgNative;
  const info = inspectUpstream(repoDir);
  _cachedRepoDir = repoDir;
  _hgNative = info._merged;
  return _hgNative;
}

function normalizeVersionTuple(raw) {
  const cleaned = String(raw || '').trim().replace(/^v/i, '').split('-')[0];
  if (!cleaned) return [];
  const nums = cleaned.split('.').map(part => parseInt(part, 10)).filter(Number.isFinite);
  while (nums.length > 1 && nums[nums.length - 1] === 0) nums.pop();
  return nums;
}

function versionsEqual(a, b) {
  const ta = normalizeVersionTuple(a);
  const tb = normalizeVersionTuple(b);
  if (!ta.length || !tb.length || ta.length !== tb.length) return false;
  return ta.every((v, i) => v === tb[i]);
}

function compareSemverTag(a, b) {
  const ta = normalizeVersionTuple(a);
  const tb = normalizeVersionTuple(b);
  const len = Math.max(ta.length, tb.length);
  for (let i = 0; i < len; i++) {
    const da = ta[i] || 0;
    const db = tb[i] || 0;
    if (da !== db) return da - db;
  }
  return 0;
}

function extractHighlights(body, maxLines = 8) {
  return String(body || '')
    .split(/\r?\n/)
    .map(l => l.trim())
    .filter(l => /^[-*]\s+/.test(l))
    .slice(0, maxLines);
}

async function fetchLatestUpstreamRelease(repoDir) {
  const headers = {
    'User-Agent': 'aicn-hg-tool',
    Accept: 'application/vnd.github+json',
  };
  const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    const resp = await fetch(UPSTREAM_RELEASE_API, {
      headers,
      signal: AbortSignal.timeout(8000),
    });
    if (resp.ok) {
      const data = await resp.json();
      if (data && data.tag_name) {
        return {
          source: 'github-api',
          tag: String(data.tag_name),
          name: String(data.name || data.tag_name),
          published_at: String(data.published_at || ''),
          url: String(data.html_url || `https://github.com/${UPSTREAM_REPO}/releases/tag/${data.tag_name}`),
          highlights: extractHighlights(data.body),
        };
      }
    }
  } catch {}

  try {
    const out = execFileSync(
      'gh',
      ['release', 'view', '-R', UPSTREAM_REPO, '--json', 'tagName,name,publishedAt,url,body'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 8000 }
    );
    const data = JSON.parse(out);
    if (data && data.tagName) {
      return {
        source: 'gh-cli',
        tag: String(data.tagName),
        name: String(data.name || data.tagName),
        published_at: String(data.publishedAt || ''),
        url: String(data.url || ''),
        highlights: extractHighlights(data.body),
      };
    }
  } catch {}

  try {
    const args = repoDir
      ? ['-C', repoDir, 'ls-remote', '--tags', '--refs', 'origin']
      : ['ls-remote', '--tags', '--refs', UPSTREAM_GIT_URL];
    const out = execFileSync('git', args, {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 8000,
    });
    const tags = out
      .split(/\r?\n/)
      .map(line => line.split(/\s+/)[1] || '')
      .filter(ref => ref.startsWith('refs/tags/v'))
      .map(ref => ref.slice('refs/tags/'.length))
      .filter(tag => /^v\d+(?:\.\d+)*$/i.test(tag))
      .sort(compareSemverTag);
    if (tags.length) {
      const latest = tags[tags.length - 1];
      return {
        source: 'git-tags',
        tag: latest,
        name: latest,
        published_at: '',
        url: `https://github.com/${UPSTREAM_REPO}/releases/tag/${latest}`,
        highlights: [],
      };
    }
  } catch {}

  return null;
}

function runNpmInstallRuntime(repoDir) {
  if (process.platform === 'win32') {
    execFileSync('cmd.exe', ['/d', '/s', '/c', 'npm.cmd install --omit=dev --ignore-scripts'], {
      cwd: repoDir,
      stdio: 'inherit',
    });
  } else {
    execFileSync('npm', ['install', '--omit=dev', '--ignore-scripts'], {
      cwd: repoDir,
      stdio: 'inherit',
    });
  }
}

function syncUpstreamToTag(targetDir, tag) {
  if (!isValidUpstreamRepo(targetDir)) {
    fs.mkdirSync(path.dirname(targetDir), { recursive: true });
    execFileSync(
      'git',
      ['clone', '--depth', '1', '--branch', tag, UPSTREAM_GIT_URL, targetDir],
      { stdio: 'inherit' }
    );
    runNpmInstallRuntime(targetDir);
    return true;
  }

  const pkgBefore = fs.readFileSync(path.join(targetDir, 'package.json'), 'utf8');
  execFileSync('git', ['-C', targetDir, 'fetch', '--tags', '--depth=1', 'origin', 'tag', tag], {
    stdio: 'inherit',
  });
  execFileSync('git', ['-C', targetDir, 'checkout', '-f', tag], {
    stdio: 'inherit',
  });
  const pkgAfter = fs.readFileSync(path.join(targetDir, 'package.json'), 'utf8');
  if (!fs.existsSync(path.join(targetDir, 'node_modules')) || pkgBefore !== pkgAfter) {
    runNpmInstallRuntime(targetDir);
  }
  return true;
}

function resolveBin(name, envKey) {
  if (process.env[envKey] && fs.existsSync(process.env[envKey])) {
    return process.env[envKey];
  }
  const exe = process.platform === 'win32' ? `${name}.exe` : name;
  const candidates = process.platform === 'win32'
    ? [path.join(os.homedir(), '.local', 'bin', exe)]
    : [`/opt/homebrew/bin/${name}`, `/usr/local/bin/${name}`, path.join(os.homedir(), '.local', 'bin', name)];
  for (const cand of candidates) {
    if (fs.existsSync(cand)) return cand;
  }
  return name;
}

const FFMPEG = resolveBin('ffmpeg', 'AICN_FFMPEG');
const FFPROBE = resolveBin('ffprobe', 'AICN_FFPROBE');

function parseArgs(argv) {
  const args = argv.slice(2);
  const cmd = args[0];
  const positional = [];
  const flags = {};
  for (let i = 1; i < args.length; i++) {
    const token = args[i];
    if (token.startsWith('--')) {
      const key = token.slice(2);
      const next = args[i + 1];
      if (!next || next.startsWith('--')) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i++;
      }
    } else {
      positional.push(token);
    }
  }
  return { cmd, positional, flags };
}

function parseEpisodeSpec(spec, total) {
  if (!spec || spec === 'all') {
    return Array.from({ length: total }, (_, i) => i + 1);
  }
  const set = new Set();
  for (const part of String(spec).split(',')) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    if (trimmed.includes('-')) {
      const [aStr, bStr] = trimmed.split('-');
      const a = Math.max(1, parseInt(aStr, 10));
      const b = Math.min(total, parseInt(bStr, 10));
      if (!Number.isFinite(a) || !Number.isFinite(b) || a > b) {
        throw new Error(`Invalid episode range: ${trimmed}`);
      }
      for (let n = a; n <= b; n++) set.add(n);
    } else {
      const n = parseInt(trimmed, 10);
      if (!Number.isFinite(n) || n < 1 || n > total) {
        throw new Error(`Invalid episode number: ${trimmed} (total=${total})`);
      }
      set.add(n);
    }
  }
  return [...set].sort((a, b) => a - b);
}

function probeDuration(filePath) {
  try {
    const out = execFileSync(FFPROBE, [
      '-v', 'error',
      '-show_entries', 'format=duration',
      '-of', 'default=noprint_wrappers=1:nokey=1',
      filePath,
    ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
    const d = parseFloat(out);
    return Number.isFinite(d) && d > 1 ? d : 0;
  } catch {
    return 0;
  }
}

async function cmdCheckUpstream(flags) {
  let repoDir = null;
  try {
    repoDir = resolveRepoDir(flags);
  } catch {
    repoDir = null;
  }

  let latest = null;
  if (!flags.offline) {
    latest = await fetchLatestUpstreamRelease(repoDir);
  }

  let synced = false;
  if (flags.sync) {
    if (!latest?.tag) {
      throw new Error('Cannot --sync: unable to fetch latest release tag from GitHub');
    }
    const targetDir = repoDir || defaultCloneTarget(flags);
    const localInfoBefore = isValidUpstreamRepo(targetDir) ? inspectUpstream(targetDir) : null;
    const alreadyCurrent = Boolean(
      localInfoBefore
      && (localInfoBefore.local_tag === latest.tag || versionsEqual(localInfoBefore.local_version, latest.tag))
      && localInfoBefore.node_modules_present
    );
    if (!alreadyCurrent || flags.force) {
      syncUpstreamToTag(targetDir, latest.tag);
      synced = true;
    }
    repoDir = targetDir;
  }

  if (!repoDir || !isValidUpstreamRepo(repoDir)) {
    const report = {
      upstream_repo: UPSTREAM_REPO,
      installed: false,
      synced,
      latest_tag: latest?.tag || null,
      latest_published_at: latest?.published_at || null,
      latest_url: latest?.url || null,
      up_to_date: false,
      wrapper_contract_ok: false,
      hint: `Run: node "${__filename}" check-upstream --sync`,
    };
    if (flags.json) console.log(JSON.stringify(report, null, 2));
    else console.error(`[hg_tool] Upstream repo not installed. Latest release: ${latest?.tag || 'unknown'}. Run with --sync.`);
    process.exitCode = 1;
    return;
  }

  const local = inspectUpstream(repoDir);
  const upToDate = latest?.tag
    ? (local.local_tag === latest.tag || versionsEqual(local.local_version, latest.tag))
    : null;

  const report = {
    upstream_repo: UPSTREAM_REPO,
    installed: true,
    synced,
    repo_dir: local.repo_dir,
    local_tag: local.local_tag || null,
    local_version: local.local_version,
    local_commit: local.local_commit,
    latest_tag: latest?.tag || null,
    latest_source: latest?.source || (flags.offline ? 'offline' : 'unavailable'),
    latest_published_at: latest?.published_at || null,
    latest_url: latest?.url || null,
    up_to_date: upToDate,
    release_highlights: latest?.highlights || [],
    node_modules_present: local.node_modules_present,
    sdk_cli: local.sdk_cli,
    api_surface: local.api_surface,
    load_errors: local.load_errors,
    missing_hooks: local.missing_hooks,
    wrapper_contract_ok: local.wrapper_contract_ok,
  };

  if (flags.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`=== Upstream Preflight: ${UPSTREAM_REPO} ===`);
    console.log(`  Local   : ${local.local_tag || 'untagged'} (pkg ${local.local_version}, commit ${local.local_commit}) @ ${local.repo_dir}`);
    console.log(`  Latest  : ${latest?.tag || 'unavailable'}${latest?.published_at ? ` (${latest.published_at})` : ''}`);
    console.log(`  Status  : ${upToDate === true ? 'UP TO DATE' : upToDate === false ? 'UPDATE AVAILABLE -> run check-upstream --sync' : 'OFFLINE/UNCHECKED'}`);
    console.log(`  Contract: ${local.wrapper_contract_ok ? 'OK' : `BROKEN (missing: ${local.missing_hooks.join(', ')})`}`);
    console.log(`  SDK CLI : ${local.sdk_cli.binary || local.sdk_cli.script || 'none'} (${local.sdk_cli.capabilities.join(', ') || 'n/a'})`);
    if (latest?.highlights?.length) {
      console.log('  Notes   :');
      for (const line of latest.highlights.slice(0, 5)) console.log(`    ${line}`);
    }
  }

  if (!local.wrapper_contract_ok) {
    process.exitCode = 1;
  } else if (flags.strict && upToDate === false) {
    process.exitCode = 2;
  }
}

async function cmdRank(flags) {
  const category = flags.category || 'hot-ai-drama';
  const page = parseInt(flags.page || '1', 10);
  const genre = flags.genre || '';
  const { browseList } = loadHongguo(flags);
  const res = await browseList({ category, genre, page, forceRefresh: true });
  if (!res || res.success === false) {
    throw new Error(`browseList failed: ${res?.error || 'unknown error'}`);
  }
  const items = res.results || res.items || res.list || res.dramas || [];
  if (flags.json) {
    console.log(JSON.stringify({ category, page, count: items.length, items }, null, 2));
    return;
  }
  console.log(`=== Hongguo Rank: ${category} (page ${page}, ${items.length} items) ===`);
  items.forEach((it, idx) => {
    console.log(
      `${String(idx + 1).padStart(2, '0')}. [${it.series_id}] ${it.series_title} | eps=${it.episode_count || '?'} | tags=${(it.tags || []).join('/')}`
    );
  });
}

async function cmdNew(flags) {
  const pages = parseInt(flags.pages || '2', 10);
  const limit = parseInt(flags.limit || '20', 10);
  const filterGenre = flags.genre || 'all';
  const { listNewReleases } = loadHongguo(flags);
  const res = await listNewReleases({ pages, useCache: false });
  const rawItems = res.items || [];
  let items = rawItems;
  let genreFilterApplied = filterGenre === 'all';
  if (filterGenre === 'ai') {
    const matched = rawItems.filter(it => (it.tags || []).some(t => /AI|漫剧|仿真/i.test(t)) || it.genre === 'ai_series');
    if (matched.length) {
      items = matched;
      genreFilterApplied = true;
    } else if (rawItems.length) {
      console.error('[hg_tool] Note: upstream listNewReleases() omits per-item genre metadata; returning unfiltered new releases (use "rank --category hot-ai-drama" for AI-only charts).');
    }
  } else if (filterGenre === 'real') {
    const matched = rawItems.filter(it => it.genre === 'short_play' || (it.tags || []).some(t => /真人/i.test(t)));
    if (matched.length) {
      items = matched;
      genreFilterApplied = true;
    } else if (rawItems.length) {
      console.error('[hg_tool] Note: upstream listNewReleases() omits per-item genre metadata; returning unfiltered new releases (use "rank --category hot-real-drama" for real-only charts).');
    }
  }
  items = items.slice(0, limit);
  if (flags.json) {
    console.log(JSON.stringify({ count: items.length, partial: !!res.partial, genre_filter_applied: genreFilterApplied, items }, null, 2));
    return;
  }
  console.log(`=== Hongguo New Releases (showing ${items.length}) ===`);
  items.forEach((it, idx) => {
    console.log(
      `${String(idx + 1).padStart(2, '0')}. [${it.series_id}] ${it.series_title} | eps=${it.episode_count || '?'} | tags=${(it.tags || it.labels || []).join('/')}`
    );
  });
}

async function cmdSearch(positional, flags) {
  const keyword = positional.join(' ').trim();
  if (!keyword) throw new Error('Usage: hg_tool.cjs search "<keyword>" [--page 1] [--json]');
  const { searchApp } = loadHongguo(flags);
  const page = parseInt(flags.page || '1', 10);
  const res = await searchApp(keyword, { page });
  const items = res?.items || res?.results || res?.list || (Array.isArray(res) ? res : []);
  if (flags.json) {
    console.log(JSON.stringify({ keyword, page, count: items.length, items }, null, 2));
    return;
  }
  console.log(`=== Hongguo Search: "${keyword}" (${items.length} items) ===`);
  items.forEach((it, idx) => {
    console.log(
      `${String(idx + 1).padStart(2, '0')}. [${it.series_id || it.id}] ${it.series_title || it.title} | eps=${it.episode_count || '?'} | tags=${(it.tags || []).join('/')}`
    );
  });
}

async function cmdDetails(positional, flags) {
  const ids = positional.filter(Boolean);
  if (!ids.length) throw new Error('Usage: hg_tool.cjs details <series_id...> [--vid <vid>] [--json]');
  const { fetchPublicDetails } = loadHongguo(flags);
  if (typeof fetchPublicDetails !== 'function') {
    throw new Error('Upstream public-details.js fetchPublicDetails() not available in this release');
  }
  const results = {};
  for (const sid of ids) {
    results[sid] = await fetchPublicDetails(sid, ids.length === 1 ? (flags.vid || '') : '');
  }
  if (flags.json) {
    console.log(JSON.stringify(ids.length === 1 && !flags.batch ? results[ids[0]] : results, null, 2));
    return;
  }
  for (const [sid, d] of Object.entries(results)) {
    if (!d?.success) {
      console.log(`[${sid}] FAIL: ${d?.error || 'unknown'}`);
    } else {
      console.log(`[${sid}] ${d.episode_label || d.episode_count || '?'} | rating=${d.rating ?? '?'} | likes=${d.like_count ?? '?'} | favs=${d.favorite_count ?? '?'} | published_at=${d.published_at || d.first_visible_time || '?'}`);
    }
  }
}

async function cmdAlbum(positional, flags) {
  const albumId = positional[0];
  if (!albumId) throw new Error('Usage: hg_tool.cjs album <album_id> [--json]');
  const { fetchCreatorAlbum } = loadHongguo(flags);
  if (typeof fetchCreatorAlbum !== 'function') {
    throw new Error('Upstream hongguo-creator-album.js fetchCreatorAlbum() not available in this release');
  }
  const data = await fetchCreatorAlbum(albumId);
  if (flags.json) {
    console.log(JSON.stringify(data, null, 2));
    return;
  }
  console.log(`Album: ${data.series_title || albumId} (${data.total} items, content_type=1001)`);
  (data.episodes || []).slice(0, 10).forEach(e => {
    console.log(`  ep_${String(e.vid_index).padStart(3, '0')}: vid=${e.vid} ${e.title}`);
  });
}

async function cmdEpisodes(positional, flags) {
  const seriesId = positional[0];
  if (!seriesId) throw new Error('Usage: hg_tool.cjs episodes <series_id> [--json]');
  const { fetchEpisodeList, catalogCover } = loadHongguo(flags);
  const data = await fetchEpisodeList(seriesId);
  const eps = data?.episodes || data?.list || (Array.isArray(data) ? data : []);
  const totalSec = eps.reduce((acc, e) => acc + (Number(e.duration) || 0), 0);
  const summary = {
    series_id: seriesId,
    title: data?.seriesTitle || data?.series_title || data?.title || '',
    total_episodes: eps.length,
    total_duration_sec: Math.round(totalSec),
    total_duration_min: Number((totalSec / 60).toFixed(1)),
    cover: data?.cover ? catalogCover(data.cover) : '',
    episodes: eps.map((e, i) => ({
      index: e.episode || e.vid_index || e.index || (i + 1),
      video_id: String(e.vid || e.video_id || e.id || ''),
      content_type: String(e.content_type || e.contentType || '1004'),
      title: e.title || `第${i + 1}集`,
      duration: Number(e.duration) || 0,
    })),
  };
  if (flags.json) {
    console.log(JSON.stringify(summary, null, 2));
    return;
  }
  console.log(`Series: ${summary.title || seriesId} (${summary.total_episodes} eps, ${summary.total_duration_min} min)`);
  summary.episodes.slice(0, 10).forEach(e => {
    console.log(`  ep_${String(e.index).padStart(3, '0')}: vid=${e.video_id} (${e.duration}s) ${e.title}`);
  });
  if (summary.episodes.length > 10) {
    console.log(`  ... (${summary.episodes.length - 10} more episodes)`);
  }
}

function resolvePythonBin() {
  const candidates = [
    process.env.AICN_PY,
    process.platform === 'win32'
      ? 'C:\\Work\\Tools\\VieNeu-TTS\\.venv\\Scripts\\python.exe'
      : '/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python',
    'python',
    'python3',
  ].filter(Boolean);
  for (const cand of candidates) {
    if (cand.includes(path.sep) && !fs.existsSync(cand)) continue;
    return cand;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

function selectSdkVariant(model, quality = '1080p') {
  const rawList = model?.video_list;
  const rows = (Array.isArray(rawList) ? rawList : Object.values(rawList || {})).map(item => {
    const meta = item?.video_meta || {};
    const codec = String(item?.codec_type || meta.codec_type || '').toLowerCase();
    const codecRank = ['h264', 'avc', 'avc1'].includes(codec)
      ? 0
      : ['hevc', 'h265', 'hvc1', 'hev1', 'bytevc1'].includes(codec)
        ? 1
        : codec === 'bytevc2'
          ? 2
          : 3;
    const width = Number(item?.vwidth || item?.width || meta.vwidth || meta.width || 0) || 0;
    const height = Number(item?.vheight || item?.height || meta.vheight || meta.height || 0) || 0;
    const defStr = String(item?.quality_desc || item?.definition || meta.definition || '');
    const pixels = width && height ? Math.min(width, height) : Number(defStr.match(/\d+/)?.[0] || 0);
    const url = item?.main_url || item?.backup_url || item?.backup_url_list?.[0] || '';
    const spadeA = item?.spade_a || item?.encrypt_info?.spade_a || '';
    return { item, codec, codecRank, width, height, pixels, defStr, url, spadeA };
  }).filter(r => r.url && r.codecRank < 2);

  if (!rows.length) return null;
  const targetP = quality === 'auto' ? 0 : parseInt(String(quality).match(/\d+/)?.[0] || '0', 10);
  rows.sort((a, b) => {
    if (targetP > 0) {
      const aOk = a.pixels >= targetP ? 1 : 0;
      const bOk = b.pixels >= targetP ? 1 : 0;
      if (aOk !== bOk) return bOk - aOk;
      if (aOk && bOk && a.pixels !== b.pixels) return a.pixels - b.pixels;
    }
    if (a.pixels !== b.pixels) return b.pixels - a.pixels;
    return a.codecRank - b.codecRank;
  });
  const chosen = rows[0];
  return {
    url: String(chosen.url).replace(/\\\//g, '/').replace(/\\u0026/g, '&').replace(/&amp;/g, '&'),
    spadeA: chosen.spadeA || null,
    codec: chosen.codec === 'bytevc1' ? 'hevc' : chosen.codec,
    quality: chosen.pixels ? `${chosen.pixels}p` : (chosen.defStr || quality),
  };
}

/**
 * Thin JSONL `--serve` client delegating directly to upstream `hongguo-drama`:
 *   1. Compiled upstream binary (`sdk-playback.js executablePath()`), if present.
 *   2. Upstream Python CLI (`scripts/sdk-playback/resolve_cli.py --serve`), supporting
 *      PR #189 cross-platform locks & session reuse + PR #190 up-to-8 batch resolution.
 */
class UpstreamSdkClient {
  constructor(repoDir, upstream) {
    this.repoDir = repoDir;
    this.upstream = upstream;
    this.scriptPath = path.join(repoDir, 'scripts', 'sdk-playback', 'resolve_cli.py');
    this.binaryPath = null;
    if (typeof upstream?.executablePath === 'function') {
      try {
        const cand = upstream.executablePath(null);
        if (cand && fs.existsSync(cand)) this.binaryPath = cand;
      } catch {}
    }
    this.maxBatch = Number(upstream?.MAX_BATCH_VIDEOS) || 8;
    this.proc = null;
    this.rl = null;
    this.nextId = 1;
    this.pending = new Map();
    this.modelCache = new Map();
    this.broken = false;
  }

  isSupported() {
    return Boolean(this.binaryPath || fs.existsSync(this.scriptPath));
  }

  _cacheKey(vid, contentType) {
    return `${contentType}:${vid}`;
  }

  _readCached(vid, contentType) {
    const key = this._cacheKey(vid, contentType);
    const entry = this.modelCache.get(key);
    if (!entry) return null;
    if (entry.expiresAt <= Date.now()) {
      this.modelCache.delete(key);
      return null;
    }
    return entry.value;
  }

  _storeCached(vid, contentType, value) {
    const key = this._cacheKey(vid, contentType);
    this.modelCache.set(key, { value, expiresAt: Date.now() + 2 * 60 * 1000 });
  }

  _start() {
    if (this.proc || this.broken) return;
    const args = ['--serve'];
    const cacheDir = typeof this.upstream?.resolveCacheDir === 'function'
      ? this.upstream.resolveCacheDir(null)
      : null;
    if (cacheDir) {
      fs.mkdirSync(cacheDir, { recursive: true });
      args.push('--cache', cacheDir);
    }

    const cmd = this.binaryPath || resolvePythonBin();
    const fullArgs = this.binaryPath ? args : [this.scriptPath, ...args];
    this.proc = spawn(cmd, fullArgs, {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.rl = readline.createInterface({ input: this.proc.stdout });
    this.rl.on('line', line => {
      const trimmed = line.trim();
      if (!trimmed) return;
      let msg;
      try {
        msg = JSON.parse(trimmed);
      } catch {
        return;
      }
      const waiter = this.pending.get(msg.id);
      if (!waiter) return;
      this.pending.delete(msg.id);
      waiter.resolve(msg);
    });
    const failAll = err => {
      this.broken = true;
      this.proc = null;
      for (const waiter of this.pending.values()) waiter.reject(err);
      this.pending.clear();
    };
    this.proc.on('error', failAll);
    this.proc.on('close', code => {
      if (this.pending.size) failAll(new Error(`Upstream SDK helper exited (${code})`));
      this.proc = null;
    });
  }

  _request(payload) {
    this._start();
    if (!this.proc || this.broken) return Promise.resolve(null);
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      try {
        this.proc.stdin.write(JSON.stringify({ id, ...payload }) + '\n');
      } catch (err) {
        this.pending.delete(id);
        reject(err);
      }
    });
  }

  async resolve(vid, { quality = '1080p', contentType = '1004', batchVids = [] } = {}) {
    if (!this.isSupported() || this.broken) return null;
    const vidStr = String(vid);
    const cached = this._readCached(vidStr, contentType);
    if (cached) {
      const picked = selectSdkVariant(cached.model, quality);
      return picked ? { ...picked, userAgent: cached.userAgent, source: 'sdk', sourceProvider: 'sdk' } : null;
    }

    const batch = [];
    const seen = new Set();
    for (const raw of [vidStr, ...batchVids]) {
      const s = String(raw || '').trim();
      if (!/^[1-9]\d{7,23}$/.test(s) || seen.has(s) || this._readCached(s, contentType)) continue;
      seen.add(s);
      batch.push(s);
      if (batch.length >= this.maxBatch) break;
    }
    if (!batch.length) return null;

    const res = batch.length > 1
      ? await this._request({ videos: batch, contentType })
      : await this._request({ video: batch[0], contentType });

    if (!res?.ok) return null;
    if (res.models && typeof res.models === 'object') {
      for (const [k, m] of Object.entries(res.models)) {
        if (m && typeof m === 'object') {
          this._storeCached(k, contentType, { model: m, userAgent: res.userAgent || '' });
        }
      }
    } else if (res.model && typeof res.model === 'object') {
      this._storeCached(vidStr, contentType, { model: res.model, userAgent: res.userAgent || '' });
    }

    const entry = this._readCached(vidStr, contentType);
    if (!entry) return null;
    const picked = selectSdkVariant(entry.model, quality);
    return picked ? { ...picked, userAgent: entry.userAgent, source: 'sdk', sourceProvider: 'sdk' } : null;
  }

  close() {
    try {
      if (this.proc && !this.proc.killed && this.proc.stdin?.writable) {
        this.proc.stdin.write(JSON.stringify({ cmd: 'quit' }) + '\n');
        this.proc.stdin.end();
      }
    } catch {}
    try { this.rl?.close(); } catch {}
    try { this.proc?.kill(); } catch {}
    this.proc = null;
  }
}

async function cmdDownload(positional, flags) {
  const seriesId = positional[0];
  const outDir = flags.out || flags.dir;
  if (!seriesId || !outDir) {
    throw new Error('Usage: hg_tool.cjs download <series_id> --out <dir> [--episodes 1-10|all] [--quality 1080p|720p|auto] [--content-type 1004|1001] [--delay-ms 1500]');
  }
  const repoDir = resolveRepoDir(flags);
  const upstream = loadHongguo(flags);
  const { fetchEpisodeList, fetchCreatorAlbum, deriveKey, fetchAppPlayUrl, fetchPublicFallbackPlayUrl, UA } = upstream;
  const quality = flags.quality || '1080p';
  const delayMs = parseInt(flags['delay-ms'] || '1500', 10);
  const preferSdk = Boolean(flags['prefer-sdk']);
  const allowWebFallback = Boolean(flags['allow-web-fallback']);
  const defaultContentType = String(flags['content-type'] || '1004');
  const sdkClient = new UpstreamSdkClient(repoDir, upstream);

  fs.mkdirSync(outDir, { recursive: true });
  console.log(`[hg_tool] Resolving episode list for series_id=${seriesId}...`);
  const data = defaultContentType === '1001' && typeof fetchCreatorAlbum === 'function'
    ? await fetchCreatorAlbum(seriesId)
    : await fetchEpisodeList(seriesId);
  const eps = data?.episodes || data?.list || (Array.isArray(data) ? data : []);
  if (!eps.length) throw new Error(`No episodes returned for series_id=${seriesId}`);

  const wanted = parseEpisodeSpec(flags.episodes || 'all', eps.length);
  console.log(`[hg_tool] Plan: download ${wanted.length}/${eps.length} episodes -> ${outDir} (quality=${quality}, delay=${delayMs}ms)`);

  let downloaded = 0;
  let skipped = 0;
  const failed = [];

  try {
    for (let i = 0; i < wanted.length; i++) {
      const epNum = wanted[i];
      const ep = eps[epNum - 1];
      const vid = String(ep.vid || ep.video_id || ep.id || '');
      const contentType = String(ep.content_type || ep.contentType || defaultContentType || '1004');
      const outFile = path.join(outDir, `ep_${String(epNum).padStart(3, '0')}.mp4`);

      if (fs.existsSync(outFile) && probeDuration(outFile) > 5) {
        console.log(`[${i + 1}/${wanted.length}] SKIP ep_${String(epNum).padStart(3, '0')}.mp4 (already valid)`);
        skipped++;
        continue;
      }

      try {
        const t0 = Date.now();
        const lookaheadVids = wanted
          .slice(i, i + sdkClient.maxBatch)
          .map(n => eps[n - 1])
          .filter(item => String(item?.content_type || item?.contentType || defaultContentType || '1004') === contentType)
          .map(item => String(item?.vid || item?.video_id || item?.id || ''))
          .filter(Boolean);

        let play = null;
        let primaryErr = null;

        if (preferSdk && sdkClient.isSupported()) {
          try {
            play = await sdkClient.resolve(vid, { quality, contentType, batchVids: lookaheadVids });
          } catch (_) {
            play = null;
          }
        }

        // Upstream v8.2 unified priority: Official App entry first -> SDK fallback -> optional web fallback.
        if (!play) {
          try {
            play = await fetchAppPlayUrl(vid, { quality, allowLowerQuality: true, contentType });
          } catch (err) {
            primaryErr = err;
          }
        }

        if (!play && !preferSdk && sdkClient.isSupported()) {
          try {
            play = await sdkClient.resolve(vid, { quality, contentType, batchVids: lookaheadVids });
          } catch (_) {
            play = null;
          }
        }

        if (!play && allowWebFallback && typeof fetchPublicFallbackPlayUrl === 'function') {
          try {
            play = await fetchPublicFallbackPlayUrl(vid);
          } catch (_) {
            play = null;
          }
        }

        if (!play) {
          throw primaryErr || new Error('No playable stream URL returned by upstream official or SDK paths');
        }

        const url = play.url || play.mainUrl || play.variants?.[0]?.url;
        if (!url) throw new Error('No playable stream URL in response');

        const spade = play.spadeA || play.spade_a || play.keyMaterial || play.variants?.[0]?.spadeA;
        let keyHex = play.contentKey || play.keyHex || play.key || '';
        if (!keyHex && spade) {
          const keyBuf = deriveKey(spade);
          if (keyBuf) keyHex = Buffer.isBuffer(keyBuf) ? keyBuf.toString('hex') : String(keyBuf);
        }

        const reqHeaders = {};
        if (play.userAgent || UA) reqHeaders['User-Agent'] = play.userAgent || UA;
        if (play.referer) reqHeaders.Referer = play.referer;

        const resp = await fetch(url, { headers: reqHeaders });
        if (!resp.ok) throw new Error(`CDN HTTP ${resp.status}`);
        const buf = Buffer.from(await resp.arrayBuffer());
        const tmpRaw = `${outFile}.raw.tmp`;
        const tmpOut = `${outFile}.dec.tmp.mp4`;
        fs.writeFileSync(tmpRaw, buf);

        if (keyHex) {
          execFileSync(FFMPEG, [
            '-y', '-loglevel', 'error',
            '-decryption_key', keyHex,
            '-i', tmpRaw,
            '-c', 'copy',
            tmpOut,
          ]);
          fs.unlinkSync(tmpRaw);
          fs.renameSync(tmpOut, outFile);
        } else {
          fs.renameSync(tmpRaw, outFile);
        }

        const dur = probeDuration(outFile);
        if (dur <= 1) throw new Error('ffprobe validation failed after decrypt');
        const mb = (fs.statSync(outFile).size / 1024 / 1024).toFixed(2);
        const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
        const provider = play.sourceProvider || play.source || 'app';
        console.log(`[${i + 1}/${wanted.length}] OK ep_${String(epNum).padStart(3, '0')}.mp4 | ${mb} MB | ${dur.toFixed(1)}s | ${play.quality || quality} (${provider}) | ${elapsed}s`);
        downloaded++;
      } catch (err) {
        console.error(`[${i + 1}/${wanted.length}] FAIL ep_${String(epNum).padStart(3, '0')} (vid=${vid}): ${err.message}`);
        failed.push({ epNum, vid, error: err.message });
      }

      if (i < wanted.length - 1 && delayMs > 0) {
        await new Promise(r => setTimeout(r, delayMs));
      }
    }
  } finally {
    sdkClient.close();
  }

  console.log(`[hg_tool] Summary: downloaded=${downloaded}, skipped=${skipped}, failed=${failed.length}, out=${outDir}`);
  if (failed.length > 0) {
    process.exitCode = 1;
  }
}

async function cmdFetchCovers(flags) {
  const inputPath = flags.input;
  const outDir = flags.out;
  if (!inputPath || !outDir) {
    throw new Error('Usage: hg_tool.cjs fetch-covers --input <catalog.json> --out <covers_dir>');
  }
  const { catalogCover } = loadHongguo(flags);
  fs.mkdirSync(outDir, { recursive: true });
  const raw = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
  const items = Array.isArray(raw) ? raw : (raw.items || raw.candidates || []);
  let ok = 0;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const coverUrl = catalogCover(it.cover || it.cover_url || '');
    if (!coverUrl) continue;
    const slug = it.slug || it.series_id || `item_${i + 1}`;
    const jpgPath = path.join(outDir, `${i + 1}_${slug}.jpg`);
    const tmpPath = `${jpgPath}.raw`;
    try {
      const resp = await fetch(coverUrl);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      fs.writeFileSync(tmpPath, Buffer.from(await resp.arrayBuffer()));
      execFileSync(FFMPEG, ['-y', '-loglevel', 'error', '-i', tmpPath, '-q:v', '2', jpgPath]);
      fs.unlinkSync(tmpPath);
      console.log(`[cover] OK ${jpgPath}`);
      ok++;
    } catch (e) {
      console.error(`[cover] FAIL ${slug}: ${e.message}`);
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    }
  }
  console.log(`[hg_tool] Covers saved: ${ok}/${items.length} -> ${outDir}`);
}

function printHelp() {
  console.log(`hg_tool.cjs — Thin Dynamic Wrapper CLI for upstream ${UPSTREAM_REPO}

Commands:
  check-upstream [--sync] [--strict] [--offline] [--json] [--repo <dir>]
                 Verify latest upstream GitHub release, optionally sync tools/hongguo-drama,
                 and introspect live exported native APIs & SDK helper capabilities.
  rank           --category hot-ai-drama|hot-real-drama|hot-comic-drama|hot-drama [--page 1] [--json]
  new            [--genre all|ai|real] [--pages 2] [--limit 20] [--json]
  search         "<keyword>" [--page 1] [--json]
  details        <series_id...> [--vid <vid>] [--json]
  album          <album_id> [--json]
  episodes       <series_id> [--json]
  download       <series_id> --out <dir> [--episodes 1-10|all] [--quality 1080p|720p|auto]
                 [--content-type 1004|1001] [--prefer-sdk] [--allow-web-fallback] [--delay-ms 1500]
  fetch-covers   --input <catalog.json> --out <covers_dir>
`);
}

async function main() {
  const { cmd, positional, flags } = parseArgs(process.argv);
  if (!cmd || cmd === '--help' || cmd === '-h' || cmd === 'help') {
    printHelp();
    return;
  }
  switch (cmd) {
    case 'check-upstream': return cmdCheckUpstream(flags);
    case 'rank': return cmdRank(flags);
    case 'new': return cmdNew(flags);
    case 'search': return cmdSearch(positional, flags);
    case 'details': return cmdDetails(positional, flags);
    case 'album': return cmdAlbum(positional, flags);
    case 'episodes': return cmdEpisodes(positional, flags);
    case 'download': return cmdDownload(positional, flags);
    case 'fetch-covers': return cmdFetchCovers(flags);
    default:
      throw new Error(`Unknown command: ${cmd}. Run with --help.`);
  }
}

main().catch(err => {
  console.error(`[hg_tool] ERROR: ${err.message}`);
  process.exit(1);
});
