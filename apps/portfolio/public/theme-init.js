// Applies a theme the visitor chose earlier before the first paint, so the
// page does not flash the system theme first. A file, not an inline script,
// because the Content-Security-Policy allows scripts from this origin only.
try {
  var t = localStorage.getItem('portfolio-theme')
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t
} catch {
  // storage unavailable: follow the system setting
}
