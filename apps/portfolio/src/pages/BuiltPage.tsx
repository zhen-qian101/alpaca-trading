import { useId } from 'react'

import { Page } from '../components/Layout.tsx'
import { site } from '../site.ts'

interface Box {
  x: number
  y: number
  w: number
  title: string
  sub: string
}

const H = 58
const A = 18
const AW = 160
const B = 250
const BW = 100
const GAP_MID = (A + AW + B) / 2

const BOXES: Box[] = [
  { x: A, y: 44, w: AW, title: 'Claude research', sub: 'five runs each weekday' },
  { x: A, y: 140, w: AW, title: 'decisions.json', sub: 'watchlist, bias, priority' },
  { x: A, y: 236, w: AW, title: 'Signal engine', sub: 'strategies, hourly bars' },
  { x: A, y: 332, w: AW, title: 'Relay', sub: 'risk gate, rotation' },
  { x: A, y: 448, w: AW, title: 'Recap renderer', sub: 'AI video, evenings' },
  { x: A, y: 548, w: AW, title: 'Public exporter', sub: 'own figures only' },
  { x: B, y: 44, w: BW, title: 'Market news', sub: 'data and web' },
  { x: B, y: 236, w: BW, title: 'Bar data', sub: 'IBKR, Yahoo' },
  { x: B, y: 332, w: BW, title: 'Alpaca', sub: 'paper API' },
  { x: B, y: 448, w: BW, title: 'YouTube', sub: 'public videos' },
  { x: B, y: 548, w: BW, title: 'Cloudflare R2', sub: 'file storage' },
  { x: B, y: 632, w: BW, title: 'This site', sub: 'static pages' },
]

interface Edge {
  d: string
  label?: { x: number; y: number; text: string; anchor?: 'start' | 'middle' | 'end' }
}

const midA = A + AW / 2
const right = A + AW
const EDGES: Edge[] = [
  // research reads news and data
  { d: `M${B} 73H${right}`, label: { x: GAP_MID, y: 66, text: 'reads', anchor: 'middle' } },
  // research writes the decisions file
  { d: `M${midA} ${44 + H}V140`, label: { x: midA + 8, y: 125, text: 'writes' } },
  // the gate reads the decisions before every order (down the left margin)
  { d: `M${A} 169H9V361H${A}`, label: { x: 14, y: 318, text: '', anchor: 'start' } },
  // bars into the engine
  { d: `M${B} 265H${right}`, label: { x: GAP_MID, y: 258, text: 'bars', anchor: 'middle' } },
  // engine hands signals to the relay, in-process
  { d: `M${midA} ${236 + H}V332`, label: { x: midA + 8, y: 317, text: 'signals' } },
  // relay sends orders
  { d: `M${right} 361H${B}`, label: { x: GAP_MID, y: 354, text: 'orders', anchor: 'middle' } },
  // research's summary of the market day is the recap's main input (down the far left margin, outside the gate edge)
  { d: `M${A} 88H3V477H${A}`, label: { x: 8, y: 424, text: 'summary', anchor: 'start' } },
  // the account's trades feed the recap too
  { d: `M${midA} ${332 + H}V448`, label: { x: midA + 8, y: 424, text: 'journal' } },
  // recap uploads
  { d: `M${right} 477H${B}`, label: { x: GAP_MID, y: 470, text: 'uploads', anchor: 'middle' } },
  // recap list for the exporter
  { d: `M${midA} ${448 + H}V548`, label: { x: midA + 8, y: 531, text: 'recap list' } },
  // exporter pushes the file
  { d: `M${right} 577H${B}`, label: { x: GAP_MID, y: 570, text: 'pushes', anchor: 'middle' } },
  // checked again and deployed with the site's static assets by a scheduled CI job
  { d: `M${B + BW / 2} ${548 + H}V632`, label: { x: B + BW / 2 - 8, y: 623, text: 'GitHub CI', anchor: 'end' } },
]

