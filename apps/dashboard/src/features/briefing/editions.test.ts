import { describe, expect, it } from 'vitest'

import type { Edition } from '../../contract.ts'
import { historyFixture } from '../../test/fixtures.ts'
import { chapterAt, chaptersFor, groupByDate, playableEditions, playbackFor } from './editions.ts'

const [recap, slideshow, story] = historyFixture.briefings

describe('editions', () => {
  it('plays a YouTube edition from its id and a fallback edition from its mp4', () => {
    expect(playbackFor(recap)).toEqual({ kind: 'youtube', id: 'FAKEyt00001' })
    expect(playbackFor(slideshow)).toEqual({ kind: 'file', src: slideshow.briefing })
    expect(playbackFor(story)).toEqual({ kind: 'youtube', id: 'FAKEyt00002' })
  })

  it('prefers YouTube when an edition somehow carries both', () => {
    expect(playbackFor({ ...slideshow, youtube_id: 'FAKEyt00003' })).toEqual({ kind: 'youtube', id: 'FAKEyt00003' })
  })

  it('refuses an edition with neither a YouTube id nor a fallback mp4', () => {
    const neither: Edition = { ...recap, youtube_id: undefined }
    expect(playbackFor(neither)).toBeNull()
    expect(playableEditions([neither, slideshow])).toEqual([slideshow])
  })

  it('never trusts a malformed YouTube id', () => {
    for (const bad of ['short', 'FAKEyt00001&x=1', '../../evil1', 'FAKEyt0000/', '']) {
      expect(playbackFor({ ...recap, youtube_id: bad }), bad).toBeNull()
      // with a fallback mp4 it plays that instead
      expect(playbackFor({ ...slideshow, youtube_id: bad }), bad).toEqual({ kind: 'file', src: slideshow.briefing })
    }
    expect(playbackFor({ ...recap, youtube_id: undefined, briefing: 'https://elsewhere.example/x.mp4' as unknown as Edition['briefing'] })).toBeNull()
  })

  it('keeps every fixture edition', () => {
    expect(playableEditions(historyFixture.briefings)).toHaveLength(3)
  })

  it('groups by session date, newest first', () => {
    expect(groupByDate(historyFixture.briefings).map((g) => [g.date, g.editions.length])).toEqual([
      ['2026-03-04', 2],
      ['2026-03-01', 1],
    ])
  })

  it('uses recorded chapter offsets for legacy story renders', () => {
    const chapters = chaptersFor(story, null)
    expect(chapters.map((c) => [c.label, c.start, c.exact])).toEqual([
      ['The Tortoise', 0, true],
      ['The Hare', 48, true],
    ])
    // Served from a fallback mp4 instead: the clips name that video.
    const served: Edition = {
      ...story,
      youtube_id: undefined,
      briefing: '/media/2026-03-01/video/weekend-recap_2026-03-01.mp4?v=ddcdc1b7b20e',
      clips: story.clips.map((c) => ({ ...c, file: '/media/2026-03-01/video/weekend-recap_2026-03-01.mp4?v=ddcdc1b7b20e' })),
    }
    expect(chaptersFor(served, 100.5).map((c) => [c.start, c.exact])).toEqual([
      [0, true],
      [48, true],
    ])
  })

  it('estimates chapter starts from scene lengths, scaled to the real video', () => {
    const chapters = chaptersFor(recap, null)
    expect(chapters.map((c) => c.start)).toEqual([0, 34.2, 63.1, 104.6].map((v) => expect.closeTo(v, 5)))
    expect(chapters.every((c) => !c.exact)).toBe(true)
    // A <video> that knows its real length rescales the offsets to it.
    const padded = chaptersFor(recap, 283.2)
    expect(padded[1].start).toBeCloseTo(68.4)
    expect(chapterAt(chapters, 70)).toBe(2)
    expect(chapterAt(chapters, 0)).toBe(0)
  })
})
