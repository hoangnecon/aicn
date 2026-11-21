#!/usr/bin/env node
'use strict';

/**
 * hg_tool.cjs — Unified Hongguo (红果短剧 / 红果漫剧 / AI仿真人剧) Discovery, Analytics & Download CLI
 * Powered by jackotom/hongguo-drama native engine (V5 video_tab GET signing + CENC-AES-CTR decryption).
 * Replaces retired openhongguo (ohg v0.4.0).
 */

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

function resolveRepoDir() {
  const candidates = [
    process.env.AICN_HONGGUO_REPO,
    path.resolve(__dirname, '../../../tools/hongguo-drama'),
    '/Users/hoangnguyen142/Work/AICN/tools/hongguo-drama',
  ].filter(Boolean);
  for (const dir of candidates) {
    if (fs.existsSync(path.join(dir, 'src/native/app-source.js'))) {
      return dir;
    }
  }
  throw new Error(
    `hongguo-drama repo not found. Clone https://github.com/jackotom/hongguo-drama into tools/hongguo-drama (or set AICN_HONGGUO_REPO) and run: npm install --omit=dev --ignore-scripts`
  );
}

const REPO_DIR = resolveRepoDir();
const nativeDir = path.join(REPO_DIR, 'src/native');

const { browseList, catalogCover } = require(path.join(nativeDir, 'hongguo-app-catalog.js'));
const { listNewReleases } = require(path.join(nativeDir, 'hongguo-app-new.js'));
const { searchApp } = require(path.join(nativeDir, 'hongguo-app-search.js'));
const { fetchEpisodeList, deriveKey } = require(path.join(nativeDir, 'hongguo.js'));
const { fetchAppPlayUrl } = require(path.join(nativeDir, 'app-source.js'));

const FFMPEG = fs.existsSync('/opt/homebrew/bin/ffmpeg') ? '/opt/homebrew/bin/ffmpeg' : 'ffmpeg';
const FFPROBE = fs.existsSync('/opt/homebrew/bin/ffprobe') ? '/opt/homebrew/bin/ffprobe' : 'ffprobe';

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

