import raw from '../site.config.json'
import { checkSiteConfig } from './lib/siteConfig.ts'

/** The checked site.config.json (see src/lib/siteConfig.ts). */
export const site = checkSiteConfig(raw)
