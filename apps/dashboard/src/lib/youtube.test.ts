import { describe, expect, it } from 'vitest'

import { EMBED_ORIGIN, embedUrl, isYoutubeId, watchUrl } from './youtube.ts'

describe('youtube', () => {
  it('embeds from the no-cookie domain, autoplaying, without related videos from other channels', () => {
    expect(EMBED_ORIGIN).toBe('https://www.youtube-nocookie.com')
    expect(embedUrl('FAKEyt00001')).toBe('https://www.youtube-nocookie.com/embed/FAKEyt00001?autoplay=1&rel=0&playsinline=1')
  })

  it('starts at whole seconds, and only when asked to', () => {
    expect(embedUrl('FAKEyt00001', 48)).toBe(
      'https://www.youtube-nocookie.com/embed/FAKEyt00001?autoplay=1&rel=0&playsinline=1&start=48',
    )
    expect(embedUrl('FAKEyt00001', 34.97)).toMatch(/&start=34$/)
    for (const none of [0, 0.4, -3, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(embedUrl('FAKEyt00001', none), String(none)).not.toContain('start=')
    }
  })

  it('refuses anything that is not a video id', () => {
    for (const bad of ['', 'FAKEyt0000', 'FAKEyt00001x', 'FAKEyt0000&', 'FAKEyt00/01', 'javascript:']) {
      expect(isYoutubeId(bad), bad).toBe(false)
      expect(() => embedUrl(bad), bad).toThrow()
      expect(() => watchUrl(bad), bad).toThrow()
    }
    expect(isYoutubeId(undefined)).toBe(false)
    expect(isYoutubeId(12345678901)).toBe(false)
    expect(watchUrl('FAKEyt00001')).toBe('https://www.youtube.com/watch?v=FAKEyt00001')
  })
})
