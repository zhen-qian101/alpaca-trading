// Pure helpers: dates, path validation, Range parsing.
import { describe, expect, it } from 'vitest'

import { approvalWindow, dayNumber, easternDate, inApprovalWindow, isoUtc } from './dates.ts'
import { noneMatchHits, parseRange, resolveRange } from './objects.ts'
import { asApprovalRequest } from './routes.ts'
import { isLookAssetKind, isSymbol, mediaTarget } from './validate.ts'

describe('dates', () => {
  it('reads today in America/New_York, not UTC or local', () => {
    expect(easternDate(new Date('2026-03-05T04:59:59Z'))).toBe('2026-03-04') // 23:59:59 EST
    expect(easternDate(new Date('2026-03-05T05:00:00Z'))).toBe('2026-03-05')
    expect(easternDate(new Date('2026-07-01T03:59:59Z'))).toBe('2026-06-30') // EDT
    expect(easternDate(new Date('2026-07-01T04:00:00Z'))).toBe('2026-07-01')
  })

  it('accepts only real calendar dates', () => {
    expect(dayNumber('1970-01-02')).toBe(1)
    expect(dayNumber('2028-02-29')).not.toBeNull()
    for (const bad of ['2026-02-29', '2026-02-30', '2026-13-01', '2026-00-10', '2026-01-32', '2026-1-1', '26-01-01', '2026-01-01T00:00', ' 2026-01-01', '']) {
      expect(dayNumber(bad), bad).toBeNull()
    }
  })

  it('computes the approval window across month and year ends', () => {
    expect(approvalWindow(new Date('2026-12-31T17:00:00Z'))).toEqual({ earliest: '2026-12-30', latest: '2027-01-03' })
    expect(approvalWindow(new Date('2026-03-01T17:00:00Z'))).toEqual({ earliest: '2026-02-28', latest: '2026-03-04' })
    const now = new Date('2026-03-01T17:00:00Z')
    expect(inApprovalWindow('2026-02-28', now)).toBe(true)
    expect(inApprovalWindow('2026-02-27', now)).toBe(false)
    expect(inApprovalWindow('2026-03-04', now)).toBe(true)
    expect(inApprovalWindow('2026-03-05', now)).toBe(false)
  })

  it('formats requested_at the way Python writes it', () => {
    expect(isoUtc(new Date('2026-03-04T20:58:12.345Z'))).toBe('2026-03-04T20:58:12+00:00')
  })
})

describe('mediaTarget', () => {
  it('maps the two allowed shapes to media/ keys with a type from the extension', () => {
    expect(mediaTarget('history.json')).toEqual({
      key: 'media/history.json',
      contentType: 'application/json; charset=utf-8',
      ext: 'json',
    })
    expect(mediaTarget('2026-03-04/slideshow-midday-briefing/midday-briefing_2026-03-04_cover.png')).toEqual({
      key: 'media/2026-03-04/slideshow-midday-briefing/midday-briefing_2026-03-04_cover.png',
      contentType: 'image/png',
      ext: 'png',
    })
    expect(mediaTarget('2026-03-04/slideshow-midday-briefing/midday-briefing_2026-03-04.MP4')?.contentType).toBe('video/mp4')
  })

  it('refuses traversal and anything outside the two shapes, given raw strings', () => {
    for (const raw of [
      '../snapshot/latest.json',
      '..',
      '.',
      './history.json',
      '2026-03-04/video/../../../snapshot/latest.json',
      '2026-03-04/../video/x.mp4',
      '2026-03-04/./video/x.mp4',
      '/etc/passwd',
      '/history.json',
      'history.json/',
      '%2e%2e/history.json',
      '2026-03-04/video/x%2Emp4',
      '2026-03-04\\video\\x.mp4',
      '2026-03-04/video/x.mp4\u0000',
      '2026-03-04/video/x.mp4?x=1',
      '2026-03-04/video/x.mp4#x',
      '2026-03-04/video/ünicode.mp4',
      'C:/2026-03-04/video/x.mp4',
      '2026-03-04/video/x.',
      '2026-03-04/video/.mp4',
      '2026-03-04/video/x.mp4.exe',
      '2026-03-04/-/x.mp4',
    ]) {
      expect(mediaTarget(raw), raw).toBeNull()
    }
  })
})

