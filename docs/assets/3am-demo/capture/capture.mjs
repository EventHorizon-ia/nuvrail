// Films the 3am demo headlessly. Called by live_run.sh / render.sh; see the
// stage-2 section of ../README.md. Two modes:
//
//   node capture.mjs ui        (live_run.sh step 7, while the op is pending)
//     Opens the REAL approval UI (web container, :3000), films the staged
//     card, clicks the real Reject button, then films the Audit page.
//     The Reject tap is the one that rejects the operation; live_run.sh
//     re-reads the op over the API afterwards and fails unless it is
//     'rejected'. Nothing here calls the reject API itself.
//       env: DEMO_WEB_URL, DEMO_UI_TOKEN (session token, masked in CI),
//            DEMO_CAPTURE_DIR (output dir)
//
//   node capture.mjs terminal  (after live_run.sh finished)
//     Replays the run's REAL transcripts (DEMO_ARTIFACT_DIR/*.txt, written by
//     live_run.sh) as terminal-style scenes. Text is shown verbatim, only
//     paced; nothing is typed in by hand.
//       env: DEMO_ARTIFACT_DIR, DEMO_CAPTURE_DIR
//
// Output (DEMO_CAPTURE_DIR):  scene-<n>-<name>.webm + screen-<n>-<name>.png
//
//   scene-1-title       title card                        (terminal mode)
//   scene-2-agent       agent.py through the proxy        (terminal mode)
//   scene-3-pending     server-side listing, op pending   (terminal mode)
//   scene-4-ui          approval card -> Reject -> Audit  (ui mode)
//   scene-5-after       server-side listing after reject  (terminal mode)
//   scene-6-end         end card + caption                (terminal mode)
//
// render.sh concatenates the scenes in that order.
import { chromium } from 'playwright'
import { readFileSync, renameSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

const SIZE = { width: 1280, height: 720 }
const CAPTION = 'Recorded in CI against a local test mail server (GreenMail). No real mailbox.'

const need = (name) => {
  const v = process.env[name]
  if (!v) { console.error(`capture: ${name} is not set`); process.exit(2) }
  return v
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const OUT = need('DEMO_CAPTURE_DIR')
mkdirSync(OUT, { recursive: true })

// One browser context per scene, so each scene is its own video file.
async function scene(browser, name, fn) {
  const context = await browser.newContext({
    viewport: SIZE,
    recordVideo: { dir: OUT, size: SIZE },
  })
  const page = await context.newPage()
  try {
    await fn(page, context)
  } finally {
    const video = page.video()
    await context.close() // flushes the video
    if (video) renameSync(await video.path(), join(OUT, `scene-${name}.webm`))
  }
  console.log(`capture: scene-${name}.webm`)
}

const shot = (page, name) => page.screenshot({ path: join(OUT, `screen-${name}.png`) })

// ---- ui mode ---------------------------------------------------------------
async function ui(browser) {
  const web = need('DEMO_WEB_URL').replace(/\/+$/, '')
  const token = need('DEMO_UI_TOKEN')
  await scene(browser, '4-ui', async (page, context) => {
    // Same state the login form leaves behind: session token + a cookie
    // choice, so the consent bar doesn't cover the card.
    await context.addInitScript((t) => {
      localStorage.setItem('nuvrail_token', t)
      localStorage.setItem('nuvrail_cookie_consent', 'necessary_only')
    }, token)
    await page.goto(`${web}/#/`)
    const reject = page.getByRole('button', { name: 'Reject', exact: true })
    await reject.waitFor({ state: 'visible', timeout: 30_000 })
    const cards = await reject.count()
    if (cards !== 1) throw new Error(`want exactly 1 pending card, got ${cards}`)
    await sleep(5000)                       // read the staged card
    await shot(page, '2-approval-card')
    await reject.hover()
    await sleep(1500)
    await reject.click()                    // the real Reject tap
    await page.getByText('No pending operations').waitFor({ timeout: 15_000 })
    await sleep(2500)
    await shot(page, '3-rejected')
    await page.goto(`${web}/#/audit`)
    await page.getByRole('heading', { name: 'Audit' }).waitFor({ timeout: 15_000 })
    await page.getByText('rejected').first().waitFor({ timeout: 15_000 })
    await sleep(5000)
    await shot(page, '4-audit')
  })
}

// ---- terminal mode ---------------------------------------------------------
const esc = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))