async function cmdRank(flags) {
  const category = flags.category || 'hot-ai-drama';
  const page = parseInt(flags.page || '1', 10);
  const genre = flags.genre || '';
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
  const filterGenre = flags.genre || 'all'; // all | ai | real
  const res = await listNewReleases({ pages, forceRefresh: true });
  let items = res.items || [];
  if (filterGenre === 'ai') {
    items = items.filter(it => (it.tags || []).some(t => /AI|漫剧|仿真/i.test(t)) || it.genre === 'ai_series');
  } else if (filterGenre === 'real') {
    items = items.filter(it => it.genre === 'short_play');
  }
  items = items.slice(0, limit);
  if (flags.json) {
    console.log(JSON.stringify({ count: items.length, partial: !!res.partial, items }, null, 2));
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

async function cmdEpisodes(positional, flags) {
  const seriesId = positional[0];
  if (!seriesId) throw new Error('Usage: hg_tool.cjs episodes <series_id> [--json]');
  const data = await fetchEpisodeList(seriesId);
  const eps = data?.episodes || data?.list || (Array.isArray(data) ? data : []);
  const totalSec = eps.reduce((acc, e) => acc + (Number(e.duration) || 0), 0);
  const summary = {
    series_id: seriesId,
    title: data?.seriesTitle || data?.title || '',
    total_episodes: eps.length,
    total_duration_sec: Math.round(totalSec),
    total_duration_min: Number((totalSec / 60).toFixed(1)),
    cover: data?.cover ? catalogCover(data.cover) : '',
    episodes: eps.map((e, i) => ({
      index: e.episode || e.index || (i + 1),
      video_id: String(e.vid || e.video_id || e.id || ''),
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

async function cmdDownload(positional, flags) {
  const seriesId = positional[0];
  const outDir = flags.out || flags.dir;
  if (!seriesId || !outDir) {
    throw new Error('Usage: hg_tool.cjs download <series_id> --out <dir> [--episodes 1-10] [--quality 1080p|auto] [--delay-ms 1500]');
  }
  const quality = flags.quality || '1080p';
  const delayMs = parseInt(flags['delay-ms'] || '1500', 10);

  fs.mkdirSync(outDir, { recursive: true });
  console.log(`[hg_tool] Resolving episode list for series_id=${seriesId}...`);
  const data = await fetchEpisodeList(seriesId);
  const eps = data?.episodes || data?.list || (Array.isArray(data) ? data : []);
  if (!eps.length) throw new Error(`No episodes returned for series_id=${seriesId}`);

  const wanted = parseEpisodeSpec(flags.episodes || 'all', eps.length);
  console.log(`[hg_tool] Plan: download ${wanted.length}/${eps.length} episodes -> ${outDir} (quality=${quality}, delay=${delayMs}ms)`);

  let downloaded = 0;
  let skipped = 0;
  const failed = [];

  for (let i = 0; i < wanted.length; i++) {
    const epNum = wanted[i];
    const ep = eps[epNum - 1];
    const vid = String(ep.vid || ep.video_id || ep.id || '');
    const outFile = path.join(outDir, `ep_${String(epNum).padStart(3, '0')}.mp4`);

    if (fs.existsSync(outFile) && probeDuration(outFile) > 5) {
      console.log(`[${i + 1}/${wanted.length}] SKIP ep_${String(epNum).padStart(3, '0')}.mp4 (already valid)`);
      skipped++;
      continue;
    }

    try {
      const t0 = Date.now();
      const play = await fetchAppPlayUrl(vid, { quality, allowLowerQuality: true });
      const url = play.url || play.mainUrl || play.variants?.[0]?.url;
      if (!url) throw new Error('No playable stream URL in response');

      const spade = play.spadeA || play.spade_a || play.keyMaterial || play.variants?.[0]?.spadeA;
      let keyHex = play.contentKey || play.keyHex || play.key || '';
      if (!keyHex && spade) {
        const keyBuf = deriveKey(spade);
        if (keyBuf) keyHex = Buffer.isBuffer(keyBuf) ? keyBuf.toString('hex') : String(keyBuf);
      }

      const resp = await fetch(url, {
        headers: {
          'User-Agent': 'com.phoenix.read/73932 (Linux; U; Android 12; zh_CN; PGT-AN10; Build/HONORPGT-AN10)',
        },
      });
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
      console.log(`[${i + 1}/${wanted.length}] OK ep_${String(epNum).padStart(3, '0')}.mp4 | ${mb} MB | ${dur.toFixed(1)}s | ${play.quality || quality} | ${elapsed}s`);
      downloaded++;
    } catch (err) {
      console.error(`[${i + 1}/${wanted.length}] FAIL ep_${String(epNum).padStart(3, '0')} (vid=${vid}): ${err.message}`);
      failed.push({ epNum, vid, error: err.message });
    }

    if (i < wanted.length - 1 && delayMs > 0) {
      await new Promise(r => setTimeout(r, delayMs));
    }
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
  console.log(`hg_tool.cjs — Hongguo Discovery, Analytics & Download CLI (powered by jackotom/hongguo-drama)

Commands:
  rank     --category hot-ai-drama|hot-real-drama|hot-comic-drama|hot-drama [--page 1] [--json]
  new      [--genre all|ai|real] [--pages 2] [--limit 20] [--json]
  search   "<keyword>" [--page 1] [--json]
  episodes <series_id> [--json]
  download <series_id> --out <dir> [--episodes 1-10|all] [--quality 1080p|720p|auto] [--delay-ms 1500]
  fetch-covers --input <catalog.json> --out <covers_dir>
`);
}

async function main() {
  const { cmd, positional, flags } = parseArgs(process.argv);
  if (!cmd || cmd === '--help' || cmd === '-h' || cmd === 'help') {
    printHelp();
    return;
  }
  switch (cmd) {
    case 'rank': return cmdRank(flags);
    case 'new': return cmdNew(flags);
    case 'search': return cmdSearch(positional, flags);
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