describe('symbol and kind', () => {
  it('accepts trading symbols and chart keys only', () => {
    for (const ok of ['AAPL', 'BRK.B', 'BTC/USD', 'BTC-USD', 'A', 'X1']) expect(isSymbol(ok), ok).toBe(true)
    for (const bad of [null, '', 'aapl', 'BTC/USD/X', '../A', 'A B', 'A/', '/A', 'A'.repeat(16)]) {
      expect(isSymbol(bad), String(bad)).toBe(false)
    }
    expect(isLookAssetKind('plate')).toBe(true)
    expect(isLookAssetKind('keyframe')).toBe(true)
    expect(isLookAssetKind('../plate')).toBe(false)
    expect(isLookAssetKind(null)).toBe(false)
  })
})

describe('Range parsing', () => {
  it('parses single byte ranges', () => {
    expect(parseRange('bytes=0-99')).toEqual({ start: 0, end: 99 })
    expect(parseRange('bytes=5-')).toEqual({ start: 5 })
    expect(parseRange('bytes=-20')).toEqual({ suffix: 20 })
    expect(parseRange(' Bytes = 1 - 2 ')).toEqual({ start: 1, end: 2 })
  })

  it('returns null (serve the whole file) for anything else', () => {
    for (const h of [null, '', 'bytes=', 'bytes=-', 'bytes=2-1', 'bytes=0-1,3-4', 'bits=0-1', 'bytes=a-b', 'bytes=1.5-2', `bytes=${'1'.repeat(16)}-`]) {
      expect(parseRange(h), String(h)).toBeNull()
    }
  })

  it('resolves against the object size', () => {
    expect(resolveRange({ start: 0, end: 9 }, 100)).toEqual({ offset: 0, length: 10 })
    expect(resolveRange({ start: 90, end: 200 }, 100)).toEqual({ offset: 90, length: 10 })
    expect(resolveRange({ start: 99 }, 100)).toEqual({ offset: 99, length: 1 })
    expect(resolveRange({ suffix: 10 }, 100)).toEqual({ offset: 90, length: 10 })
    expect(resolveRange({ suffix: 500 }, 100)).toEqual({ offset: 0, length: 100 })
    expect(resolveRange({ start: 100 }, 100)).toBeNull()
    expect(resolveRange({ suffix: 0 }, 100)).toBeNull()
    expect(resolveRange({ start: 0 }, 0)).toBeNull()
    expect(resolveRange({ suffix: 5 }, 0)).toBeNull()
  })

  it('compares If-None-Match weakly', () => {
    expect(noneMatchHits('"abc"', '"abc"')).toBe(true)
    expect(noneMatchHits('W/"abc"', '"abc"')).toBe(true)
    expect(noneMatchHits('"x", "abc"', '"abc"')).toBe(true)
    expect(noneMatchHits('*', '"abc"')).toBe(true)
    expect(noneMatchHits('"abcd"', '"abc"')).toBe(false)
  })
})

describe('asApprovalRequest', () => {
  const good = { date: '2026-03-04', approved: true, requested_at: '2026-03-04T20:58:12+00:00', requested_by: 'r@example.com' }
  it('keeps the four contract fields only', () => {
    expect(asApprovalRequest({ ...good, extra: 'x' }, '2026-03-04')).toEqual(good)
  })
  it('rejects a mismatched date or wrong types', () => {
    expect(asApprovalRequest(good, '2026-03-05')).toBeNull()
    expect(asApprovalRequest({ ...good, approved: 'true' }, '2026-03-04')).toBeNull()
    expect(asApprovalRequest({ ...good, requested_by: null }, '2026-03-04')).toBeNull()
    expect(asApprovalRequest([good], '2026-03-04')).toBeNull()
    expect(asApprovalRequest(null, '2026-03-04')).toBeNull()
  })
})