// A terminal-looking page that reveals `lines` one at a time (verbatim text).
function terminalHtml(title, lines, { lineMs, highlight = [] }) {
  const rows = lines.map((l) => {
    const hot = highlight.some((h) => l.includes(h))
    return `<div class="l${hot ? ' hot' : ''}">${esc(l) || '&nbsp;'}</div>`
  }).join('')
  return `<!doctype html><meta charset="utf-8"><style>
    html,body{margin:0;height:100%;background:#0d1117;color:#c9d1d9;
      font:20px/1.45 ui-monospace,"DejaVu Sans Mono",Menlo,monospace}
    .bar{background:#161b22;color:#8b949e;padding:10px 18px;font-size:16px}
    .t{padding:18px 24px;height:calc(100% - 90px);overflow:hidden}
    .l{white-space:pre;visibility:hidden}.l.on{visibility:visible}
    .hot{color:#3fb950;font-weight:bold}
    .cap{position:fixed;bottom:0;left:0;right:0;background:#161b22;color:#8b949e;
      font-size:15px;padding:8px 18px}
  </style><div class="bar">${esc(title)}</div><div class="t" id="t">${rows}</div>
  <div class="cap">${esc(CAPTION)}</div>
  <script>
    const ls=[...document.querySelectorAll('.l')];let i=0;
    (function next(){ if(i>=ls.length){window.__done=true;return}
      ls[i].classList.add('on');ls[i].scrollIntoView({block:'end'});i++;
      setTimeout(next, ${lineMs}) })();
  </script>`
}

function cardHtml(big, small) {
  return `<!doctype html><meta charset="utf-8"><style>
    html,body{margin:0;height:100%;background:#0d1117;color:#e6edf3;display:flex;
      flex-direction:column;align-items:center;justify-content:center;text-align:center;
      font-family:system-ui,"DejaVu Sans",sans-serif}
    h1{font-size:46px;margin:0 60px 24px;font-weight:700}
    p{font-size:22px;color:#8b949e;margin:6px 60px;max-width:1000px}
  </style><h1>${big}</h1>${small.map((s) => `<p>${s}</p>`).join('')}`
}

async function play(page, html, holdMs) {
  await page.setContent(html)
  await page.waitForFunction(() => window.__done === true || !document.getElementById('t'),
    null, { timeout: 120_000 })
  await sleep(holdMs)
}

const readLines = (dir, f) => readFileSync(join(dir, f), 'utf8').replace(/\s+$/, '').split('\n')

async function terminal(browser) {
  const art = need('DEMO_ARTIFACT_DIR')
  const agent = readLines(art, '1-agent.txt')
  const pending = readLines(art, '2-pending.txt')
  const after = readLines(art, '4-after-reject.txt')

  await scene(browser, '1-title', (p) => play(p, cardHtml('3am. An agent is told to “clean up my inbox”.',
    ['It decides 38 old threads are resolved, flags them \\Deleted, and sends EXPUNGE.',
     'It is connected through Nuvrail.']), 6000))

  await scene(browser, '2-agent', async (p) => {
    await play(p, terminalHtml('$ python3 agent.py      # IMAP via Nuvrail, localhost:10143',
      agent, { lineMs: 1400, highlight: ['OK [STAGED]', 'OK Noted'] }), 9000)
    await shot(p, '1-agent-terminal')
  })

  await scene(browser, '3-pending', (p) => play(p, terminalHtml(
    '$ verify_mailbox.py    # server side, bypassing Nuvrail, while the op waits for approval',
    pending, { lineMs: 90, highlight: ['mailbox:'] }), 5000))

  await scene(browser, '5-after', (p) => play(p, terminalHtml(
    '$ verify_mailbox.py    # server side, after Reject',
    after, { lineMs: 90, highlight: ['mailbox:'] }), 5000))

  await scene(browser, '6-end', (p) => play(p, cardHtml('Rejected. All 40 messages are still there.',
    ['The delete was staged for a human. The EXPUNGE was answered locally and never sent.',
     CAPTION, 'Reproduce it: github.com/agens-field/nuvrail · docs/assets/3am-demo']), 6000))
}

const mode = process.argv[2]
const modes = { ui, terminal }
if (!modes[mode]) { console.error('usage: node capture.mjs ui|terminal'); process.exit(2) }
const browser = await chromium.launch()
try { await modes[mode](browser) } finally { await browser.close() }
