import { describe, expect, it } from 'vitest'

import { historyFixture } from '../../test/fixtures.ts'
import { chapterAt, chaptersFor, effectiveLang, groupByDate, hasZh, videoSrc } from './editions.ts'

const [recap, slideshow, story] = historyFixture.briefings

describe('editions', () => {
  it('offers zh only where a zh cut exists', () => {
    expect(hasZh(recap)).toBe(true)
    expect(hasZh(slideshow)).toBe(false)
    expect(effectiveLang(slideshow, 'zh')).toBe('en')
    expect(videoSrc(recap, 'zh')).toBe(recap.briefing_zh)
  })

  it('groups by session date, newest first', () => {
    expect(groupByDate(historyFixture.briefings).map((g) => [g.date, g.editions.length])).toEqual([
      ['2026-03-04', 2],
      ['2026-03-01', 1],
    ])
  })

  it('uses recorded chapter offsets for legacy story renders', () => {
    const chapters = chaptersFor(story, 'en', null)
    expect(chapters.map((c) => [c.label, c.start, c.exact])).toEqual([
      ['The Tortoise', 0, true],
      ['The Hare', 48, true],
    ])
  })

  it('estimates chapter starts from scene lengths, scaled to the real video', () => {
    const en = chaptersFor(recap, 'en', null)
    expect(en.map((c) => c.start)).toEqual([0, 34.2, 63.1, 104.6].map((v) => expect.closeTo(v, 5)))
    expect(en.every((c) => !c.exact)).toBe(true)
    // The zh cut runs to its own length: offsets scale with it.
    const zh = chaptersFor(recap, 'zh', 283.2)
    expect(zh[1].start).toBeCloseTo(68.4)
    expect(chapterAt(en, 70)).toBe(2)
    expect(chapterAt(en, 0)).toBe(0)
  })
})
