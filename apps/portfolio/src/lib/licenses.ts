/**
 * The open-source notices the public site carries.
 *
 * The build writes the licence of every package bundled into the site (React,
 * lightweight-charts and what they pull in) to LICENSES_FILE at the site root
 * (vite.config.ts, `build.license`), so the deployed site serves it at
 * LICENSES_PATH and the footer links to it. Not Vite's default
 * `.vite/license.md`: a file under a dot directory is easy to lose on the way
 * to the Worker, and a .txt file opens as text in every browser.
 * scripts/licenseNotices.ts completes and checks that file.
 *
 * TradingView Lightweight Charts is Apache-2.0, and its README asks that the
 * attribution notice in its NOTICE file, with a link to tradingview.com, be
 * shown to the site's users. The npm package ships its LICENSE but not the
 * NOTICE, so the text is kept here, for the footer and the licences file. The
 * build fails when the bundled version moves off NOTICE_VERSION, so an upgrade
 * re-reads the NOTICE of the new release.
 */
export const LICENSES_FILE = 'third-party-licenses.txt'
export const LICENSES_PATH = `/${LICENSES_FILE}`

/** The lightweight-charts release whose NOTICE file is quoted below. */
export const NOTICE_VERSION = '5.3.0'

/**
 * https://github.com/tradingview/lightweight-charts/blob/v5.3.0/NOTICE as it
 * reads there, except that the "с" in its "(с)" is a Cyrillic letter upstream
 * and a Latin one here.
 */
export const TRADINGVIEW_NOTICE = {
  product: 'TradingView Lightweight Charts™',
  copyright: 'Copyright (c) 2025 TradingView, Inc.',
  url: 'https://www.tradingview.com/',
} as const
