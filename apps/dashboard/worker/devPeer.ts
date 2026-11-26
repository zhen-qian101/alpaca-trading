/**
 * How the dev auth bypass knows a request came from this machine.
 *
 * The request's Host header cannot tell: the Vite dev and preview servers
 * build `request.url` from it, and any client can send `Host: localhost`. So
 * `vite dev` runs devserver/peerStamp.ts in front of the Worker. On every
 * request it OVERWRITES DEV_PEER_HEADER with the TCP peer address of the
 * connection (Node's `socket.remoteAddress`) and DEV_PEER_KEY_HEADER with a
 * random key made for that dev server process, which it also gives the
 * Worker as the var DEV_PEER_KEY. Whatever a client sent under those names is
 * discarded. auth.ts honours DEV_AUTH_BYPASS only when the key matches the
 * Worker's and the peer is loopback:
 *   - nobody who reaches the port over the network can claim to be local;
 *   - a runtime without the middleware (`wrangler dev`, `vite preview`) has
 *     no key, so forged headers get nowhere there.
 *
 * Nothing sets either header on a deployed Worker, which has no DEV_PEER_KEY;
 * the bypass there also needs a loopback Host (the edge never routes one), no
 * CF-Connecting-IP other than loopback (the edge always sets the real
 * client's), and both Access secrets unset.
 *
 * No imports, no Worker types: devserver/peerStamp.ts (Node, tsconfig.node)
 * shares this file with the Worker.
 */
export const DEV_PEER_HEADER = 'X-Alpaca-Dev-Peer'
export const DEV_PEER_KEY_HEADER = 'X-Alpaca-Dev-Peer-Key'
/** The Worker var holding the dev server's key (set by vite.config.ts under `vite dev` only). */
export const DEV_PEER_KEY_VAR = 'DEV_PEER_KEY'
/** devserver/peerStamp.ts makes 64 hex characters; anything much shorter is not its key. */
export const DEV_PEER_KEY_MIN_LENGTH = 32

/** Equal strings, compared in time that does not depend on where they differ. */
export function sameKey(a: string | null | undefined, b: string | null | undefined): boolean {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false
  let diff = 0
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i)
  return diff === 0
}

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/

function isLoopbackV4(text: string): boolean {
  const m = IPV4.exec(text)
  if (!m) return false
  const octets = m.slice(1).map(Number)
  return octets[0] === 127 && octets.every((o) => o <= 255)
}

/**
 * True for a loopback IP literal: 127.0.0.0/8, ::1, or 127.x written as an
 * IPv4-mapped IPv6 address (::ffff:127.0.0.1, what a dual-stack socket
 * reports for an IPv4 client). Anything else, including a host name, a list
 * or an empty value, is not loopback.
 */
export function isLoopbackAddress(address: string | null | undefined): boolean {
  if (typeof address !== 'string') return false
  const a = address.trim().toLowerCase()
  if (a === '::1' || a === '0:0:0:0:0:0:0:1') return true
  if (a.startsWith('::ffff:')) return isLoopbackV4(a.slice('::ffff:'.length))
  return isLoopbackV4(a)
}