function Diagram() {
  const marker = useId().replace(/:/g, '')
  const titleId = useId()
  const descId = useId()
  return (
    <svg className="diagram" viewBox="0 0 360 700" role="img" aria-labelledby={`${titleId} ${descId}`}>
      <title id={titleId}>How the system fits together</title>
      <desc id={descId}>
        On the home computer, Claude research reads market news and writes decisions.json. The signal engine reads bar
        data and passes signals to the relay, which checks each order against decisions.json and sends it to Alpaca’s
        paper API. In the evening the recap renderer makes an AI-generated video from research’s summary of the day,
        the account’s figures and trade journal, and a screenshot of the live dashboard, and uploads it to YouTube. The
        public exporter pushes the account’s own figures to Cloudflare R2, where a scheduled GitHub Actions job checks
        them again and deploys them with this site.
      </desc>
      <defs>
        <marker id={marker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10Z" className="arrowhead" />
        </marker>
      </defs>
      <text className="zone-label" x={A} y="24">
        Home computer
      </text>
      <text className="zone-label" x={B} y="24">
        Internet
      </text>
      <line className="divider" x1={GAP_MID} x2={GAP_MID} y1="8" y2="692" />
      {EDGES.map((e, i) => (
        <g key={i}>
          <path className="edge" d={e.d} markerEnd={`url(#${marker})`} />
          {e.label?.text ? (
            <text className="edge-label" x={e.label.x} y={e.label.y} textAnchor={e.label.anchor ?? 'start'}>
              {e.label.text}
            </text>
          ) : null}
        </g>
      ))}
      {BOXES.map((b) => (
        <g key={b.title}>
          <rect className="box" x={b.x} y={b.y} width={b.w} height={H} rx="6" />
          <text className="title" x={b.x + 10} y={b.y + 24} fontSize={b.w <= 100 ? 13 : 14}>
            {b.title}
          </text>
          <text className="sub" x={b.x + 10} y={b.y + 42}>
            {b.sub}
          </text>
        </g>
      ))}
    </svg>
  )
}

export function BuiltPage() {
  const flowId = useId()
  const piecesId = useId()
  const stackId = useId()
  const leftOutId = useId()
  return (
    <Page
      title="How it’s built"
      heading="How it’s built"
      intro={
        <p className="lede">
          Everything that trades runs on one desktop computer at home. It sends orders to the broker’s paper API and
          finished videos to YouTube. For this site it pushes out one file of figures; the site itself has no server
          code and never contacts that computer.
        </p>
      }
    >
      <section className="section" aria-labelledby={flowId}>
        <h2 id={flowId}>The parts and how they connect</h2>
        <div className="diagram-layout">
          <Diagram />
          <div className="prose">
            <ol className="list-decimal pl-5">
              <li>
                <strong>Research</strong> runs as a headless Claude Code session on a schedule. It reads market data
                and news, and writes <code>decisions.json</code>: which symbols may be bought, and in what order.
              </li>
              <li>
                <strong>The signal engine</strong> pulls hourly bars every five minutes and re-runs each symbol’s
                strategies. A new signal goes straight to the relay, in the same process; there is no web hook.
              </li>
              <li>
                <strong>The relay</strong> checks every order against the decisions and the hard limits, decides
                rotations, sends what passes to Alpaca’s paper API, and journals every decision.
              </li>
              <li>
                <strong>The recap renderer</strong> runs each weekday evening, after research has written a summary of the
                market day. A language model turns that summary and the account’s own figures into a script and a
                title. A speech model voices the script, and video models on the local GPU animate an AI-generated
                presenter. One slide is usually a screenshot of the live dashboard, market candles and current prices
                included. The finished video goes to YouTube.
              </li>
              <li>
                <strong>The public exporter</strong> collects the account’s own figures into one checked file and
                pushes it to Cloudflare R2. A scheduled GitHub Actions job checks it against the same schema again and
                deploys this static site with it.
              </li>
            </ol>
          </div>
        </div>
      </section>

      <section className="section" aria-labelledby={piecesId}>
        <h2 id={piecesId}>Design choices</h2>
        <dl className="defs">
          <div>
            <dt>Backtest and live share one loop</dt>
            <dd>
              <p>
                The engine does not reimplement the strategies. It re-runs the backtest code over a sliding window of
                live bars and acts only on events newer than the last bar it handled, so the live signals cannot drift
                from what was tested.
              </p>
            </dd>
          </div>
          <div>
            <dt>Limits live in code, not in the prompt</dt>
            <dd>
              <p>
                The research model writes its decisions to a file. The relay re-clamps every value to ceilings written
                in code, so the model can tighten risk but never loosen it past them.
              </p>
            </dd>
          </div>
          <div>
            <dt>Exits are never blocked</dt>
            <dd>
              <p>
                The loss halt, the allowlist and the trading-hours permission all apply to buys. A stop or a strategy
                exit can always reach the broker while a session is open.
              </p>
            </dd>
          </div>
          <div>
            <dt>This site only receives</dt>
            <dd>
              <p>
                This site has no server code and never contacts the home computer. The home computer pushes one file of
                figures outward for it, and the broker keys never leave the home computer.
              </p>
            </dd>
          </div>
        </dl>
      </section>

      <section className="section" aria-labelledby={stackId}>
        <h2 id={stackId}>Tech stack</h2>
        <dl className="stack-list">
          <div>
            <dt>Trading</dt>
            <dd>
              Python with pandas and NumPy. alpaca-py for orders and the account; ib_async for Interactive Brokers bars,
              with yfinance as the fallback; a SQLite bar cache. tmux and cron keep it running.
            </dd>
          </div>
          <div>
            <dt>Research</dt>
            <dd>
              Claude Code run headless on cron, with Alpaca’s MCP server for market data and news (data tools only, no
              trading tools) and web search.
            </dd>
          </div>
          <div>
            <dt>Recap video</dt>
            <dd>
              A language model writes the script and the title; open-weight video models run in ComfyUI on an NVIDIA GPU,
              and a speech model voices the script. Playwright screenshots the live dashboard for one slide, ffmpeg
              assembles the cut, and the YouTube Data API uploads it.
            </dd>
          </div>
          <div>
            <dt>This site</dt>
            <dd>
              Vite, React and TypeScript with Tailwind CSS and TradingView Lightweight Charts, served as static assets by
              Cloudflare Workers. The figures arrive through Cloudflare R2 and a scheduled GitHub Actions deploy.
            </dd>
          </div>
          <div>
            <dt>Checks</dt>
            <dd>
              pytest and Ruff for the Python, Vitest and Testing Library here, a JSON schema that every published file
              must pass, and a secrets scan of the repository history.
            </dd>
          </div>
        </dl>
      </section>

      <section className="section" aria-labelledby={leftOutId}>
        <h2 id={leftOutId}>What this site leaves out, and why</h2>
        <ul className="prose m-0 list-disc pl-5">
          <li>
            Market data, from the site’s own figures and charts. The data providers’ terms restrict republishing
            prices, candles and quotes, so nothing in the figures, tables or charts on these pages comes from a
            market-data feed: they show the account’s own equity, profit and loss and fill prices, and the levels the
            system set itself. The recap videos are not covered by this: most of them show the live trading dashboard,
            including market candles and current prices.
          </li>
          <li>
            Equity during a session. With a position open, the account’s equity at any moment, together with the
            position’s size and entry price shown here, gives back that symbol’s price at that moment. So equity, the
            returns and the curve stop at the last completed session, one point per session: from them a held
            symbol’s price at the end of that session can be worked out, but never a live one.
          </li>
          <li>
            Model-written text. The research board shows only the bias, priority, size and standing orders research
            set, not the model’s notes, and each recap is listed by its date, not by its title. The recap videos are not covered by
            this either: a language model writes their scripts and YouTube titles.
          </li>
          <li>Account numbers, order ids, logs, file paths and machine names.</li>
          <li>Recaps that are not public on YouTube, and every cut other than the English one.</li>
        </ul>
      </section>

      <section className="section prose">
        <p>
          The code is on GitHub: <a href={site.repoUrl} rel="noopener noreferrer">{site.repoUrl.replace(/^https:\/\//, '')}</a>.
        </p>
      </section>
    </Page>
  )
}
